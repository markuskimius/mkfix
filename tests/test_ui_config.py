"""Static integrity checks on the UI config (app.json) and its assets.

app.json drives the whole UI declaratively, so a dangling pane reference or a
JS module that no longer exists fails silently in the browser rather than at
import time. These tests fail the build instead.
"""

import json
import re
import tomllib
from pathlib import Path

import pytest

from mkfix import __version__

STATIC = Path(__file__).resolve().parent.parent / "mkfix" / "static"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def app_config() -> dict:
    return json.loads((STATIC / "app.json").read_text())


@pytest.fixture(scope="module")
def index_imports() -> list[str]:
    html = (STATIC / "index.html").read_text()
    return re.findall(r'import\s+"([^"]+)"', html)


@pytest.fixture(scope="module")
def toml_config() -> dict:
    return tomllib.loads((ROOT / "mkfix" / "mkfix.toml").read_text())


@pytest.fixture(scope="module")
def known_services(toml_config) -> set[str]:
    """TOML-declared services plus the ones registered in code (fix_cmd)."""
    main = (ROOT / "mkfix" / "__main__.py").read_text()
    return set(toml_config["services"]) | set(re.findall(r'add_service\(\s*"([^"]+)"', main))


def _frame_pane_ids(layout: dict) -> list[str]:
    """Pane ids referenced by a frame layout, recursing through splits/tabs."""
    ids = []
    for child in layout.get("children", []):
        if isinstance(child, str):
            ids.append(child)
        else:
            ids.extend(_frame_pane_ids(child))
    return ids


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


def _dialog_field_names(dialog: dict) -> set[str]:
    """Named payload fields of a dialog spec, flattening row groups."""
    names = set()
    for item in dialog.get("fields", []):
        for field in item.get("row", [item]):
            if "name" in field:
                names.add(field["name"])
    return names


