"""The macro UI's pure parts, run under node (skipped without it), and
the guards that keep the help pages, the vendored editor and the wiring true."""

import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from mkfix import macro

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "mkfix" / "static"
APP_TOML = ROOT / "mkfix" / "config" / "app.toml"
TWO = ("client", "market")
SIDES = (*TWO, "end-to-end")          # the kinds of macro: one a side, and the ones that hold both
HELP = STATIC / "help"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def run_js(tmp_path: Path, body: str):
    """Evaluate ``body`` (an expression) with the two modules imported as L and M."""
    for name in ("macro-lang", "markdown", "line-diff", "macro-status-lib"):
        (tmp_path / f"{name}.mjs").write_text((STATIC / f"{name}.js").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "vocab.json").write_text(json.dumps(macro.vocabulary()), encoding="utf-8")
    script = tmp_path / "run.mjs"
    script.write_text(
        'import * as L from "./macro-lang.mjs";\nimport * as M from "./markdown.mjs";\n'
        'import * as D from "./line-diff.mjs";\nimport * as S from "./macro-status-lib.mjs";\n'
        'import fs from "node:fs";\n'
        'const vocab = JSON.parse(fs.readFileSync(new URL("./vocab.json", import.meta.url), "utf8"));\n'
        f"console.log(JSON.stringify({body}));\n", encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
class TestColouring:
    def tokens(self, tmp_path, line):
        got = run_js(tmp_path, f"L.tokenizeLine(L.buildRules(vocab), {json.dumps(line)})")
        return [(t, x.strip()) for t, x in got if t != "text"]

    def test_a_statement_line(self, tmp_path):
        assert self.tokens(tmp_path, "    when cancel rejected and order.leaves_qty == 0   # late") == [
            ("keyword", "when"), ("constant.language", "cancel rejected"), ("keyword.operator", "and"),
            ("variable.language", "order"), ("keyword.operator", "=="), ("constant.numeric", "0"), ("comment", "# late")]

    def test_an_action_line(self, tmp_path):
        got = self.tokens(tmp_path, "    fill qty: MIN(100, order.leaves_qty), price: TICK(px, 0.01), side: buy")
        assert got[0] == ("entity.name.function", "fill")
        assert ("variable.parameter", "qty") in got and ("support.function", "TICK") in got
        assert ("support.constant", "buy") in got and ("constant.numeric", "0.01") in got

    def test_verbs_are_verbs_only_at_the_start_of_a_line(self, tmp_path):
        assert ("constant.language", "fill") in self.tokens(tmp_path, "    wait fill or timeout 2s")
        assert ("entity.name.function", "fill") in self.tokens(tmp_path, "    fill qty: 1, price: 2")

    def test_headers_durations_strings(self, tmp_path):
        assert self.tokens(tmp_path, "on sent order where symbol in ['IBM # not a comment']") == [
            ("keyword.control", "on sent order"), ("keyword.operator", "where"), ("keyword.operator", "in"),
            ("string", "'IBM # not a comment'")]
        assert self.tokens(tmp_path, "    after 1.5s ± 250ms") == [
            ("keyword", "after"), ("constant.numeric", "1.5s"), ("keyword.operator", "±"), ("constant.numeric", "250ms")]

    def test_every_word_of_the_vocabulary_is_coloured(self, tmp_path):
        """The rules are built from the vocabulary, so a new verb or event is
        coloured the day the parser learns it."""
        v = macro.vocabulary()
        for verb in v["verbs"]:
            assert self.tokens(tmp_path, f"    {verb}")[0] == ("entity.name.function", verb), verb
        for event in v["events"]:
            assert ("constant.language", event) in self.tokens(tmp_path, f"    when {event}"), event

    def test_ace_gets_no_lookbehind(self, tmp_path):
        rules = run_js(tmp_path, "L.aceRules(vocab).start.map((r) => r.regex)")
        assert rules and not any("(?<" in r for r in rules)

    def test_highlight_escapes_html(self, tmp_path):
        html = run_js(tmp_path, "L.highlight(\"    log '<b>' # <i>\", vocab)")
        assert "<b>" not in html and "&lt;b&gt;" in html and 'class="macro-keyword"' in html


@needs_node
class TestCompletion:
    LINES = ["on order", "    ", "    when ", "    fill ", "    fill qty: 1, ", "    restate qty: 1, reason: ",
             "    if order.", "    bust ", "    fill using '", "run on ", "", "on sent order", "    ", "    when cancel rejected and event.",
             "run ", "    ",
             "on ioi", "    ", "    when ", "    if ioi.", "    new symbol: ioi.symbol, side: ",
             "on allocation", "    reject allocation status: ", "    when ",
             "run", "    ioi symbol: 'A', side: ", "    ", "    when ", "    if event.prev."]

    def names(self, tmp_path, row, limit=None, side=None):
        extras = {"sessions": ["S1", "S2"], "templates": {"fill": ["half", "all"]}, "templateScopes": {"fill": "fill"}}
        if side:
            extras["side"] = side
        got = run_js(tmp_path, f"L.completionsAt(vocab, {json.dumps(self.LINES)}, {row}, {len(self.LINES[row])}, "
                               f"{json.dumps(extras)}).map((c) => c.caption)")
        return got[:limit] if limit else got

    def test_a_line_starts_with_what_the_block_allows(self, tmp_path):
        market = self.names(tmp_path, 1)
        assert {"accept", "fill", "unsol cxl", "when", "after"} <= set(market) and "new" not in market and "cancel" not in market
        sending = self.names(tmp_path, 12)
        assert {"replace", "cancel", "dk"} <= set(sending) and "accept" not in sending and "new" not in sending

    def test_events_for_the_side(self, tmp_path):
        assert self.names(tmp_path, 2, 3) == ["cancel", "replace", "dk"] and "ack" not in self.names(tmp_path, 2)

    def test_terms_then_the_ones_left(self, tmp_path):
        assert self.names(tmp_path, 3) == ["qty", "price", "text", "extra", "leg", "report_legs", "using"]
        assert self.names(tmp_path, 4) == ["price", "text", "extra", "leg", "report_legs"]

    def test_words_for_a_term_templates_sessions_fields_targets_headers(self, tmp_path):
        assert "repricing" in self.names(tmp_path, 5)
        assert {"leaves_qty", "pending_action", "cxl_rej_reason"} <= set(self.names(tmp_path, 6))
        assert self.names(tmp_path, 7, 3) == ["last trade", "first trade", "trade where"]
        assert self.names(tmp_path, 8) == ["half", "all"]
        assert self.names(tmp_path, 9) == ["S1", "S2"]
        assert self.names(tmp_path, 10) == ["seed", "on error", "share", "define", *macro.vocabulary()["blocks"]]
        assert self.names(tmp_path, 14) == ["on"], "`run` may name its session, or leave it to Run…"
        assert "new" in self.names(tmp_path, 15), "a bare `run` opens a client block"

    def test_the_families_blocks_offer_their_own_words(self, tmp_path):
        received_ioi = self.names(tmp_path, 17)
        assert "new" in received_ioi and "cancel ioi" not in received_ioi and "accept" not in received_ioi
        assert self.names(tmp_path, 18, 2) == ["replaced", "canceled"] and "ack" not in self.names(tmp_path, 18)
        assert {"ioi_id", "ioi_qty", "qualifiers"} <= set(self.names(tmp_path, 19)) and "leaves_qty" not in self.names(tmp_path, 19)
        assert self.names(tmp_path, 20) == ["buy", "sell", "sell_short", "sell_short_exempt"], "an order's sides on `new`"
        assert self.names(tmp_path, 22)[:2] == ["accepted", "block_level_reject"], "the Ack's status words"
        assert self.names(tmp_path, 23, 2) == ["cancel", "replace"]
        sending = self.names(tmp_path, 25)
        assert sending == ["buy", "sell", "undisclosed", "cross"], "an IOI's sides on `ioi`"
        assert {"replace ioi", "cancel ioi"} <= set(self.names(tmp_path, 26)) and "new" not in self.names(tmp_path, 26)
        assert set(self.names(tmp_path, 27)) == {"message", "manual", "session down", "session up", "error", "signal"}, \
            "nothing answers an IOI"
        assert {"ioi_id", "ioi_qty"} <= set(self.names(tmp_path, 28)), "event.prev is the block's own subject"
        assert run_js(tmp_path, f"L.blockAt(vocab, {json.dumps(self.LINES)}, 26)") == {"kind": "client", "subject": "ioi"}
        assert run_js(tmp_path, f"L.blockAt(vocab, {json.dumps(self.LINES)}, 17)") == {"kind": "market", "subject": "ioi"}

    def test_an_editor_offers_only_its_own_sides_blocks(self, tmp_path):
        """The market side receives orders and RFQs and sends the rest; the
        client side the other way round. `run` sends for either."""
        assert self.names(tmp_path, 10, side="market") == [
            "seed", "on error", "share", "define", "on order", "run", "on signal", "on sent ioi", "on sent advert",
            "on sent allocation", "on rfq", "on sent quote", "on sent rfq request", "on list"]
        assert self.names(tmp_path, 10, side="client") == [
            "seed", "on error", "share", "define", "on sent order", "run", "on signal", "on ioi", "on advert", "on allocation",
            "on sent rfq", "on quote", "on rfq request", "on sent list"]
        assert {"response_to", "reason", "prev", "tag"} <= set(self.names(tmp_path, 13))

    TOGETHER = ["share done = 0", "run", "    new symbol: 'IBM', side: buy, qty: 1", "    share last = order.cl_ord_id",
                "    signal 'parent filled' with 1   # not signal 'in a comment'", "    signal 'go'",
                "    when signal '", "    log shared.", "    log event.sender.", "    ", "    when ",
                "on signal '", "    allocate symbol: 'A', side: ", "    ", "on signal "]

    def together(self, tmp_path, row):
        return run_js(tmp_path, f"L.completionsAt(vocab, {json.dumps(self.TOGETHER)}, {row}, "
                                f"{len(self.TOGETHER[row])}, {{}}).map((c) => c.caption)")

    def test_do_offers_the_macros_defines(self, tmp_path):
        lines = ["define halves(px)", "    accept", "define rules", "    accept", "on order", "    do "]
        got = run_js(tmp_path, f"L.completionsAt(vocab, {json.dumps(lines)}, 5, 7, {{}})"
                               ".map((c) => [c.caption, c.value ?? c.snippet ?? c.text])")
        assert [c for c, _ in got] == ["halves", "rules"]
        assert [v for _, v in got] == ["halves(px)", "rules()"], "the call, its parameters to fill in"

    def test_an_order_line_is_offered_only_under_new_list(self, tmp_path):
        lines = ["run", "    new list mode: list", "        ", "        repeat 3", "            ", "    "]
        def at(row):
            return run_js(tmp_path, f"L.completionsAt(vocab, {json.dumps(lines)}, {row}, {len(lines[row])}, {{}})"
                                    ".map((c) => c.caption)")
        assert "order" in at(2) and "order" in at(4), "under the list, and in a repeat under it"
        assert "order" not in at(5)

    def test_what_the_macros_of_a_run_say_to_each_other(self, tmp_path):
        assert self.together(tmp_path, 6) == ["go", "parent filled"], "the signals the macro's own lines send"
        assert self.together(tmp_path, 11) == ["go", "parent filled"], "and in a block's header"
        assert self.together(tmp_path, 7) == ["done", "last"], "what it shares, wherever it is set"
        sender = self.together(tmp_path, 8)
        assert {"cl_ord_id", "leaves_qty", "ioi_id", "alloc_id", "adv_id"} <= set(sender), "a row of any kind"
        assert sender == sorted(set(sender))
        statements = self.together(tmp_path, 9)
        assert {"signal", "share"} <= set(statements) and "on signal" not in statements
        assert "signal" in self.together(tmp_path, 10)
        assert self.together(tmp_path, 14) == ["'NAME'"]
        # an `on signal` block is a sending block: its subject is what it sends
        assert run_js(tmp_path, f"L.blockAt(vocab, {json.dumps(self.TOGETHER)}, 13)") == {"kind": "client", "subject": "allocation"}
        assert self.together(tmp_path, 12) == ["buy", "sell", "sell_short", "sell_short_exempt"]
        assert {"replace allocation", "cancel allocation"} <= set(self.together(tmp_path, 13))
        assert run_js(tmp_path, f"L.signalNames({json.dumps(self.TOGETHER)})") == ["go", "parent filled"]
        assert run_js(tmp_path, f"L.sharedNames({json.dumps(self.TOGETHER)})") == ["done", "last"]

    def test_the_new_words_are_coloured_and_explained(self, tmp_path):
        rules = "L.buildRules(vocab)"
        tokens = lambda line: run_js(tmp_path, f"L.tokenizeLine({rules}, {json.dumps(line)})")  # noqa: E731
        assert tokens("    signal 'go' with n")[:3] == [["keyword", "    signal"], ["text", " "], ["string", "'go'"]]
        assert ["constant.language", "signal"] in tokens("    when signal 'go' or fill")
        assert tokens("on signal 'go'")[0] == ["keyword.control", "on signal"]
        assert tokens("share done = 0")[0][0].startswith("keyword")
        assert ["variable.language", "shared"] in tokens("    log shared.done + LEN(orders)")
        assert ["variable.language", "orders"] in tokens("    log shared.done + LEN(orders)")
        for line, col, name in (("    signal 'go' with n", 6, "signal"), ("    share a = 1", 6, "share"),
                                ("    when signal 'go'", 11, "signal"), ("    log shared.a", 10, "shared"),
                                ("on signal 'go'", 4, "on signal")):
            assert run_js(tmp_path, f"L.helpAt(vocab, {json.dumps([line])}, 0, {col}).name") == name, line

    def test_help_for_the_word_under_the_cursor(self, tmp_path):
        line = ["    expect cancel rejected within 2s"]
        assert run_js(tmp_path, f"L.helpAt(vocab, {json.dumps(line)}, 0, 6).name") == "expect"
        assert run_js(tmp_path, f"L.helpAt(vocab, {json.dumps(line)}, 0, 20).name") == "cancel rejected"
        assert "OrderCancelReject" in run_js(tmp_path, f"L.helpAt(vocab, {json.dumps(line)}, 0, 20).doc")
        assert run_js(tmp_path, 'L.helpAt(vocab, ["    fill qty: TICK(1, 2)"], 0, 16).name') == "TICK"
        assert run_js(tmp_path, 'L.helpAt(vocab, ["    # nothing here"], 0, 8)') is None


@needs_node
class TestHover:
    LINES = ["    fill qty: 100, price: TICK(order.price, 0.01)  # a comment about fill",
             "    new symbol: 'IBM', side: buy, tif: day",
             "    expect cancel rejected within 2s",
             "    dk last trade reason: other",
             "    restate qty: 50, reason: gt_renewal",
             "    log 'fill # accept'",
             "# accept everything"]

    def hover(self, tmp_path, row, word, nth=0):
        col = [m.start() for m in re.finditer(re.escape(word), self.LINES[row])][nth] + 1
        return run_js(tmp_path, f"L.hoverAt(vocab, {json.dumps(self.LINES)}, {row}, {col})")

    def test_an_action_lists_its_terms(self, tmp_path):
        tip = self.hover(tmp_path, 0, "fill")
        assert tip["title"] == "fill" and (tip["start"], tip["end"]) == (4, 8)
        assert tip["lines"][1] == "Terms: qty, price, text, extra, leg, report_legs" and "template" in tip["lines"][2]
        assert "which trade: last trade, first trade, trade where" in self.hover(tmp_path, 3, "dk")["lines"][1]

    def test_statements_events_names_and_functions(self, tmp_path):
        assert self.hover(tmp_path, 2, "expect")["title"].startswith("expect EVENT")
        rejected = self.hover(tmp_path, 2, "rejected")
        assert rejected["title"] == "cancel rejected" and (rejected["start"], rejected["end"]) == (11, 26)
        assert self.hover(tmp_path, 0, "TICK")["title"] == "TICK"
        order = self.hover(tmp_path, 0, "order")
        assert order["title"] == "order" and self.LINES[0][order["start"]:order["end"]] == "order"

    def test_a_word_given_to_a_term_says_its_fix_code(self, tmp_path):
        assert self.hover(tmp_path, 1, "buy")["title"] == "buy = 1"
        assert self.hover(tmp_path, 1, "day")["title"] == "day = 0"
        assert self.hover(tmp_path, 3, "other")["title"] == "other = Z", "dk's reason is DKReason(127)"
        renew = self.hover(tmp_path, 4, "gt_renewal")
        assert renew["title"] == "gt_renewal = 1" and self.LINES[4][renew["start"]:renew["end"]] == "gt_renewal"
        assert run_js(tmp_path, "L.hoverAt(vocab, ['    restate reason: nonsense'], 0, 22)") is None
        assert all(code == run_js(tmp_path, f"L.hoverAt(vocab, ['    new side: {word}'], 0, 15).title.split(' = ')[1]")
                   for word, code in macro.vocabulary()["enums"]["side"].items())

    def test_nothing_in_comments_strings_or_blank_space(self, tmp_path):
        assert self.hover(tmp_path, 0, "fill", nth=1) is None, "a word in a trailing comment"
        assert self.hover(tmp_path, 6, "accept") is None
        assert self.hover(tmp_path, 5, "accept") is None and self.hover(tmp_path, 5, "fill") is None, "words in a string"
        assert run_js(tmp_path, f"L.hoverAt(vocab, {json.dumps(self.LINES)}, 1, 1)") is None
        assert run_js(tmp_path, f"L.hoverAt(vocab, {json.dumps(self.LINES)}, 1, 400)") is None


@needs_node
@needs_node
class TestRecordingNames:
    """What Stop suggests and what Export writes, both pure functions of the
    language module so a browser is not needed to hold them."""

    def test_the_suggestion_is_the_side_then_the_local_date_and_time(self, tmp_path):
        assert run_js(tmp_path, 'L.recordingName("market", new Date(2026, 8, 22, 14, 30, 5))') == "Market 2026-09-22 14:30:05"
        assert run_js(tmp_path, 'L.recordingName("client", new Date(2026, 0, 1, 0, 0, 0))') == "Client 2026-01-01 00:00:00"
        assert run_js(tmp_path, 'L.recordingName("client", new Date(2026, 11, 31, 23, 59, 59))') == "Client 2026-12-31 23:59:59"
        now = run_js(tmp_path, 'L.recordingName("market")')
        assert re.fullmatch(r"Market \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", now), now
        from mkfix.macro.store import NAME
        assert NAME.fullmatch(now), "what Stop suggests is a name save accepts"

    def test_export_writes_a_colon_as_a_dot(self, tmp_path):
        assert run_js(tmp_path, 'L.exportFileName("Market 2026-09-22 14:30:15")') == "Market 2026-09-22 14.30.15.macro"
        assert run_js(tmp_path, 'L.exportFileName("slow-fill")') == "slow-fill.macro"
        assert run_js(tmp_path, 'L.exportFileName("a:b:c d.e_f-g")') == "a.b.c d.e_f-g.macro"
        # every name save accepts becomes a file name Windows and macOS accept
        from mkfix.macro.store import NAME
        for name in ("Market 2026-09-22 14:30:15", "my macro-2.b", "x:y"):
            assert NAME.fullmatch(name)
            out = run_js(tmp_path, f"L.exportFileName({json.dumps(name)})")
            assert not re.search(r'[<>:"/\\|?*]', out) and out.endswith(".macro"), out

    def test_both_stops_suggest_it_and_export_writes_it(self):
        """Two paths end a recording: the editor's Stop and the blotter toolbar's ● Stop,
        whose dialog lives in app.toml. Both must offer the stamped name."""
        pane = (STATIC / "panes" / "macros.js").read_text(encoding="utf-8")
        assert "recordingName(side)" in pane and "download: exportFileName(current)" in pane
        assert "exportFileName, helpAt, hoverAt, recordingName" in pane
        assert '"recorded"' not in pane and "recorded-${n}" not in pane, "the counter gave way to the stamp"
        toolbar = (STATIC / "macro-status.js").read_text(encoding="utf-8")
        assert 'import { recordingName } from "/static/macro-lang.js";' in toolbar
        assert ('const stopRecording = (side) => app.dialog("stop_recording", '
                '{ row: { side, Side: Side(side), name: recordingName(side) } });') in toolbar, "computed at the click, not at mount"
        app = tomllib.loads(APP_TOML.read_text(encoding="utf-8"))
        name = next(f for f in app["dialogs"]["stop_recording"]["fields"] if f.get("name") == "name")
        assert name["value"] == "${row.name}" and name["required"] is True
        assert not [f for f in app["dialogs"]["stop_recording"]["fields"] if f.get("value") == "recorded"]


class TestMacroStatus:
    """What the status bar says and what enables the order blotters' macro
    controls: one state, worked out from the runs table and the recordings."""

    NOW = "Date.UTC(2026, 8, 20, 12, 0, 0)"

    @staticmethod
    def run(id, side, status, macro="slow-fill", verdict="", orders=0, passed=0, failed=0, started="20260920-11:00:00.000", ended=""):
        return {"id": id, "side": side, "status": status, "macro": macro, "verdict": verdict, "orders": orders,
                "passed": passed, "failed": failed, "started_at": started, "ended_at": ended, "session": ""}

    def state(self, tmp_path, runs, recordings=None):
        return run_js(tmp_path, f"S.macroState({json.dumps(runs)}, {json.dumps(recordings or {})}, {self.NOW})")

    def items(self, tmp_path, runs, recordings=None):
        got = run_js(tmp_path, f"S.statusItems(S.macroState({json.dumps(runs)}, {json.dumps(recordings or {})}, {self.NOW}))")
        return [(i["kind"], i["text"], i.get("frame", i.get("pane"))) for i in got]

    def test_idle_says_nothing_and_enables_nothing(self, tmp_path):
        state = self.state(tmp_path, [])
        assert set(state) == {"client", "market", "end-to-end"}
        assert all((s["recording"], s["playing"], s["paused"], s["live"], s["last"]) == (False, 0, 0, 0, None) for s in state.values())
        assert self.items(tmp_path, []) == []

    def test_each_side_counts_its_own_runs(self, tmp_path):
        runs = [self.run(1, "market", "armed", orders=3), self.run(2, "market", "paused", macro="desk"),
                self.run(3, "client", "armed", macro="burst", orders=20), self.run(4, "client", "armed", macro="chase", orders=1)]
        state = self.state(tmp_path, runs)
        assert [(state[s]["playing"], state[s]["paused"], state[s]["live"], state[s]["orders"]) for s in ("client", "market")] == [
            (2, 0, 2, 21), (1, 1, 2, 3)]
        assert self.items(tmp_path, runs) == [
            ("playing", "▶ client 2 runs · 21 orders", "client-runs"),
            ("playing", "▶ market slow-fill · 3 orders", "market-runs"),
            ("paused", "⏸ market desk paused", "market-runs")]

    def test_recording_is_said_first_and_links_to_the_editor(self, tmp_path):
        rec = {"market": {"recording": True, "actions": 1, "session": "LOOP-MKT"}, "client": {"recording": False, "actions": 0}}
        assert self.items(tmp_path, [self.run(1, "market", "armed")], rec) == [
            ("recording", "● REC market · 1 action on LOOP-MKT", "market-macros"),
            ("playing", "▶ market slow-fill · 0 orders", "market-runs")]
        state = self.state(tmp_path, [], rec)
        assert (state["market"]["recording"], state["market"]["actions"], state["client"]["recording"]) == (True, 1, False)
        # paused, it says so, and ⏸ has the light
        rec["market"]["paused"] = True
        assert self.items(tmp_path, [], rec) == [("recording", "⏸ REC market · paused · 1 action on LOOP-MKT", "market-macros")]
        state = self.state(tmp_path, [], rec)
        assert (state["market"]["recordingPaused"], state["client"]["recordingPaused"]) == (True, False)

    def test_an_ended_run_is_news_for_a_minute(self, tmp_path):
        fresh = self.run(1, "client", "finished", macro="chase", verdict="passed", orders=3, passed=3, ended="20260920-11:59:30.000")
        stale = self.run(2, "client", "finished", macro="old", verdict="passed", orders=1, passed=1, ended="20260920-11:58:00.000")
        stopped = self.run(3, "market", "stopped", ended="20260920-11:59:59.500")
        interrupted = self.run(4, "market", "interrupted", macro="desk", ended="20260920-11:50:00.000")
        assert self.items(tmp_path, [fresh, stale, stopped, interrupted]) == [
            ("passed", "■ chase passed · 3 of 3", "client-runs"), ("ended", "■ slow-fill stopped", "market-runs")]
        assert self.items(tmp_path, [stale]) == []

    def test_a_failure_stays_until_something_else_happens_on_its_side(self, tmp_path):
        failed = self.run(1, "client", "finished", macro="suite", verdict="failed", orders=3, passed=2, failed=1,
                          ended="20260920-11:55:00.000")
        assert self.items(tmp_path, [failed]) == [("failed", "■ suite failed · 1 of 3 failed", "client-runs")]
        later = self.run(2, "client", "armed", macro="chase", started="20260920-11:56:00.000")
        assert self.items(tmp_path, [failed, later]) == [("playing", "▶ client chase · 0 orders", "client-runs")]
        other_side = self.run(3, "market", "armed", started="20260920-11:56:00.000")
        assert ("failed", "■ suite failed · 1 of 3 failed", "client-runs") in self.items(tmp_path, [failed, other_side])
        # a stop or an interruption gives way the same way; a pass keeps its minute
        interrupted = self.run(5, "market", "interrupted", ended="20260920-11:59:40.000")
        again = self.run(6, "market", "armed", started="20260920-11:59:50.000")
        assert self.items(tmp_path, [interrupted]) == [("ended", "■ slow-fill interrupted", "market-runs")]
        assert self.items(tmp_path, [interrupted, again]) == [("playing", "▶ market slow-fill · 0 orders", "market-runs")]
        passed = self.run(7, "client", "finished", macro="chase", verdict="passed", orders=1, passed=1, ended="20260920-11:59:40.000")
        assert ("passed", "■ chase passed · 1 of 1", "client-runs") in self.items(
            tmp_path, [passed, self.run(8, "client", "armed", macro="burst", started="20260920-11:59:50.000")])
        old = {**failed, "ended_at": "20260920-11:49:00.000"}
        assert self.items(tmp_path, [old]) == [], "ten minutes at most"

    def test_fix_stamps_are_utc(self, tmp_path):
        assert run_js(tmp_path, "S.stampMs('20260920-12:00:00.250') - Date.UTC(2026, 8, 20, 12, 0, 0)") == 250
        assert run_js(tmp_path, "S.stampMs('20260920-12:00:00') === Date.UTC(2026, 8, 20, 12, 0, 0)") is True
        assert run_js(tmp_path, "[Number.isNaN(S.stampMs('')), Number.isNaN(S.stampMs(null))]") == [True, True]


@needs_node
class TestLineDiff:
    """History shows a saved version with its differences from the script as
    it is saved now, drawn on the version's own lines."""

    def ops(self, tmp_path, a, b):
        return "".join({"same": "=", "del": "-", "add": "+"}[o["op"]]
                       for o in run_js(tmp_path, f"D.lineDiff({json.dumps(a)}, {json.dumps(b)})"))

    def marks(self, tmp_path, a, b):
        return run_js(tmp_path, f"D.diffMarks({json.dumps(a)}, {json.dumps(b)})")

    def test_the_shortest_edit(self, tmp_path):
        assert self.ops(tmp_path, list("abc"), list("abc")) == "==="
        assert self.ops(tmp_path, list("abcd"), list("acd")) == "=-=="
        assert self.ops(tmp_path, list("acd"), list("abcd")) == "=+=="
        assert self.ops(tmp_path, list("abc"), list("xbz")) .count("=") == 1
        assert self.ops(tmp_path, [], list("ab")) == "++" and self.ops(tmp_path, list("ab"), []) == "--"
        assert self.ops(tmp_path, [], []) == ""

    def test_every_line_of_both_texts_is_accounted_for_in_order(self, tmp_path):
        a, b = list("the quick brown fox"), list("a quick brown dog barks")
        got = run_js(tmp_path, f"D.lineDiff({json.dumps(a)}, {json.dumps(b)})")
        assert [o["a"] for o in got if o["a"] is not None] == list(range(len(a)))
        assert [o["b"] for o in got if o["b"] is not None] == list(range(len(b)))
        assert all(a[o["a"]] == b[o["b"]] for o in got if o["op"] == "same")
        assert sum(o["op"] == "same" for o in got) >= len(" quick brown ")

    def test_marks_are_on_the_versions_own_rows(self, tmp_path):
        old = ["# t", "on order", "    after 250ms", "    accept"]
        new = ["# t", "on order where symbol == 'IBM'", "    accept", "    when cancel", "        accept"]
        assert self.marks(tmp_path, old, new) == {
            "only": [1, 2], "removed": 2, "added": 3,
            # the line that replaced rows 1-2 belongs after them; a gap past the last row is after the text
            "gaps": [{"row": 3, "count": 1}, {"row": 4, "count": 2}]}
        assert self.marks(tmp_path, old, old) == {"only": [], "gaps": [], "removed": 0, "added": 0}
        assert self.marks(tmp_path, [], ["x"]) == {"only": [], "gaps": [{"row": 0, "count": 1}], "removed": 0, "added": 1}

    def test_a_text_too_large_for_the_table_is_still_told_apart(self, tmp_path):
        got = run_js(tmp_path, "(() => { const a = ['head', ...Array.from({length: 2100}, (_, i) => 'a' + i), 'tail'];"
                               " const b = ['head', ...Array.from({length: 2100}, (_, i) => 'b' + i), 'tail'];"
                               " const m = D.diffMarks(a, b); return [m.removed, m.added, m.only[0], m.only.at(-1), m.gaps]; })()")
        assert got == [2100, 2100, 1, 2100, [{"row": 2101, "count": 2100}]]

    def test_lines_are_split_the_way_the_editor_shows_them(self, tmp_path):
        assert run_js(tmp_path, 'D.splitLines("a\\r\\nb\\n")') == ["a", "b"]
        assert run_js(tmp_path, 'D.splitLines("a\\n\\nb")') == ["a", "", "b"]


@needs_node
class TestMarkdown:
    def render(self, tmp_path, text):
        return run_js(tmp_path, f"M.renderMarkdown({json.dumps(text)})")

    def test_blocks(self, tmp_path):
        html = self.render(tmp_path, "# Title `x`\n\nOne\ntwo.\n\n## Next one\n\n---\n")
        assert '<h1 id="title-x">Title <code>x</code></h1>' in html and "<p>One two.</p>" in html
        assert '<h2 id="next-one">Next one</h2>' in html and "<hr>" in html

    def test_lists_nest_and_wrap(self, tmp_path):
        html = self.render(tmp_path, "- one\n  more\n  - inner\n- two\n\n1. first\n2. second\n")
        assert html == ("<ul><li>one more<ul><li>inner</li></ul></li><li>two</li></ul>\n"
                        "<ol><li>first</li><li>second</li></ol>")

    def test_tables_and_escaped_pipes(self, tmp_path):
        html = self.render(tmp_path, "| a | b |\n|---|---|\n| `x\\|y` | **2** |\n")
        assert "<th>a</th><th>b</th>" in html and "<td><code>x|y</code></td><td><strong>2</strong></td>" in html

    def test_code_is_escaped_and_may_be_coloured(self, tmp_path):
        assert '<pre class="md-code" data-lang="macro"><code>if a &lt; b</code></pre>' == self.render(
            tmp_path, "```macro\nif a < b\n```\n")
        painted = run_js(tmp_path, 'M.renderMarkdown("```macro\\naccept\\n```", { highlight: (c, l) => `[${l}:${c}]` })')
        assert "<code>[macro:accept]</code>" in painted

    def test_inline_and_hostile_input(self, tmp_path):
        html = self.render(tmp_path, "A [link](other.md#top), <script>x</script>, [bad](javascript:alert(1)) and *em*.")
        assert '<a href="other.md#top">link</a>' in html and "<em>em</em>" in html
        assert "<script>" not in html and "javascript:" not in html
        assert 'target="_blank" rel="noopener"' in self.render(tmp_path, "[out](https://example.com)")

    def test_headings_for_the_contents_list(self, tmp_path):
        got = run_js(tmp_path, 'M.headings("# A\\n```\\n# not one\\n```\\n## B `c`\\n")')
        assert got == [{"level": 1, "text": "A", "id": "a"}, {"level": 2, "text": "B c", "id": "b-c"}]


class TestHelpPages:
    PAGES = json.loads((HELP / "index.json").read_text(encoding="utf-8"))

    def test_every_page_is_there_and_reachable_from_the_help_menu(self):
        app = tomllib.loads(APP_TOML.read_text(encoding="utf-8"))
        for page in self.PAGES:
            assert page.get("builtin") == "examples" or (HELP / page["file"]).is_file(), page
        help_menu = app["menubar"][-1]["items"]
        ids = [p["id"] for p in self.PAGES]
        assert ids == ["user-guide", "macro-language", "macro-examples", "replaying-a-log"]
        # The menu has a pane for each page it names, and each pane names its page: a pane
        # without one opens on the first page, whichever that is this release.
        for label, pane, page in (("User Guide", "help-guide", "user-guide"),
                                  ("Macro Language", "help-viewer", "macro-language"),
                                  ("Replaying a Log", "help-replay", "replaying-a-log")):
            assert {"label": label, "action": "pane.show", "args": pane} in help_menu
            assert app["panes"][pane] == {"title": "Help", "type": "help-viewer", "page": page}
            assert page in ids
        assert help_menu[0]["label"] == "User Guide", "the guide is where a newcomer starts"

    def test_a_viewer_opens_on_its_own_page_whatever_f1_last_named(self):
        """F1 in the editor leaves `help_target` set for the session; read
        first, it turned every Help pane opened afterwards to the macro
        reference — Replaying a Log and the User Guide included."""
        viewer = (STATIC / "panes" / "help-viewer.js").read_text(encoding="utf-8")
        assert "const home = spec.page ?? pages[0].id;" in viewer
        assert "await show(home, start?.page === home ? start.anchor : undefined);" in viewer
        assert "if (target.page === home || target.page === currentId) show(target.page, target.anchor);" in viewer
        assert "start?.page ?? spec.page" not in viewer
        editor = (STATIC / "panes" / "macros.js").read_text(encoding="utf-8")
        assert 'app.fireAction("pane.show", "help-viewer")' in editor, "the editor's ? opens the macro reference"
        assert 'app.state.set("help_target", { page: "macro-language"' in editor

    def test_a_heading_asked_for_stays_in_view_while_a_new_pane_settles(self):
        """A pane opened for a heading (Keyboard Shortcuts' User Guide
        button, F1) was scrolled before its window had its size, and the
        reflow left it at the foot of the page. The heading stays pinned
        through resizes until the reader scrolls, clicks or types."""
        viewer = (STATIC / "panes" / "help-viewer.js").read_text(encoding="utf-8")
        assert 'new ResizeObserver(() => pinned?.isConnected && pinned.scrollIntoView({ block: "start" })).observe(page);' in viewer
        assert "pinned = target || null;" in viewer, "each show() pins its heading, or releases the last one"
        unpin = re.search(r'for \(const type of (\[[^\]]+\])\) host\.addEventListener\(type, unpin, \{ passive: true, capture: true \}\);', viewer)
        assert unpin and set(json.loads(unpin.group(1))) == {"wheel", "pointerdown", "keydown", "touchstart"}
        assert "const unpin = () => { pinned = null; };" in viewer
        guide = (HELP / "user-guide.md").read_text(encoding="utf-8")
        from_box = tomllib.loads(APP_TOML.read_text(encoding="utf-8"))["dialogs"]["shortcuts"]["buttons"][0]["set"]["help_target"]
        assert from_box["page"] == "user-guide" and f"\n## Sloppy focus\n" in guide and from_box["anchor"] == "sloppy-focus"

    def test_the_user_guide_names_what_the_application_has(self):
        """The guide is prose about the UI, so what it names in bold as a
        menu, a pane or a button has to be there under that name."""
        app = tomllib.loads(APP_TOML.read_text(encoding="utf-8"))
        text = (HELP / "user-guide.md").read_text(encoding="utf-8")
        menus = {m["label"]: m["items"] for m in app["menubar"]}
        # Menu › Item paths
        for menu, item in re.findall(r"\*\*(\w+) › ([^*]+)\*\*", text):
            assert menu in menus, menu
            assert item in {i.get("label") for i in menus[menu]}, f"{menu} › {item}"
        # every pane menu's blotters are named, and every menu is in the table of menus
        for menu in menus:
            assert f"| **{menu}** |" in text, f"the menus table leaves out {menu}"
        titles = {p["title"] for p in app["panes"].values()}
        for title in ("Sessions", "Messages", "Detail", "Sent Orders", "Received Orders", "Sent Trades",
                      "Received Trades", "Client Macros", "Market Macros", "Templates", "Dictionaries"):
            assert title in titles and title in text, title
        # the button tables: every button a table names is on that blotter, and none of the blotter's is left out
        sections = {"Sent Orders": "order-blotter", "Received Orders": "market-order-blotter",
                    "Sent Trades": "market-trade-blotter"}
        for heading, pane in sections.items():
            section = text.split(f"### {heading}\n", 1)[1].split("\n## ", 1)[0].split("\n### ", 1)[0]
            named = re.findall(r"^\| \*\*([^*]+)\*\* \|", section, re.M)
            # a template label names the words it can read: Hide, Unhide
            buttons = [word for b in app["panes"][pane]["buttons"]
                       for word in (re.findall(r"'([^']+)'", b["label"]) if "${" in b["label"] else [b["label"]])]
            assert set(named) <= set(buttons), (heading, set(named) - set(buttons))
            assert set(buttons) - set(named) <= {"History"}, (heading, set(buttons) - set(named))
            if "Macro…" in buttons:
                assert "Macro…" in named, heading
        # session statuses are the engine's
        engine = (ROOT / "mkfix" / "fix" / "session.py").read_text(encoding="utf-8") \
            + (ROOT / "mkfix" / "fix" / "engine.py").read_text(encoding="utf-8")
        statuses = re.findall(r"^\| `([A-Z_]+)` \|", text.split("### Running one\n", 1)[1].split("\n## ", 1)[0], re.M)
        assert statuses == ["DOWN", "LISTENING", "LOGON_SENT", "ACTIVE", "LOGOUT_SENT", "ERROR"]
        for status in statuses:
            assert f'"{status}"' in engine, status
        # the keys are the Keyboard Shortcuts box's
        for fact in app["dialogs"]["shortcuts"]["facts"]:
            if fact["label"] == "Sloppy focus":  # says whether it is on; the guide has a section
                assert "\n## Sloppy focus\n" in text
                continue
            assert f"| {fact['label']} |" in text, fact["label"]
        # what the status bar and the dialogs say
        assert app["mkio"]["incompatible"]["status.message"] == "Server version mismatch" and "*Server version mismatch*" in text
        assert app["mkio"]["disconnected"]["status.message"] == "Disconnected" and "*Disconnected*" in text
        new_order = app["panes"]["order-blotter"]["buttons"][0]["action"]["dialog"]
        assert new_order["submit"]["label"] == "Send Order" and "**Send Order**" in text
        # the subcommands are the ones the command line takes
        main = (ROOT / "mkfix" / "__main__.py").read_text(encoding="utf-8")
        for sub in ("check", "run", "archive", "restore"):
            assert f"`mkfix {sub}" in text and f'"{sub}"' in main, sub

    def test_every_example_in_the_pages_is_a_script_that_checks(self):
        blocks = 0
        for page in self.PAGES:
            if "file" not in page:
                continue
            text = (HELP / page["file"]).read_text(encoding="utf-8")
            for block in re.findall(r"```macro\n(.*?)```", text, re.S):
                _, diags = macro.check(block)
                assert diags == [], (page["file"], block.splitlines()[0], [str(d) for d in diags])
                blocks += 1
        assert blocks >= 3

    def test_the_reference_tables_are_the_vocabulary(self):
        text = (HELP / "macro-language.md").read_text(encoding="utf-8")
        v = macro.vocabulary()

        def first_column(heading):
            section = text.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
            return re.findall(r"^\| `([^`]+)`", section, re.M)

        assert set(first_column("Actions")) == set(v["verbs"]), "every action, and nothing that is not one"
        assert set(first_column("Events")) == set(v["events"])
        names = set(first_column("What an expression can see"))
        assert set(v["context"]) <= names
        for verb, terms in re.findall(r"^\| `([^`]+)` \|[^|]*\| (.*?) \|$", text.split("## Actions\n")[1].split("\n## ")[0], re.M):
            assert re.findall(r"`([a-z_]+)`", terms) == v["verbs"][verb]["terms"], verb
        for keyword in v["statements"]:
            assert f"`{keyword}" in text, f"the reference never shows `{keyword}`"
        assert "TICK(" in text and "RANDOM()" in text

    def test_pages_keep_to_what_the_renderer_renders(self):
        for page in self.PAGES:
            if "file" not in page:
                continue
            text = (HELP / page["file"]).read_text(encoding="utf-8")
            prose = re.sub(r"```.*?```", "", text, flags=re.S)
            assert not re.search(r"^\s{0,3}>", prose, re.M), "no blockquotes"
            assert not re.search(r"!\[", prose), "no images"
            assert not re.search(r"^#{5,}", prose, re.M), "headings stop at ####"
            assert not re.search(r"<[a-zA-Z/][^>]*>", re.sub(r"`[^`]*`", "", prose)), "no raw HTML"
            for target in re.findall(r"\]\(#([^)]+)\)", prose):
                slugs = {re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", h.replace("`", "").lower()).strip())
                         for h in re.findall(r"^#{1,4}\s+(.*)$", text, re.M)}
                assert target in slugs, f"{page['file']}: no heading for #{target}"


class TestWiring:
    def test_the_editor_is_vendored_whole(self):
        ace = STATIC / "vendor" / "ace"
        for name in ("ace.js", "ext-language_tools.js", "ext-searchbox.js", "keybinding-vim.js", "LICENSE"):
            assert (ace / name).is_file() and (ace / name).stat().st_size > 1000, name
        assert "BSD" in (ace / "LICENSE").read_text(encoding="utf-8") or "Redistribution" in (ace / "LICENSE").read_text(encoding="utf-8")
        pane = (STATIC / "panes" / "macros.js").read_text(encoding="utf-8")
        for name in ("ace.js", "ext-language_tools.js", "ext-searchbox.js"):
            assert name in pane, f"the pane never loads {name}"
        assert 'useWorker: false' in pane, "our mode has no worker file to load"
        assert "ace-builds 1.44.0" in pane and "1.44.0" in (ROOT / "README.md").read_text(encoding="utf-8")

    def test_the_panes_are_loaded_declared_and_on_a_menu(self):
        app = tomllib.loads(APP_TOML.read_text(encoding="utf-8"))
        index = (STATIC / "index.html").read_text(encoding="utf-8")
        for module in ("macros.js", "help-viewer.js"):
            assert f'/static/panes/{module}' in index
        shown = {i.get("args") for m in app["menubar"] for i in m["items"] if i.get("action") == "pane.show"}
        assert {f"{side}-macros" for side in SIDES} | {"help-viewer"} <= shown
        windows = {i.get("args") for m in app["menubar"] for i in m["items"] if i.get("action") == "frame.show"}
        assert windows == {f"{side}-runs" for side in SIDES}
        for state in ("selected_macro", "selected_macro_order", "help_target", "open_example"):
            assert state in app["state"], state

    @pytest.mark.parametrize("side", SIDES)
    def test_each_side_has_its_own_panes_over_its_own_rows(self, side):
        """The two sides share tables and services; a pane's `filter` is all
        that keeps a client run out of Market Runs."""
        import tomllib
        app = tomllib.loads(APP_TOML.read_text(encoding="utf-8"))
        toml = tomllib.loads((ROOT / "mkfix" / "mkfix.toml").read_text(encoding="utf-8", errors="replace"))
        word = side.capitalize()
        editor = app["panes"][f"{side}-macros"]
        assert editor == {"title": f"{word} Macros", "type": "macros", "side": side}
        for pane, service, title in (("macro-runs", "macro_runs_tree", "Macro Runs"),
                                     ("macro-log", "macro_log_query", "Macro Log")):
            spec = app["panes"][f"{side}-{pane}"]
            assert (spec["title"], spec["service"], spec["filter"]) == (f"{word} {title}", service, f"side == '{side}'")
            assert "side" in toml["services"][service]["filterable"]
        for service in ("macros_query", "macro_runs_query", "macro_orders_query"):
            assert "side" in toml["services"][service]["filterable"], service
        assert app["panes"][f"{side}-macro-runs"]["select"] == {"state": "selected_macro_order"}
        # The window: the runs tree over the log, the log filtered by the tree's
        # selection through a table link. A selected run broadcasts its subtree,
        # so `<side>_macro_order` carries its orders' rows (and its own blank,
        # which is what a run-level log line has) and `<side>_macro_run` keeps it
        # to that run. Link names are one namespace across the app, so each side
        # has its own: shared names let the Client tree filter the Market log.
        tree = app["panes"][f"{side}-macro-runs"]
        # Flattened, the tree drops its run rows: a run only groups its orders
        assert tree["tree"] == {"child": "parent_key", "parent": "key", "expand": 1, "flat": "leaves"}, "expand lives inside `tree`: at the pane level mkui ignores it"
        # It opens on what says whether a run is worth opening — what ran, how it
        # ended, what it took, when — and on today's runs; the picker has the rest
        assert tree["visible"] == ["run_id", "macro", "status", "verdict", "subject", "cl_ord_id", "symbol", "message",
                                   "orders", "live", "passed", "failed", "started_at"]
        assert tree["filters"] == {"started_at": {"preset": "today"}} and tree["types"]["started_at"]["type"] == "time"
        names = {f"{side}_macro_run": "run_id", f"{side}_macro_order": "order_row"}
        assert tree["link"] == {"broadcast": names}
        assert app["panes"][f"{side}-macro-log"]["link"] == {"listen": names}
        frame = next(f for f in app["frames"] if f["id"] == f"{side}-runs")
        assert frame["open"] is False and frame["title"] == f"{word} Macro Runs"
        assert frame["layout"]["children"] == [{"type": "tabs", "active": 0, "children": [f"{side}-macro-runs"]},
                                               {"type": "tabs", "active": 0, "children": [f"{side}-macro-log"]}]
        # A closed frame and `frame.show` are mkui 1.16.0: an older mkui opens the window at startup and the menu item does nothing
        from mkui.__init__ import __version__ as mkui_version
        assert tuple(map(int, mkui_version.split(".")[:2])) >= (1, 16)
        floor = re.search(r'"mkui>=(\d+)\.(\d+)\.\d+,<2"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert floor and tuple(map(int, floor.groups())) >= (1, 16), "the pyproject pin must reach closed frames"
        for table in ("fix_macros", "fix_macro_runs", "fix_macro_orders", "fix_macro_log"):
            assert "side" in toml["tables"][table]["columns"], table

    def test_the_two_sides_panes_differ_only_by_side(self):
        app = tomllib.loads(APP_TOML.read_text(encoding="utf-8"))
        for pane in ("macro-runs", "macro-log"):
            client = json.dumps(app["panes"][f"client-{pane}"]).replace("client", "market").replace("Client", "Market")
            assert client == json.dumps(app["panes"][f"market-{pane}"]), pane
        frames = {f["id"]: f for f in app["frames"]}
        client = json.dumps(frames["client-runs"]).replace("client", "market").replace("Client", "Market")
        assert client == json.dumps(frames["market-runs"])

    @pytest.mark.parametrize("side", SIDES)
    def test_the_runs_tree_opens_on_today_and_on_what_matters(self, side):
        """The default view is a choice among the columns, never a loss: what
        is hidden stays in the picker, the buttons still read what they gate
        on, and the Today filter sits on a stamp every row carries — a run's
        and an order's alike — in the form its column type parses."""
        import tomllib
        from datetime import datetime
        from mkfix.fix.message import _fix_timestamp
        app = tomllib.loads(APP_TOML.read_text(encoding="utf-8"))
        toml = tomllib.loads((ROOT / "mkfix" / "mkfix.toml").read_text(encoding="utf-8", errors="replace"))
        tree = app["panes"][f"{side}-macro-runs"]
        shown, listed = tree["visible"], tree["columns"]
        assert shown[0] == "run_id", "the tree's toggle rides the first column"
        assert len(shown) == len(set(shown)) and not [c for c in shown if c not in listed]
        hidden = [c for c in listed if c not in shown]
        assert {"session", "market_session", "line", "waiting_for", "actions", "priority", "speed", "seed", "version",
                "ended_at", "updated_at"} <= set(hidden), "the detail is a click away, not in the way"
        grouped = [c for g in tree["groups"] for c in g["columns"]]
        assert sorted(grouped) == sorted(listed), "a hidden column is still in the picker"
        internal = next(g["columns"] for g in tree["groups"] if g["label"] == "Internal")
        assert not [c for c in shown if c in internal]
        # What a hidden column still does: buttons gate on it, styles colour by it, the sort and links read it
        read = " ".join(json.dumps(b.get("enable", "")) for b in tree["buttons"]) + json.dumps(tree["rowStyle"])
        for column in ("kind", "priority", "status"):
            assert f"r.{column}" in read or f"{column} ==" in read, column
            assert column in listed, column
        assert tree["sort"] == ["-id"] and "id" in listed
        assert set(tree["link"]["broadcast"].values()) <= set(listed)
        # Today: one filter, on a column both halves of the union fill
        assert list(tree["filters"]) == ["started_at"]
        runs, orders = toml["services"]["macro_runs_tree"]["sql"].split("UNION ALL")
        assert "r.started_at" in runs and "o.started_at" in orders, "a row without the stamp would never be today's"
        assert "'' AS updated_at" in runs, "updated_at is blank on a run row: filtering on it would hide every run"
        kind = tree["types"]["started_at"]
        assert kind["type"] == "time" and kind["zone"] == "local"
        datetime.strptime(_fix_timestamp(), kind["parse"])
        # Flat: the leaves alone is mkui 1.32.0 — an older one warns of nothing and shows every row
        assert tree["tree"]["flat"] == "leaves"
        from mkui.__init__ import __version__ as mkui_version
        assert tuple(map(int, mkui_version.split(".")[:2])) >= (1, 32)
        floor = re.search(r'"mkui>=(\d+)\.(\d+)\.\d+,<2"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert floor and tuple(map(int, floor.groups())) >= (1, 32), "the pyproject pin must reach `tree.flat`"
        table = (Path(__import__("mkui").__file__).parent / "static" / "src" / "widgets" / "mkio-table.js").read_text(encoding="utf-8")
        assert 'flat !== "all" && flat !== "leaves"' in table, "mkui no longer reads `tree.flat` this way"

    @pytest.mark.parametrize("side", SIDES)
    def test_a_run_row_is_named_for_whatever_it_is(self, side):
        """A macro's row is an order, an IOI, an advert or an allocation: the
        tree says which (Subject, among the row's own columns) and both panes
        call its identifier ID — under ClOrdID an IOIID read as an order's."""
        app = tomllib.loads(APP_TOML.read_text(encoding="utf-8"))
        tree, log = app["panes"][f"{side}-macro-runs"], app["panes"][f"{side}-macro-log"]
        assert tree["labels"]["subject"] == "Subject"
        assert tree["labels"]["cl_ord_id"] == log["labels"]["cl_ord_id"] == "ID"
        grouped = {g["label"]: g["columns"] for g in tree["groups"]}
        assert grouped["Order"][:2] == ["subject", "cl_ord_id"]
        everywhere = [c for columns in grouped.values() for c in columns]
        assert len(everywhere) == len(set(everywhere)), "a column sits in one group"
        assert not [c for c in tree["visible"] if c not in everywhere], "every column shown belongs to a group"

    def test_history_is_wired_to_the_versions_the_server_keeps(self):
        import tomllib
        toml = tomllib.loads((ROOT / "mkfix" / "mkfix.toml").read_text(encoding="utf-8", errors="replace"))
        assert toml["tables"]["fix_macros"]["versioned"] is True
        service = toml["services"]["macro_versions"]
        assert service["protocol"] == "reqrep" and "fix_macros__history WHERE id = :id" in service["sql"]
        assert "ORDER BY _mkio_version DESC" in service["sql"]
        pane = (STATIC / "panes" / "macros.js").read_text(encoding="utf-8")
        assert 'client.request("macro_versions", { id })' in pane
        assert 'from "/static/line-diff.js"' in pane and (STATIC / "line-diff.js").is_file()
        for column in re.findall(r"\bv\.(\w+)|\brow\.(updated_at|source)", pane):
            name = column[0] or column[1]
            assert name in service["sql"], f"the pane reads {name}, which macro_versions does not select"
        for act in ("history", "restore", "back"):
            assert f'data-act="{act}"' in pane and f'act === "{act}"' in pane, act
        # A version is looked at, never edited or run; Restore is an unsaved edit, so nothing is lost to it.
        assert "if (current === null || viewing) return;" in pane
        assert 'button("save").disabled = !dirty() || !!viewing;' in pane
        css = (STATIC / "mkfix.css").read_text(encoding="utf-8")
        for rule in (".macro-history", ".macro-version", ".macro-diff-only", ".macro-diff-gap"):
            assert rule in css, rule

    def test_hover_is_wired_and_its_tooltip_is_themed(self):
        pane = (STATIC / "panes" / "macros.js").read_text(encoding="utf-8")
        ace = (STATIC / "vendor" / "ace" / "ace.js").read_text(encoding="utf-8")
        assert "HoverTooltip" in ace and 'ace.require("ace/tooltip")' in pane, "the vendored Ace must carry the tooltip the pane asks for"
        assert "hoverAt(vocab," in pane and "hover.addToEditor(editor)" in pane
        assert "session.getAnnotations()" in pane, "a problem's message is part of the hover"
        css = (STATIC / "mkfix.css").read_text(encoding="utf-8")
        assert ".ace_tooltip.ace-mkfix" in css, "Ace hangs the hover tooltip on <body> with the theme class on the tooltip"

    def test_the_order_blotters_carry_the_macro_controls_and_the_status_bar_the_status(self):
        from tests.test_ui_config import _fix_cmd_commands
        import tomllib
        app = tomllib.loads(APP_TOML.read_text(encoding="utf-8"))
        toml = tomllib.loads((ROOT / "mkfix" / "mkfix.toml").read_text(encoding="utf-8", errors="replace"))
        module = (STATIC / "macro-status.js").read_text(encoding="utf-8")
        assert 'import "/static/macro-status.js";' in (STATIC / "index.html").read_text(encoding="utf-8")
        assert app["statusbar"]["right"][0] == {"type": "macro-status"} and 'registerWidget("macro-status"' in module
        # One deck per side, in every blotter of that side: the pane ids the
        # module mounts into, read from its map, are the Client and Market
        # menus' blotters (TestMenubar.PANES pins those).
        mount = re.search(r"const BLOTTERS = \{([^}]*)\};", module).group(1)
        blotters = dict(re.findall(r'"([\w-]+)": "(client|market)"', mount))
        # the order, IOI, advert and allocation blotters of each side; the
        # trade blotters, which only answer, and the legs panes, which follow
        # an order, carry none
        menus = {m["label"]: [i["args"] for i in m["items"] if i.get("action") == "pane.show"
                              and app["panes"][i["args"]]["type"] == "mkio-table" and "trade" not in i["args"]
                              and "legs" not in i["args"]]
                 for m in app["menubar"] if m["label"] in ("Client", "Market")}
        assert blotters == {**dict.fromkeys(menus["Client"], "client"), **dict.fromkeys(menus["Market"], "market")}
        assert len(blotters) == 16 and set(blotters) <= set(app["panes"]), "the panes the controls are put into"
        ops = {"play_macro": "play_macro", "pause_runs": "pause_runs", "stop_runs": "stop_runs",
               "record_macro": "record_start", "stop_recording": "record_stop"}
        for dialog, op in ops.items():
            spec = app["dialogs"][dialog]
            # the context is read at the click: it carries the side's recording, which the lists are fed
            opened = ('app.dialog("stop_recording", { row: { side, Side: Side(side), name: recordingName(side) } })'
                      if dialog == "stop_recording" else f'app.dialog("{dialog}", context())')
            assert opened in module, dialog
            assert spec["submit"]["service"] == "fix_cmd" and spec["submit"]["op"] == op and op in _fix_cmd_commands()
            assert spec["fields"][0] == {"name": "side", "type": "hidden", "value": "${row.side}"}
            assert "modal" not in spec
        rec = {"recording": "${row.recording}"}
        for dialog, service, params in (("play_macro", "macro_play_options", {"side": "${row.side}", **rec}),
                                        ("pause_runs", "macro_run_options", {"side": "${row.side}", "paused": 0, **rec}),
                                        ("stop_runs", "macro_run_options", {"side": "${row.side}", "paused": 1, **rec})):
            select = app["dialogs"][dialog]["fields"][1]
            assert select["optionsFrom"]["service"] == service and select["optionsFrom"]["params"] == params
            assert "empty" not in select["optionsFrom"], "Play… must be given a macro, and a checklist has no blank row"
            assert set(re.findall(r":(\w+)", toml["services"][service]["sql"].replace("'resume:", "").replace("'macro:", ""))) == set(params)
        # ⏸ and ■ are a recording's too: at once when no run is beside it, else as the lists' `recording` row,
        # which Stop… hands on to the dialog that names the macro
        assert 'recording: !now().recording ? "" : now().recordingPaused ? "paused" : "on"' in module
        assert 'cmd(now().recordingPaused ? "record_resume" : "record_pause", { side }).then(poll, poll)' in module
        assert "now().recording && !now().playing" in module and "now().recording && !now().live" in module
        assert "buttons.pause.disabled = !s.playing && !s.recording;" in module
        assert "buttons.stop.disabled = !s.live && !s.recording;" in module
        assert app["dialogs"]["stop_runs"]["submit"]["then"] == {
            "action": "macro.stopped", "args": {"side": "${row.side}", "runs": "${runs}"}}
        assert 'app.registerAction("macro.stopped"' in module and '.split(",").includes("recording")) stopRecording(args.side)' in module
        assert app["dialogs"]["pause_runs"]["submit"]["then"] == {"action": "macro.refresh"}
        assert {"record_pause", "record_resume"} <= _fix_cmd_commands()
        from mkfix.macro.store import RECORDING
        assert RECORDING == "recording" and "'resume:recording'" in toml["services"]["macro_play_options"]["sql"]
        # Pause and Stop show the runs together, as a scrolling list rather than behind a dropdown — a
        # `size` on a select, which mkui reads from 1.11.0: an older one shows a dropdown and says nothing
        from mkui.__init__ import __version__ as mkui_version
        assert tuple(map(int, mkui_version.split(".")[:2])) >= (1, 11)
        floor = re.search(r'"mkui>=(\d+)\.(\d+)\.\d+,<2"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert floor and tuple(map(int, floor.groups())) >= (1, 11), "the pyproject pin must reach the checklist field"
        import mkui
        assert 'field.type === "checklist"' in (Path(mkui.static_dir) / "src" / "widgets" / "mkui-dialog.js").read_text(encoding="utf-8")
        for dialog, every in (("pause_runs", "${IF(row.recording == 'on', 'Everything', 'Every playing run')}"),
                              ("stop_runs", "${IF(row.recording != '', 'Everything', 'Every live run')}")):
            runs = app["dialogs"][dialog]["fields"][1]
            assert (runs["name"], runs["type"], runs["size"], runs["all"]) == ("runs", "checklist", 8, every)
            assert (runs["value"], runs["required"]) == ("*", True), "opens with every run ticked; nothing ticked is nothing to do"
            assert "runs" in (ROOT / "mkfix" / "services" / "fix_command.py").read_text(encoding="utf-8")
        # Play… is a checklist too, opening with nothing ticked: several macros, and paused runs, at once
        play = app["dialogs"]["play_macro"]["fields"]
        what = play[1]
        assert (what["name"], what["type"], what["value"], what["required"], what["all"]) == ("what", "checklist", "", True, "All of them")
        assert not [f for f in play if str(f.get("name", "")).startswith("_")], "no scratch fields: a checklist fills nothing"
        session = next(f for f in play if f.get("name") == "session")
        starting = "CONTAINS(what ?? '', 'macro:') or CONTAINS(what ?? '', 'needs:')"
        assert session["required"] == "CONTAINS(what ?? '', 'needs:')" and session["showWhen"] == starting
        from mkio import expr
        for ticked, shown, required in (("", False, False), ("resume:3", False, False), ("macro:slow", True, False),
                                        ("resume:3,needs:send", True, True), ("macro:needs session", True, False)):
            scope = {"what": ticked}
            assert expr.truthy(expr.evaluate(starting, scope)) is shown, ticked
            assert expr.truthy(expr.evaluate(session["required"], scope)) is required, ticked
        sql = toml["services"]["macro_play_options"]["sql"]
        assert "'needs:'" in sql and "'macro:'" in sql and "'resume:'" in sql
        stop = app["dialogs"]["stop_recording"]
        assert {"name": "save", "type": "hidden", "value": "1"} in stop["fields"]
        # how the macro is written is chosen at the end: the recording holds the timing either way
        assert {"name": "delays", "label": "Keep my delays", "type": "checkbox", "value": False} in stop["fields"]
        assert 'delays=yes("delays")' in (ROOT / "mkfix" / "services" / "fix_command.py").read_text(encoding="utf-8")
        assert 'label: "Keep my delays"' in (STATIC / "panes" / "macros.js").read_text(encoding="utf-8")
        assert stop["submit"]["then"] == {"action": "macro.recorded", "args": {"side": "${row.side}", "name": "${name}"}}
        assert app["dialogs"]["record_macro"]["submit"]["then"] == {"action": "macro.refresh"}
        # in place as soon as the blotter is: a pane's `_ready`, not the next poll, brings them
        assert "Promise.resolve(el._ready).then(() => renderControls(state)" in module and "new MutationObserver(" in module
        # the rule between the table's buttons and ours has the same space either side: mkui's toolbar
        # gap stands to its left, so margin + gap must equal the padding
        import mkui
        mkui_css = (Path(mkui.static_dir) / "styles" / "mkui.css").read_text(encoding="utf-8")
        gap = int(re.search(r"\.mkui-table-toolbar \{[^}]*?\bgap: (\d+)px", mkui_css).group(1))
        rule = re.search(r"\.macro-controls \{[^}]*\}", (STATIC / "mkfix.css").read_text(encoding="utf-8")).group(0)
        margin, padding = (int(re.search(rf"{prop}: (\d+)px", rule).group(1)) for prop in ("margin-left", "padding-left"))
        assert gap + margin == padding, (gap, margin, padding)
        # lit like a tape deck: ▶ while the side plays, ⏸ while it has a paused run, ● while it records
        css = (STATIC / "mkfix.css").read_text(encoding="utf-8")
        for button, cls, when in (("play", "macro-playing", "s.playing > 0"), ("pause", "macro-paused", "s.paused > 0 || s.recordingPaused"),
                                  ("record", "macro-recording", "s.recording"),
                                  ("record", "macro-recording-paused", "s.recordingPaused")):
            assert f'buttons.{button}.classList.toggle("{cls}", {when});' in module, button
            assert f".mkui-btn.macro-control.{cls}" in css, cls
        # dark while the server is away: the last picture goes with the connection, since a restart
        # stops every macro and the reconnect's snapshot says what is true
        assert 'app.state.subscribe("mkio.connected"' in module and "if (!online) { runs.clear(); recordings = {}; }" in module
        assert 'b.title = "The server is away"' in module
        # symbols only on the blotters: the words are the tooltip, and what a screen reader says
        assert [m for m in re.findall(r'button\("([^"]*)"', module)] == ["●", "▶", "⏸", "■"], "record, play, pause, stop: the editors' order"
        assert 'b.setAttribute("aria-label", title);' in module and "buttons.record.textContent" not in module
        for action in ("macro.refresh", "macro.recorded"):
            assert f'app.registerAction("{action}"' in module, action
        assert {"macros", "open_macro"} <= set(app["state"])
        pane = (STATIC / "panes" / "macros.js").read_text(encoding="utf-8")
        assert 'app.state.subscribe("open_macro"' in pane and "app.state.subscribe(`macros.${side}`" in pane
        css = (STATIC / "mkfix.css").read_text(encoding="utf-8")
        for rule in (".macro-statusbar", ".macro-controls", ".macro-status-recording", ".macro-status-failed"):
            assert rule in css, rule

    def test_every_template_in_the_macro_dialogs_is_in_the_expression_language(self):
        """A `?:` slipped into a label once: mkui warns on the console and shows nothing."""
        from mkio import expr
        app = tomllib.loads(APP_TOML.read_text(encoding="utf-8"))
        seen = 0
        for name in ("play_macro", "pause_runs", "stop_runs", "record_macro", "stop_recording", "arm_macro", "run_macro"):
            def walk(node):
                if isinstance(node, dict):
                    for value in node.values():
                        yield from walk(value)
                elif isinstance(node, list):
                    for value in node:
                        yield from walk(value)
                elif isinstance(node, str) and "${" in node:
                    yield node
            for text in walk(app["dialogs"][name]):
                expr.compile_template(text, expr.Env(strict=False))
                seen += 1
        assert seen >= 12

    def test_the_editors_names_are_declared_once(self):
        """The rename to macros once left a local `macros` shadowing the map
        of macros inside the list renderer: a ReferenceError only a browser
        would meet. The pane's long-lived names must each be declared once."""
        pane = (STATIC / "panes" / "macros.js").read_text(encoding="utf-8")
        for name in ("macros", "runs", "instances", "sessions", "templates", "versions", "recording", "current", "saved"):
            assert len(re.findall(rf"\b(?:const|let|var)\s+{name}\b", pane)) == 1, name

    def test_the_editor_keeps_to_its_side(self):
        pane = (STATIC / "panes" / "macros.js").read_text(encoding="utf-8")
        for call in ('cmd("check_macro", { source: editor.getValue(), side })', 'cmd("list_examples", { side })',
                     'cmd("stop_macro", { name: current })', "const mine = `side == '${side}'`"):
            assert call in pane, call
        saves = re.findall(r'cmd\("save_macro", \{([^}]*)\}', pane)
        assert len(saves) == 4 and all(re.search(r"\bside\b", s) for s in saves), "every save says which side it is for"
        for call in ('cmd("record_start", { side, session })', 'cmd("record_stop", { side, name, delays: delays ? "1" : "" })',
                     'cmd("record_status", { side })'):
            assert call in pane, call
        assert "wanted.side !== side" in pane, "an example opened for the other editor is not this one's"
        viewer = (STATIC / "panes" / "help-viewer.js").read_text(encoding="utf-8")
        assert 'app.fireAction("pane.show", `${side}-macros`)' in viewer

    def test_the_editor_toolbar_is_ordered_like_the_blotters(self):
        """[New] [Clone] [Save] [Delete] [History] | [●] [▶] [⏸] [■] on the
        left, [Example…] [Import] [Export] on the right: the tape deck in the
        blotters' order, symbols only, the words in the tooltips. Clone is a
        Save As; Delete takes the list's selection, whole or not at all."""
        from tests.test_ui_config import _fix_cmd_commands
        pane = (STATIC / "panes" / "macros.js").read_text(encoding="utf-8")
        toolbar = re.search(r'<div class="mkfix-toolbar macro-toolbar">(.*?)</div>\n\s*<div class="macro-body">', pane, re.S).group(1)
        acts = re.findall(r'data-act="(\w+)"', toolbar)
        assert acts == ["new", "clone", "save", "delete", "history", "record", "play", "pause", "stop",
                        "example", "import", "export", "vim", "help"], acts
        assert toolbar.index("macro-gap") > toolbar.index('data-act="stop"') and toolbar.index("macro-gap") < toolbar.index('data-act="example"')
        deck = re.search(r'<span class="macro-controls">(.*?)</span>', toolbar, re.S).group(1)
        assert re.findall(r"macro-control[^>]*>(.)</button>", deck) == ["●", "▶", "⏸", "■"]
        for act in acts:
            assert f'act === "{act}"' in pane or act == "vim", act
        assert "Record…</button>" not in pane and "Stop all" not in pane and "From example" not in pane
        # the same lit states as the blotters' deck, from the open macro's runs
        for cls, when in (("macro-playing", "playing.length > 0"), ("macro-paused", "paused > 0 || (taping && recording.paused)"),
                          ("macro-recording", "!!recording"), ("macro-recording-paused", "!!recording?.paused")):
            assert f'classList.toggle("{cls}", {when})' in pane, cls
        assert 'cmd(playing ? "pause_macro" : "resume_macro", { name: current })' in pane
        # with no run of the open macro to mean, ⏸ and ■ are the recording's
        assert "const taping = !live.length && !!recording;" in pane
        assert "pause.disabled = !live.length && !taping;" in pane and "stop.disabled = !live.length && !taping;" in pane
        assert "if (recording && !liveRuns(current).length) return pauseRecord();" in pane
        assert "if (recording && !liveRuns(current).length) return toggleRecord();" in pane
        assert 'cmd(recording.paused ? "record_resume" : "record_pause", { side })' in pane
        assert {"pause_macro", "resume_macro", "stop_macro", "delete_macro"} <= _fix_cmd_commands()
        # Clone: the text shown, the original untouched, so no discard question
        assert "const source = viewing ? viewing.source : editor.getValue();" in pane and "`${base} copy`" in pane and 'current.replace(/ copy( \\d+)?$/, "")' in pane
        # Delete: Ctrl/Cmd-click and Shift-click build the selection, sent as one list
        assert 'cmd("delete_macro", { names })' in pane and "e.shiftKey" in pane and "e.ctrlKey || e.metaKey" in pane
        assert "selected.size ? [...selected] : current ? [current] : []" in pane
        css = (STATIC / "mkfix.css").read_text(encoding="utf-8")
        assert ".macro-selected" in css and ".macro-toolbar .macro-controls" in css
        # the editor's own gap (4px) plus its margin is the deck rule's padding, as on the blotters
        gap = int(re.search(r"\.mkfix-toolbar \{[^}]*?\bgap: (\d+)px", css).group(1))
        margin = int(re.search(r"\.macro-toolbar \.macro-controls \{[^}]*?margin-left: (\d+)px", css).group(1))
        padding = int(re.search(r"\.macro-controls \{[^}]*?padding-left: (\d+)px", css).group(1))
        assert gap + margin == padding, (gap, margin, padding)
        keys = tomllib.loads(APP_TOML.read_text(encoding="utf-8"))["dialogs"]["macro_keys"]["facts"]
        assert any("Ctrl/Cmd+Click" in f["label"] and "Shift+Click" in f["label"] for f in keys), "the list's selection is a documented key"

    def test_the_list_is_resizable_and_remembers_its_width(self):
        """A long name is cut short at 190px: the list's edge drags, a
        double-click fits the longest name, and the width is a browser pref
        like vim mode."""
        pane = (STATIC / "panes" / "macros.js").read_text(encoding="utf-8")
        assert '<div class="macro-splitter"' in pane and 'const LIST_PREF = "mkfix.macro.list"' in pane
        assert 'splitter.addEventListener("mousedown"' in pane and 'splitter.addEventListener("dblclick"' in pane
        assert "localStorage.setItem(LIST_PREF, String(w))" in pane and "localStorage.getItem(LIST_PREF)" in pane
        assert 'el.querySelector(".macro-name").scrollWidth' in pane, "fit measures the name, not the cell"
        css = (STATIC / "mkfix.css").read_text(encoding="utf-8")
        assert re.search(r"\.macro-splitter \{[^}]*cursor: col-resize", css)
        assert re.search(r"\.macro-list \{[^}]*width: 190px", css) and not re.search(r"\.macro-list \{[^}]*border-right", css), \
            "the border moved to the splitter, or a dragged list would show two"

    def test_the_run_panes_send_commands_that_exist(self):
        from tests.test_ui_config import _fix_cmd_commands
        app = tomllib.loads(APP_TOML.read_text(encoding="utf-8"))
        for side in SIDES:
            buttons = app["panes"][f"{side}-macro-runs"]["buttons"]
            ops = {b["action"]["op"] for b in buttons}
            assert ops == {"pause_run", "resume_run", "stop_run", "move_run", "detach_order"} and ops <= _fix_cmd_commands()
            # One tree holds both kinds of row, so every gate names the kind it acts on.
            for b in buttons:
                kind = "order" if b["action"]["op"] == "detach_order" else "run"
                assert b["enable"]["when"].startswith(f"ALL(rows, r -> r.kind == '{kind}' and "), (b["label"], b["enable"]["when"])
                assert b["action"]["data"][("order_row" if kind == "order" else "run_id")] == "${row." + ("order_row" if kind == "order" else "run_id") + "}"
            moves = [b for b in buttons if b["action"]["op"] == "move_run"]
            assert [(b["label"], b["action"]["data"]["direction"]) for b in moves] == [("Move Up", "up"), ("Move Down", "down")]
            assert all(b["enable"]["maxSelected"] == 1 and "r.priority > 0" in b["enable"]["when"] for b in moves)
            assert "priority" in app["panes"][f"{side}-macro-runs"]["columns"]
        assert {"run_macro", "stop_macro", "run_loopback_tour"} <= _fix_cmd_commands()
        for name, label in (("arm_macro", "Arm"), ("run_macro", "Run")):
            spec = app["dialogs"][name]
            assert spec["submit"] == {"label": label, "service": "fix_cmd", "op": name}
            fields = {f.get("name") for item in spec["fields"] for f in item.get("row", [item])}
            assert fields == {"name", "side", "session", "speed", "seed"}
            assert spec["fields"][1] == {"name": "side", "type": "hidden", "value": "${row.side}"}, \
                "either side has macros that send and macros that wait: the editor says which side it is"
        session = app["dialogs"]["run_macro"]["fields"][2]
        assert (session["required"], session["value"]) == ("row.needs_session", "${row.session}")
        pane = (STATIC / "panes" / "macros.js").read_text(encoding="utf-8")
        assert ("checked = { needs_session: !!result.needs_session, needs_market_session: !!result.needs_market_session,\n"
                "      session: result.session ?? \"\", sends: !!result.sends };") in pane
        # ▶ follows what the macro does: Run… when it has a `run` block, Arm… when it only waits
        assert 'return checked.sends ? app.dialog("run_macro", context) : app.dialog("arm_macro", context);' in pane
        assert "const context = { row: { name: current, side, ...checked } };" in pane
        # … and its tooltip says the same: worded by the side it named the dialog the click did not open
        assert "checked.sends ? `Run… — " in pane and ": `Arm… — " in pane
        assert "startWord" not in pane and 'side === "client" ? `' not in pane

    def test_a_macro_written_from_history_opens_in_its_editor_and_says_so(self):
        """The blotters' Macro… dialog fires `macro.recorded` like Stop
        recording does, with `from: "history"`: the status bar's action hands
        it on and the editor words its status line by it."""
        status = (STATIC / "macro-status.js").read_text(encoding="utf-8")
        assert 'app.state.set("open_macro", { name: args.name, side: args.side, from: args.from ?? "recording" });' in status
        editor = (STATIC / "panes" / "macros.js").read_text(encoding="utf-8")
        assert '${wanted.from === "history" ? "Written from history" : "Recorded"} as ${wanted.name}' in editor
        app = tomllib.loads(APP_TOML.read_text(encoding="utf-8"))
        fired = [b["action"]["dialog"]["submit"]["then"] for pane in app["panes"].values() for b in pane.get("buttons", [])
                 if b.get("label") == "Macro…"]
        assert len(fired) == 15 and all(t["action"] == "macro.recorded" and t["args"]["from"] == "history" for t in fired)
        assert app["dialogs"]["stop_recording"]["submit"]["then"]["args"].get("from") is None, "a recording is the default"
        # the help says where the button is and what it writes
        text = (HELP / "macro-language.md").read_text(encoding="utf-8")
        section = text.split("## From history\n", 1)[1].split("\n## ", 1)[0]
        for said in ("**Macro…**", "Sent Orders and Received Orders", "Sent IOIs, Sent Adverts and Sent Allocations",
                     "Received IOIs and Received Allocations", "**Keep the delays**", "Message Replay", "archived"):
            assert said in section, said

    def test_the_end_to_end_panes_are_a_sides_with_a_session_for_each(self):
        """A third set of the same panes, over the same tables: they differ
        from the client's by the name, and by the second session a run of
        both sides has."""
        app = tomllib.loads(APP_TOML.read_text(encoding="utf-8"))
        panes = app["panes"]
        as_end_to_end = lambda spec: json.loads(json.dumps(spec).replace("Client", "End-to-end").replace("client", "end-to-end"))  # noqa: E731
        assert panes["end-to-end-macros"] == {"title": "End-to-end Macros", "type": "macros", "side": "end-to-end"}
        assert panes["end-to-end-macro-log"] == as_end_to_end(panes["client-macro-log"])
        runs, clients = panes["end-to-end-macro-runs"], as_end_to_end(panes["client-macro-runs"])
        # Session is the run's client session — and, on a row under it, the session of that row, either side's
        assert runs["labels"]["session"] == "Session" and runs["labels"]["market_session"] == "Market Session"
        at = runs["columns"].index("session")
        assert runs["columns"][at:at + 2] == ["session", "market_session"]
        assert runs == clients
        for side in TWO:
            spec = panes[f"{side}-macro-runs"]
            assert "market_session" in spec["columns"] and "market_session" not in spec["visible"], \
                "listed, so the picker has it; a side's run has one session"
            assert any("market_session" in g["columns"] for g in spec["groups"])
        frames = {f["id"]: f for f in app["frames"]}
        assert frames["end-to-end-runs"] == as_end_to_end(frames["client-runs"])
        assert [f["id"] for f in app["frames"]][-3:] == ["client-runs", "market-runs", "end-to-end-runs"]

    def test_an_end_to_end_macro_is_run_on_two_sessions(self):
        app = tomllib.loads(APP_TOML.read_text(encoding="utf-8"))
        dialog = app["dialogs"]["run_end_to_end"]
        assert dialog["submit"] == {"label": "Run", "service": "fix_cmd", "op": "run_macro"} and "modal" not in dialog
        fields = {f["name"]: f for item in dialog["fields"] for f in item.get("row", [item]) if f.get("name")}
        assert list(fields) == ["name", "side", "session", "market_session", "speed", "seed"]
        assert fields["side"] == {"name": "side", "type": "hidden", "value": "${row.side}"}
        for name, needs in (("session", "row.needs_session"), ("market_session", "row.needs_market_session")):
            field = fields[name]
            assert (field["type"], field["required"]) == ("select", needs)
            assert field["optionsFrom"]["service"] == "sessions_list" and field["remember"] == {"key": f"mkfix.end-to-end.{name}"}
        assert fields["session"]["value"] == "${row.session}"
        pane = (STATIC / "panes" / "macros.js").read_text(encoding="utf-8")
        assert 'const side = ["client", "market", END_TO_END].includes(spec.side) ? spec.side : "market";' in pane
        assert 'if (both) return app.dialog("run_end_to_end", context);' in pane
        lang = (STATIC / "macro-lang.js").read_text(encoding="utf-8")
        assert 'export const END_TO_END = "end-to-end";' in lang
        from mkfix.macro import vocab
        assert vocab.E2E == "end-to-end"
        # the viewer's Examples page has a section for them and opens them in their editor
        viewer = (STATIC / "panes" / "help-viewer.js").read_text(encoding="utf-8")
        assert '["end-to-end", "End-to-end macros", "end-to-end-examples",' in viewer
        assert '["client", "market", "end-to-end"].includes(example.dataset.side)' in viewer

    @needs_node
    def test_the_end_to_end_editor_offers_every_block_and_the_status_bar_says_its_runs(self, tmp_path):
        every = run_js(tmp_path, 'L.completionsAt(vocab, [""], 0, 0, { side: "end-to-end" }).map((c) => c.caption)')
        assert every == ["seed", "on error", "share", "define", *macro.vocabulary()["blocks"]]
        assert "on order" in every and "on sent order" in every and "on ioi" in every
        runs = [{"id": 1, "side": "end-to-end", "status": "armed", "macro": "round trip", "verdict": "", "orders": 2,
                 "passed": 0, "failed": 0, "started_at": "20260920-11:00:00.000", "ended_at": "", "session": "C"},
                {"id": 2, "side": "client", "status": "armed", "macro": "burst", "verdict": "", "orders": 5,
                 "passed": 0, "failed": 0, "started_at": "20260920-11:00:00.000", "ended_at": "", "session": "C"}]
        recordings = {"end-to-end": {"recording": True, "actions": 3, "session": ""}}
        items = run_js(tmp_path, f"S.statusItems(S.macroState({json.dumps(runs)}, {json.dumps(recordings)}, "
                                 "Date.UTC(2026, 8, 20, 12, 0, 0))).map((i) => [i.kind, i.text, i.frame ?? i.pane])")
        assert items == [["playing", "▶ client burst · 5 orders", "client-runs"],
                         ["recording", "● REC end-to-end · 3 actions", "end-to-end-macros"],
                         ["playing", "▶ end-to-end round trip · 2 orders", "end-to-end-runs"]]
        assert run_js(tmp_path, "S.KINDS") == ["client", "market", "end-to-end"] and run_js(tmp_path, "S.SIDES") == ["client", "market"]
        assert run_js(tmp_path, 'L.recordingName("end-to-end", new Date(2026, 8, 22, 14, 30, 15))') == "End-to-end 2026-09-22 14:30:15"
        from mkfix.macro.store import NAME
        assert NAME.fullmatch("End-to-end 2026-09-22 14:30:15")

    def test_the_blotters_controls_are_a_sides(self):
        """An end-to-end macro is of neither side: no blotter's deck plays,
        pauses, stops or records one."""
        status = (STATIC / "macro-status.js").read_text(encoding="utf-8")
        blotters = re.search(r"const BLOTTERS = \{(.*?)\};", status, re.S).group(1)
        assert set(re.findall(r'": "([a-z-]+)"', blotters)) == set(TWO)
        assert "KINDS.includes(args.side)" in status, "but a macro written or recorded opens in the editor of its kind"

    def test_the_examples_page_can_set_up_the_sessions_its_examples_name(self):
        from mkfix.macro.store import EXAMPLES, LOOPBACK
        from tests.test_ui_config import _fix_cmd_commands
        viewer = (STATIC / "panes" / "help-viewer.js").read_text(encoding="utf-8")
        from mkfix.macro.store import TOUR, example_header
        assert 'cmd("setup_loopback")' in viewer and "setup_loopback" in _fix_cmd_commands()
        assert 'cmd("run_loopback_tour")' in viewer
        for path in EXAMPLES.glob("*.macro"):
            text = path.read_text(encoding="utf-8")
            sc, _ = macro.check(text)
            assert not any(b.session for b in sc.blocks), f"{path.name}: an example leaves its session to Run…"
            if sc.needs_session:
                assert LOOPBACK["client"] in example_header(text)["needs"], path.name
        assert all(name in viewer for name in LOOPBACK.values())
        for side, name in TOUR.items():
            assert macro.check((EXAMPLES / f"{name}.macro").read_text(encoding="utf-8"))[0].side == side
            assert name in viewer

    def test_orders_show_which_macro_took_them(self):
        app = tomllib.loads(APP_TOML.read_text(encoding="utf-8"))
        for pane in ("order-blotter", "market-order-blotter"):
            assert "macro" in app["panes"][pane]["columns"] and app["panes"][pane]["labels"]["macro"] == "Macro"
