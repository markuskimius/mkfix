"""Static integrity checks on the UI config (app.json) and its assets.

app.json drives the whole UI declaratively, so a dangling pane reference or a
JS module that no longer exists fails silently in the browser rather than at
import time. These tests fail the build instead.
"""

import contextlib
import functools
import io
import json
import re
import sqlite3
import tomllib
from pathlib import Path

import pytest

from mkfix import __version__

STATIC = Path(__file__).resolve().parent.parent / "mkfix" / "static"
TEMPLATE_SCOPES = {"order", "cancel", "accept", "reject", "fill", "unsolicited", "restate", "dk", "correct", "bust",
                   "renotify"}
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def app_config() -> dict:
    return json.loads((STATIC / "app.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def index_imports() -> list[str]:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return re.findall(r'import\s+"([^"]+)"', html)


@pytest.fixture(scope="module")
def toml_config() -> dict:
    return tomllib.loads((ROOT / "mkfix" / "mkfix.toml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def known_services(toml_config) -> set[str]:
    """TOML-declared services plus the ones registered in code (fix_cmd)."""
    main = (ROOT / "mkfix" / "__main__.py").read_text(encoding="utf-8")
    return set(toml_config["services"]) | set(re.findall(r'add_service\(\s*"([^"]+)"', main))


def _fix_cmd_commands() -> set[str]:
    """Every command fix_cmd answers: the branches of its _dispatch, and the
    order and trade actions it hands to FixEngine.perform by table."""
    from mkfix.fix.actions import ACTIONS
    source = (ROOT / "mkfix" / "services" / "fix_command.py").read_text(encoding="utf-8")
    assert "if command in ACTIONS" in source, "fix_cmd no longer routes the action table"
    return set(re.findall(r'command == "([^"]+)"', source)) | set(ACTIONS)


def _frame_pane_ids(layout: dict) -> list[str]:
    """Pane ids referenced by a frame layout, recursing through splits/tabs."""
    ids = []
    for child in layout.get("children", []):
        if isinstance(child, str):
            ids.append(child)
        else:
            ids.extend(_frame_pane_ids(child))
    return ids


_COND_EQ = re.compile(r"(?:\br\.)?([A-Za-z_]\w*) (?:==|!=) '([^']*)'")
_COND_IN = re.compile(r"CONTAINS\(\[([^\]]*)\], (?:r\.)?([A-Za-z_]\w*)\)")


def _conditions(when):
    """Column -> values an app.json `when` expression compares it against.
    Cell rules name the cell `value`; enable gates name the row `r`."""
    out = {}
    for name, value in _COND_EQ.findall(when or ""):
        out.setdefault(name, []).append(value)
    for values, name in _COND_IN.findall(when or ""):
        out.setdefault(name, []).extend(re.findall(r"'([^']*)'", values))
    return out


@functools.lru_cache(maxsize=None)
def _schema_conn() -> sqlite3.Connection:
    """The TOML tables, created empty in memory, to run a service's sql against."""
    from mkio.migration import migrate_schema
    conn = sqlite3.connect(":memory:")
    with contextlib.redirect_stdout(io.StringIO()):
        migrate_schema(conn, tomllib.loads((ROOT / "mkfix" / "mkfix.toml").read_text(encoding="utf-8"))["tables"])
    return conn


def _service_columns(toml_config, service_name):
    """The columns a query service's rows carry: the primary table's under
    the default SQL, else whatever its `sql` returns — joined and computed
    columns included — read against the TOML schema. None for a service
    without a primary table."""
    service = toml_config["services"].get(service_name, {})
    table = service.get("primary_table")
    if not table:
        return None
    sql = service.get("sql")
    if not sql:
        return set(toml_config["tables"][table]["columns"])
    cur = _schema_conn().execute(f"SELECT * FROM ({sql.strip().rstrip(';')}) LIMIT 0")
    return {d[0] for d in cur.description}


def _walk_dicts(obj):
    """Every dict reachable inside a nested JSON structure."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_dicts(value)


def _find_dialog(app_config: dict, op: str) -> dict:
    dialog = next(
        (node for node in _walk_dicts(app_config)
         if node.get("submit", {}).get("op") == op),
        None,
    )
    assert dialog, f"no {op} dialog in app.json"
    return dialog


def _session_button(app_config: dict, label: str) -> dict:
    buttons = app_config["panes"]["session-blotter"]["buttons"]
    button = next((b for b in buttons if b["label"] == label), None)
    assert button, f"no {label!r} button on the session blotter"
    return button


def _leaves(item: dict):
    """The fields one dialog item stands for: itself, a `{ row }`'s, or a
    bounded `{ group, fields }` section's (mkui 1.2.0)."""
    if "row" in item:
        for f in item["row"]:
            yield from _leaves(f)
    elif "fields" in item:
        for f in item["fields"]:
            yield from _leaves(f)
    else:
        yield item


def _dialog_field_names(dialog: dict) -> set[str]:
    """Named payload fields of a dialog spec, flattening rows and sections."""
    return {
        f["name"] for item in dialog.get("fields", []) for f in _leaves(item) if "name" in f
    }


def _menubar_pane_ids(menubar: list) -> list[str]:
    return [
        item["args"]
        for menu in menubar
        for item in menu.get("items", [])
        if item.get("action") == "pane.show"
    ]


def _button_pane_ids(panes: dict) -> list[str]:
    """Panes a toolbar button opens (`pane.show` actions)."""
    return [
        b["action"]["args"]
        for spec in panes.values()
        for b in spec.get("buttons", [])
        if b["action"].get("type") == "action" and b["action"].get("name") == "pane.show"
    ]


def _pane_frame_ids(app_config: dict) -> dict[str, str]:
    """Map each pane id to the frame that hosts it."""
    hosts = {}
    for frame in app_config["frames"]:
        for pane_id in _frame_pane_ids(frame["layout"]):
            hosts[pane_id] = frame["id"]
    return hosts


class TestPaneReferences:
    def test_menubar_references_existing_panes(self, app_config):
        panes = app_config["panes"]
        for pane_id in _menubar_pane_ids(app_config["menubar"]):
            assert pane_id in panes, f"menubar opens unknown pane {pane_id!r}"

    def test_frames_reference_existing_panes(self, app_config):
        panes = app_config["panes"]
        for frame in app_config["frames"]:
            for pane_id in _frame_pane_ids(frame["layout"]):
                assert pane_id in panes, f"frame {frame['id']!r} hosts unknown pane {pane_id!r}"

    def test_every_pane_is_reachable(self, app_config):
        """A pane nobody opens is dead config."""
        referenced = set(_menubar_pane_ids(app_config["menubar"]))
        referenced.update(_button_pane_ids(app_config["panes"]))
        for frame in app_config["frames"]:
            referenced.update(_frame_pane_ids(frame["layout"]))
        orphans = set(app_config["panes"]) - referenced
        assert not orphans, f"panes defined but never opened: {sorted(orphans)}"


class TestPaneModules:
    def test_custom_pane_types_have_modules(self, app_config, index_imports):
        """Every non-builtin pane type is backed by a module index.html loads."""
        imported = {Path(p).stem for p in index_imports}
        for pane_id, spec in app_config["panes"].items():
            pane_type = spec["type"]
            if pane_type.startswith("mkio-"):
                continue
            assert (STATIC / "panes" / f"{pane_type}.js").is_file(), \
                f"pane {pane_id!r} has type {pane_type!r} with no panes/{pane_type}.js"
            assert pane_type in imported, \
                f"panes/{pane_type}.js is never imported by index.html"

    def test_imports_resolve_to_existing_files(self, index_imports):
        """Catches an import left behind by a deleted pane module."""
        for spec in index_imports:
            if not spec.startswith("/static/"):
                continue  # mkui/mkio are served by the framework
            path = STATIC / spec[len("/static/"):]
            assert path.is_file(), f"index.html imports missing file {spec}"

    def test_no_unused_pane_modules(self, app_config, index_imports):
        """A module on disk that no pane type uses is dead code."""
        used = {spec["type"] for spec in app_config["panes"].values()}
        for module in (STATIC / "panes").glob("*.js"):
            assert module.stem in used, f"panes/{module.name} matches no pane type"


@pytest.fixture(scope="module")
def pane_sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted((STATIC / "panes").glob("*.js"))}


def _resolve_js_import(spec: str, js_file: Path) -> Path | None:
    """Map an import specifier to the file the server would serve, or None
    for specifiers outside the trees we can check."""
    import mkui

    if spec.startswith("/mkui/"):
        return Path(mkui.static_dir) / spec[len("/mkui/"):]
    if spec.startswith("/static/"):
        return STATIC / spec[len("/static/"):]
    if spec.startswith("."):
        return (js_file.parent / spec).resolve()
    return None


class TestPaneModuleIntegrity:
    """The pane modules are plain ES modules with no build step, so a broken
    import, a renamed export, or a stale service name only fails when the
    pane is opened in a browser."""

    IMPORT_RE = re.compile(r'import\s+(?:\{([^}]*)\}\s+from\s+)?"([^"]+)"')

    def _imports(self, pane_sources):
        for name, source in pane_sources.items():
            js_file = STATIC / "panes" / name
            for match in self.IMPORT_RE.finditer(source):
                names = [n.strip() for n in (match.group(1) or "").split(",") if n.strip()]
                yield name, names, match.group(2), _resolve_js_import(match.group(2), js_file)

    def test_imports_resolve(self, pane_sources):
        for name, _, spec, target in self._imports(pane_sources):
            assert target is not None, f"{name} imports unresolvable path {spec!r}"
            assert target.is_file(), f"{name} imports missing file {spec!r} ({target})"

    def test_named_imports_are_exported(self, pane_sources):
        """Catches importing a symbol the installed mkui (or a local module)
        no longer exports — e.g. openDialog from mkui-dialog.js."""
        for name, names, spec, target in self._imports(pane_sources):
            source = target.read_text(encoding="utf-8")
            for symbol in names:
                exported = re.search(
                    rf'export\s+(?:async\s+)?(?:function|const|let|class)\s+{symbol}\b', source
                ) or re.search(rf'export\s*\{{[^}}]*\b{symbol}\b[^}}]*\}}', source)
                assert exported, f"{name} imports {symbol!r} which {spec} does not export"

    def test_services_called_from_js_exist(self, pane_sources, known_services):
        for name, source in pane_sources.items():
            for service in re.findall(r'client\.(?:send|subscribe)\(\s*"(\w+)"', source):
                assert service in known_services, \
                    f"{name} calls unknown service {service!r}"

    def test_transaction_ops_called_from_js_exist(self, pane_sources, toml_config):
        """A dead op name nacks the transaction only when the button is
        clicked. Covers both client.send(..., {op}) and dialog submit specs,
        including ternaries like `op: isEdit ? "update" : "add"`."""
        pairs = set()
        for name, source in pane_sources.items():
            for service, op in re.findall(r'client\.send\("(\w+)".*\{ op: "(\w+)" \}', source):
                pairs.add((name, service, op))
            for service, op_expr in re.findall(r'service:\s*"(\w+)",\s*op:\s*([^}]+)', source):
                for op in re.findall(r'"(\w+)"', op_expr):
                    pairs.add((name, service, op))
        assert pairs, "no transaction ops referenced by pane modules"
        for name, service, op in pairs:
            spec = toml_config["services"].get(service, {})
            if spec.get("protocol") != "transaction":
                continue
            assert op in spec["ops"], f"{name} sends unknown {service} op {op!r}"

    def test_fix_cmd_commands_have_dispatch_branches(self, pane_sources):
        """Same guard app.json gets, for commands sent from pane JS. The
        replay pane derives its command from the button action, so its
        candidates are matched by their _replay suffix."""
        handled = _fix_cmd_commands()
        used = set()
        for name, source in pane_sources.items():
            if 'client.send("fix_cmd"' not in source:
                continue
            for command in re.findall(r'command:\s*"(\w+)"', source):
                used.add((name, command))
            for command in re.findall(r'"(\w+_replay)"', source):
                used.add((name, command))
        assert used, "no fix_cmd commands referenced by pane modules"
        for name, command in used:
            assert command in handled, f"{name} sends unhandled fix_cmd command {command!r}"

    def test_dialog_fields_match_transaction_schema(self, app_config, toml_config):
        """A dialog's field names become the transaction payload verbatim; a
        name the op does not list is silently dropped (or, if a required
        field goes missing, nacks on save)."""
        services = toml_config["services"]
        checked = 0
        for node in _walk_dicts(app_config):
            submit = node.get("submit")
            if not isinstance(submit, dict) or "fields" not in node:
                continue
            svc = services.get(submit.get("service"), {})
            if svc.get("protocol") != "transaction":
                continue
            allowed = set()
            for entry in svc["ops"][submit["op"]]:
                allowed.update(entry["fields"])
                allowed.update(entry.get("key", []))
            names = _dialog_field_names(node)
            unknown = names - allowed
            assert not unknown, \
                f"dialog {node.get('title')!r} sends fields {submit['service']} ignores: {sorted(unknown)}"
            checked += 1
        assert checked, "no transaction dialogs found in app.json"

    def test_button_row_tokens_name_real_columns(self, app_config, toml_config):
        """`${row.X}` resolves against the pane's service rows; a token naming
        a column the rows do not carry silently interpolates empty."""
        checked = 0
        for pane_id, spec in app_config["panes"].items():
            columns = _service_columns(toml_config, spec.get("service"))
            if columns is None or "buttons" not in spec:
                continue
            tokens = set(re.findall(r"\$\{row\.(\w+)\}", json.dumps(spec["buttons"])))
            unknown = tokens - columns
            assert not unknown, \
                f"pane {pane_id!r} buttons reference columns {spec['service']} rows lack: {sorted(unknown)}"
            checked += 1
        assert checked, "no panes with buttons and a table-backed service"

    def test_change_seq_dialog_fields_match_dispatch(self, app_config):
        """The Change Seq dialog's field names become the fix_cmd payload; the
        dispatch reads them by exact key, so a renamed field is silently
        dropped and the change falls back to defaults."""
        button = _session_button(app_config, "Change Seq")
        assert button["action"]["type"] == "dialog"
        dialog = button["action"]["dialog"]
        assert dialog["submit"]["op"] == "reset_sequence"
        fields = _dialog_field_names(dialog)
        assert {"session_id", "tx_seq_num", "rx_seq_num"} <= fields
        dispatch = (ROOT / "mkfix" / "services" / "fix_command.py").read_text(encoding="utf-8")
        branch = dispatch.split('command == "reset_sequence"')[1].split("elif")[0]
        for field in fields:
            assert field in branch, f"change dialog field {field!r} not read by reset_sequence dispatch"

    def test_reset_seq_is_one_click_reset_to_one(self, app_config):
        """Reset Seq is a session reset, not a form: it submits reset_sequence
        straight away with both numbers at 1. The dispatch also defaults to
        1, but the literals keep the button's meaning explicit."""
        button = _session_button(app_config, "Reset Seq")
        action = button["action"]
        assert action["type"] == "transaction"
        assert (action["service"], action["op"]) == ("fix_cmd", "reset_sequence")
        assert action["data"] == {
            "session_id": "${row.session_id}",
            "tx_seq_num": 1,
            "rx_seq_num": 1,
        }

    def test_seq_buttons_only_while_down(self, app_config):
        """Both sequence buttons act on a stopped session only: resetting a
        live session's numbers would desync the counterparty."""
        for label in ("Reset Seq", "Change Seq"):
            when = _session_button(app_config, label)["enable"]["when"]
            assert _conditions(when) == {"status": ["DOWN", "ERROR"]}, \
                f"{label} gate must allow only DOWN/ERROR: {when}"


class TestServiceReferences:
    def test_pane_services_exist(self, app_config, known_services):
        for pane_id, spec in app_config["panes"].items():
            if "service" in spec:
                assert spec["service"] in known_services, \
                    f"pane {pane_id!r} uses unknown service {spec['service']!r}"

    def test_button_action_services_exist(self, app_config, known_services):
        for pane_id, spec in app_config["panes"].items():
            for button in spec.get("buttons", []):
                action = button["action"]
                if action["type"] == "action":
                    continue
                target = action.get("service") or action.get("dialog", {}).get("submit", {}).get("service")
                assert target in known_services, \
                    f"pane {pane_id!r} button {button['label']!r} calls unknown service {target!r}"

    def test_no_dialog_blocks_the_application(self, app_config):
        """A dialog floats over a workspace that stays live: the blotters keep
        updating, another order can be looked up, a second dialog opened. It
        acts on the rows it was opened on (mkui captures them at the click,
        and the title names them), so nothing is lost by letting go of the
        pointer. `modal: true` dims the workspace and stills the menubar
        until the dialog is answered; only a confirmation asks for that, and
        mkui makes those modal by itself."""
        modal = [node.get("title") for node in _walk_dicts(app_config) if node.get("modal")]
        assert modal == [], f"modal dialogs: {modal}"
        dialogs = [b["action"]["dialog"] for pane in app_config["panes"].values()
                   for b in pane.get("buttons", []) if isinstance(b.get("action"), dict)
                   and isinstance(b["action"].get("dialog"), dict)]
        assert len(dialogs) >= 16, "the guard looks at the dialogs it thinks it does"

    def test_fix_cmd_ops_have_dispatch_branches(self, app_config):
        """An op in app.json with no _dispatch branch fails only when the
        button is clicked, and only in the browser."""
        handled = _fix_cmd_commands()
        used = {
            node["op"]
            for node in _walk_dicts(app_config["panes"])
            if node.get("service") == "fix_cmd" and "op" in node
        }
        assert used, "no fix_cmd ops referenced by app.json"
        missing = used - handled
        assert not missing, f"app.json sends unhandled fix_cmd ops: {sorted(missing)}"

    def test_dialog_options_services_are_reqrep(self, app_config, toml_config):
        """optionsFrom fetches via request-reply; a query/stream service there
        nacks the request and leaves the dropdown empty in the browser."""
        def walk_fields(items):
            for item in items:
                if "row" in item:
                    yield from walk_fields(item["row"])
                else:
                    yield item

        for pane_id, spec in app_config["panes"].items():
            for button in spec.get("buttons", []):
                dialog = button["action"].get("dialog", {})
                for field in walk_fields(dialog.get("fields", [])):
                    source = field.get("optionsFrom")
                    if not source:
                        continue
                    service = toml_config["services"].get(source["service"])
                    assert service is not None, \
                        f"pane {pane_id!r} dialog field {field.get('name')!r} " \
                        f"pulls options from unknown service {source['service']!r}"
                    assert service["protocol"] == "reqrep", \
                        f"pane {pane_id!r} dialog field {field.get('name')!r} " \
                        f"pulls options from non-reqrep service {source['service']!r}"

    def test_pane_filters_use_filterable_columns(self, app_config, toml_config):
        """A filter on a non-filterable column is silently ignored server-side."""
        for pane_id, spec in app_config["panes"].items():
            expr = spec.get("filter")
            if not expr:
                continue
            service = toml_config["services"].get(spec["service"], {})
            filterable = set(service.get("filterable", []))
            no_strings = re.sub(r"'[^']*'", "", expr)
            fields = {t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", no_strings)
                      if not t.isupper()}
            assert fields <= filterable, \
                f"pane {pane_id!r} filters on non-filterable fields {sorted(fields - filterable)}"

    def test_new_order_entry_lives_on_client_blotter(self, app_config):
        """The Order Pad pane was replaced by the New dialog in 0.5; order
        entry must stay reachable, and from an empty blotter — a selection
        requirement on the button would dead-lock first use."""
        buttons = {b["label"]: b for b in app_config["panes"]["order-blotter"]["buttons"]}
        new = buttons["New"]
        assert new["action"]["type"] == "dialog"
        assert new["action"]["dialog"]["submit"]["op"] == "send_new_order"
        assert "minSelected" not in new.get("enable", {})
        assert "when" not in new.get("enable", {})

    def test_blotters_split_by_direction(self, app_config):
        """Client and market blotters share services; the direction filter is
        the only thing keeping received orders out of the client view."""
        panes = app_config["panes"]
        assert panes["order-blotter"]["filter"] == "direction == 'TX'"
        assert panes["market-order-blotter"]["filter"] == "direction == 'RX'"
        assert panes["trade-blotter"]["filter"] == "direction == 'RX'"
        assert panes["market-trade-blotter"]["filter"] == "direction == 'TX'"

    def test_sent_orders_button_order(self, app_config):
        """Deliberate 0.6.2 ordering: entry first, then the two amend actions
        with the destructive one last. Each label must keep driving its op —
        a reorder that swaps actions under the labels would be worse than the
        old order."""
        buttons = app_config["panes"]["order-blotter"]["buttons"]
        assert [b["label"] for b in buttons] == ["New", "Replace", "Cancel", "History"]
        ops = {
            b["label"]: b["action"].get("op")
            or b["action"]["dialog"]["submit"]["op"]
            for b in buttons if b["action"]["type"] != "action"
        }
        assert ops == {"New": "send_new_order", "Replace": "send_cancel_replace",
                       "Cancel": "send_cancel"}

    def test_order_blotters_show_the_request_slot(self, app_config, toml_config):
        """Both sides show what is pending and under which ClOrdID — the
        counterparty's request on Received Orders, our own on Sent Orders,
        which alone can be refused and so alone carries Rej Reason. The
        parked replace terms are engine bookkeeping, not a column."""
        slot = ["pending_action", "pending_cl_ord_id", "pending_qty", "pending_price"]
        table = toml_config["tables"]["fix_orders"]["columns"]
        for pane_id in ("order-blotter", "market-order-blotter"):
            pane = app_config["panes"][pane_id]
            assert [c for c in pane["columns"] if c in slot] == slot, pane_id
            assert all(c in pane["labels"] and c in table for c in slot), pane_id
            assert "pending_entered" not in pane["columns"], pane_id
        sent, received = (app_config["panes"][p] for p in ("order-blotter", "market-order-blotter"))
        assert "cxl_rej_reason" in sent["columns"] and "cxl_rej_reason" in sent["labels"]
        assert "cxl_rej_reason" not in received["columns"]
        assert {"cxl_rej_reason", "pending_entered"} <= set(table)

    def test_sent_requests_do_not_gate_on_the_slot(self, app_config):
        """A cancel on top of a pending replace is a scenario worth sending:
        Sent Orders' Replace and Cancel never test pending_action."""
        by = {b["label"]: b for b in app_config["panes"]["order-blotter"]["buttons"]}
        for label in ("Replace", "Cancel"):
            assert "pending_action" not in _conditions(by[label]["enable"]["when"]), label

    def test_received_orders_buttons_gate_on_pending_action(self, app_config):
        """One Accept/Reject pair handles new orders and cancel/replace
        requests alike: both gate on pending_action (a new order arrives as
        pending "New") and dispatch via the request ops, while Fill, the
        unsolicited cancel and the restatement gate on status only — a
        pending request must not block them on the still-working order."""
        buttons = app_config["panes"]["market-order-blotter"]["buttons"]
        assert [b["label"] for b in buttons] == ["Accept", "Reject", "Fill", "Unsol Cxl", "Restate", "History"]
        by = {b["label"]: b for b in buttons}
        pending = {"pending_action": ["New", "Cancel", "Replace"], "session_status": ["ACTIVE"]}
        assert _conditions(by["Accept"]["enable"]["when"]) == pending
        assert _conditions(by["Reject"]["enable"]["when"]) == pending
        assert by["Accept"]["action"]["dialog"]["submit"]["op"] == "accept_request"
        assert by["Reject"]["action"]["dialog"]["submit"]["op"] == "reject_request"
        assert "pending_action" not in _conditions(by["Fill"]["enable"].get("when"))
        assert by["Unsol Cxl"]["action"]["dialog"]["submit"]["op"] == "unsolicited_cancel"
        assert _conditions(by["Unsol Cxl"]["enable"]["when"]) == _conditions(by["Fill"]["enable"]["when"])
        assert by["Restate"]["action"]["dialog"]["submit"]["op"] == "restate_order"
        assert _conditions(by["Restate"]["enable"]["when"]) == _conditions(by["Fill"]["enable"]["when"])

    def test_order_and_trade_actions_gate_on_live_session(self, app_config):
        """Every button that acts on an existing order or trade requires the
        row's session to be up (session_status is the owning session's live
        status, joined onto the row by orders_query/executions_query). New
        order entry is exempt — its
        dialog picks the session itself, and the server rejects a dead one."""
        gated = {
            "order-blotter": ["Replace", "Cancel"],
            "market-order-blotter": ["Accept", "Reject", "Fill", "Unsol Cxl", "Restate"],
            "market-trade-blotter": ["Correct", "Bust", "Re-notify"],
            "trade-blotter": ["DK"],
        }
        for pane_id, labels in gated.items():
            by = {b["label"]: b for b in app_config["panes"][pane_id]["buttons"]}
            for label in labels:
                match = _conditions(by[label]["enable"]["when"]).get("session_status")
                assert match == ["ACTIVE"], \
                    f"{pane_id} {label} must gate on session_status ACTIVE"

    def test_multi_row_gates_check_every_selected_row(self, app_config):
        """Buttons that submit per selected row must gate on all of them —
        `row` alone would test the first selected row and let a mixed
        selection act on rows in the wrong state."""
        checked = 0
        for pane_id, spec in app_config["panes"].items():
            for button in spec.get("buttons", []):
                when = button.get("enable", {}).get("when")
                if not when or button.get("unit") == "row":
                    continue
                checked += 1
                assert when.startswith("ALL(rows, r -> "), \
                    f"pane {pane_id!r} button {button['label']!r} gate must quantify over rows"
        assert checked
    def test_every_send_action_dialog_offers_extra_tags(self, app_config):
        """Every button that sends a FIX message must expose the optional
        extra_tags field — the whole point of the feature is that no send
        path is exempt. Flatten row groups: fields may nest one level."""
        send_panes = ["order-blotter", "market-order-blotter", "market-trade-blotter",
                      "trade-blotter"]
        checked = 0
        for pane_id in send_panes:
            for button in app_config["panes"][pane_id]["buttons"]:
                if button["action"]["type"] == "action":
                    continue
                dialog = button["action"]["dialog"]
                if dialog["submit"]["service"] != "fix_cmd":
                    continue
                names = _dialog_field_names(dialog)
                assert "extra_tags" in names, \
                    f"{pane_id} {button['label']} dialog must offer extra_tags"
                assert not any(
                    f.get("required") for f in dialog["fields"]
                    if f.get("name") == "extra_tags"
                ), "extra_tags must stay optional"
                checked += 1
        assert checked >= 8

    def test_renotify_gates_on_the_dk_alone(self, app_config):
        """Re-notify answers a DontKnowTrade, so it opens on a DK'd row
        whatever the trade's state — a DK'd bust is restated like a DK'd fill,
        the one action a busted row takes — and on nothing else; the preview
        shows the ExecRefID(19) a correction or bust will carry again."""
        from mkio import expr
        buttons = app_config["panes"]["market-trade-blotter"]["buttons"]
        assert [b["label"] for b in buttons] == ["Correct", "Bust", "Re-notify", "History"]
        when = next(b for b in buttons if b["label"] == "Re-notify")["enable"]["when"]
        def enabled(*rows):
            return expr.evaluate(when, {"rows": [
                {"session_status": "ACTIVE", "exec_type": "Fill", "dk_reason": "", **r} for r in rows]})
        assert enabled({"dk_reason": "WrongSide"})
        assert enabled({"dk_reason": "Other", "exec_type": "Cancel"})
        assert enabled({"dk_reason": "Other", "exec_type": "TradeCancel"})
        assert not enabled({})
        assert not enabled({"dk_reason": "WrongSide"}, {})
        assert not enabled({"dk_reason": "WrongSide", "session_status": "DOWN"})
        dialog = _find_dialog(app_config, "renotify_trade")
        preview = dialog["fields"][-1]["compute"]
        row = {"exec_ref_id": "EX1", "last_qty": 40.0, "last_price": 150.5}
        assert expr.evaluate(preview, {"text": "", "extra_tags": "", "row": row}) == "17=(new)|19=EX1|32=40|31=150.5"

    def test_trade_blotters_show_the_dispute_and_the_order(self, app_config, toml_config):
        """Both sides of a DK are visible where they happened: the
        counterparty's on Sent Trades, our own on Received Trades — reason
        and text, highlighted alike — and every trade names its OrderID(37)."""
        table = toml_config["tables"]["fix_executions"]["columns"]
        sent, received = (app_config["panes"][p] for p in ("market-trade-blotter", "trade-blotter"))
        for pane in (sent, received):
            for col in ("order_id", "dk_reason", "dk_text"):
                assert col in pane["columns"] and col in pane["labels"] and col in table, (pane["title"], col)
            assert pane["columns"].index("dk_text") == pane["columns"].index("dk_reason") + 1
        assert received["styles"]["dk_reason"] == sent["styles"]["dk_reason"]

    def test_a_dk_does_not_end_the_received_trade(self, app_config):
        """A DK'd trade can be DK'd again (another reason, a lost message),
        so the DK button never tests the mark; Re-notify, which does, is the
        market side's alone."""
        received = {b["label"]: b for b in app_config["panes"]["trade-blotter"]["buttons"]}
        assert "dk_reason" not in _conditions(received["DK"]["enable"]["when"])
        assert "Re-notify" not in received

    def test_dk_reason_options_cover_every_shipped_dictionary(self, app_config):
        """The DK dialog lists DKReason(127) codes by hand; a regenerated
        dictionary that adds one would otherwise leave it unreachable."""
        from mkfix.fix.dictionary import FixDictionary, STANDARD_VERSIONS
        dialog = _find_dialog(app_config, "dk_trade")
        field = next(f for f in dialog["fields"] if f.get("name") == "dk_reason")
        offered = [o["value"] for o in field["options"]]
        assert len(offered) == len(set(offered))
        assert field["value"] in offered
        defined = set()
        for version in STANDARD_VERSIONS:
            defined |= set(FixDictionary(version).enums["127"])
        assert set(offered) == defined

    def test_restatement_reason_options_cover_every_shipped_dictionary(self, app_config):
        """The Restate dialog and the template editor list
        ExecRestatementReason(378) codes by hand, plus a blank that withholds
        the tag; a regenerated dictionary that adds a code would otherwise
        leave it unreachable."""
        from mkfix.fix.dictionary import FixDictionary, STANDARD_VERSIONS
        defined = set()
        for version in STANDARD_VERSIONS:
            defined |= set(FixDictionary(version).enums.get("378", {}))
        dialog = _find_dialog(app_config, "restate_order")
        field = next(f for f in dialog["fields"] if f.get("name") == "restate_reason")
        offered = [o["value"] for o in field["options"]]
        assert len(offered) == len(set(offered))
        assert field["value"] in offered
        assert set(offered) == defined | {""}
        assert not field.get("required"), "a blank reason withholds 378"
        edit = next(b for b in app_config["panes"]["templates"]["buttons"]
                    if b["label"] == "Edit")["action"]["dialog"]
        editor = next(f for item in edit["fields"] for f in _leaves(item)
                      if f.get("name") == "restate_reason")
        assert editor["options"] == field["options"]
        assert editor["showWhen"] == "row.scope == 'restate'"

    def test_order_dialog_codes_are_dictionary_values(self, app_config):
        """Side, Order Type and Time in Force offer FIX codes by hand; each
        must be a value some standard dictionary defines for its tag (a typo
        would silently send a bad code), and New and Replace offer the same
        lists. The lists are deliberately not narrowed to the session's
        version: a value the session's dictionary lacks (Market on Close on
        4.4, At the Close on 4.1) is a test scenario, so the engine sends
        whatever code is picked and the labels only advise where the value
        is standard."""
        from mkfix.fix.dictionary import STANDARD_VERSIONS, FixDictionary
        dictionaries = [FixDictionary(v) for v in STANDARD_VERSIONS]
        tags = {"side": "54", "ord_type": "40", "tif": "59"}
        lists = {}
        for op in ("send_new_order", "send_cancel_replace"):
            for item in _find_dialog(app_config, op)["fields"]:
                for f in _leaves(item):
                    if f.get("name") in tags:
                        lists[(op, f["name"])] = [o["value"] for o in f["options"]]
        for (op, name), values in lists.items():
            assert values, f"{op} {name} offers nothing"
            assert len(values) == len(set(values)), f"{op} {name} repeats a code"
            for v in values:
                assert any(d.has_enum(tags[name], v) for d in dictionaries), \
                    f"{op} {name} offers {v!r}, not a value of tag {tags[name]} in any standard dictionary"
        for name in tags:
            assert lists[("send_new_order", name)] == lists[("send_cancel_replace", name)]
        assert lists[("send_new_order", "ord_type")] == ["1", "2", "5", "B", "I"]
        assert lists[("send_new_order", "tif")] == ["0", "1", "2", "3", "4", "5", "6", "7"]
        assert "6" in lists[("send_new_order", "side")], "Sell Short Exempt"

    def test_order_dialog_version_notes_follow_the_dictionaries(self, app_config):
        """The version annotations on the on-close values are advisory
        labels, so a regenerated dictionary can't move them; this pins the
        facts they state. Market/Limit on Close (40=5/B) are defined through
        4.3, dropped by 4.4 (the label says <= 4.3; 5.0 restored them), and
        At the Close (59=7) exists from 4.2. Each annotated value also shows
        a readonly note only while it is selected."""
        from mkfix.fix.dictionary import FixDictionary
        for code in ("5", "B"):
            assert FixDictionary("FIX.4.3").has_enum("40", code)
            assert not FixDictionary("FIX.4.4").has_enum("40", code)
            assert FixDictionary("FIX.5.0").has_enum("40", code)
        assert not FixDictionary("FIX.4.1").has_enum("59", "7")
        assert FixDictionary("FIX.4.2").has_enum("59", "7")
        for op in ("send_new_order", "send_cancel_replace"):
            labels = {}
            notes = []
            for item in _find_dialog(app_config, op)["fields"]:
                if item.get("type") == "readonly" and "showWhen" in item:
                    notes.append(item["showWhen"])
                for f in _leaves(item):
                    if f.get("name") in ("ord_type", "tif"):
                        labels.update({(f["name"], o["value"]): o["label"] for o in f["options"]})
            assert labels[("ord_type", "5")].endswith("(<= FIX 4.3)")
            assert labels[("ord_type", "B")].endswith("(<= FIX 4.3)")
            assert labels[("tif", "7")].endswith("(>= FIX 4.2)")
            assert "(" not in labels[("ord_type", "I")], "Funari carries no annotation"
            assert "(" not in labels[("tif", "2")], "At the Opening is in every version"
            assert notes == ["CONTAINS(['5', 'B'], ord_type)", "tif == '7'"], op

    def test_new_order_dialog_has_no_account_field(self, app_config):
        """Account rides as an extra tag (1=...); a dedicated field would be
        silently dropped by the dispatch, which no longer reads it."""
        dialog = _find_dialog(app_config, "send_new_order")
        assert "account" not in _dialog_field_names(dialog)

    def test_replace_dialog_offers_every_new_order_field_prefilled(self, app_config, toml_config):
        """The Replace dialog shows every New-dialog field (session aside —
        a replace stays on its order's session) prefilled from the row's
        as-submitted terms, so the last entered values can be edited."""
        new_fields = _dialog_field_names(_find_dialog(app_config, "send_new_order")) - {"session_id"}
        replace = _find_dialog(app_config, "send_cancel_replace")
        replace_fields = _dialog_field_names(replace)
        assert new_fields <= replace_fields, \
            f"Replace dialog lacks New-dialog fields: {sorted(new_fields - replace_fields)}"
        columns = set(toml_config["tables"]["fix_orders"]["columns"])
        prefills = {}
        for item in replace["fields"]:
            for f in _leaves(item):
                if f.get("name") in new_fields:
                    prefills[f["name"]] = f.get("value", "")
        expected = {
            "symbol": "symbol", "side": "side_code", "qty": "entered_qty",
            "ord_type": "ord_type_code", "price": "entered_price", "tif": "tif_code",
            "extra_tags": "extra_tags",
        }
        for name, column in expected.items():
            assert prefills.get(name) == "${row.%s}" % column, \
                f"Replace field {name!r} must prefill from row.{column}"
            assert column in columns
        assert set(replace["rowData"]) == {"session_id", "orig_cl_ord_id"}, \
            "only the identity rides as rowData — everything else is an editable field"

    def test_expiry_fields_are_native_pickers(self, app_config):
        """The order dialogs take ExpireTime/ExpireDate through one mkui
        native-picker field; the Replace dialog reads the row's FIX stamps
        back through `parse` formats that match what the engine records. The
        stamp's precision is the session's (its Timestamps setting), so no
        dialog asks for one."""
        import mkio.expr
        mkio.expr.compile("IF(row.expire_time != '', row.expire_time, row.expire_date)")
        for op in ("send_new_order", "send_cancel_replace"):
            fields = {
                f["name"]: f for item in _find_dialog(app_config, op)["fields"]
                for f in _leaves(item) if f.get("name")
            }
            assert fields["expire_time"]["type"] == "datetime"
            assert fields["expire_time"].get("time") == "optional", \
                "one Expire field: a date alone is an ExpireDate, with a time an ExpireTime"
            assert fields["expire_time"].get("step") == 1, "the picker must take seconds"
            assert "expire_date" not in fields and "expire_precision" not in fields
            if op == "send_cancel_replace":
                assert fields["expire_time"]["value"] == \
                    "${IF(row.expire_time != '', row.expire_time, row.expire_date)}"
                assert fields["expire_time"]["parse"] == \
                    ["%Y%m%d-%H:%M:%S.%f", "%Y%m%d-%H:%M:%S", "%Y%m%d"]

    def test_order_dialogs_fold_expiry_and_save_as_under_advanced(self, app_config):
        """New and Replace fold the Expire picker and the Save-as name — the
        two fields most orders leave blank — into a collapsible section,
        closed by default, sitting right above the closing preview. The
        section is bounded by its own `fields` (mkui 1.2.0; an unbounded
        header would claim the preview too). It has no `remember`, so it
        opens folded every time; mkui unfolds it on a validation error
        under it, and a folded head counts the fields edited beneath it."""
        for op in ("send_new_order", "send_cancel_replace"):
            fields = _find_dialog(app_config, op)["fields"]
            heads = [f for f in fields if "group" in f]
            assert len(heads) == 1, f"{op}: one section"
            head = heads[0]
            assert {k: v for k, v in head.items() if k != "fields"} == \
                {"group": "Advanced", "collapsible": True, "collapsed": True}, op
            under = [f["name"] for item in head["fields"] for f in _leaves(item)]
            assert under == ["expire_time", "save_as"], f"{op}: {under}"
            assert fields[-2] is head and fields[-1].get("label") == "Terms as tags", \
                f"{op}: the section sits right above the closing preview"

    def test_market_dialogs_echo_extra_tags(self, app_config, toml_config):
        """Accept/Reject prefill the pending request's custom tags and Fill the
        order's, so inbound tags can be viewed, edited, and echoed back."""
        expected = {
            "accept_request": "${row.pending_extra_tags}",
            "reject_request": "${row.pending_extra_tags}",
            "fill_order": "${row.extra_tags}",
        }
        columns = set(toml_config["tables"]["fix_orders"]["columns"])
        assert {"pending_extra_tags", "extra_tags"} <= columns
        for op, value in expected.items():
            dialog = _find_dialog(app_config, op)
            field = next(
                f for item in dialog["fields"]
                for f in _leaves(item)
                if f.get("name") == "extra_tags"
            )
            assert field.get("value") == value, \
                f"{op} dialog must prefill extra_tags from {value}"

    def test_trade_dialogs_open_on_the_trades_extra_tags(self, app_config, toml_config):
        """Correct, Bust and Re-notify restate a trade, so they open on the
        tags its latest report went out with (fix_executions.extra_tags)."""
        assert "extra_tags" in toml_config["tables"]["fix_executions"]["columns"]
        for op in ("correct_trade", "bust_trade", "renotify_trade"):
            dialog = _find_dialog(app_config, op)
            field = next(f for item in dialog["fields"] for f in _leaves(item)
                         if f.get("name") == "extra_tags")
            assert field.get("value") == "${row.extra_tags}", op

    def test_handl_inst_options_are_the_dictionaries_codes(self, app_config):
        """HandlInst(21) is listed by hand on New, Replace and the template
        editor; every shipped dictionary defines the same three codes."""
        from mkfix.fix.dictionary import FixDictionary, STANDARD_VERSIONS
        defined = {frozenset(FixDictionary(v).enums["21"]) for v in STANDARD_VERSIONS}
        assert len(defined) == 1
        edit = next(b for b in app_config["panes"]["templates"]["buttons"]
                    if b["label"] == "Edit")["action"]["dialog"]
        dialogs = {op: _find_dialog(app_config, op) for op in ("send_new_order", "send_cancel_replace")}
        dialogs["templates"] = edit
        for op, dialog in dialogs.items():
            field = next(f for item in dialog["fields"] for f in _leaves(item)
                         if f.get("name") == "handl_inst")
            assert field["label"] == "Handling Inst (21)" and field["type"] == "select", op
            assert {o["value"] for o in field["options"]} == set(next(iter(defined))), op
            assert all(o["label"].startswith(o["value"] + " - ") for o in field["options"]), op
        new = next(f for f in dialogs["send_new_order"]["fields"] if f.get("name") == "handl_inst")
        assert new["value"] == "1", "the engine's default, shown"
        replace = next(f for f in dialogs["send_cancel_replace"]["fields"] if f.get("name") == "handl_inst")
        assert "row.handl_inst_code" in replace["value"], "Replace opens on the order's as-sent code"
        for op in ("send_cancel", "accept_request", "fill_order"):
            assert "handl_inst" not in _dialog_field_names(_find_dialog(app_config, op)), \
                f"{op}: only an order or replace request carries HandlInst"

    def test_every_send_dialog_offers_text(self, app_config):
        """Text(58) is typed in every order and trade dialog, right above
        Extra Tags (which can still override it), and never prefilled."""
        ops = ("send_new_order", "send_cancel_replace", "send_cancel", "accept_request",
               "reject_request", "fill_order", "dk_trade", "correct_trade", "bust_trade",
               "renotify_trade")
        for op in ops:
            names = [f.get("name") for item in _find_dialog(app_config, op)["fields"]
                     for f in _leaves(item)]
            assert "text" in names, op
            assert names.index("text") == names.index("extra_tags") - 1, op
            field = next(f for item in _find_dialog(app_config, op)["fields"]
                         for f in _leaves(item) if f.get("name") == "text")
            assert field.get("value", "") == "", f"{op}: text belongs to one message"

    def test_blotters_show_handling_text_and_extra_tags(self, app_config, toml_config):
        orders = set(toml_config["tables"]["fix_orders"]["columns"])
        trades = set(toml_config["tables"]["fix_executions"]["columns"])
        for pane_id in ("order-blotter", "market-order-blotter"):
            pane = app_config["panes"][pane_id]
            shown = {"handl_inst", "sent_text", "text", "extra_tags"}
            assert shown <= set(pane["columns"]) and shown <= orders, pane_id
            assert pane["labels"]["sent_text"] == "Sent Text", pane_id
            assert pane["labels"]["text"] == "Rcvd Text", pane_id
        for pane_id in ("trade-blotter", "market-trade-blotter"):
            pane = app_config["panes"][pane_id]
            assert {"text", "extra_tags"} <= set(pane["columns"]) and "extra_tags" in trades, pane_id
            assert "handl_inst" not in pane["columns"], pane_id

    def test_pane_columns_exist_in_service_rows(self, app_config, toml_config):
        """A misspelled column renders as a permanently empty blotter column."""
        checked = 0
        for pane_id, spec in app_config["panes"].items():
            columns = _service_columns(toml_config, spec.get("service"))
            if columns is None or "columns" not in spec:
                continue
            checked += 1
            unknown = set(spec["columns"]) - columns
            assert not unknown, f"pane {pane_id!r} shows unknown columns {sorted(unknown)}"
        assert checked, "no pane columns checked against a service's rows"

    def test_blotter_titles_state_direction(self, app_config):
        """Blotter titles say the direction outright (Sent = TX, Received = RX);
        a title contradicting the pane's filter would mislead."""
        panes = app_config["panes"]
        for pane_id in ("order-blotter", "trade-blotter",
                        "market-order-blotter", "market-trade-blotter"):
            spec = panes[pane_id]
            word = "Sent" if "'TX'" in spec["filter"] else "Received"
            assert spec["title"].startswith(word), \
                f"pane {pane_id!r} titled {spec['title']!r} but filters {spec['filter']!r}"

    def test_client_and_market_blotters_in_separate_frames(self, app_config):
        """Since 0.6 both sides of the flow are visible at once; folding them
        back into shared tabs hides one side and breaks the two-window demo."""
        hosts = _pane_frame_ids(app_config)
        assert hosts["order-blotter"] != hosts["market-order-blotter"]
        assert hosts["trade-blotter"] != hosts["market-trade-blotter"]


class TestSavedLayouts:
    """mkui's Layout menu is opt-in per app and its three halves fail silently
    when one is missing: the `layouts` block constructs the LayoutManager (no
    block, no `layout.*` actions), the menubar declares the entries, and the
    server carries the store the client calls. Keep them together."""

    def _layout_menu(self, app_config):
        menus = [m for m in app_config["menubar"] if m.get("label") == "Layout"]
        assert len(menus) == 1, "expected exactly one Layout menu"
        return menus[0]

    def test_layouts_block_enables_the_feature(self, app_config):
        layouts = app_config.get("layouts")
        assert isinstance(layouts, dict), "app.json needs a `layouts` block for the Layout menu to do anything"
        assert layouts.get("key") == "mkfix", "store key must not ride on the app title"
        assert layouts.get("store", "mkio") == "mkio"

    def test_layout_menu_wires_every_action(self, app_config):
        items = self._layout_menu(app_config)["items"]
        actions = {i.get("action") for i in items if "action" in i}
        assert actions == {"layout.save", "layout.reset"}
        assert any(i.get("layouts") is True for i in items), "Restore needs a `layouts: true` submenu"

    def test_layout_menu_precedes_window_menu(self, app_config):
        labels = [m.get("label") for m in app_config["menubar"]]
        assert labels.index("Layout") == labels.index("Window") - 1

    def test_server_carries_the_mkio_layout_store(self, toml_config):
        """The shapes mkui's MkioLayoutStore calls (`mkui init` scaffold)."""
        services = toml_config["services"]
        tables = toml_config["tables"]
        assert set(tables["mkui_layouts"]["columns"]) >= {"id", "app", "owner", "saved", "layout"}
        store = services["mkui_layouts"]
        assert store["protocol"] == "transaction"
        assert set(store["ops"]) == {"save", "delete"}
        assert set(store["ops"]["save"]["fields"]) == {"app", "owner", "layout"}
        assert store["ops"]["save"]["table"] == "mkui_layouts"
        assert store["ops"]["delete"]["key"] == ["id"]
        for name, params in (("mkui_layouts_list", {":app", ":owner"}), ("mkui_layouts_get", {":id"})):
            svc = services[name]
            assert svc["protocol"] == "reqrep"
            assert "mkui_layouts" in svc["sql"]
            assert params <= set(re.findall(r":\w+", svc["sql"]))

    def test_layout_store_has_no_login_gated_access(self, toml_config):
        """mkfix has no `_mkio_users`; an `access` view naming it would fail."""
        assert "_mkio_users" not in toml_config["tables"]
        services = toml_config["services"]
        for name in ("mkui_layouts", "mkui_layouts_list", "mkui_layouts_get"):
            assert "access" not in services[name]
        for op in services["mkui_layouts"]["ops"].values():
            assert "access" not in op


    def test_store_matches_the_client_calls(self, toml_config):
        """mkui's MkioLayoutStore (lib/layouts.js) sends `save` with app/owner/
        layout, `delete` with id, and requests `_list` with app/owner and `_get`
        with id. Read those shapes off the installed client so the TOML can't
        drift from what the client actually sends."""
        import mkui
        src = (Path(mkui.static_dir) / "src" / "lib" / "layouts.js").read_text(encoding="utf-8")
        save = re.search(r"\{\s*app:[^}]*owner,?\s*layout:[^}]*\}\s*,\s*\{\s*op:\s*\"save\"", src)
        assert save, "client save payload not found in mkui's layouts.js"
        assert re.search(r"\{\s*id\s*\},\s*\{\s*op:\s*\"delete\"", src)
        assert re.search(r"request\(this\._list,\s*\{\s*app:[^}]*owner\s*\}", src)
        assert re.search(r"request\(this\._get,\s*\{\s*id\s*\}", src)
        ops = toml_config["services"]["mkui_layouts"]["ops"]
        assert set(ops["save"]["fields"]) == {"app", "owner", "layout"}
        assert set(ops["save"].get("defaults", {})) == set(), "the client always sends every field"

    def test_store_sql_round_trips_a_layout(self, toml_config):
        """Drive the table and the three services' SQL through sqlite the way
        the client does: save twice, list newest first, get one, delete one."""
        import sqlite3
        tables = toml_config["tables"]
        services = toml_config["services"]
        cols = tables["mkui_layouts"]["columns"]
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE mkui_layouts (" + ", ".join(f"{k} {v}" for k, v in cols.items()) + ")")
        fields = services["mkui_layouts"]["ops"]["save"]["fields"]
        insert = f"INSERT INTO mkui_layouts ({', '.join(fields)}) VALUES ({', '.join(':' + f for f in fields)})"
        for n in (1, 2):
            db.execute(insert, {"app": "mkfix", "owner": "", "layout": json.dumps({"version": 1, "n": n})})
        db.execute(insert, {"app": "other-app", "owner": "", "layout": "{}"})
        db.execute(insert, {"app": "mkfix", "owner": "someone", "layout": "{}"})

        rows = db.execute(services["mkui_layouts_list"]["sql"], {"app": "mkfix", "owner": ""}).fetchall()
        assert [r["id"] for r in rows] == [2, 1], "newest first, scoped to app and owner"
        assert set(rows[0].keys()) == {"id", "saved"}
        assert rows[0]["saved"], "saved must default to the insert time"

        got = db.execute(services["mkui_layouts_get"]["sql"], {"id": 1}).fetchone()
        assert json.loads(got["layout"]) == {"version": 1, "n": 1}
        assert set(got.keys()) == {"id", "saved", "layout"}

        key = services["mkui_layouts"]["ops"]["delete"]["key"]
        db.execute(f"DELETE FROM mkui_layouts WHERE {' AND '.join(k + ' = :' + k for k in key)}", {"id": 2})
        rows = db.execute(services["mkui_layouts_list"]["sql"], {"app": "mkfix", "owner": ""}).fetchall()
        assert [r["id"] for r in rows] == [1]

    def test_store_passes_mkio_config_validation(self):
        """mkio normalizes and validates transaction ops at load; a bad op_type,
        a missing key, or an unknown table fails here rather than at startup."""
        from mkfix.__main__ import _load_config
        cfg = _load_config(ROOT / "mkfix" / "mkfix.toml")
        store = cfg["services"]["mkui_layouts"]
        assert store["protocol"] == "transaction"
        for name in ("mkui_layouts_list", "mkui_layouts_get"):
            assert cfg["services"][name]["protocol"] == "reqrep"
        assert "mkui_layouts" in cfg["tables"]

    def test_retention_defaults_are_sane(self, app_config):
        layouts = app_config["layouts"]
        assert layouts["keep"] > 0 and layouts["keepDays"] > 0, "both at 0 would keep every save forever"
        assert layouts.get("autoload", True) is True, "the newest save must come back at startup"


class TestStyleAndGateValues:
    """Style rules and enable gates compare against displayed values; a
    renamed column or display value leaves them silently dead in the browser,
    so both are checked statically like everything else in app.json."""

    LEGACY_KEYS = {"rowMatch", "formatters", "eq", "ne", "in", "lt", "lte",
                   "gt", "gte", "match"}

    def test_no_pre_expression_config_remains(self, app_config):
        """mkui 0.2 replaced the eq/in/match rule keys and rowMatch with
        `when` expressions and dropped formatters; the old keys are ignored
        without a warning, which would leave a rule matching every row or a
        button always enabled."""
        for node in _walk_dicts(app_config):
            for key in self.LEGACY_KEYS:
                assert key not in node, f"legacy mkui key {key!r} in {node!r}"
            if isinstance(node.get("showWhen"), dict):
                pytest.fail(f"showWhen must be an expression: {node['showWhen']!r}")
            if isinstance(node.get("when"), dict):
                pytest.fail(f"when must be an expression: {node['when']!r}")

    def test_expressions_compile_against_mkio(self, app_config):
        """A `when` or `filter` that mkio's parser rejects is only reported as
        a browser console warning; the same grammar runs server-side, and
        `compile` also rejects unknown function names."""
        from mkio import expr

        env = expr.Env(strict=False)
        checked = 0
        for node in _walk_dicts(app_config):
            for key in ("when", "filter"):
                source = node.get(key)
                if isinstance(source, str):
                    expr.compile(source, env)
                    checked += 1
        assert checked > 50

    def test_display_templates_compile_and_render(self, app_config):
        """`display` templates are the pane's only say over what a cell
        shows; the Messages pane relies on one to render the stored SOH
        delimiters as pipes, and a template mkio rejects would show #ERR."""
        from mkio import expr

        env = expr.Env(strict=False)
        templates = [(name, col, src) for name, pane in app_config["panes"].items()
                     for col, src in (pane.get("display") or {}).items()]
        assert ("raw-messages", "raw_message") in [(n, c) for n, c, _ in templates]
        for name, col, src in templates:
            assert col in app_config["panes"][name]["columns"], f"{name}.display.{col}"
            expr.compile_template(src, env)
        raw = next(src for n, c, src in templates if (n, c) == ("raw-messages", "raw_message"))
        rendered = expr.compile_template(raw, env).evaluate(
            expr.Scope({"value": "8=FIX.4.2\x0135=D\x0158=a|b\x01"}))
        assert rendered == "8=FIX.4.2|35=D|58=a|b|"

    ENGINE_STATUSES = {"DOWN", "ERROR", "INITIATING", "LISTENING",
                       "LOGON_SENT", "ACTIVE", "LOGOUT_SENT"}

    @pytest.fixture(scope="class")
    def value_domains(self):
        """Known display-value domains keyed by (primary table, column) —
        a joined column (`session_status`, the sessions blotter's `status`)
        under the pane's primary table."""
        from mkfix.fix.dictionary import FixDictionary
        d = FixDictionary("FIX.4.2")
        sides = set(d.enums["54"].values())
        return {
            ("fix_sessions", "status"): self.ENGINE_STATUSES,
            ("fix_orders", "status"): set(d.enums["39"].values()),
            ("fix_orders", "side"): sides,
            ("fix_orders", "direction"): {"TX", "RX"},
            ("fix_orders", "pending_action"): {"New", "Cancel", "Replace"},
            ("fix_orders", "session_status"): self.ENGINE_STATUSES,
            ("fix_executions", "side"): sides,
            ("fix_executions", "exec_type"):
                set(d.enums["150"].values()) | set(d.enums["20"].values())
                | set(FixDictionary("FIX.4.4").enums["150"].values()),
            ("fix_executions", "direction"): {"TX", "RX"},
            ("fix_executions", "session_status"): self.ENGINE_STATUSES,
            ("fix_messages", "direction"): {"TX", "RX"},
            ("fix_messages", "msg_type_name"):
                {m["name"] for m in d.messages.values()},
            ("fix_iois", "side"): sides,
            ("fix_iois", "direction"): {"TX", "RX"},
            ("fix_allocations", "side"): sides,
            ("fix_allocations", "direction"): {"TX", "RX"},
            ("fix_templates", "scope"): TEMPLATE_SCOPES,
        }

    def _pane_table(self, spec, toml_config):
        service = toml_config["services"].get(spec.get("service"), {})
        return service.get("primary_table")

    def test_style_rules_reference_real_columns(self, app_config, toml_config):
        checked = 0
        for pane_id, spec in app_config["panes"].items():
            cols = _service_columns(toml_config, spec.get("service"))
            if cols is None:
                continue
            for col in spec.get("styles", {}):
                checked += 1
                assert col in cols, f"pane {pane_id!r} styles unknown column {col!r}"
            for rule in spec.get("rowStyle", []):
                for col in _conditions(rule.get("when")):
                    checked += 1
                    assert col in cols, \
                        f"pane {pane_id!r} rowStyle conditions on unknown column {col!r}"
        assert checked, "no style rules checked"
    def test_styled_values_exist(self, app_config, toml_config, value_domains):
        checked = 0
        for pane_id, spec in app_config["panes"].items():
            table = self._pane_table(spec, toml_config)
            for col, rules in spec.get("styles", {}).items():
                domain = value_domains.get((table, col))
                if domain is None:
                    continue
                for rule in rules:
                    for value in _conditions(rule.get("when")).get("value", []):
                        checked += 1
                        assert value in domain, \
                            f"pane {pane_id!r} styles {col!r} on unknown value {value!r}"
            for row_rule in spec.get("rowStyle", []):
                for col, values in _conditions(row_rule.get("when")).items():
                    domain = value_domains.get((table, col))
                    if domain is None:
                        continue
                    for value in values:
                        checked += 1
                        assert value in domain, \
                            f"pane {pane_id!r} rowStyle matches {col!r} on unknown value {value!r}"
        assert checked, "no styled values checked"
    def test_gate_values_exist(self, app_config, toml_config, value_domains):
        """Gate expressions compare against live row values; a value the
        engine never writes disables the button forever."""
        checked = 0
        for pane_id, spec in app_config["panes"].items():
            table = self._pane_table(spec, toml_config)
            for button in spec.get("buttons", []):
                when = button.get("enable", {}).get("when")
                for col, values in _conditions(when).items():
                    domain = value_domains.get((table, col))
                    if domain is None:
                        continue
                    for value in values:
                        checked += 1
                        assert value in domain, \
                            f"pane {pane_id!r} button {button['label']!r} gates " \
                            f"{col!r} on unknown value {value!r}"
        assert checked, "no gate values checked"

class TestStateBindings:
    def test_select_state_paths_are_declared(self, app_config):
        """`select.state` publishes into app state; the key must be declared."""
        declared = app_config["state"]
        for pane_id, spec in app_config["panes"].items():
            path = spec.get("select", {}).get("state")
            if path:
                assert path.split(".")[0] in declared, \
                    f"pane {pane_id!r} publishes to undeclared state {path!r}"

    def test_message_detail_follows_raw_messages(self, app_config):
        """Message Detail renders whatever Raw Messages publishes."""
        panes = app_config["panes"]
        assert panes["raw-messages"]["select"]["state"] == "selected_message"
        assert panes["message-detail"]["type"] == "message-detail"


class TestTimeTypedColumns:
    """mkui's range-filter time detection recognises only ISO-8601 and mkio
    refs; FIX-format stamps (`YYYYMMDD-HH:MM:SS.mmm`) must be declared via the
    `types` pane key or the filter dropdown silently stays a values list."""

    # mkui timeparse.js strptime tokens (lib/timeparse.js TOKEN_RE)
    TOKEN_RE = {"Y": r"\d{4}", "m": r"\d{1,2}", "d": r"\d{1,2}",
                "H": r"\d{1,2}", "M": r"\d{1,2}", "S": r"\d{1,2}",
                "f": r"\d{1,9}", "z": r"(?:Z|[+-]\d{2}:?\d{2})"}

    ENGINE_STAMPED = {"timestamp", "created_at", "updated_at", "transact_time"}

    def _parse_regex(self, fmt: str) -> re.Pattern:
        out, i = [], 0
        while i < len(fmt):
            if fmt[i] == "%":
                token = fmt[i + 1]
                assert token in self.TOKEN_RE or token == "%", \
                    f"unknown strptime token %{token} in {fmt!r}"
                out.append("%" if token == "%" else self.TOKEN_RE[token])
                i += 2
            else:
                out.append(re.escape(fmt[i]))
                i += 1
        return re.compile("".join(out))

    def test_types_name_real_columns(self, app_config):
        """A `types` entry for a column the pane doesn't show does nothing."""
        checked = 0
        for pane_id, spec in app_config["panes"].items():
            for col, type_spec in spec.get("types", {}).items():
                checked += 1
                assert col in spec.get("columns", []), \
                    f"pane {pane_id!r} types unknown column {col!r}"
                assert type_spec.get("type") in ("number", "time", "text"), \
                    f"pane {pane_id!r} column {col!r} has bad type {type_spec!r}"
        assert checked, "no types entries checked"

    def test_engine_timestamps_declared_and_parseable(self, app_config):
        """Every displayed engine-stamped timestamp column must declare a
        time type whose parse matches `_fix_timestamp()` output — a precision
        change in message.py would otherwise silently kill the range filter."""
        from mkfix.fix.message import _fix_timestamp

        sample = _fix_timestamp()
        checked = 0
        for pane_id, spec in app_config["panes"].items():
            if spec.get("type") != "mkio-table":
                continue
            for col in spec.get("columns", []):
                if col not in self.ENGINE_STAMPED:
                    continue
                checked += 1
                type_spec = spec.get("types", {}).get(col)
                assert type_spec, \
                    f"pane {pane_id!r} shows {col!r} without a time type"
                assert self._parse_regex(type_spec["parse"]).fullmatch(sample), \
                    f"pane {pane_id!r} column {col!r} parse " \
                    f"{type_spec['parse']!r} does not match {sample!r}"
        assert checked, "no engine-stamped columns checked"


class TestMenubar:
    """The menubar is app.json data mkui renders verbatim: an item with a
    typo'd key or an action mkui does not register is a dead entry with at
    most a console warning, and the order of the menus is a layout the eye
    learns, so both are pinned here."""

    MENUS = ["Sessions", "Messages", "Edit", "Trading", "Tools", "Layout", "Window", "Help"]
    BUILTIN_ACTIONS = {
        "pane.show", "edit.copy", "edit.selectAll", "edit.undo", "edit.redo",
        "layout.save", "layout.reset",
        "window.tileH", "window.tileV", "window.grid", "window.cascade",
        "dialog.open", "dialog.about",
    }

    def test_menu_order(self, app_config):
        assert [m["label"] for m in app_config["menubar"]] == self.MENUS

    def test_every_item_is_an_action_separator_or_submenu(self, app_config):
        for menu in app_config["menubar"]:
            for item in menu["items"]:
                kinds = set(item) & {"action", "sep", "layouts", "windows"}
                assert len(kinds) == 1, f"{menu['label']}: ambiguous item {item}"
                if "action" in item:
                    assert item["label"], f"{menu['label']}: action without a label"
                    assert item["action"] in self.BUILTIN_ACTIONS, \
                        f"{menu['label']}: {item['action']!r} is not an mkui built-in"

    def test_dialog_open_items_name_a_declared_dialog(self, app_config):
        """`dialog.open` with a name mkui cannot find under `dialogs` opens
        nothing and warns only in the console."""
        for menu in app_config["menubar"]:
            for item in menu["items"]:
                if item.get("action") == "dialog.open":
                    assert item["args"] in app_config.get("dialogs", {}), \
                        f"{menu['label']}: no dialog named {item['args']!r}"

    def test_pane_show_items_carry_a_pane_id(self, app_config):
        for menu in app_config["menubar"]:
            for item in menu["items"]:
                if item.get("action") == "pane.show":
                    assert isinstance(item["args"], str), \
                        f"{menu['label']}: pane.show args must be a pane id"

    def test_no_menu_repeats_a_label(self, app_config):
        for menu in app_config["menubar"]:
            labels = [i["label"] for i in menu["items"] if "label" in i]
            assert len(labels) == len(set(labels)), f"{menu['label']} repeats a label"

    def test_separators_never_lead_trail_or_double(self, app_config):
        for menu in app_config["menubar"]:
            items = menu["items"]
            assert not items[0].get("sep") and not items[-1].get("sep"), \
                f"{menu['label']} starts or ends with a separator"
            for a, b in zip(items, items[1:]):
                assert not (a.get("sep") and b.get("sep")), \
                    f"{menu['label']} has adjacent separators"

    def test_edit_menu_leads_with_undo_and_redo(self, app_config):
        edit = next(m for m in app_config["menubar"] if m["label"] == "Edit")
        actions = [item.get("action") for item in edit["items"]]
        assert actions[:2] == ["edit.undo", "edit.redo"]
        assert actions[2:] == [None, "edit.copy", "edit.selectAll"]

    def test_undo_labels_name_the_only_undoable_table(self, app_config, toml_config):
        """mkui's undo steps a record's recorded versions, not an editor's
        text; the example label "Undo record" says that, but here only
        sessions are undoable (orders and trades are read-only history),
        so the label says which."""
        undoable = {
            spec["history"]["table"]
            for spec in app_config["panes"].values()
            if "undo" in spec.get("history", {})
        }
        assert undoable == {"fix_sessions"}
        ops = toml_config["services"]["session_mgmt"]["ops"]
        assert ops["undo"][0]["op_type"] == "undo"
        assert ops["redo"][0]["op_type"] == "redo"
        edit = next(m for m in app_config["menubar"] if m["label"] == "Edit")
        labels = {i["action"]: i["label"] for i in edit["items"] if "action" in i}
        assert labels["edit.undo"] == "Undo Session Change"
        assert labels["edit.redo"] == "Redo Session Change"

    def test_no_menu_opens_a_filter_view(self, app_config):
        """The one-click Messages views were dropped in 0.29: the header
        filters cover them and the entries hid the default heartbeat
        exclusion's own reset. `TestConfiguredFilters` still validates any
        that come back."""
        for menu in app_config["menubar"]:
            for item in menu["items"]:
                assert item.get("action") != "table.filter", \
                    f"{menu['label']} carries filter view {item.get('label')!r}"


class TestConfiguredFilters:
    """Pane `filters` defaults (and any `table.filter` menu items) name
    panes, columns, and values by string; a typo leaves an entry that
    silently filters nothing (an unknown column) or everything (a value the
    engine never writes)."""

    PRESETS = {"today", "1h", "15m"}
    RANGE_KEYS = {"from", "to", "empty", "preset", "type"}
    VALUES_KEYS = {"include", "exclude"}

    @pytest.fixture(scope="class")
    def filter_items(self, app_config):
        return [item for menu in app_config["menubar"]
                for item in menu.get("items", [])
                if item.get("action") == "table.filter"]

    @pytest.fixture(scope="class")
    def filter_maps(self, app_config, filter_items):
        """Every configured filter map: (owner, pane spec, filters)."""
        maps = [(f"menu {item['label']!r}",
                 app_config["panes"].get(item["args"].get("pane")),
                 item["args"]["filters"])
                for item in filter_items]
        maps += [(f"pane {pane_id!r}", spec, spec["filters"])
                 for pane_id, spec in app_config["panes"].items()
                 if "filters" in spec]
        return maps

    def test_filters_target_existing_pane_columns(self, filter_maps):
        for owner, pane, filters in filter_maps:
            assert pane, f"{owner} filters an unknown pane"
            for col, spec in filters.items():
                assert col in pane["columns"], \
                    f"{owner} filters unknown column {col!r}"
                if isinstance(spec, dict) and not (set(spec) & self.VALUES_KEYS):
                    keys = set(spec) - self.RANGE_KEYS
                    assert not keys, \
                        f"{owner} column {col!r} has unknown filter keys {keys}"
                    preset = spec.get("preset")
                    assert preset is None or preset in self.PRESETS, \
                        f"{owner} uses unknown preset {preset!r}"

    def test_excluded_message_types_exist(self, filter_maps):
        """Hide views and defaults exclude by displayed message name; a
        renamed dictionary entry would bring the hidden messages back."""
        from mkfix.fix.dictionary import FixDictionary
        names = {m["name"] for m in FixDictionary("FIX.4.2").messages.values()}
        checked = 0
        for owner, _, filters in filter_maps:
            spec = filters.get("msg_type_name")
            if not isinstance(spec, dict):
                continue
            for value in spec.get("exclude", []) + spec.get("include", []):
                checked += 1
                assert value in names, \
                    f"{owner} filters unknown message name {value!r}"
        assert checked, "no message-name filter values checked"

    def test_reset_entries_clear_without_merge(self, filter_items):
        """A reset entry (empty filters) must replace, not merge, or the
        stacked views and configured defaults could never be undone from
        the menu."""
        resets = [i for i in filter_items if i["args"]["filters"] == {}]
        for item in resets:
            assert not item["args"].get("merge"), \
                f"menu {item['label']!r} merges an empty filter map (a no-op)"

    def test_time_preset_columns_are_time_typed(self, filter_maps):
        """A preset range needs the column's time frame; without a `types`
        entry the FIX-format stamps parse as nothing and the view goes empty."""
        checked = 0
        for owner, pane, filters in filter_maps:
            for col, spec in filters.items():
                if isinstance(spec, dict) and "preset" in spec:
                    checked += 1
                    assert pane.get("types", {}).get(col, {}).get("type") == "time", \
                        f"{owner} presets {col!r} without a time type"
        assert checked, "no preset filters checked"

    def test_messages_exclude_heartbeats_by_default(self, app_config):
        """The default hides only Heartbeat, and as an *exclusion* — every
        other message type, including ones never seen at config time, must
        keep showing; an include list would hide them."""
        spec = app_config["panes"]["raw-messages"]["filters"]["msg_type_name"]
        assert spec == {"exclude": ["Heartbeat"]}

    def test_blotters_default_to_today(self, app_config):
        """Orders and trades open showing today's activity; the rolling
        preset re-applies on a timer so rows age out at midnight."""
        for pane_id, col in [("order-blotter", "updated_at"),
                             ("market-order-blotter", "updated_at"),
                             ("trade-blotter", "transact_time"),
                             ("market-trade-blotter", "transact_time")]:
            spec = app_config["panes"][pane_id]["filters"][col]
            assert spec == {"preset": "today"}, \
                f"pane {pane_id!r} default filter is {spec!r}, expected today preset"


def _dependency_floor(name: str) -> tuple[int, ...]:
    """The `>=` floor pyproject.toml declares for a framework dependency."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    floors = [d for d in pyproject["project"]["dependencies"] if d.startswith(name)]
    assert floors, f"{name} missing from dependencies"
    floor = re.search(r">=\s*(\d+(?:\.\d+)*)", floors[0])
    assert floor, f"{name} has no >= floor: {floors[0]!r}"
    return tuple(int(n) for n in floor.group(1).split("."))


def _mkui_floor() -> tuple[int, ...]:
    return _dependency_floor("mkui")


class TestHelpMenu:
    """The Help menu is two mkui message boxes built from config alone. mkui
    ignores what it does not know — an `app.about` key, a dialog name, a
    template that comes out blank — so every half is pinned here, against
    the installed mkui where the contract is its to keep."""

    ABOUT_KEYS = {"title", "heading", "message", "width", "builtins", "facts"}

    @staticmethod
    def _mkui_source(*parts: str) -> str:
        import mkui
        return Path(mkui.static_dir).joinpath("src", *parts).read_text(encoding="utf-8")

    def test_menu_shape(self, app_config):
        menu = app_config["menubar"][-1]
        assert menu["label"] == "Help"
        assert menu["items"] == [
            {"label": "Scenario Language", "action": "pane.show", "args": "help-viewer"},
            {"label": "Scenario Editor Keys", "action": "dialog.open", "args": "scenario_keys"},
            {"sep": True},
            {"label": "Keyboard Shortcuts", "action": "dialog.open", "args": "shortcuts"},
            {"sep": True},
            {"label": "About mkfix", "action": "dialog.about"},
        ]

    def test_every_menu_action_is_registered_by_the_installed_mkui(self, app_config):
        """TestMenubar's list is hand-kept; this reads the registrations."""
        registered = set(re.findall(
            r'registerAction\(\s*"([^"]+)"',
            self._mkui_source("components", "app.js") + self._mkui_source("layouts.js")))
        used = {item["action"] for menu in app_config["menubar"]
                for item in menu["items"] if "action" in item}
        assert {"dialog.open", "dialog.about"} <= used
        assert used <= registered, f"not registered by mkui: {sorted(used - registered)}"

    def test_every_declared_dialog_is_opened_by_something(self, app_config, pane_sources):
        """By a menu's `dialog.open`, or by a pane's `app.dialog("name", …)`;
        a named dialog nothing opens is dead config, and a name nothing
        declares fails only on the click."""
        opened = {item["args"] for menu in app_config["menubar"]
                  for item in menu["items"] if item.get("action") == "dialog.open"}
        for source in pane_sources.values():
            opened |= set(re.findall(r'app\.dialog\("([a-z_]+)"', source))
        assert set(app_config["dialogs"]) == opened

    def test_shortcuts_box_is_a_message_box_with_one_way_out(self, app_config):
        """No `fields`, so it is not a form; its one button is `cancel` (what
        Escape and × press) and `default` (what Enter presses)."""
        spec = app_config["dialogs"]["shortcuts"]
        assert spec["title"] == "Keyboard Shortcuts"
        assert "fields" not in spec and "submit" not in spec
        assert spec["buttons"] == [
            {"id": "ok", "label": "OK", "kind": "primary", "cancel": True, "default": True}]
        labels = [f["label"] for f in spec["facts"]]
        assert len(labels) == len(set(labels)), "a key is listed twice"
        for fact in spec["facts"]:
            assert set(fact) == {"label", "value"}
            assert fact["label"].strip() and fact["value"].strip()
            assert "${" not in fact["value"], "a shortcut line is plain text"

    def test_shortcuts_box_covers_the_menu_hints(self, app_config):
        """A key the Edit menu advertises must be in the list too."""
        hints = {item["shortcut"] for menu in app_config["menubar"]
                 for item in menu["items"] if "shortcut" in item}
        assert hints, "the Edit menu lost its shortcut hints"
        labels = {f["label"] for f in app_config["dialogs"]["shortcuts"]["facts"]}
        for hint in hints:
            assert hint.replace("mod+", "Ctrl/Cmd+") in labels, f"{hint} is not listed"

    def test_shortcuts_are_keys_the_installed_mkui_binds(self, app_config):
        """The list is hand-written; each key is looked up where mkui handles
        it — the workspace's window keydown, the dialog's `onKey`."""
        workspace = self._mkui_source("components", "workspace.js")
        dialog = self._mkui_source("widgets", "mkui-dialog.js")
        for fact in app_config["dialogs"]["shortcuts"]["facts"]:
            label = fact["label"]
            if label == "Escape":
                assert 'e.key === "Escape"' in workspace and 'e.key === "Escape"' in dialog
            elif label.endswith("Enter"):
                assert 'e.key !== "Enter"' in dialog
                if label.startswith("Ctrl/Cmd+"):
                    assert re.search(r"ctrlKey\s*\|\|\s*e\.metaKey", dialog)
            else:
                key = re.fullmatch(r"Ctrl/Cmd\+([A-Z])", label)
                assert key, f"unrecognised shortcut label {label!r}"
                assert f'k === "{key.group(1).lower()}"' in workspace, f"mkui does not bind {label}"
        assert 'this.editAction(e.shiftKey ? "findPrev" : "findNext")' in workspace

    def test_tables_answer_the_find_keys(self):
        """Ctrl/Cmd+F and +G only do something over a pane whose edit hook
        has find; the blotters are `mkio-table`s."""
        table = self._mkui_source("widgets", "mkio-table.js")
        for action in ("copy", "selectAll", "clearSelection", "find", "findNext", "findPrev"):
            assert re.search(rf"\b{action}:\s*\(", table), f"mkio-table has no {action}"

    def test_about_keys_are_ones_the_installed_mkui_reads(self, app_config):
        about = app_config["app"]["about"]
        assert set(about) == self.ABOUT_KEYS
        source = self._mkui_source("lib", "dialogs.js")
        for key in about:
            assert f"about.{key}" in source, f"mkui's aboutSpec does not read about.{key}"
        for key in ("title", "version", "description", "copyright", "links"):
            assert key in app_config["app"]
        assert "a.links" in source

    def test_about_templates_compile_and_render(self, app_config):
        """The box as a connected client renders it, and with the server
        gone: mkui drops a line whose value is blank, so Server and mkio go
        and the rest stays."""
        from mkio import expr

        env = expr.Env(strict=False)
        app = {**app_config["app"], "mkui": "1.8.0"}
        about = app["about"]

        def render(src, state):
            return expr.compile_template(src, env).evaluate(expr.Scope({"app": app, "state": state}))

        up = {"mkio": {"server": {"name": "mkfix", "version": __version__, "mkio": "1.4.0"}}}
        assert render(about["heading"], up) == f"mkfix {app['version']}"
        assert [render(m, up) for m in about["message"]] == [
            app["description"], app["copyright"],
            "Free software under the GNU GPL version 2, with no warranty."]
        assert [(f["label"], render(f["value"], up)) for f in about["facts"]] == [
            ("Server", f"mkfix {__version__}"), ("mkui", "1.8.0"), ("mkio", "1.4.0")]

        down = {"mkio": {"server": {}}}
        # A pure `${x}` template yields NULL, which mkui's resolveExpr blanks.
        values = {f["label"]: (render(f["value"], down) or "").strip() for f in about["facts"]}
        assert values == {"Server": "", "mkui": "1.8.0", "mkio": ""}

    def test_about_reads_state_the_installed_mkui_sets(self, app_config):
        source = self._mkui_source("components", "app.js")
        paths = set()
        for fact in app_config["app"]["about"]["facts"]:
            paths.update(re.findall(r"state\.(mkio\.server\.\w+)", fact["value"]))
        assert paths == {"mkio.server.name", "mkio.server.version", "mkio.server.mkio"}
        for path in paths:
            assert f'st.set("{path}"' in source, f"mkui never sets {path}"

    def test_index_sets_the_mkui_version_before_the_config_is_handed_over(self):
        index = (STATIC / "index.html").read_text(encoding="utf-8")
        assert index.index("config.app.mkui = window.Mkui.VERSION") < index.index(".setConfig(config)")
        assert re.search(r"window\.mkui = window\.Mkui = \{\s*\.\.\.Mkui", self._mkui_source("index.js"))
        assert re.search(r"^\s*VERSION,", self._mkui_source("index.js"), re.M)

    def test_about_box_is_wide_enough_for_the_notice(self, app_config):
        """At mkui's default 420 the no-warranty line wraps its last word."""
        assert app_config["app"]["about"]["width"] >= 470

    def test_about_box_carries_the_gpl_notice(self, app_config):
        """What the GPL asks an interactive program to announce: the
        copyright, that there is no warranty, and where the terms are."""
        app = app_config["app"]
        message = app["about"]["message"]
        assert "${app.description}" in message and "${app.copyright}" in message
        assert any("no warranty" in line for line in message)
        hrefs = [link["href"] for link in app["links"]]
        assert any(h.endswith("/blob/main/LICENSE") for h in hrefs)

    def test_about_facts_are_mkfix_own(self, app_config):
        """mkui's built-in lines are off so the box can order its own, without
        a Connection line. mkui drops a line whose value comes out blank, so
        `${app.mkui}` needs index.html to set it or the line silently goes."""
        about = app_config["app"]["about"]
        assert about["builtins"] is False
        assert [f["label"] for f in about["facts"]] == ["Server", "mkui", "mkio"]
        values = {f["label"]: f["value"] for f in about["facts"]}
        assert values["mkui"] == "${app.mkui}"
        index = (STATIC / "index.html").read_text(encoding="utf-8")
        assert "config.app.mkui = window.Mkui.VERSION" in index

    def test_about_links_resolve(self, app_config):
        """Links must be the project's own, and one into the repository must
        name a file that exists: mkio serves NOTICE as a download, so the
        About box links to its GitHub page instead."""
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        repo = project["urls"]["Repository"]
        for link in app_config["app"]["links"]:
            href = link["href"]
            assert href.startswith(repo), href
            if "/blob/main/" in href:
                path = href.split("/blob/main/", 1)[1]
                assert (ROOT / path).is_file(), f"{href} names no file"


class TestVersions:
    def test_expected_version_matches_package(self, app_config):
        """A stale `expect` makes every client report a version mismatch."""
        major_minor = ".".join(__version__.split(".")[:2])
        assert app_config["mkio"]["expect"]["version"] == major_minor

    def test_server_version_injected_from_package(self, toml_config):
        """The server must report mkfix.__version__; a version in mkfix.toml
        would suggest it is the source of truth and invite drift."""
        assert "version" not in toml_config

        from mkfix.__main__ import _load_config
        cfg = _load_config(ROOT / "mkfix" / "mkfix.toml")
        assert cfg["version"] == __version__

    def test_expected_expr_version_matches_installed(self, app_config):
        """`expect.expr` is checked by exact match against the server, so a
        stale value makes every client report an incompatible server after
        an expression-language change."""
        from mkio.expr import LANGUAGE_VERSION

        assert app_config["mkio"]["expect"]["expr"] == str(LANGUAGE_VERSION)

    def test_both_ends_speak_the_pinned_language(self, app_config):
        """The handshake only compares the server's language with the pin.
        The browser evaluates app.json with the copy mkui vendors, so an mkui
        behind the installed mkio would leave `and`/`in`/durations parsing on
        the server (and in these tests) and failing in the page."""
        import re
        import mkui
        from mkio import expr

        vendored = (Path(mkui.__file__).parent / "static" / "src" / "lib" / "expr.js").read_text(encoding="utf-8")
        browser = re.search(r'LANGUAGE_VERSION = "(\d+)"', vendored).group(1)
        assert browser == expr.LANGUAGE_VERSION == app_config["mkio"]["expect"]["expr"]
        gate = expr.compile("status in ['New', 'Replaced'] and not pending and age < 1.5m "
                            "and COUNT(rows, r -> r.ok) == 1")
        assert gate({"status": "New", "pending": "", "age": 60, "rows": [{"ok": True}, {"ok": False}]}) is True

    def test_framework_floors_are_semver_majors(self):
        """mkio and mkui follow Semantic Versioning from 1.0.0: a minor is an
        addition, a major may remove anything. The floors must sit on a 1.x
        line and cap the next major, since a 2.x server fails the `_mkio`
        handshake regardless of what pip installed."""
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        for name in ("mkio", "mkui"):
            floor = _dependency_floor(name)
            assert floor >= (1, 0, 0), f"{name} floor {floor} predates semver"
            spec = next(d for d in pyproject["project"]["dependencies"] if d.startswith(name))
            assert f"<{floor[0] + 1}" in spec.replace(" ", ""), \
                f"{name} spec {spec!r} does not cap the next major"

    def test_expected_mkio_matches_dependency_floor(self, app_config):
        """The server checks `expect.mkio` by caret semver: same major, at
        least the requested minor. The pin must therefore be the floor
        pyproject installs, as major.minor only, so a patch release on
        either side stays compatible and a later 1.x minor still answers
        "compatible"."""
        expected = app_config["mkio"]["expect"]["mkio"]
        assert re.fullmatch(r"\d+\.\d+", expected), \
            f"expect.mkio {expected!r} should pin major.minor only"
        floor = _dependency_floor("mkio")
        assert tuple(int(n) for n in expected.split(".")) == floor[:2], \
            f"app.json expects mkio {expected}, pyproject installs >= {'.'.join(map(str, floor))}"

    def test_expected_mkio_pin_is_caret_compatible_with_installed(self):
        """Mirror of mkio's own rule, run against the installed package, so a
        framework upgrade fails here before a browser reports it."""
        from importlib.metadata import version as pkg_version
        from mkio.services.info import _semver_compatible

        expected = ".".join(map(str, _dependency_floor("mkio")[:2]))
        assert _semver_compatible(pkg_version("mkio"), expected), \
            f"installed mkio {pkg_version('mkio')} is not caret-compatible with {expected}"

    def test_statusbar_version_matches_package(self, app_config):
        major_minor = ".".join(__version__.split(".")[:2])
        texts = [item.get("text", "") for item in app_config["statusbar"]["right"]]
        assert any(t == f"mkfix v{major_minor}" for t in texts), \
            f"statusbar shows {texts}, expected 'mkfix v{major_minor}'"

    def test_about_box_matches_package(self, app_config):
        """The About box is built from the `app` block: its version is the
        third baked client stamp, and the description and licence repeat
        pyproject.toml's."""
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        app = app_config["app"]
        assert app["version"] == ".".join(__version__.split(".")[:2])
        assert app["description"] == project["description"]
        assert project["license"] in app["copyright"]
        assert project["authors"][0]["name"] in app["copyright"]
        assert "${app.version}" in app["about"]["heading"]

    def test_mkui_floor_supports_message_boxes(self, app_config):
        """`dialog.*` actions and the `dialogs` block are mkui 1.8.0; an
        earlier build leaves the Help menu dead with a console warning."""
        uses_dialogs = any(
            item.get("action", "").startswith("dialog.")
            for menu in app_config["menubar"] for item in menu.get("items", [])
        )
        if not uses_dialogs:
            pytest.skip("no menu item opens a dialog")

        floor = _mkui_floor()
        assert floor >= (1, 8, 0), f"mkui floor {floor} predates message boxes"

    def test_mkui_floor_supports_configured_table_options(self, app_config):
        """`live` and `select` on mkio-table need mkui 0.1.52+."""
        uses_new_options = any(
            "live" in spec or "select" in spec
            for spec in app_config["panes"].values()
            if spec["type"] == "mkio-table"
        )
        if not uses_new_options:
            pytest.skip("no pane uses live/select")

        floor = _mkui_floor()
        assert floor >= (0, 1, 52), f"mkui floor {floor} predates live/select support"

    def test_mkui_floor_supports_bounded_sections(self, app_config):
        """A dialog header's own `fields` bound its section in mkui 1.2.0;
        an earlier build ignores the key and renders the header with nothing
        under it, so Expire and Save-as would vanish from the order dialogs."""
        bounded = [
            node for node in _walk_dicts(app_config["panes"])
            if "group" in node and "fields" in node
        ]
        assert bounded, "no dialog uses a bounded section"
        floor = _mkui_floor()
        assert floor >= (1, 2, 0), f"mkui floor {floor} predates bounded sections"

    def test_mkui_floor_supports_dialog_fill(self, app_config):
        """A select's `fill` is mkui 0.7.0; an earlier build ignores the key,
        so a Template pick would fill nothing and say nothing."""
        uses_fill = any(
            node.get("type") == "select" and "fill" in node
            for node in _walk_dicts(app_config)
        )
        if not uses_fill:
            pytest.skip("no dialog uses fill")

        floor = _mkui_floor()
        assert floor >= (0, 7, 0), f"mkui floor {floor} predates dialog fill"

    def test_mkui_floor_supports_expression_config(self, app_config):
        """`when` rules and gates are mkui 0.2.0 expressions; earlier builds
        ignore them (styles never match, buttons never gate)."""
        uses_when = any("when" in node for node in _walk_dicts(app_config))
        if not uses_when:
            pytest.skip("no expression config")

        floor = _mkui_floor()
        assert floor >= (0, 2, 0), f"mkui floor {floor} predates expression config"

    def test_mkui_floor_supports_configured_filters(self, app_config):
        """`table.filter` and the `filters` pane key are mkui 0.2.3; an
        earlier build leaves the menu items dead and the default filters
        silently unapplied."""
        uses_action = any(
            item.get("action") == "table.filter"
            for menu in app_config["menubar"] for item in menu.get("items", [])
        ) or any("filters" in spec for spec in app_config["panes"].values())
        if not uses_action:
            pytest.skip("no configured filters")

        floor = _mkui_floor()
        assert floor >= (0, 2, 3), f"mkui floor {floor} predates table.filter"

    def test_mkui_floor_regates_buttons_on_live_updates(self, app_config):
        """Blotter buttons gate on row columns that change live under them
        (`status`, `session_status`, `pending_action`). mkui 0.2.8 re-evaluates
        `enable.when` when a selected row is replaced; before it the gate ran
        only on selection changes, so Start stayed enabled and Stop disabled
        after a session came up until the row was re-clicked."""
        gates_on_row = any(
            "when" in button.get("enable", {}) and "r." in button["enable"]["when"]
            for spec in app_config["panes"].values()
            for button in spec.get("buttons", [])
        )
        if not gates_on_row:
            pytest.skip("no button gates on row columns")

        floor = _mkui_floor()
        assert floor >= (0, 2, 8), f"mkui floor {floor} predates live re-gating"

    def test_readme_dependency_floors_match_pyproject(self):
        """README's Dependencies section restates the floor and the
        next-major cap by hand."""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in ("mkio", "mkui"):
            match = re.search(
                rf"\[{name}\]\([^)]*\) >= (\d+(?:\.\d+)*), < (\d+)", readme)
            assert match, f"README does not state a {name} floor and cap"
            floor = _dependency_floor(name)
            stated = tuple(int(n) for n in match.group(1).split("."))
            assert stated == floor, \
                f"README says {name} >= {match.group(1)}, pyproject says {floor}"
            assert int(match.group(2)) == floor[0] + 1, \
                f"README caps {name} at < {match.group(2)}, pyproject at < {floor[0] + 1}"

    def test_mkio_caret_rule_tolerates_framework_minors(self):
        """What the 1.x floors buy: mkio's handshake accepts any server on
        the pinned major at or above the pinned minor, so a framework minor
        release no longer forces an mkfix release, while a major does.
        Pinned against mkio's own rule so a change there fails here."""
        from mkio.services.info import _semver_compatible

        major, minor = _dependency_floor("mkio")[:2]
        pin = f"{major}.{minor}"
        assert _semver_compatible(f"{major}.{minor}.0", pin)
        assert _semver_compatible(f"{major}.{minor}.9", pin)
        assert _semver_compatible(f"{major}.{minor + 7}.0", pin)
        assert not _semver_compatible(f"{major + 1}.0.0", pin)
        assert not _semver_compatible(f"{major - 1}.99.0", pin)
        if minor:
            assert not _semver_compatible(f"{major}.{minor - 1}.0", pin)

    def test_installed_frameworks_satisfy_dependency_specs(self):
        """The checks in this file run against the installed mkio and mkui;
        a version outside pyproject's range (below the floor or on the next
        major) would pass the static checks while the app misbehaves."""
        from importlib.metadata import version as pkg_version
        from packaging.requirements import Requirement

        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        for name in ("mkio", "mkui"):
            spec = next(d for d in pyproject["project"]["dependencies"] if d.startswith(name))
            req = Requirement(spec)
            assert req.specifier.contains(pkg_version(name), prereleases=True), \
                f"installed {name} {pkg_version(name)} is outside {spec!r}"

    def test_mkui_floor_supports_time_typed_columns(self, app_config):
        """The `types` pane key is mkui 0.2.1; earlier builds ignore it and
        the timestamp columns silently lose their range filters."""
        uses_types = any("types" in spec for spec in app_config["panes"].values())
        if not uses_types:
            pytest.skip("no pane declares column types")

        floor = _mkui_floor()
        assert floor >= (0, 2, 1), f"mkui floor {floor} predates the types pane key"

    def test_mkui_floor_supports_pin_keep(self, app_config):
        """`pin: "keep"` arrived in mkui 1.4.0; an earlier build ignores it
        and a pinned submit resets the order dialogs to their template."""
        uses_keep = any(
            spec.get("pin") == "keep"
            for pane in app_config["panes"].values()
            for button in pane.get("buttons", [])
            for spec in [button.get("action", {}).get("dialog", {})]
        )
        if not uses_keep:
            pytest.skip("no dialog keeps its values when pinned")

        floor = _mkui_floor()
        assert floor >= (1, 4, 0), f"mkui floor {floor} predates pin keep"

    def test_mkui_floor_supports_session_dialog(self):
        """openDialog before 0.1.54 clips a body taller than the default
        frame; the session form is tall enough to need the auto-grow."""
        uses_dialog = any(
            "mkui-dialog.js" in p.read_text(encoding="utf-8") for p in (STATIC / "panes").glob("*.js")
        ) or '"type": "dialog"' in (STATIC / "app.json").read_text(encoding="utf-8")
        if not uses_dialog:
            pytest.skip("no pane opens an mkui dialog")

        floor = _mkui_floor()
        assert floor >= (0, 1, 54), f"mkui floor {floor} predates dialog auto-grow"


class TestDictionaryConfig:
    def test_standard_versions_consistent_everywhere(self, app_config, toml_config):
        """The standard-version list lives in dictionary.py, fix-dictionary.js,
        the dictionaries_list sql, and the session dialogs' fix_version
        options, and each version must ship a generated JSON; drift in any
        copy surfaces only in the browser."""
        from mkfix.fix.dictionary import STANDARD_VERSIONS

        js = (STATIC / "fix-dictionary.js").read_text(encoding="utf-8")
        js_block = js.split("export const STANDARD_VERSIONS")[1].split("]")[0]
        js_versions = re.findall(r'"(FIX\.[0-9.]+(?:SP\d)?)"', js_block)
        assert js_versions == list(STANDARD_VERSIONS)

        sql = toml_config["services"]["dictionaries_list"]["sql"]
        for version in STANDARD_VERSIONS:
            assert f"'{version}'" in sql, f"dictionaries_list sql misses {version}"

        selects = [node for node in _walk_dicts(app_config)
                   if node.get("name") == "fix_version" and node.get("type") == "select"]
        assert selects, "no fix_version selects in app.json"
        for select in selects:
            values = [o["value"] for o in select["options"]]
            assert values == list(STANDARD_VERSIONS)

        data_dir = ROOT / "mkfix" / "fix" / "dictionary_data"
        for version in STANDARD_VERSIONS:
            path = data_dir / (version.replace(".", "") + ".json")
            assert path.exists(), f"missing generated dictionary {path.name}"

    def test_session_dialogs_offer_dictionary_dropdown(self, app_config):
        selects = [node for node in _walk_dicts(app_config)
                   if node.get("name") == "dictionary" and node.get("type") == "select"]
        assert len(selects) >= 2, "session New/Edit dialogs must offer a Dictionary select"
        for select in selects:
            assert select["optionsFrom"]["service"] == "dictionaries_list"

    def test_dictionaries_pane_commands_have_branches(self):
        """The dictionaries pane calls fix_cmd through its cmd() helper; a
        command with no dispatch branch fails only on click, in the browser."""
        js = (STATIC / "panes" / "dictionaries.js").read_text(encoding="utf-8")
        used = set(re.findall(r'cmd\("([a-z_]+)"', js))
        handled = _fix_cmd_commands()
        assert used, "dictionaries pane calls no fix_cmd commands"
        missing = used - handled
        assert not missing, f"dictionaries pane sends unhandled fix_cmd commands: {sorted(missing)}"

    def test_curated_fix42_names_survive_regeneration(self):
        """The FIX42 overlay pins display names the engine writes into rows and
        the style rules test; the generated FIX42.json must keep them."""
        overlay = json.loads((ROOT / "tools" / "overlays" / "FIX42.json").read_text(encoding="utf-8"))
        generated = json.loads(
            (ROOT / "mkfix" / "fix" / "dictionary_data" / "FIX42.json").read_text(encoding="utf-8"))
        for tag, entry in overlay.get("fields", {}).items():
            assert generated["fields"].get(tag) == entry, f"field {tag} lost its curated name"
        for tag, values in overlay.get("enums", {}).items():
            for code, name in values.items():
                assert generated["enums"].get(tag, {}).get(code) == name, \
                    f"enum {tag}={code} lost its curated name"


class TestSessionDialogs:
    """The New and Edit session dialogs are the only way a session's
    settings get set, so a setting missing from one of them is unreachable,
    and a New default drifting from the TOML default means the dialog and a
    scripted insert create different sessions."""

    SETTINGS = ("heartbeat_interval", "logout_timeout", "logout_test_request", "reset_on_logon")

    def _dialogs(self, app_config):
        found = {}
        for node in _walk_dicts(app_config):
            submit = node.get("submit")
            if isinstance(submit, dict) and submit.get("service") == "session_mgmt":
                found[submit["op"]] = node
        assert set(found) >= {"add", "update"}, sorted(found)
        return found

    def _fields(self, dialog):
        out = {}
        for item in dialog.get("fields", []):
            for f in _leaves(item):
                if "name" in f:
                    out[f["name"]] = f
        return out

    def test_new_defaults_match_toml_defaults(self, app_config, toml_config):
        defaults = toml_config["services"]["session_mgmt"]["ops"]["add"][0]["defaults"]
        fields = self._fields(self._dialogs(app_config)["add"])
        for name in self.SETTINGS:
            assert name in fields, f"New Session dialog lacks {name}"
            assert str(fields[name]["value"]) == str(defaults[name]), \
                f"New Session default for {name} is {fields[name]['value']!r}, TOML says {defaults[name]!r}"

    def test_edit_prefills_every_setting_from_the_row(self, app_config):
        fields = self._fields(self._dialogs(app_config)["update"])
        for name in self.SETTINGS:
            assert name in fields, f"Edit Session dialog lacks {name}"
            assert fields[name]["value"] == "${row.%s}" % name

    def test_logout_settings_are_columns_and_op_fields(self, toml_config):
        columns = toml_config["tables"]["fix_sessions"]["columns"]
        assert columns["logout_timeout"] == "INTEGER DEFAULT 0", "0 = twice the heartbeat interval"
        assert columns["logout_test_request"] == "INTEGER DEFAULT 1", "the spec recommends it"
        ops = toml_config["services"]["session_mgmt"]["ops"]
        for op in ("add", "update"):
            assert {"logout_timeout", "logout_test_request"} <= set(ops[op][0]["fields"]), op


def _primary_key(table_cfg: dict) -> list[str]:
    pk = list(table_cfg.get("primary_key", []) or [])
    if pk:
        return pk
    return [name for name, col in table_cfg["columns"].items()
            if "PRIMARY KEY" in col.upper()]


class TestRecordHistory:
    """The five blotters' `history` blocks name services mkio never writes on
    its own; a missing or misnamed one leaves the History pane, the As of…
    button, or Undo silently dead in the browser (mkui warns at most)."""

    HISTORY_PANES = ("session-blotter", "order-blotter", "trade-blotter",
                     "market-order-blotter", "market-trade-blotter")

    @pytest.fixture(scope="class")
    def history_panes(self, app_config):
        panes = {pid: app_config["panes"][pid] for pid in self.HISTORY_PANES}
        for pid, spec in panes.items():
            assert "history" in spec, f"{pid} declares no history block"
        return panes

    def test_only_the_five_blotters_carry_history(self, app_config):
        with_history = {pid for pid, spec in app_config["panes"].items() if "history" in spec}
        assert with_history == set(self.HISTORY_PANES)

    def test_history_table_is_the_versioned_primary_table(self, history_panes, toml_config):
        for pid, spec in history_panes.items():
            table = spec["history"]["table"]
            assert table == toml_config["services"][spec["service"]]["primary_table"], \
                f"{pid}: history.table is not the pane's table"
            assert toml_config["tables"][table].get("versioned") is True, \
                f"{pid}: {table} is not versioned in mkfix.toml"

    def test_history_key_is_the_tables_primary_key(self, history_panes, toml_config):
        for pid, spec in history_panes.items():
            table = spec["history"]["table"]
            assert spec["history"]["key"] == _primary_key(toml_config["tables"][table]), \
                f"{pid}: history.key must be the primary key of {table}"

    def test_reqrep_services_read_the_history_table_by_key(self, history_panes, toml_config):
        for pid, spec in history_panes.items():
            h = spec["history"]
            hist_table = f"{h['table']}__history"
            for role in ("versions", "state"):
                svc = toml_config["services"].get(h[role])
                assert svc and svc["protocol"] == "reqrep", f"{pid}: history.{role} is not a reqrep"
                assert hist_table in svc["sql"], f"{pid}: {h[role]} does not read {hist_table}"
                for key in h["key"]:
                    assert f":{key}" in svc["sql"], f"{pid}: {h[role]} does not bind :{key}"
            svc = toml_config["services"].get(h["asOf"])
            assert svc and svc["protocol"] == "reqrep", f"{pid}: history.asOf is not a reqrep"
            assert hist_table in svc["sql"] and ":as_of" in svc["sql"]

    def test_feed_is_a_query_over_the_history_table_filterable_by_key(self, history_panes, toml_config):
        """mkio-history builds its table on the feed and narrows it to one
        record server-side, so the key must be filterable."""
        for pid, spec in history_panes.items():
            h = spec["history"]
            svc = toml_config["services"].get(h["feed"])
            assert svc and svc["protocol"] == "query", f"{pid}: history.feed is not a query"
            assert svc["primary_table"] == f"{h['table']}__history"
            for key in h["key"]:
                assert key in svc.get("filterable", []), \
                    f"{pid}: {h['feed']} must list {key} as filterable"

    def test_as_of_matches_the_panes_direction(self, history_panes, toml_config):
        """A pane's `filter` narrows only its live subscription; the as-of
        rows come from a reqrep, so each direction-split blotter needs an
        as-of service that applies the same split."""
        for pid, spec in history_panes.items():
            match = re.fullmatch(r"direction == '(TX|RX)'", spec.get("filter", ""))
            sql = toml_config["services"][spec["history"]["asOf"]]["sql"]
            if match:
                assert f"h.direction = '{match.group(1)}'" in sql, \
                    f"{pid}: as-of view must be limited to direction {match.group(1)}"
            else:
                assert "direction" not in sql

    def test_undo_redo_only_on_sessions(self, history_panes, toml_config):
        """An undo rewinds only the local row — the counterparty's view of an
        order or trade does not move — so orders and trades are read-only."""
        for pid, spec in history_panes.items():
            h = spec["history"]
            if pid != "session-blotter":
                assert "undo" not in h and "redo" not in h, f"{pid} must not offer undo"
                continue
            for direction in ("undo", "redo"):
                step = h[direction]
                ops = toml_config["services"][step["service"]]["ops"]
                op = ops[step["op"]]
                assert [(o["table"], o["op_type"], o["key"]) for o in op] == [
                    (h["table"], direction, h["key"])]
            assert h.get("confirm", True) is True, "a cursor move is a shared write"

    def test_session_history_needs_no_column_override(self, history_panes, toml_config):
        """Every fix_sessions column is versioned config, so Diff and Blame
        take the history row as it is. The live columns the blotter shows
        (status, sequence numbers) come from the sessions_query join and
        never reach the history table, so a `columns` override would only
        be a list to keep in step."""
        h = history_panes["session-blotter"]["history"]
        assert "columns" not in h
        config_cols = set(toml_config["tables"]["fix_sessions"]["columns"])
        assert not {"status", "tx_seq_num", "rx_seq_num"} & config_cols

    def test_trade_history_diffs_the_execution_columns(self, history_panes, toml_config):
        """A trade's chain is its fill, corrections and bust — every version
        rewrites the execution columns, so Diff/Blame list those and skip the
        identity columns."""
        table = toml_config["tables"]["fix_executions"]
        for pid in ("trade-blotter", "market-trade-blotter"):
            cols = history_panes[pid]["history"]["columns"]
            assert set(cols) <= set(table["columns"]), pid
            assert {"exec_id", "exec_ref_id", "exec_type", "last_qty", "last_price"} <= set(cols), pid
            assert {"dk_reason", "dk_text"} <= set(cols), \
                f"{pid}: a DK is a row version on either side — theirs on a sent trade, ours on a received one"
            assert "exec_ref_id" in history_panes[pid]["columns"], pid

    def test_live_columns_come_from_the_state_join(self, history_panes, toml_config):
        """A session's live status and sequence numbers live in
        fix_session_state alone; the blotters read them through a join that
        mkio re-runs when the state table changes. The SQL must return the
        primary key under its own name (that arms mkio's re-read), name the
        state table in watch_tables next to the primary table (mkio
        subscribes the list as written), and pin `key` to the primary key,
        since a watched table's key would otherwise join the row identity
        the history blocks key records by."""
        tables = toml_config["tables"]
        services = toml_config["services"]
        assert not any("unversioned" in t for t in tables.values()), \
            "no engine-written mirror columns remain"
        assert not {"status", "tx_seq_num", "rx_seq_num"} & set(tables["fix_sessions"]["columns"])
        for table in ("fix_orders", "fix_executions"):
            assert "session_status" not in tables[table]["columns"]

        joined = {
            "sessions_query": ("fix_sessions", {"status", "tx_seq_num", "rx_seq_num", "error_text"}),
            "orders_query": ("fix_orders", {"session_status"}),
            "executions_query": ("fix_executions", {"session_status"}),
        }
        for name, (table, live) in joined.items():
            svc = services[name]
            assert svc["primary_table"] == table, name
            assert svc["watch_tables"] == [table, "fix_session_state"], name
            assert "LEFT JOIN fix_session_state" in svc["sql"], name
            cols = _service_columns(toml_config, name)
            assert live <= cols, name
            pk = [c for c, d in tables[table]["columns"].items() if "PRIMARY KEY" in d]
            assert set(pk) <= cols, f"{name}: the primary key must come back under its own name"
            assert svc["key"] == pk, name
            assert set(live) <= set(svc["filterable"]) | {"tx_seq_num", "rx_seq_num", "error_text"}, name

        # watch_columns must equal the state columns the sql reads: fewer
        # would miss a change, more would re-query for nothing. The sessions
        # query reads all but seq_epoch (never written alone), so it lists
        # none and re-runs on every state write, as it must.
        state_cols = set(tables["fix_session_state"]["columns"]) - {"session_id"}
        for name in joined:
            svc = services[name]
            alias = re.search(r"JOIN fix_session_state (\w+) ON", svc["sql"]).group(1)
            read = set(re.findall(rf"\b{alias}\.(\w+)", svc["sql"])) - {"session_id"}
            assert read <= state_cols, name
            if "watch_columns" in svc:
                assert svc["watch_columns"] == {"fix_session_state": sorted(read)}, name
            else:
                assert read >= state_cols - {"seq_epoch"}, \
                    f"{name} reads only {sorted(read)}: declare watch_columns"

        for pane_id, spec in history_panes.items():
            assert spec["history"]["key"] == services[spec["service"]]["key"], \
                f"{pane_id}: history.key must be the identity the query stamps as _mkio_row"

    def test_menus_do_not_open_histories(self, app_config):
        """The timeline is reached only through each blotter's History
        button; a menu entry would open it over whichever blotter's
        selection happened to be current."""
        opened = [
            item for menu in app_config["menubar"] for item in menu.get("items", [])
            if item.get("action") == "table.history"
        ]
        assert not opened

    def test_every_history_pane_has_a_history_button(self, history_panes):
        """The version timeline opens only through `table.history` (Undo,
        Redo and As of… arrive with the `history` block, the timeline does
        not), so each blotter offers it next to its other buttons, aimed at
        itself: `pane` must name the blotter the button sits on, or the
        timeline opens over another table's selection. Single-record like
        mkui's own example, since a history is one record's chain."""
        for pane_id, spec in history_panes.items():
            button = next((b for b in spec.get("buttons", []) if b["label"] == "History"), None)
            assert button is not None, f"{pane_id} has no History button"
            assert button["action"] == {
                "type": "action", "name": "table.history", "args": {"pane": pane_id},
            }, f"{pane_id} History button must open its own history"
            assert button.get("unit") == "row"
            assert button["enable"] == {"connected": True}

    def test_engine_registers_the_undo_redo_hook(self):
        main = (ROOT / "mkfix" / "__main__.py").read_text(encoding="utf-8")
        assert "app.on_undo_redo(" in main
        engine = (ROOT / "mkfix" / "fix" / "engine.py").read_text(encoding="utf-8")
        assert "async def handle_undo_redo" in engine

    def test_mkio_floor_supports_joined_queries_with_a_key(self, toml_config):
        """A query that watches a table beyond its primary one live-updates
        only from mkio 0.8, `key` is 0.9 and `watch_columns` 0.10: an
        earlier mkio rejects the keys, and without `key` identifies a
        joined row by a composite the history blocks and mkui's as-of view
        do not match."""
        services = toml_config["services"].values()
        joined = [s for s in services if s.get("protocol") == "query" and len(s.get("watch_tables", [])) > 1]
        if not joined:
            pytest.skip("no joined query services")
        assert _dependency_floor("mkio") >= (0, 9, 0)
        if any("watch_columns" in s for s in joined):
            assert _dependency_floor("mkio") >= (0, 10, 0)


class TestTagPreviews:
    """Every order and trade dialog names the FIX tag on each field label,
    prefixes each hand-listed option with its code, and ends with a
    computed "Terms as tags" line showing the entered terms as tag=value
    pairs. The line is a promise about what the engine sends, so each
    expression is compiled and evaluated here, and every `row.*` it reads
    must be a column of the table behind the blotter."""

    OPS = ("send_new_order", "send_cancel_replace", "send_cancel", "dk_trade",
           "accept_request", "reject_request", "fill_order", "unsolicited_cancel", "restate_order",
           "correct_trade", "bust_trade", "renotify_trade")
    ORDER_OPS = {"send_new_order", "send_cancel_replace", "send_cancel",
                 "accept_request", "reject_request", "fill_order", "unsolicited_cancel", "restate_order"}
    LABELS = {
        "symbol": "(55)", "side": "(54)", "qty": "(38)", "ord_type": "(40)", "price": "(44)",
        "tif": "(59)", "expire_time": "(126/432", "dk_reason": "(127)", "text": "(58)",
        "restate_reason": "(378)",
    }
    TRADE_LABELS = {"qty": "(32)", "price": "(31)"}

    @staticmethod
    def _fields(dialog):
        for item in dialog["fields"]:
            yield from _leaves(item)

    @staticmethod
    def _preview(dialog):
        lines = [f for f in dialog["fields"] if f.get("label") == "Terms as tags"]
        assert len(lines) == 1, "one Terms as tags line per dialog"
        line = lines[0]
        assert line["type"] == "readonly" and "compute" in line
        assert dialog["fields"][-1] is line, "the preview closes the dialog"
        return line["compute"]

    def test_labels_name_the_tag(self, app_config):
        for op in self.OPS:
            labels = self.TRADE_LABELS if op in ("fill_order", "correct_trade") else self.LABELS
            for f in self._fields(_find_dialog(app_config, op)):
                if f.get("name") in labels:
                    assert labels[f["name"]] in f["label"], f"{op} {f['name']}: {f['label']!r}"

    def test_options_lead_with_their_code(self, app_config):
        for op in self.OPS:
            for f in self._fields(_find_dialog(app_config, op)):
                for o in f.get("options", []):
                    if o["value"] == "":
                        continue  # "send no such tag" has no code to lead with
                    assert o["label"].startswith(o["value"] + " - "), f"{op} {f['name']}: {o!r}"

    def test_previews_read_real_columns(self, app_config, toml_config):
        from mkio import expr
        orders = set(toml_config["tables"]["fix_orders"]["columns"])
        executions = set(toml_config["tables"]["fix_executions"]["columns"])
        for op in self.OPS:
            dialog = _find_dialog(app_config, op)
            source = self._preview(dialog)
            names = _dialog_field_names(dialog)
            columns = orders if op in self.ORDER_OPS else executions
            for ref in expr.field_refs(expr.parse(source)):
                assert ref == "row" or ref in names, f"{op} preview reads unknown field {ref!r}"
            for attr in re.findall(r"\brow\.(\w+)", source):
                assert attr in columns, f"{op} preview reads row.{attr}, not a column"

    def test_previews_render_the_entered_terms(self, app_config):
        from mkio import expr
        row = {
            "cl_ord_id": "C2", "pending_cl_ord_id": "C3", "pending_action": "Replace",
            "pending_qty": 200.0, "pending_price": 151.5, "symbol": "AAPL", "side_code": "1",
            "order_qty": 100.0, "order_id": "OR1", "exec_id": "EX1",
            "exec_ref_id": "", "last_qty": 40.0, "last_price": 150.5,
        }
        cases = {
            "send_new_order": (
                {"symbol": "AAPL", "side": "1", "qty": 100.0, "ord_type": "2", "price": 150.25, "tif": "0",
                 "handl_inst": "3", "text": "work it", "extra_tags": "5001=X"},
                "55=AAPL|54=1|38=100|40=2|44=150.25|59=0|21=3|58=work it|5001=X"),
            "send_cancel_replace": (
                {"symbol": "AAPL", "side": "1", "qty": 100.0, "ord_type": "1", "price": None, "tif": "7",
                 "handl_inst": "1", "text": "", "extra_tags": ""},
                "41=C2|55=AAPL|54=1|38=100|40=1|59=7|21=1"),
            "send_cancel": ({"text": "pull", "extra_tags": ""}, "41=C2|55=AAPL|54=1|38=100|58=pull"),
            "dk_trade": ({"dk_reason": "D", "text": "", "extra_tags": ""}, "37=OR1|17=EX1|127=D"),
            "accept_request": ({"text": "ok", "extra_tags": ""}, "11=C3|41=C2|38=200|44=151.5|58=ok"),
            "reject_request": ({"text": "no", "extra_tags": ""}, "11=C3|41=C2|434=2|58=no"),
            "fill_order": ({"qty": "50", "price": "150.5", "text": None, "extra_tags": ""}, "11=C2|32=50|31=150.5"),
            "unsolicited_cancel": ({"text": "halted", "extra_tags": "378=6"}, "11=C2|150=4|39=4|58=halted|378=6"),
            "restate_order": ({"qty": "80", "price": "149.5", "restate_reason": "3", "text": "", "extra_tags": ""},
                              "11=C2|150=D|38=80|44=149.5|378=3"),
            "correct_trade": ({"qty": "40", "price": "149", "text": "fat finger", "extra_tags": ""},
                              "19=EX1|32=40|31=149|58=fat finger"),
            "bust_trade": ({"text": "oops", "extra_tags": "5001=X"}, "19=EX1|58=oops|5001=X"),
            "renotify_trade": ({"text": "", "extra_tags": "5001=X"}, "17=(new)|32=40|31=150.5|5001=X"),
        }
        assert set(cases) == set(self.OPS)
        for op, (fields, expected) in cases.items():
            source = self._preview(_find_dialog(app_config, op))
            assert expr.evaluate(source, {**fields, "row": row}) == expected, op
        pending_new = {**row, "pending_action": "New"}
        for op, expected in (("accept_request", "11=C2"), ("reject_request", "11=C2")):
            source = self._preview(_find_dialog(app_config, op))
            assert expr.evaluate(source, {"text": "", "extra_tags": "", "row": pending_new}) == expected, op
        source = self._preview(_find_dialog(app_config, "restate_order"))
        bare = {"qty": "80", "price": None, "restate_reason": "", "text": None, "extra_tags": None}
        assert expr.evaluate(source, {**bare, "row": row}) == "11=C2|150=D|38=80", \
            "a blank price and reason are withheld"
        blank = {"symbol": None, "side": "1", "qty": None, "ord_type": "2", "price": None, "tif": "0",
                 "handl_inst": "1", "text": None, "extra_tags": None}
        source = self._preview(_find_dialog(app_config, "send_new_order"))
        assert expr.evaluate(source, {**blank, "row": row}) == "55=|54=1|38=|40=2|59=0|21=1", "an empty form must not error"


class TestTemplates:
    """Blotter action templates: every order and trade dialog opens on a
    Template dropdown — `templates_list` rows of the dialog's scope, mkui's
    `fill` copying the picked row's terms into the form — and closes on a
    Save-as name, under which the fix_cmd op keeps the terms it was sent
    (`TEMPLATE_TERMS` in fix_command.py, `save_template` in the engine, a
    name unique within its scope). One Templates pane under the Trading menu
    edits and deletes; no blotter button opens a template list any more."""

    # fix_cmd op -> the template scope its dialog loads and saves
    SCOPES = {
        "send_new_order": "order", "send_cancel_replace": "order", "send_cancel": "cancel",
        "accept_request": "accept", "reject_request": "reject", "fill_order": "fill",
        "unsolicited_cancel": "unsolicited", "restate_order": "restate",
        "dk_trade": "dk", "correct_trade": "correct", "bust_trade": "bust",
        "renotify_trade": "renotify",
    }

    @staticmethod
    def _fields(dialog):
        for item in dialog["fields"]:
            yield from _leaves(item)

    def test_scopes_agree_everywhere(self):
        from mkfix.fix.engine import TEMPLATE_SCOPES as engine_scopes
        from mkfix.services.fix_command import TEMPLATE_TERMS
        assert set(engine_scopes) == TEMPLATE_SCOPES
        assert {op: scope for op, (scope, _) in TEMPLATE_TERMS.items()} == self.SCOPES
        assert set(self.SCOPES.values()) == TEMPLATE_SCOPES

    def test_every_dialog_opens_on_a_template_dropdown(self, app_config, toml_config):
        """The pick fills exactly the terms Save-as keeps; a term the dialog
        does not ask for (Replace's session) rides as rowData instead. Only
        an order template records a session — the one field a pick fills."""
        from mkfix.services.fix_command import TEMPLATE_TERMS
        columns = set(toml_config["tables"]["fix_templates"]["columns"])
        for op, scope in self.SCOPES.items():
            dialog = _find_dialog(app_config, op)
            first = dialog["fields"][0]
            assert first["type"] == "select" and first["name"].startswith("_"), \
                f"{op}: the pick itself must never be submitted"
            assert first["optionsFrom"] == {
                "service": "templates_list", "params": {"scope": scope},
                "value": "name", "label": "name",
            }, f"{op}: options are keyed by name, what Save-as and remember know"
            names = _dialog_field_names(dialog)
            fill = first["fill"]
            assert set(fill.values()) <= columns, op
            keys = set(TEMPLATE_TERMS[op][1])
            assert ("session_id" in keys) == (scope == "order"), op
            assert set(fill) == keys & names, op
            assert keys - names <= set(dialog.get("rowData", {})), op
            assert all(fill[k] == k for k in fill), f"{op}: template columns are named as the fields"

    def test_each_dialog_remembers_its_last_template(self, app_config):
        """mkui's `remember` reopens the dialog on the template last picked
        or saved there — the Save-as name when one was typed, since that is
        the template the sent terms now live under — keyed per dialog, so
        New and Replace keep separate memories of the order scope."""
        from mkio import expr
        keys = set()
        for op in self.SCOPES:
            first = _find_dialog(app_config, op)["fields"][0]
            remember = first["remember"]
            assert remember["key"] == f"mkfix.template.{op}"
            keys.add(remember["key"])
            source = remember["value"]
            for ref in expr.field_refs(expr.parse(source)):
                assert ref in ("save_as", "_template"), f"{op} remembers from {ref!r}"
            assert expr.evaluate(source, {"save_as": "", "_template": "big"}) == "big"
            assert expr.evaluate(source, {"save_as": None, "_template": ""}) == ""
            assert expr.evaluate(source, {"save_as": "mine", "_template": "big"}) == "mine"
        assert len(keys) == len(self.SCOPES)

    def test_every_dialog_closes_on_an_optional_save_as(self, app_config):
        """Save-as is the last named field of every dialog, right before the
        closing preview — on New and Replace as the last field of the folded
        Advanced section (`TestServiceReferences` pins that section)."""
        for op in self.SCOPES:
            fields = _find_dialog(app_config, op)["fields"]
            before = fields[-2]
            save = before["fields"][-1] if "group" in before else before
            assert save.get("name") == "save_as" and save["type"] == "text", op
            assert "required" not in save and "value" not in save, f"{op}: saving is optional"
            assert fields[-1].get("label") == "Terms as tags", f"{op}: the preview still closes"

    def test_pinned_dialogs_keep_their_terms_but_not_save_as(self, app_config):
        """A pinned dialog is a run of sends varying a term or two, so the
        form keeps what was entered rather than resetting to the template
        (mkui 1.4.0 `pin: "keep"`); the Save-as name alone resets, or every
        later submit would re-save the template under it — silently
        overwriting it once a term is edited."""
        for op in self.SCOPES:
            dialog = _find_dialog(app_config, op)
            assert dialog.get("pin") == "keep", op
            for field in self._fields(dialog):
                expected = "reset" if field.get("name") == "save_as" else None
                assert field.get("pin") == expected, f"{op}: {field.get('name')!r} pin={field.get('pin')!r}"

    def test_templates_list_serves_every_term_of_one_scope(self, toml_config):
        svc = toml_config["services"]["templates_list"]
        assert svc["protocol"] == "reqrep"
        selected = set(re.findall(r"\w+", svc["sql"].split(" FROM ")[0][len("SELECT "):]))
        assert selected == set(toml_config["tables"]["fix_templates"]["columns"]) - {"created_at"}
        assert "WHERE scope = :scope" in svc["sql"]

    def test_one_templates_pane_under_the_trading_menu(self, app_config, toml_config):
        spec = app_config["panes"]["templates"]
        assert spec["type"] == "mkio-table" and spec["service"] == "templates_query"
        assert "filter" not in spec, "every scope in the one pane"
        assert toml_config["services"]["templates_query"]["primary_table"] == "fix_templates"
        assert [b["label"] for b in spec["buttons"]] == ["Edit", "Delete"]
        trading = next(m for m in app_config["menubar"] if m["label"] == "Trading")
        assert trading["items"][-1] == {"label": "Templates", "action": "pane.show", "args": "templates"}
        assert _menubar_pane_ids(app_config["menubar"]).count("templates") == 1
        for pane_id, pane in app_config["panes"].items():
            for b in pane.get("buttons", []):
                assert b["label"] != "Templates", f"{pane_id} still opens a template pane"
                dialog = b["action"].get("dialog")
                if dialog and pane_id != "templates":
                    assert dialog["submit"]["service"] != "templates", \
                        f"{pane_id} {b['label']}: dialogs save through their fix_cmd op"

    def test_edit_shows_each_scope_the_terms_its_dialog_loads(self, app_config, toml_config):
        from mkio import expr
        from mkfix.services.fix_command import TEMPLATE_TERMS
        spec = app_config["panes"]["templates"]
        edit = next(b for b in spec["buttons"] if b["label"] == "Edit")["action"]["dialog"]
        assert edit["submit"] == {"label": "Save Template", "service": "templates", "op": "update"}
        assert edit["rowData"] == {"id": "${row.id}"}
        terms = set(toml_config["tables"]["fix_templates"]["columns"]) - {"id", "scope", "created_at"}
        assert _dialog_field_names(edit) == terms, "a template keeps its kind"
        for f in self._fields(edit):
            if f.get("name") in terms:
                assert f.get("value") == "${row.%s}" % f["name"] or f.get("compute") == "row.%s" % f["name"], \
                    f"Edit must prefill {f['name']} from the row"
        for op, (scope, keys) in TEMPLATE_TERMS.items():
            shown = {
                f["name"] for f in self._fields(edit)
                if f.get("name") in terms - {"name"}
                and expr.evaluate(f.get("showWhen", "TRUE"), {"row": {"scope": scope}})
            }
            assert shown == set(keys), scope
        delete = next(b for b in spec["buttons"] if b["label"] == "Delete")["action"]
        assert delete == {"type": "transaction", "service": "templates", "op": "delete",
                          "data": {"id": "${row.id}"}}

    def test_templates_table_is_config_data(self, toml_config):
        from mkfix.archive import ALIASES
        from mkfix.fix.engine import TEMPLATE_TERM_COLS
        table = toml_config["tables"]["fix_templates"]
        assert table["archive"] == {"group": "config"}
        assert ALIASES["templates"] == "fix_templates"
        columns = set(table["columns"]) - {"id", "created_at"}
        assert set(TEMPLATE_TERM_COLS) == columns - {"scope", "name"}
        ops = toml_config["services"]["templates"]["ops"]
        assert set(ops["add"][0]["fields"]) == columns
        assert set(ops["update"][0]["fields"]) == columns - {"scope"}
        for op in ("add", "update"):
            required = set(ops[op][0]["fields"]) - set(ops[op][0]["defaults"])
            assert required == ({"scope", "name"} if op == "add" else {"name"})


class TestClientColumn:
    """The client rides on a per-session tag list (fix_sessions.client_tags,
    asked for in the session dialogs) and lands as `client` on orders,
    trades and messages: every blotter shows and can filter it, the order
    dialogs carry a Client field the engine stamps on the session's tag,
    and Cancel passes the row's client along as rowData."""

    BLOTTERS = ("order-blotter", "market-order-blotter", "trade-blotter",
                "market-trade-blotter", "raw-messages")

    def test_blotters_show_and_filter_client(self, app_config, toml_config):
        for pane_id in self.BLOTTERS:
            pane = app_config["panes"][pane_id]
            assert "client" in pane["columns"], pane_id
            assert "client" in toml_config["services"][pane["service"]]["filterable"], pane_id
        sessions = app_config["panes"]["session-blotter"]
        assert "client_tags" in sessions["columns"]

    def test_session_dialogs_ask_for_client_tags(self, app_config, toml_config):
        ops = toml_config["services"]["session_mgmt"]["ops"]
        for label, op in (("New", "add"), ("Edit", "update")):
            dialog = _session_button(app_config, label)["action"]["dialog"]
            assert "client_tags" in _dialog_field_names(dialog), label
            step = next(s for s in ops[op] if s["table"] == "fix_sessions")
            assert "client_tags" in step["fields"] and step["defaults"]["client_tags"] == ""

    def test_order_dialogs_carry_the_client(self, app_config):
        from mkfix.services.fix_command import ORDER_TERMS
        assert "client" in ORDER_TERMS
        for op in ("send_new_order", "send_cancel_replace"):
            assert "client" in _dialog_field_names(_find_dialog(app_config, op)), op
        cancel = _find_dialog(app_config, "send_cancel")
        assert cancel["rowData"]["client"] == "${row.client}"
        assert "client" in app_config["panes"]["templates"]["columns"]
