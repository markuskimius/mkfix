"""The macro UI's pure parts, run under node (skipped without it), and
the guards that keep the help pages, the vendored editor and the wiring true."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from mkfix import macro

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "mkfix" / "static"
SIDES = ("client", "market")
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
             "run ", "    "]

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
        assert self.names(tmp_path, 3) == ["qty", "price", "text", "extra", "using"]
        assert self.names(tmp_path, 4) == ["price", "text", "extra"]

    def test_words_for_a_term_templates_sessions_fields_targets_headers(self, tmp_path):
        assert "repricing" in self.names(tmp_path, 5)
        assert {"leaves_qty", "pending_action", "cxl_rej_reason"} <= set(self.names(tmp_path, 6))
        assert self.names(tmp_path, 7, 3) == ["last trade", "first trade", "trade where"]
        assert self.names(tmp_path, 8) == ["half", "all"]
        assert self.names(tmp_path, 9) == ["S1", "S2"]
        assert self.names(tmp_path, 10) == ["seed", "on error", "on sent order", "on order", "run"]
        assert self.names(tmp_path, 14) == ["on"], "`run` may name its session, or leave it to Run…"
        assert "new" in self.names(tmp_path, 15), "a bare `run` opens a client block"

    def test_an_editor_offers_only_its_own_sides_blocks(self, tmp_path):
        assert self.names(tmp_path, 10, side="market") == ["seed", "on error", "on order"]
        assert self.names(tmp_path, 10, side="client") == ["seed", "on error", "on sent order", "run"]
        assert {"response_to", "reason", "prev", "tag"} <= set(self.names(tmp_path, 13))

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
        assert tip["lines"][1] == "Terms: qty, price, text, extra" and "template" in tip["lines"][2]
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
        whose dialog lives in app.json. Both must offer the stamped name."""
        pane = (STATIC / "panes" / "macros.js").read_text(encoding="utf-8")
        assert "recordingName(side)" in pane and "download: exportFileName(current)" in pane
        assert "exportFileName, helpAt, hoverAt, recordingName" in pane
        assert '"recorded"' not in pane and "recorded-${n}" not in pane, "the counter gave way to the stamp"
        toolbar = (STATIC / "macro-status.js").read_text(encoding="utf-8")
        assert 'import { recordingName } from "/static/macro-lang.js";' in toolbar
        assert 'app.dialog("stop_recording", { row: { ...context.row, name: recordingName(side) } })' in toolbar, \
            "computed at the click, not at mount"
        app = json.loads((STATIC / "app.json").read_text(encoding="utf-8"))
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
        return [(i["kind"], i["text"], i["pane"]) for i in got]

    def test_idle_says_nothing_and_enables_nothing(self, tmp_path):
        state = self.state(tmp_path, [])
        assert set(state) == {"client", "market"}
        assert all((s["recording"], s["playing"], s["paused"], s["live"], s["last"]) == (False, 0, 0, 0, None) for s in state.values())
        assert self.items(tmp_path, []) == []

    def test_each_side_counts_its_own_runs(self, tmp_path):
        runs = [self.run(1, "market", "armed", orders=3), self.run(2, "market", "paused", macro="desk"),
                self.run(3, "client", "armed", macro="burst", orders=20), self.run(4, "client", "armed", macro="chase", orders=1)]
        state = self.state(tmp_path, runs)
        assert [(state[s]["playing"], state[s]["paused"], state[s]["live"], state[s]["orders"]) for s in ("client", "market")] == [
            (2, 0, 2, 21), (1, 1, 2, 3)]
        assert self.items(tmp_path, runs) == [
            ("playing", "▶ client 2 runs · 21 orders", "client-macro-runs"),
            ("playing", "▶ market slow-fill · 3 orders", "market-macro-runs"),
            ("paused", "⏸ market desk paused", "market-macro-runs")]

    def test_recording_is_said_first_and_links_to_the_editor(self, tmp_path):
        rec = {"market": {"recording": True, "actions": 1, "session": "LOOP-MKT"}, "client": {"recording": False, "actions": 0}}
        assert self.items(tmp_path, [self.run(1, "market", "armed")], rec) == [
            ("recording", "● REC market · 1 action on LOOP-MKT", "market-macros"),
            ("playing", "▶ market slow-fill · 0 orders", "market-macro-runs")]
        state = self.state(tmp_path, [], rec)
        assert (state["market"]["recording"], state["market"]["actions"], state["client"]["recording"]) == (True, 1, False)

    def test_an_ended_run_is_news_for_a_minute(self, tmp_path):
        fresh = self.run(1, "client", "finished", macro="chase", verdict="passed", orders=3, passed=3, ended="20260920-11:59:30.000")
        stale = self.run(2, "client", "finished", macro="old", verdict="passed", orders=1, passed=1, ended="20260920-11:58:00.000")
        stopped = self.run(3, "market", "stopped", ended="20260920-11:59:59.500")
        interrupted = self.run(4, "market", "interrupted", macro="desk", ended="20260920-11:50:00.000")
        assert self.items(tmp_path, [fresh, stale, stopped, interrupted]) == [
            ("passed", "■ chase passed · 3 of 3", "client-macro-runs"), ("ended", "■ slow-fill stopped", "market-macro-runs")]
        assert self.items(tmp_path, [stale]) == []

    def test_a_failure_stays_until_something_else_happens_on_its_side(self, tmp_path):
        failed = self.run(1, "client", "finished", macro="suite", verdict="failed", orders=3, passed=2, failed=1,
                          ended="20260920-11:55:00.000")
        assert self.items(tmp_path, [failed]) == [("failed", "■ suite failed · 1 of 3 failed", "client-macro-runs")]
        later = self.run(2, "client", "armed", macro="chase", started="20260920-11:56:00.000")
        assert self.items(tmp_path, [failed, later]) == [("playing", "▶ client chase · 0 orders", "client-macro-runs")]
        other_side = self.run(3, "market", "armed", started="20260920-11:56:00.000")
        assert ("failed", "■ suite failed · 1 of 3 failed", "client-macro-runs") in self.items(tmp_path, [failed, other_side])
        # a stop or an interruption gives way the same way; a pass keeps its minute
        interrupted = self.run(5, "market", "interrupted", ended="20260920-11:59:40.000")
        again = self.run(6, "market", "armed", started="20260920-11:59:50.000")
        assert self.items(tmp_path, [interrupted]) == [("ended", "■ slow-fill interrupted", "market-macro-runs")]
        assert self.items(tmp_path, [interrupted, again]) == [("playing", "▶ market slow-fill · 0 orders", "market-macro-runs")]
        passed = self.run(7, "client", "finished", macro="chase", verdict="passed", orders=1, passed=1, ended="20260920-11:59:40.000")
        assert ("passed", "■ chase passed · 1 of 1", "client-macro-runs") in self.items(
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
        app = json.loads((STATIC / "app.json").read_text(encoding="utf-8"))
        for page in self.PAGES:
            assert page.get("builtin") == "examples" or (HELP / page["file"]).is_file(), page
        help_menu = app["menubar"][-1]["items"]
        assert {"label": "Macro Language", "action": "pane.show", "args": "help-viewer"} in help_menu
        assert app["panes"]["help-viewer"]["type"] == "help-viewer"
        assert [p["id"] for p in self.PAGES][0] == "macro-language", "the page the menu item lands on"

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
        app = json.loads((STATIC / "app.json").read_text(encoding="utf-8"))
        index = (STATIC / "index.html").read_text(encoding="utf-8")
        for module in ("macros.js", "help-viewer.js"):
            assert f'/static/panes/{module}' in index
        shown = {i.get("args") for m in app["menubar"] for i in m["items"] if i.get("action") == "pane.show"}
        assert {f"{side}-{pane}" for side in SIDES for pane in ("macros", "macro-runs", "macro-orders", "macro-log")} | {"help-viewer"} <= shown
        for state in ("selected_macro", "selected_macro_order", "help_target", "open_example"):
            assert state in app["state"], state

    @pytest.mark.parametrize("side", SIDES)
    def test_each_side_has_its_own_four_panes_over_its_own_rows(self, side):
        """The two sides share tables and services; a pane's `filter` is all
        that keeps a client run out of Market Runs."""
        import tomllib
        app = json.loads((STATIC / "app.json").read_text(encoding="utf-8"))
        toml = tomllib.loads((ROOT / "mkfix" / "mkfix.toml").read_text(encoding="utf-8", errors="replace"))
        word = side.capitalize()
        editor = app["panes"][f"{side}-macros"]
        assert editor == {"title": f"{word} Macros", "type": "macros", "side": side}
        for pane, service, title in (("macro-runs", "macro_runs_query", "Macro Runs"),
                                     ("macro-orders", "macro_orders_query", "Macro Orders"),
                                     ("macro-log", "macro_log_query", "Macro Log")):
            spec = app["panes"][f"{side}-{pane}"]
            assert (spec["title"], spec["service"], spec["filter"]) == (f"{word} {title}", service, f"side == '{side}'")
            assert "side" in toml["services"][service]["filterable"]
        assert "side" in toml["services"]["macros_query"]["filterable"]
        assert app["panes"][f"{side}-macro-orders"]["select"] == {"state": "selected_macro_order"}
        for table in ("fix_macros", "fix_macro_runs", "fix_macro_orders", "fix_macro_log"):
            assert "side" in toml["tables"][table]["columns"], table

    def test_the_two_sides_panes_differ_only_by_side(self):
        app = json.loads((STATIC / "app.json").read_text(encoding="utf-8"))
        for pane in ("macro-runs", "macro-orders", "macro-log"):
            client = json.dumps(app["panes"][f"client-{pane}"]).replace("client", "market").replace("Client", "Market")
            assert client == json.dumps(app["panes"][f"market-{pane}"]), pane

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
        app = json.loads((STATIC / "app.json").read_text(encoding="utf-8"))
        toml = tomllib.loads((ROOT / "mkfix" / "mkfix.toml").read_text(encoding="utf-8", errors="replace"))
        module = (STATIC / "macro-status.js").read_text(encoding="utf-8")
        assert 'import "/static/macro-status.js";' in (STATIC / "index.html").read_text(encoding="utf-8")
        assert app["statusbar"]["right"][0] == {"type": "macro-status"} and 'registerWidget("macro-status"' in module
        assert 'const BLOTTERS = { "order-blotter": "client", "market-order-blotter": "market" };' in module
        assert {"order-blotter", "market-order-blotter"} <= set(app["panes"]), "the panes the controls are put into"
        ops = {"play_macro": "play_macro", "pause_runs": "pause_runs", "stop_runs": "stop_runs",
               "record_macro": "record_start", "stop_recording": "record_stop"}
        for dialog, op in ops.items():
            spec = app["dialogs"][dialog]
            opened = ('app.dialog("stop_recording", { row: { ...context.row, name: recordingName(side) } })'
                      if dialog == "stop_recording" else f'app.dialog("{dialog}", context)')
            assert opened in module, dialog
            assert spec["submit"]["service"] == "fix_cmd" and spec["submit"]["op"] == op and op in _fix_cmd_commands()
            assert spec["fields"][0] == {"name": "side", "type": "hidden", "value": "${row.side}"}
            assert "modal" not in spec
        for dialog, service, params in (("play_macro", "macro_play_options", {"side": "${row.side}"}),
                                        ("pause_runs", "macro_run_options", {"side": "${row.side}", "paused": 0}),
                                        ("stop_runs", "macro_run_options", {"side": "${row.side}", "paused": 1})):
            select = app["dialogs"][dialog]["fields"][1]
            assert select["optionsFrom"]["service"] == service and select["optionsFrom"]["params"] == params
            assert "empty" not in select["optionsFrom"], "Play… must be given a macro, and a checklist has no blank row"
            assert set(re.findall(r":(\w+)", toml["services"][service]["sql"].replace("'resume:", "").replace("'macro:", ""))) == set(params)
        # Pause and Stop show the runs together, as a scrolling list rather than behind a dropdown — a
        # `size` on a select, which mkui reads from 1.11.0: an older one shows a dropdown and says nothing
        from mkui.__init__ import __version__ as mkui_version
        assert tuple(map(int, mkui_version.split(".")[:2])) >= (1, 11)
        assert "mkui>=1.11.0,<2" in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        import mkui
        assert 'field.type === "checklist"' in (Path(mkui.static_dir) / "src" / "widgets" / "mkui-dialog.js").read_text(encoding="utf-8")
        for dialog, every in (("pause_runs", "Every playing run"), ("stop_runs", "Every live run")):
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
        for button, cls, when in (("play", "macro-playing", "s.playing > 0"), ("pause", "macro-paused", "s.paused > 0"),
                                  ("record", "macro-recording", "s.recording")):
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
        app = json.loads((STATIC / "app.json").read_text(encoding="utf-8"))
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
        for cls, when in (("macro-playing", "playing.length > 0"), ("macro-paused", "paused > 0"), ("macro-recording", "!!recording")):
            assert f'classList.toggle("{cls}", {when})' in pane, cls
        assert 'cmd(playing ? "pause_macro" : "resume_macro", { name: current })' in pane
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
        keys = json.loads((STATIC / "app.json").read_text(encoding="utf-8"))["dialogs"]["macro_keys"]["facts"]
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
        app = json.loads((STATIC / "app.json").read_text(encoding="utf-8"))
        for side in SIDES:
            ops = {b["action"]["op"] for pane in ("macro-runs", "macro-orders") for b in app["panes"][f"{side}-{pane}"]["buttons"]}
            assert ops == {"pause_run", "resume_run", "stop_run", "move_run", "detach_order"} and ops <= _fix_cmd_commands()
            moves = [b for b in app["panes"][f"{side}-macro-runs"]["buttons"] if b["action"]["op"] == "move_run"]
            assert [(b["label"], b["action"]["data"]["direction"]) for b in moves] == [("Move Up", "up"), ("Move Down", "down")]
            assert all(b["enable"]["maxSelected"] == 1 and "r.priority > 0" in b["enable"]["when"] for b in moves)
            assert "priority" in app["panes"][f"{side}-macro-runs"]["columns"]
        assert {"run_macro", "stop_macro", "run_loopback_tour"} <= _fix_cmd_commands()
        for name, label in (("arm_macro", "Arm"), ("run_macro", "Run")):
            spec = app["dialogs"][name]
            assert spec["submit"] == {"label": label, "service": "fix_cmd", "op": name}
            fields = {f.get("name") for item in spec["fields"] for f in item.get("row", [item])}
            assert fields == {"name", "session", "speed", "seed"}
        session = app["dialogs"]["run_macro"]["fields"][1]
        assert (session["required"], session["value"]) == ("row.needs_session", "${row.session}")
        pane = (STATIC / "panes" / "macros.js").read_text(encoding="utf-8")
        assert "checked = { needs_session: !!result.needs_session, session: result.session ?? \"\" }" in pane

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
        app = json.loads((STATIC / "app.json").read_text(encoding="utf-8"))
        for pane in ("order-blotter", "market-order-blotter"):
            assert "macro" in app["panes"][pane]["columns"] and app["panes"][pane]["labels"]["macro"] == "Macro"
