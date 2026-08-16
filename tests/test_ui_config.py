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
        """Same guard app.json gets, for commands sent from pane JS."""
        handled = set(re.findall(
            r'command == "([^"]+)"',
            (ROOT / "mkfix" / "services" / "fix_command.py").read_text(),
        ))
        used = {
            (name, command)
            for name, source in pane_sources.items()
            for command in re.findall(r'command:\s*"(\w+)"', source)
        }
        assert used, "no fix_cmd commands referenced by pane modules"
        for name, command in used:
            assert command in handled, f"{name} sends unhandled fix_cmd command {command!r}"

    def test_session_dialog_fields_match_transaction_schema(self, pane_sources, toml_config):
        """The session dialog's field names become the session_mgmt payload
        verbatim; a name the op does not list is silently dropped (or, if a
        required field goes missing, nacks on save)."""
        ops = toml_config["services"]["session_mgmt"]["ops"]
        allowed = set()
        for op in ("add", "update"):
            for entry in ops[op]:
                allowed.update(entry["fields"])
                allowed.update(entry.get("key", []))
        dialog_fields = set(re.findall(r'name: "(\w+)"', pane_sources["session-manager.js"]))
        assert dialog_fields, "session dialog defines no named fields"
        unknown = dialog_fields - allowed
        assert not unknown, f"session dialog sends fields session_mgmt ignores: {sorted(unknown)}"


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
        pending = {"pending_action": ["New", "Cancel", "Replace"]}
        assert by["Accept"]["enable"]["rowMatch"] == pending
        assert by["Reject"]["enable"]["rowMatch"] == pending
        assert by["Accept"]["action"]["op"] == "accept_request"
        assert by["Reject"]["action"]["dialog"]["submit"]["op"] == "reject_request"
        assert "pending_action" not in by["Fill"]["enable"].get("rowMatch", {})

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
        )
        if not uses_dialog:
            pytest.skip("no pane module opens an mkui dialog")

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