def _menubar_pane_ids(menubar: list) -> list[str]:
    return [
        item["args"]
        for menu in menubar
        for item in menu.get("items", [])
        if item.get("action") == "pane.show"
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
    return {p.name: p.read_text() for p in sorted((STATIC / "panes").glob("*.js"))}


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
            source = target.read_text()
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
        handled = set(re.findall(
            r'command == "([^"]+)"',
            (ROOT / "mkfix" / "services" / "fix_command.py").read_text(),
        ))
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
        a column the table does not have silently interpolates empty."""
        services = toml_config["services"]
        tables = toml_config["tables"]
        checked = 0
        for pane_id, spec in app_config["panes"].items():
            table = services.get(spec.get("service"), {}).get("primary_table")
            if not table or "buttons" not in spec:
                continue
            columns = set(tables[table]["columns"])
            tokens = set(re.findall(r"\$\{row\.(\w+)\}", json.dumps(spec["buttons"])))
            unknown = tokens - columns
            assert not unknown, \
                f"pane {pane_id!r} buttons reference non-columns of {table}: {sorted(unknown)}"
            checked += 1
        assert checked, "no panes with buttons and a table-backed service"

    def test_reset_seq_dialog_fields_match_dispatch(self, app_config):
        """The reset-seq dialog's field names become the fix_cmd payload; the
        dispatch reads them by exact key, so a renamed field is silently
        dropped and the reset falls back to defaults."""
        dialog = next(
            (node for node in _walk_dicts(app_config)
             if node.get("submit", {}).get("op") == "reset_sequence"),
            None,
        )
        assert dialog, "no reset_sequence dialog in app.json"
        fields = _dialog_field_names(dialog)
        assert fields, "reset-seq dialog defines no named fields"
        dispatch = (ROOT / "mkfix" / "services" / "fix_command.py").read_text()
        branch = dispatch.split('command == "reset_sequence"')[1].split("elif")[0]
        for field in fields:
            assert field in branch, f"reset dialog field {field!r} not read by reset_sequence dispatch"


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
                target = action.get("service") or action.get("dialog", {}).get("submit", {}).get("service")
                assert target in known_services, \
                    f"pane {pane_id!r} button {button['label']!r} calls unknown service {target!r}"

    def test_fix_cmd_ops_have_dispatch_branches(self, app_config):
        """An op in app.json with no _dispatch branch fails only when the
        button is clicked, and only in the browser."""
        source = (ROOT / "mkfix" / "services" / "fix_command.py").read_text()
        handled = set(re.findall(r'command == "([^"]+)"', source))
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
        assert "rowMatch" not in new.get("enable", {})

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
        assert [b["label"] for b in buttons] == ["New", "Replace", "Cancel"]
        ops = {
            b["label"]: b["action"].get("op")
            or b["action"]["dialog"]["submit"]["op"]
            for b in buttons
        }
        assert ops == {"New": "send_new_order", "Replace": "send_cancel_replace",
                       "Cancel": "send_cancel"}

    def test_received_orders_buttons_gate_on_pending_action(self, app_config):
        """One Accept/Reject pair handles new orders and cancel/replace
        requests alike: both gate on pending_action (a new order arrives as
        pending "New") and dispatch via the request ops, while Fill gates on
        status only — a pending request must not block fills on the
        still-working order."""
        buttons = app_config["panes"]["market-order-blotter"]["buttons"]
        assert [b["label"] for b in buttons] == ["Accept", "Reject", "Fill"]
        by = {b["label"]: b for b in buttons}
        pending = {"pending_action": ["New", "Cancel", "Replace"], "session_status": "ACTIVE"}
        assert by["Accept"]["enable"]["rowMatch"] == pending
        assert by["Reject"]["enable"]["rowMatch"] == pending
        assert by["Accept"]["action"]["dialog"]["submit"]["op"] == "accept_request"
        assert by["Reject"]["action"]["dialog"]["submit"]["op"] == "reject_request"
        assert "pending_action" not in by["Fill"]["enable"].get("rowMatch", {})

    def test_order_and_trade_actions_gate_on_live_session(self, app_config):
        """Every button that acts on an existing order or trade requires the
        row's session to be up (session_status is the engine-written mirror of
        the owning session's live status). New order entry is exempt — its
        dialog picks the session itself, and the server rejects a dead one."""
        gated = {
            "order-blotter": ["Replace", "Cancel"],
            "market-order-blotter": ["Accept", "Reject", "Fill"],
            "market-trade-blotter": ["Correct", "Bust"],
        }
        for pane_id, labels in gated.items():
            by = {b["label"]: b for b in app_config["panes"][pane_id]["buttons"]}
            for label in labels:
                match = by[label]["enable"]["rowMatch"].get("session_status")
                assert match == "ACTIVE", \
                    f"{pane_id} {label} must gate on session_status ACTIVE"

    def test_every_send_action_dialog_offers_extra_tags(self, app_config):
        """Every button that sends a FIX message must expose the optional
        extra_tags field — the whole point of the feature is that no send
        path is exempt. Flatten row groups: fields may nest one level."""
        send_panes = ["order-blotter", "market-order-blotter", "market-trade-blotter"]
        checked = 0
        for pane_id in send_panes:
            for button in app_config["panes"][pane_id]["buttons"]:
                dialog = button["action"]["dialog"]
                names = _dialog_field_names(dialog)
                assert "extra_tags" in names, \
                    f"{pane_id} {button['label']} dialog must offer extra_tags"
                assert not any(
                    f.get("required") for f in dialog["fields"]
                    if f.get("name") == "extra_tags"
                ), "extra_tags must stay optional"
                checked += 1
        assert checked >= 8

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
            for f in (item["row"] if "row" in item else [item]):
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
                for f in (item["row"] if "row" in item else [item])
                if f.get("name") == "extra_tags"
            )
            assert field.get("value") == value, \
                f"{op} dialog must prefill extra_tags from {value}"

    def test_pane_columns_exist_in_primary_table(self, app_config, toml_config):
        """A misspelled column renders as a permanently empty blotter column."""
        checked = 0
        for pane_id, spec in app_config["panes"].items():
            service = toml_config["services"].get(spec.get("service"), {})
            table = toml_config["tables"].get(service.get("primary_table"), {})
            if not table or "columns" not in spec:
                continue
            checked += 1
            unknown = set(spec["columns"]) - set(table["columns"])
            assert not unknown, f"pane {pane_id!r} shows unknown columns {sorted(unknown)}"
        assert checked, "no pane columns checked against a table schema"

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


class TestStyleAndGateValues:
    """Style rules and rowMatch gates compare against displayed values; a
    renamed column or display value leaves them silently dead in the browser,
    so both are checked statically like everything else in app.json."""

    ENGINE_STATUSES = {"DOWN", "ERROR", "INITIATING", "LISTENING",
                       "LOGON_SENT", "ACTIVE", "LOGOUT_SENT"}

    @pytest.fixture(scope="class")
    def value_domains(self):
        """Known display-value domains keyed by (table, column)."""
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
        }

    def _pane_table(self, spec, toml_config):
        service = toml_config["services"].get(spec.get("service"), {})
        return service.get("primary_table")

    def test_style_rules_reference_real_columns(self, app_config, toml_config):
        checked = 0
        for pane_id, spec in app_config["panes"].items():
            table = toml_config["tables"].get(self._pane_table(spec, toml_config), {})
            if not table:
                continue
            cols = set(table["columns"])
            for col in spec.get("styles", {}):
                checked += 1
                assert col in cols, f"pane {pane_id!r} styles unknown column {col!r}"
            for rule in spec.get("rowStyle", []):
                for col in rule.get("when", {}):
                    checked += 1
                    assert col in cols, \
                        f"pane {pane_id!r} rowStyle conditions on unknown column {col!r}"
        assert checked, "no style rules checked"

    @staticmethod
    def _rule_values(rule):
        for key in ("eq", "ne"):
            if key in rule:
                yield rule[key]
        yield from rule.get("in", [])

    def test_styled_values_exist(self, app_config, toml_config, value_domains):
        checked = 0
        for pane_id, spec in app_config["panes"].items():
            table = self._pane_table(spec, toml_config)
            for col, rules in spec.get("styles", {}).items():
                domain = value_domains.get((table, col))
                if domain is None:
                    continue
                for rule in rules:
                    for value in self._rule_values(rule):
                        checked += 1
                        assert value in domain, \
                            f"pane {pane_id!r} styles {col!r} on unknown value {value!r}"
            for row_rule in spec.get("rowStyle", []):
                for col, cond in row_rule.get("when", {}).items():
                    domain = value_domains.get((table, col))
                    if domain is None:
                        continue
                    values = cond if isinstance(cond, list) \
                        else list(self._rule_values(cond)) if isinstance(cond, dict) \
                        else [cond]
                    for value in values:
                        checked += 1
                        assert value in domain, \
                            f"pane {pane_id!r} rowStyle matches {col!r} on unknown value {value!r}"
        assert checked, "no styled values checked"

    def test_gate_values_exist(self, app_config, toml_config, value_domains):
        """rowMatch values are compared as strings against live rows; a value
        the engine never writes disables the button forever."""
        checked = 0
        for pane_id, spec in app_config["panes"].items():
            table = self._pane_table(spec, toml_config)
            for button in spec.get("buttons", []):
                for col, expected in button.get("enable", {}).get("rowMatch", {}).items():
                    domain = value_domains.get((table, col))
                    if domain is None:
                        continue
                    values = expected if isinstance(expected, list) else [expected]
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

    def test_statusbar_version_matches_package(self, app_config):
        major_minor = ".".join(__version__.split(".")[:2])
        texts = [item.get("text", "") for item in app_config["statusbar"]["right"]]
        assert any(t == f"mkfix v{major_minor}" for t in texts), \
            f"statusbar shows {texts}, expected 'mkfix v{major_minor}'"

    def test_mkui_floor_supports_configured_table_options(self, app_config):
        """`live` and `select` on mkio-table need mkui 0.1.52+."""
        uses_new_options = any(
            "live" in spec or "select" in spec
            for spec in app_config["panes"].values()
            if spec["type"] == "mkio-table"
        )
        if not uses_new_options:
            pytest.skip("no pane uses live/select")

        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
        floors = [d for d in pyproject["project"]["dependencies"] if d.startswith("mkui")]
        assert floors, "mkui missing from dependencies"
        floor = tuple(int(n) for n in floors[0].split(">=")[1].split("."))
        assert floor >= (0, 1, 52), f"mkui floor {floor} predates live/select support"

    def test_mkui_floor_supports_session_dialog(self):
        """openDialog before 0.1.54 clips a body taller than the default
        frame; the session form is tall enough to need the auto-grow."""
        uses_dialog = any(
            "mkui-dialog.js" in p.read_text() for p in (STATIC / "panes").glob("*.js")
        ) or '"type": "dialog"' in (STATIC / "app.json").read_text()
        if not uses_dialog:
            pytest.skip("no pane opens an mkui dialog")

        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
        floors = [d for d in pyproject["project"]["dependencies"] if d.startswith("mkui")]
        assert floors, "mkui missing from dependencies"
        floor = tuple(int(n) for n in floors[0].split(">=")[1].split("."))
        assert floor >= (0, 1, 54), f"mkui floor {floor} predates dialog auto-grow"

    def test_readme_dependency_floors_match_pyproject(self):
        """README repeats the mkio/mkui floors in prose; keep them honest."""
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
        readme = (ROOT / "README.md").read_text()
        for dep in pyproject["project"]["dependencies"]:
            name, floor = dep.split(">=")
            assert f"{name}) >= {floor}" in readme, \
                f"README floor for {name} does not match pyproject ({dep})"


class TestDictionaryConfig:
    def test_standard_versions_consistent_everywhere(self, app_config, toml_config):
        """The standard-version list lives in dictionary.py, fix-dictionary.js,
        the dictionaries_list sql, and the session dialogs' fix_version
        options, and each version must ship a generated JSON; drift in any
        copy surfaces only in the browser."""
        from mkfix.fix.dictionary import STANDARD_VERSIONS

        js = (STATIC / "fix-dictionary.js").read_text()
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
        source = (ROOT / "mkfix" / "services" / "fix_command.py").read_text()
        js = (STATIC / "panes" / "dictionaries.js").read_text()
        used = set(re.findall(r'cmd\("([a-z_]+)"', js))
        handled = set(re.findall(r'command == "([^"]+)"', source))
        assert used, "dictionaries pane calls no fix_cmd commands"
        missing = used - handled
        assert not missing, f"dictionaries pane sends unhandled fix_cmd commands: {sorted(missing)}"

    def test_curated_fix42_names_survive_regeneration(self):
        """The FIX42 overlay pins display names the engine writes into rows and
        the style rules test; the generated FIX42.json must keep them."""
        overlay = json.loads((ROOT / "tools" / "overlays" / "FIX42.json").read_text())
        generated = json.loads(
            (ROOT / "mkfix" / "fix" / "dictionary_data" / "FIX42.json").read_text())
        for tag, entry in overlay.get("fields", {}).items():
            assert generated["fields"].get(tag) == entry, f"field {tag} lost its curated name"
        for tag, values in overlay.get("enums", {}).items():
            for code, name in values.items():
                assert generated["enums"].get(tag, {}).get(code) == name, \
                    f"enum {tag}={code} lost its curated name"
