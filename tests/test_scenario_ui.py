"""The scenario UI's pure parts, run under node (skipped without it), and
the guards that keep the help pages, the vendored editor and the wiring true."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from mkfix import scenario

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "mkfix" / "static"
SIDES = ("client", "market")
HELP = STATIC / "help"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def run_js(tmp_path: Path, body: str):
    """Evaluate ``body`` (an expression) with the two modules imported as L and M."""
    for name in ("scenario-lang", "markdown", "line-diff"):
        (tmp_path / f"{name}.mjs").write_text((STATIC / f"{name}.js").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "vocab.json").write_text(json.dumps(scenario.vocabulary()), encoding="utf-8")
    script = tmp_path / "run.mjs"
    script.write_text(
        'import * as L from "./scenario-lang.mjs";\nimport * as M from "./markdown.mjs";\n'
        'import * as D from "./line-diff.mjs";\n'
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
        v = scenario.vocabulary()
        for verb in v["verbs"]:
            assert self.tokens(tmp_path, f"    {verb}")[0] == ("entity.name.function", verb), verb
        for event in v["events"]:
            assert ("constant.language", event) in self.tokens(tmp_path, f"    when {event}"), event

    def test_ace_gets_no_lookbehind(self, tmp_path):
        rules = run_js(tmp_path, "L.aceRules(vocab).start.map((r) => r.regex)")
        assert rules and not any("(?<" in r for r in rules)

    def test_highlight_escapes_html(self, tmp_path):
        html = run_js(tmp_path, "L.highlight(\"    log '<b>' # <i>\", vocab)")
        assert "<b>" not in html and "&lt;b&gt;" in html and 'class="scn-keyword"' in html


@needs_node
class TestCompletion:
    LINES = ["scenario t", "on order", "    ", "    when ", "    fill ", "    fill qty: 1, ", "    restate qty: 1, reason: ",
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
        market = self.names(tmp_path, 2)
        assert {"accept", "fill", "unsol cxl", "when", "after"} <= set(market) and "new" not in market and "cancel" not in market
        sending = self.names(tmp_path, 13)
        assert {"replace", "cancel", "dk"} <= set(sending) and "accept" not in sending and "new" not in sending

    def test_events_for_the_side(self, tmp_path):
        assert self.names(tmp_path, 3, 3) == ["cancel", "replace", "dk"] and "ack" not in self.names(tmp_path, 3)

    def test_terms_then_the_ones_left(self, tmp_path):
        assert self.names(tmp_path, 4) == ["qty", "price", "text", "extra", "using"]
        assert self.names(tmp_path, 5) == ["price", "text", "extra"]

    def test_words_for_a_term_templates_sessions_fields_targets_headers(self, tmp_path):
        assert "repricing" in self.names(tmp_path, 6)
        assert {"leaves_qty", "pending_action", "cxl_rej_reason"} <= set(self.names(tmp_path, 7))
        assert self.names(tmp_path, 8, 3) == ["last trade", "first trade", "trade where"]
        assert self.names(tmp_path, 9) == ["half", "all"]
        assert self.names(tmp_path, 10) == ["S1", "S2"]
        assert self.names(tmp_path, 11) == ["scenario", "seed", "on error", "on sent order", "on order", "run"]
        assert self.names(tmp_path, 15) == ["on"], "`run` may name its session, or leave it to Run…"
        assert "new" in self.names(tmp_path, 16), "a bare `run` opens a client block"

    def test_an_editor_offers_only_its_own_sides_blocks(self, tmp_path):
        assert self.names(tmp_path, 11, side="market") == ["scenario", "seed", "on error", "on order"]
        assert self.names(tmp_path, 11, side="client") == ["scenario", "seed", "on error", "on sent order", "run"]
        assert {"response_to", "reason", "prev", "tag"} <= set(self.names(tmp_path, 14))

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
                   for word, code in scenario.vocabulary()["enums"]["side"].items())

    def test_nothing_in_comments_strings_or_blank_space(self, tmp_path):
        assert self.hover(tmp_path, 0, "fill", nth=1) is None, "a word in a trailing comment"
        assert self.hover(tmp_path, 6, "accept") is None
        assert self.hover(tmp_path, 5, "accept") is None and self.hover(tmp_path, 5, "fill") is None, "words in a string"
        assert run_js(tmp_path, f"L.hoverAt(vocab, {json.dumps(self.LINES)}, 1, 1)") is None
        assert run_js(tmp_path, f"L.hoverAt(vocab, {json.dumps(self.LINES)}, 1, 400)") is None


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
        old = ["scenario t", "on order", "    after 250ms", "    accept"]
        new = ["scenario t", "on order where symbol == 'IBM'", "    accept", "    when cancel", "        accept"]
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
        assert '<pre class="md-code" data-lang="scenario"><code>if a &lt; b</code></pre>' == self.render(
            tmp_path, "```scenario\nif a < b\n```\n")
        painted = run_js(tmp_path, 'M.renderMarkdown("```scenario\\naccept\\n```", { highlight: (c, l) => `[${l}:${c}]` })')
        assert "<code>[scenario:accept]</code>" in painted

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
        assert {"label": "Scenario Language", "action": "pane.show", "args": "help-viewer"} in help_menu
        assert app["panes"]["help-viewer"]["type"] == "help-viewer"
        assert [p["id"] for p in self.PAGES][0] == "scenario-language", "the page the menu item lands on"

    def test_every_example_in_the_pages_is_a_script_that_checks(self):
        blocks = 0
        for page in self.PAGES:
            if "file" not in page:
                continue
            text = (HELP / page["file"]).read_text(encoding="utf-8")
            for block in re.findall(r"```scenario\n(.*?)```", text, re.S):
                _, diags = scenario.check(block)
                assert diags == [], (page["file"], block.splitlines()[0], [str(d) for d in diags])
                blocks += 1
        assert blocks >= 3

    def test_the_reference_tables_are_the_vocabulary(self):
        text = (HELP / "scenario-language.md").read_text(encoding="utf-8")
        v = scenario.vocabulary()

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
        pane = (STATIC / "panes" / "scenarios.js").read_text(encoding="utf-8")
        for name in ("ace.js", "ext-language_tools.js", "ext-searchbox.js"):
            assert name in pane, f"the pane never loads {name}"
        assert 'useWorker: false' in pane, "our mode has no worker file to load"
        assert "ace-builds 1.44.0" in pane and "1.44.0" in (ROOT / "README.md").read_text(encoding="utf-8")

    def test_the_panes_are_loaded_declared_and_on_a_menu(self):
        app = json.loads((STATIC / "app.json").read_text(encoding="utf-8"))
        index = (STATIC / "index.html").read_text(encoding="utf-8")
        for module in ("scenarios.js", "help-viewer.js"):
            assert f'/static/panes/{module}' in index
        shown = {i.get("args") for m in app["menubar"] for i in m["items"] if i.get("action") == "pane.show"}
        assert {f"{side}-{pane}" for side in SIDES for pane in ("scenarios", "runs", "scripts", "log")} | {"help-viewer"} <= shown
        for state in ("selected_scenario", "selected_scenario_instance", "help_target", "open_example"):
            assert state in app["state"], state

    @pytest.mark.parametrize("side", SIDES)
    def test_each_side_has_its_own_four_panes_over_its_own_rows(self, side):
        """The two sides share tables and services; a pane's `filter` is all
        that keeps a client run out of Market Runs."""
        import tomllib
        app = json.loads((STATIC / "app.json").read_text(encoding="utf-8"))
        toml = tomllib.loads((ROOT / "mkfix" / "mkfix.toml").read_text(encoding="utf-8", errors="replace"))
        word = side.capitalize()
        editor = app["panes"][f"{side}-scenarios"]
        assert editor == {"title": f"{word} Scenarios", "type": "scenarios", "side": side}
        for pane, service, title in (("runs", "scenario_runs_query", "Runs"), ("scripts", "scenario_instances_query", "Scripts"),
                                     ("log", "scenario_log_query", "Log")):
            spec = app["panes"][f"{side}-{pane}"]
            assert (spec["title"], spec["service"], spec["filter"]) == (f"{word} {title}", service, f"side == '{side}'")
            assert "side" in toml["services"][service]["filterable"]
        assert "side" in toml["services"]["scenarios_query"]["filterable"]
        assert app["panes"][f"{side}-scripts"]["select"] == {"state": "selected_scenario_instance"}
        for table in ("fix_scenarios", "fix_scenario_runs", "fix_scenario_instances", "fix_scenario_log"):
            assert "side" in toml["tables"][table]["columns"], table

    def test_the_two_sides_panes_differ_only_by_side(self):
        app = json.loads((STATIC / "app.json").read_text(encoding="utf-8"))
        for pane in ("runs", "scripts", "log"):
            client = json.dumps(app["panes"][f"client-{pane}"]).replace("client", "market").replace("Client", "Market")
            assert client == json.dumps(app["panes"][f"market-{pane}"]), pane

    def test_history_is_wired_to_the_versions_the_server_keeps(self):
        import tomllib
        toml = tomllib.loads((ROOT / "mkfix" / "mkfix.toml").read_text(encoding="utf-8", errors="replace"))
        assert toml["tables"]["fix_scenarios"]["versioned"] is True
        service = toml["services"]["scenario_versions"]
        assert service["protocol"] == "reqrep" and "fix_scenarios__history WHERE id = :id" in service["sql"]
        assert "ORDER BY _mkio_version DESC" in service["sql"]
        pane = (STATIC / "panes" / "scenarios.js").read_text(encoding="utf-8")
        assert 'client.request("scenario_versions", { id })' in pane
        assert 'from "/static/line-diff.js"' in pane and (STATIC / "line-diff.js").is_file()
        for column in re.findall(r"\bv\.(\w+)|\brow\.(updated_at|source)", pane):
            name = column[0] or column[1]
            assert name in service["sql"], f"the pane reads {name}, which scenario_versions does not select"
        for act in ("history", "restore", "back"):
            assert f'data-act="{act}"' in pane and f'act === "{act}"' in pane, act
        # A version is looked at, never edited or run; Restore is an unsaved edit, so nothing is lost to it.
        assert "if (current === null || viewing) return;" in pane
        assert 'button("save").disabled = !dirty() || !!viewing;' in pane
        css = (STATIC / "mkfix.css").read_text(encoding="utf-8")
        for rule in (".scn-history", ".scn-version", ".scn-diff-only", ".scn-diff-gap"):
            assert rule in css, rule

    def test_hover_is_wired_and_its_tooltip_is_themed(self):
        pane = (STATIC / "panes" / "scenarios.js").read_text(encoding="utf-8")
        ace = (STATIC / "vendor" / "ace" / "ace.js").read_text(encoding="utf-8")
        assert "HoverTooltip" in ace and 'ace.require("ace/tooltip")' in pane, "the vendored Ace must carry the tooltip the pane asks for"
        assert "hoverAt(vocab," in pane and "hover.addToEditor(editor)" in pane
        assert "session.getAnnotations()" in pane, "a problem's message is part of the hover"
        css = (STATIC / "mkfix.css").read_text(encoding="utf-8")
        assert ".ace_tooltip.ace-mkfix" in css, "Ace hangs the hover tooltip on <body> with the theme class on the tooltip"

    def test_the_editor_keeps_to_its_side(self):
        pane = (STATIC / "panes" / "scenarios.js").read_text(encoding="utf-8")
        for call in ('cmd("check_scenario", { source: editor.getValue(), side })', 'cmd("list_examples", { side })',
                     'cmd("stop_scenario", { name: current })', "const mine = `side == '${side}'`"):
            assert call in pane, call
        saves = re.findall(r'cmd\("save_scenario", \{([^}]*)\}', pane)
        assert len(saves) == 3 and all(re.search(r"\bside\b", s) for s in saves), "every save says which side it is for"
        for call in ('cmd("record_start", { side, session })', 'cmd("record_stop", { side, name })', 'cmd("record_status", { side })'):
            assert call in pane, call
        assert "wanted.side !== side" in pane, "an example opened for the other editor is not this one's"
        viewer = (STATIC / "panes" / "help-viewer.js").read_text(encoding="utf-8")
        assert 'app.fireAction("pane.show", `${side}-scenarios`)' in viewer

    def test_the_run_panes_send_commands_that_exist(self):
        from tests.test_ui_config import _fix_cmd_commands
        app = json.loads((STATIC / "app.json").read_text(encoding="utf-8"))
        for side in SIDES:
            ops = {b["action"]["op"] for pane in ("runs", "scripts") for b in app["panes"][f"{side}-{pane}"]["buttons"]}
            assert ops == {"pause_run", "resume_run", "stop_run", "move_run", "detach_instance"} and ops <= _fix_cmd_commands()
            moves = [b for b in app["panes"][f"{side}-runs"]["buttons"] if b["action"]["op"] == "move_run"]
            assert [(b["label"], b["action"]["data"]["direction"]) for b in moves] == [("Move Up", "up"), ("Move Down", "down")]
            assert all(b["enable"]["maxSelected"] == 1 and "r.priority > 0" in b["enable"]["when"] for b in moves)
            assert "priority" in app["panes"][f"{side}-runs"]["columns"]
        assert {"run_scenario", "stop_scenario", "run_loopback_tour"} <= _fix_cmd_commands()
        for name, label in (("arm_scenario", "Arm"), ("run_scenario", "Run")):
            spec = app["dialogs"][name]
            assert spec["submit"] == {"label": label, "service": "fix_cmd", "op": name}
            fields = {f.get("name") for item in spec["fields"] for f in item.get("row", [item])}
            assert fields == {"name", "session", "speed", "seed"}
        session = app["dialogs"]["run_scenario"]["fields"][1]
        assert (session["required"], session["value"]) == ("row.needs_session", "${row.session}")
        pane = (STATIC / "panes" / "scenarios.js").read_text(encoding="utf-8")
        assert "checked = { needs_session: !!result.needs_session, session: result.session ?? \"\" }" in pane

    def test_the_examples_page_can_set_up_the_sessions_its_examples_name(self):
        from mkfix.scenario.store import EXAMPLES, LOOPBACK
        from tests.test_ui_config import _fix_cmd_commands
        viewer = (STATIC / "panes" / "help-viewer.js").read_text(encoding="utf-8")
        from mkfix.scenario.store import TOUR, example_header
        assert 'cmd("setup_loopback")' in viewer and "setup_loopback" in _fix_cmd_commands()
        assert 'cmd("run_loopback_tour")' in viewer
        for path in EXAMPLES.glob("*.scenario"):
            text = path.read_text(encoding="utf-8")
            sc, _ = scenario.check(text)
            assert not any(b.session for b in sc.blocks), f"{path.name}: an example leaves its session to Run…"
            if sc.needs_session:
                assert LOOPBACK["client"] in example_header(text)["needs"], path.name
        assert all(name in viewer for name in LOOPBACK.values())
        for side, name in TOUR.items():
            assert scenario.check((EXAMPLES / f"{name}.scenario").read_text(encoding="utf-8"))[0].side == side
            assert name in viewer

    def test_orders_show_which_scenario_took_them(self):
        app = json.loads((STATIC / "app.json").read_text(encoding="utf-8"))
        for pane in ("order-blotter", "market-order-blotter"):
            assert "scenario" in app["panes"][pane]["columns"] and app["panes"][pane]["labels"]["scenario"] == "Scenario"
