"""The scenario language: parser, checker, vocabulary, and the bundled
examples, which are its acceptance set."""

import json
import re
from pathlib import Path

import pytest
from mkio import expr

from mkfix import scenario
from mkfix.scenario import functions, nodes, vocab
from mkfix.scenario.nodes import (
    Action, After, Expect, Finish, If, Let, Log, Repeat, Stop, Wait, When, While,
)
from mkfix.fix.actions import ACTIONS
from mkfix.fix.events import _REPORT_KINDS, _TRANS_KINDS
from mkfix.services.fix_command import TEMPLATE_TERMS

ROOT = Path(__file__).parent.parent
EXAMPLES = sorted((ROOT / "mkfix" / "scenario" / "examples").glob("*.scenario"))


def market(body: str, header: str = "on order") -> str:
    lines = "\n".join("    " + line if line.strip() else line for line in body.strip("\n").splitlines())
    return f"scenario t\n{header}\n{lines}\n"


def problems(text: str, **known) -> list[tuple[int, int, str]]:
    return [(d.line, d.col, d.message) for d in scenario.check(text, **known)[1]]


def clean(text: str, **known):
    sc, diags = scenario.check(text, **known)
    assert diags == [], [str(d) for d in diags]
    return sc


def body_of(text: str):
    return clean(text).blocks[0].body


# -- form ---------------------------------------------------------------------------

class TestParser:
    def test_header(self):
        sc = clean("# a comment\nscenario slow fill-2.b   # trailing\nseed 42\non error continue\n\non order\n    accept\n")
        assert (sc.name, sc.seed, sc.on_error) == ("slow fill-2.b", 42, "continue")
        assert clean("scenario t\non order\n    accept\n").on_error == "fail"

    def test_three_kinds_of_block(self):
        sc = clean("scenario t\n"
                   "on order where symbol in ['IBM'] and order.order_qty > 0\n    accept\n"
                   "on sent order where client == 'ACME'\n    cancel\n"
                   "run on BROKER-1.a\n    new symbol: 'IBM', side: buy, qty: 100\n")
        assert [(b.kind, b.session, b.where.source if b.where else None) for b in sc.blocks] == [
            (vocab.MARKET, None, "symbol in ['IBM'] and order.order_qty > 0"),
            (vocab.ATTACHED, None, "client == 'ACME'"),
            (vocab.CLIENT, "BROKER-1.a", None)]
        assert clean("scenario t\non order\n    accept\n").blocks[0].where is None

    def test_after_and_jitter(self):
        plain, jitter, ascii_jitter, computed = body_of(market(
            "after 200ms\nafter 1s ± 250ms\nafter 2s +/- 0.5s\nafter 100ms * n + elapsed\n"))
        assert (plain.delay.node.value, plain.jitter) == (0.2, None)
        assert (jitter.delay.node.value, jitter.jitter.node.value) == (1, 0.25)
        assert (ascii_jitter.delay.node.value, ascii_jitter.jitter.node.value) == (2, 0.5)
        assert computed.delay.source == "100ms * n + elapsed" and isinstance(computed, After)

    def test_wait_and_expect(self):
        text = ("scenario t\non sent order\n"
                "    wait fill\n"
                "    wait replaced or cancel rejected or timeout 2s\n"
                "    wait fill where trade.last_qty > 100 or order.leaves_qty == 0 or timeout 1.5m\n"
                "    expect ack within 2s\n"
                "    expect canceled or filled where event.source == 'wire' within 5s else fail 'no answer for ${order.cl_ord_id}'\n")
        w1, w2, w3, e1, e2 = body_of(text)
        assert (w1.events, w1.where, w1.timeout) == (["fill"], None, None)
        assert w2.events == ["replaced", "cancel rejected"] and w2.timeout.node.value == 2
        assert w3.where.source == "trade.last_qty > 100 or order.leaves_qty == 0", \
            "`or timeout` ends the condition; any other `or` is part of it"
        assert w3.timeout.node.value == 90
        assert isinstance(e1, Expect) and e1.within.node.value == 2 and e1.message is None
        assert e2.events == ["canceled", "filled"] and e2.where.source == "event.source == 'wire'"
        assert e2.message.template is True

    def test_longest_event_name_wins(self):
        (w,) = body_of("scenario t\non sent order\n    wait cancel rejected or done for day or session down\n")
        assert w.events == ["cancel rejected", "done for day", "session down"]

    def test_when_with_guard_and_body(self):
        (wh,) = body_of(market("when cancel or replace and order.leaves_qty == 0 and not order.pending_qty\n"
                               "    reject text: 'late'\n    stop\n"))
        assert isinstance(wh, When) and wh.events == ["cancel", "replace"]
        assert wh.guard.source == "order.leaves_qty == 0 and not order.pending_qty"
        assert [type(s) for s in wh.body] == [Action, Stop]

    def test_control_flow(self):
        text = market("let limit = 100\n"
                      "if order.order_qty > limit\n    reject\n"
                      "else if order.order_qty == 0\n    reject text: 'zero'\n"
                      "else\n    accept\n"
                      "while order.leaves_qty > 0\n    fill qty: 1, price: 2\n"
                      "log 'done ${order.cl_ord_id}'\npass\n")
        let, branch, loop, log, done = body_of(text)
        assert isinstance(let, Let) and let.name == "limit"
        assert isinstance(branch, If) and len(branch.branches) == 2 and len(branch.orelse) == 1
        assert isinstance(loop, While) and isinstance(log, Log) and log.message.template
        assert isinstance(done, Finish) and (done.verdict, done.message) == ("pass", None)

    def test_repeat_forms(self):
        text = ("scenario t\nrun on S\n    new symbol: 'A', side: buy, qty: 1\n"
                "    repeat 50 at 10/s with sym = ['IBM', 'MSFT']\n        log sym\n"
                "    repeat 3 every 250ms\n        cancel\n"
                "    repeat n + 1\n        cancel\n"
                "    repeat 2 at 30/m\n        cancel\n")
        _, a, b, c, d = body_of(text)
        assert isinstance(a, Repeat) and (a.interval, a.var, a.values.source) == (0.1, "sym", "['IBM', 'MSFT']")
        assert (b.interval, b.every.node.value) == (None, 0.25)
        assert (c.count.source, c.interval, c.every) == ("n + 1", None, None)
        assert d.interval == 2.0

    def test_actions_terms_targets_templates(self):
        text = market("fill qty: MIN(100, order.leaves_qty), price: order.price, text: 'a, b: c', extra: '58=x|9001=y'\n"
                      "bust last trade, text: 'oops'\ncorrect first trade qty: 1, price: 2\n"
                      "bust trade where trade.last_price > 100 and trade.exec_type != 'Cancel', text: 'x'\n"
                      "unsol cxl\nfill using 'half-fill'\nfill using 'half-fill', qty: 5\n"
                      "restate qty: 1, reason: repricing\nrestate qty: 1, reason: 'Broker Option'\n"
                      "restate qty: 1, reason: order.text\n")
        fill, bust, correct, where, cxl, tmpl, tmpl2, word, name, computed = body_of(text)
        assert [(t.name, t.key) for t in fill.terms] == [
            ("qty", "qty"), ("price", "price"), ("text", "text"), ("extra", "extra_tags")]
        assert fill.terms[2].value.node.value == "a, b: c", "commas and colons inside quotes are the value's"
        assert (bust.target.which, correct.target.which, where.target.which) == ("last", "first", "where")
        assert where.target.where.source == "trade.last_price > 100 and trade.exec_type != 'Cancel'"
        assert (cxl.verb, cxl.terms) == ("unsol cxl", [])
        assert (tmpl.template, tmpl.terms) == ("half-fill", []) and tmpl2.terms[0].name == "qty"
        assert (word.terms[1].word, word.terms[1].value) == ("repricing", None)
        assert name.terms[1].value.node.value == "Broker Option" and computed.terms[1].word is None

    def test_an_enum_word_is_only_one_when_it_stands_alone(self):
        text = "scenario t\nrun on S\n    let buy = '2'\n    new symbol: 'A', side: buy, qty: 1\n    new symbol: 'A', side: IF(n > 0, buy, '1'), qty: 1\n"
        _, plain, computed = body_of(text)
        assert plain.terms[1].word == "buy" and computed.terms[1].word is None

    def test_comments_and_strings(self):
        (act,) = body_of(market("reject text: 'not # a comment', extra: \"58=it's\"   # but this is\n"))
        assert act.terms[0].value.node.value == "not # a comment" and act.terms[1].value.node.value == "58=it's"

    def test_words_are_case_insensitive_names_are_not(self):
        (wh,) = body_of(market("WHEN Cancel AND order.leaves_qty == 0\n    Reject Text: 'late'\n"))
        assert wh.events == ["cancel"] and wh.body[0].terms[0].name == "text"

    @pytest.mark.parametrize("text, line, col, message", [
        ("on order\n    accept\n", 1, 0, "A script starts with `scenario NAME`"),
        ("scenario a\nscenario b\non order\n    accept\n", 2, 0, "A script names itself once"),
        ("scenario t\nseed x\non order\n    accept\n", 2, 5, "Expected a whole number"),
        ("scenario t\non error maybe\non order\n    accept\n", 2, 9, "Expected `continue` or `fail`"),
        ("scenario t\nbanana\n", 2, 0, "Expected `scenario`, `seed`, `on error`, or a block"),
        ("scenario t\n    accept\n", 2, 0, "Unexpected indent: this line belongs to no block"),
        ("scenario t\non order\naccept\n", 2, 0, "Expected an indented block under this line"),
        ("scenario t\nrun on\n    cancel\n", 2, 6, "Expected a session name"),
        ("scenario t\non order where\n    accept\n", 2, 14, "Expected an expression"),
        ("scenario t\non order where symbol ==\n    accept\n", 2, 24, "Unexpected end of expression"),
        ("scenario t\non order\n\taccept\n", 3, 0, "Indent with spaces, not tabs"),
        (market("accept\n    reject\n"), 4, 0, "Unexpected indent"),
        (market("if order.cum_qty\n        accept\n    reject\n"), 5, 0, "This indent matches no enclosing block"),
        (market("when cancel\naccept\n"), 3, 4, "Expected an indented block under this line"),
        (market("else\n    accept\n"), 3, 4, "`else` must follow an `if` at the same indent"),
        (market("if 1\n    accept\nelse\n    reject\nelse\n    reject\n"), 7, 4, "`else` must follow"),
        (market("acept\n"), 3, 4, "Unknown statement 'acept' — did you mean 'accept'?"),
        (market("scenario x\n"), 3, 4, "`scenario` belongs at the start of a line"),
        (market("after\n"), 3, 9, "Expected a duration"),
        (market("after 5min\n"), 3, 10, "Bad number literal: 5m"),
        (market("after 1s ± \n"), 3, 14, "Expected a duration"),
        (market("after 1s extra\n"), 3, 13, "Unexpected 'extra'"),
        (market("wait\n"), 3, 8, "Expected an event, got the end of the line"),
        (market("wait cancle\n"), 3, 9, "Expected an event, got 'cancle' — did you mean 'cancel"),
        (market("wait cancel or\n"), 3, 18, "Expected an event"),
        (market("expect cancel\n"), 3, 17, "Expected `within DURATION`"),
        (market("expect cancel within 2s else stop\n"), 3, 33, "Expected `fail 'WHY'`"),
        (market("fail\n"), 3, 8, "Expected the reason: fail 'WHY'"),
        (market("let = 1\n"), 3, 8, "Expected a name"),
        (market("let x 1\n"), 3, 10, "Expected `=`"),
        (market("let order = 1\n"), 3, 8, "'order' is the script's own name for something"),
        (market("repeat 3 at fast\n    accept\n"), 3, 16, "Expected a rate such as 10/s"),
        (market("repeat 3 at 0/s\n    accept\n"), 3, 16, "A rate must be more than zero"),
        (market("repeat 3 with n = [1]\n    accept\n"), 3, 18, "'n' is the script's own name"),
        (market("fill qty 100\n"), 3, 13, "Expected `:` after 'qty'"),
        (market("fill qty: , price: 1\n"), 3, 14, "Unexpected token: ','"),
        (market("fill qty: (1 + , price: 1\n"), 3, 19, "Unexpected token: ','"),
        (market("fill using half\n"), 3, 15, "A template is named in quotes"),
        (market("reject text: 'open\n"), 3, 17, "Unterminated string literal"),
        (market("bust trade where\n"), 3, 20, "Expected a condition"),
    ])
    def test_problems_are_placed(self, text, line, col, message):
        found = problems(text)
        assert any(l == line and c == col and message in m for l, c, m in found), found

    def test_every_problem_is_reported_not_the_first(self):
        found = problems(market("acept\nafter\nwait nothing\naccept\n"))
        assert [l for l, _, _ in found] == [3, 4, 5]
        sc, _ = scenario.check(market("acept\naccept\n"))
        assert [s.verb for s in sc.blocks[0].body] == ["accept"], "the good lines still parse"

    def test_a_bad_compound_line_takes_its_body_with_it(self):
        found = problems(market("when nonsense\n    accept\n    fill qty: 1, price: 1\nreject\n"))
        assert [l for l, _, _ in found] == [3]

    def test_an_expression_cannot_run_onto_the_next_line(self):
        found = problems(market("if order.cum_qty > 0 and\n    accept\n"))
        assert (3, 28, "Unexpected end of expression") in found

    def test_empty_text(self):
        assert [m for _, _, m in problems("")] == [
            "A script needs at least one block: `on order`, `on sent order` or `run on SESSION`",
            "A script starts with `scenario NAME`"]


# -- meaning ------------------------------------------------------------------------

class TestChecker:
    @pytest.mark.parametrize("text, line, col, message", [
        (market("new symbol: 'A', side: buy, qty: 1\n"), 3, 4, "`new` belongs in a `run on` block, not an `on order` block"),
        (market("cancel\n"), 3, 4, "`cancel` belongs in a `run on` block or an `on sent order` block"),
        ("scenario t\non sent order\n    accept\n", 3, 4, "`accept` belongs in an `on order` block"),
        ("scenario t\non sent order\n    new symbol: 'A', side: buy, qty: 1\n", 3, 4, "one order per script"),
        ("scenario t\nrun on S\n    cancel\n", 2, 0, "A `run on` block sends its own orders: it needs a `new`"),
        ("scenario t\nrun on S\n    expect ack within 1s\n    repeat 2\n        new symbol: 'A', side: buy, qty: 1\n",
         3, 4, "This line runs before any order exists: move it inside the `repeat` that sends"),
        ("scenario t\nrun on S\n    if n == 0\n        cancel\n    repeat 2\n        new symbol: 'A', side: buy, qty: 1\n",
         4, 8, "This line runs before any order exists"),
        (market("wait ack\n"), 3, 4, "`ack` never happens in an `on order` block. Events there: cancel, dk"),
        ("scenario t\non sent order\n    when replace\n        cancel\n", 3, 4, "`replace` never happens in an `on sent order` block"),
        (market("fill qty: 1\n"), 3, 4, "`fill` needs price"),
        (market("fill\n"), 3, 4, "`fill` needs qty, price"),
        (market("fill qty: 1, price: 2, size: 3\n"), 3, 27, "`fill` has no term 'size'. It takes: qty, price, text, extra"),
        (market("fill qty: 1, price: 2, prise: 3\n"), 3, 27, "did you mean 'price'?"),
        (market("fill qty: 1, qty: 2, price: 3\n"), 3, 17, "'qty' is given twice"),
        (market("bust\n"), 3, 4, "Which trade? `bust last trade`"),
        (market("accept last trade\n"), 3, 11, "`accept` acts on the order, not on a trade"),
        (market("when cancel\n    bust\n"), 4, 8, "Which trade?"),
        (market("when dk or cancel\n    bust\n"), 4, 8, "Which trade?"),
        (market("if order.leave_qty > 0\n    accept\n"), 3, 13, "Unknown field: 'order.leave_qty'. order has:"),
        (market("fill qty: MIN(100, order.leave_qty), price: 1\n"), 3, 29, "Unknown field: 'order.leave_qty'"),
        (market("if COUNT(history, h -> h.pendng_action == 'x') > 0\n    accept\n"), 3, 29, "Unknown field: 'history.*.pendng_action'"),
        (market("if trades[0].last_px > 1\n    accept\n"), 3, 17, "Unknown field: 'trades.*.last_px'"),
        (market("if event.prev.cumqty > 1\n    accept\n"), 3, 18, "Unknown field: 'event.prev.cumqty'"),
        (market("if ordr.cum_qty > 1\n    accept\n"), 3, 7, "Unknown field: 'ordr'. Available fields:"),
        (market("if NOPE(1)\n    accept\n"), 3, 7, "Unknown function: NOPE"),
        (market("reject text: 'qty ${order.qty}'\n"), 3, 17, "In the text's ${…}: Unknown field: 'order.qty'"),
        (market("reject text: 'qty ${1 +}'\n"), 3, 17, "In the text's ${…}:"),
        ("scenario t\non order where symbl == 'IBM'\n    accept\n", 2, 15, "Unknown field: 'symbl'"),
        (market("bust trade where trade.px > 1\n"), 3, 27, "Unknown field: 'trade.px'"),
    ])
    def test_problems(self, text, line, col, message):
        found = problems(text)
        assert any(l == line and c == col and message in m for l, c, m in found), found

    def test_a_trade_verb_under_a_trade_event_needs_no_target(self):
        clean(market("when dk\n    if trade.dk_reason == 'Other'\n        bust\n    else\n        renotify\n"))
        clean("scenario t\non sent order\n    when fill or filled and trade.last_price > 1\n        dk reason: wrong_side\n")

    def test_a_template_may_supply_the_required_terms(self):
        clean(market("fill using 'half'\n"))

    def test_let_and_with_names_are_known_block_wide(self):
        clean(market("let cap = 5\nwhen cancel and order.cum_qty > cap\n    reject\nif cap.anything\n    accept\n"))
        clean("scenario t\nrun on S\n    repeat 2 with sym = ['A']\n        new symbol: sym, side: buy, qty: n + 1\n")
        found = problems("scenario t\non order\n    let cap = 5\n    accept\non order\n    if cap > 1\n        accept\n")
        assert [(l, m.split('.')[0]) for l, _, m in found] == [(6, "Unknown field: 'cap'")], "one block's names are not another's"

    def test_event_tags_and_undescribed_values_pass(self):
        clean("scenario t\non sent order\n    wait er where event.tag['150'] == 'F' and event.tag.39 in ['1', '2']\n    cancel\n")
        clean(market("if order.symbol.anything.at.all\n    accept\n"))

    def test_scenario_only_functions(self):
        clean(market("fill qty: 1, price: TICK(order.price + RANDOM() * 0.02, 0.01)\n"))
        with pytest.raises(expr.ExprError, match="Unknown function: TICK"):
            expr.compile("TICK(1, 1)")

    def test_known_sessions_and_templates_are_checked_when_given(self):
        text = "scenario t\nrun on BROKR\n    new using 'ibm-buy'\n"
        assert problems(text) == []
        found = problems(text, sessions=["BROKER", "OTHER"], templates={"order": ["ibm-buys"], "fill": ["ibm-buy"]})
        assert found == [(2, 0, "No session named 'BROKR' — did you mean 'BROKER'?"),
                         (3, 4, "No order template named 'ibm-buy' — did you mean 'ibm-buys'?")]
        assert problems(text, sessions=["BROKR"], templates={"order": ["ibm-buy"]}) == []

    def test_an_unknown_enum_name_is_a_warning_and_a_code_is_fine(self):
        text = market("restate qty: 1, reason: 'Reprising'\nrestate qty: 1, reason: '3'\nrestate qty: 1, reason: '99'\n")
        sc, diags = scenario.check(text)
        assert [(d.line, d.severity) for d in diags] == [(3, "warning")] and "did you mean 'repricing'" in diags[0].message
        assert scenario.errors(diags) == []

    def test_diagnostics_are_sorted_and_carry_a_span(self):
        _, diags = scenario.check(market("fill qty: 1, size: 2\nacept\n"))
        assert [(d.line, d.col) for d in diags] == sorted((d.line, d.col) for d in diags)
        assert all(d.end > d.col for d in diags)
        assert str(diags[0]).startswith("line 3, col 5: ")


# -- the words ------------------------------------------------------------------------

class TestVocabulary:
    def test_every_verb_is_an_engine_action_with_its_terms(self):
        for verb in vocab.VERBS.values():
            assert verb.op in ACTIONS, verb.name
            keys = set(TEMPLATE_TERMS[verb.op][1]) | {"expire_time"}
            assert set(verb.terms.values()) <= keys, f"{verb.name}: {set(verb.terms.values()) - keys}"
            assert set(verb.required) <= set(verb.terms) and verb.doc and set(verb.sides) <= set(vocab.SIDES)
        assert {v.op for v in vocab.VERBS.values()} == set(TEMPLATE_TERMS), "one verb per dialog"

    def test_every_report_the_engine_names_is_an_event(self):
        named = set(_REPORT_KINDS.values()) | set(_TRANS_KINDS.values()) | {"filled", "er", "cancel rejected", "message"}
        assert named <= set(vocab.EVENTS)
        for kind in named - {"message"}:
            assert vocab.MARKET not in vocab.EVENTS[kind].sides, kind
        assert {"cancel", "replace", "dk"} <= {n for n, e in vocab.EVENTS.items() if e.sides == (vocab.MARKET,)}

    def test_enum_words_are_the_dialogs_options(self):
        app = json.loads((ROOT / "mkfix" / "static" / "app.json").read_text(encoding="utf-8"))
        def options(name):
            found = {}
            def walk(o):
                if isinstance(o, dict):
                    if o.get("name") == name and o.get("options"):
                        found.update({opt["value"]: opt["label"] for opt in o["options"] if opt["value"] != ""})
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
            walk(app)
            return found
        for enum, field in [("side", "side"), ("type", "ord_type"), ("tif", "tif"), ("handl_inst", "handl_inst"),
                            ("dk reason", "dk_reason")]:
            assert set(vocab.ENUMS[enum].values()) == set(options(field)), enum
        assert set(vocab.ENUMS["restate reason"].values()) <= set(options("restate_reason"))

    def test_enum_code(self):
        assert vocab.enum_code("dk reason", "Price exceeds limit") == "E"
        assert vocab.enum_code("dk reason", "price_exceeds_limit") == vocab.enum_code("dk reason", "PRICE-EXCEEDS  LIMIT") == "E"
        assert vocab.enum_code("side", "buy") == "1" and vocab.enum_code("side", "8") == "8" and vocab.enum_code("tif", 3) == "3"

    def test_fields_are_the_tables_columns(self):
        assert {"leaves_qty", "pending_action", "entered_qty", "cxl_rej_reason"} <= set(vocab.ORDER_FIELDS)
        assert {"dk_reason", "dk_text", "last_price", "trade_id"} <= set(vocab.TRADE_FIELDS)
        assert vocab.EVENT_FIELDS["prev"] is vocab.ORDER_FIELDS

    def test_vocabulary_is_plain_data_with_help_for_every_word(self):
        v = json.loads(json.dumps(scenario.vocabulary()))
        assert set(v) == {"statements", "verbs", "events", "trade_targets", "enums", "context", "fields", "functions"}
        for group in ("statements", "verbs", "events"):
            assert all(entry["doc"] for entry in v[group].values()), group
        assert {"TICK", "RANDOM", "COUNT", "MIN"} <= set(v["functions"]) and v["functions"]["TICK"]["doc"]
        assert set(v["context"]) == set(vocab.scope_schema())

    def test_tick_and_random(self):
        run = lambda s, scope=None: expr.compile(s, functions.ENV)(scope or {})
        assert run("TICK(100.1 + 0.2, 0.01)") == 100.3 and run("TICK(101.337, 0.05)") == 101.35
        assert run("TICK(99.999, 1)") == 100 and run("TICK(NULL, 0.01)") is None
        with pytest.raises(expr.ExprError, match="positive"):
            run("TICK(1, 0)")
        import random
        first = run("RANDOM()", {functions.RNG_NAME: random.Random(7)})
        assert first == random.Random(7).random() and 0 <= run("MAP([1], x -> RANDOM())", {functions.RNG_NAME: random.Random(7)})[0] < 1
        assert any("Unknown field: '__rng'" in m for _, _, m in problems(market("log __rng\n"))), \
            "a script cannot name the generator"


# -- the bundled examples ---------------------------------------------------------------

HEADER = re.compile(r"\A# (?P<title>.+)\n#\n(?P<fields>(?:#.*\n)+)\n")


class TestExamples:
    def test_there_are_examples(self):
        assert len(EXAMPLES) >= 9

    @pytest.mark.parametrize("path", EXAMPLES, ids=[p.stem for p in EXAMPLES])
    def test_example_is_clean_and_introduces_itself(self, path):
        text = path.read_text(encoding="utf-8")
        sc, diags = scenario.check(text)
        assert diags == [], [str(d) for d in diags]
        assert sc.name == path.stem, "an example is named for its file"
        m = HEADER.match(text)
        assert m, "an example opens with `# Title`, a bare `#`, then its Shows/Needs/Watch/Outcome lines"
        labels = re.findall(r"^# (\w+):", m.group("fields"), re.M)
        assert labels == ["Shows", "Needs", "Watch", "Outcome"], labels
        assert all(len(line) <= 110 for line in text.splitlines()), "an example reads without scrolling"

    def test_the_market_side_is_covered(self):
        """Comprehensive by construction: every verb, event and statement the
        market side has, every name an expression may use there and both
        scenario functions appear in some example — so a new word fails here
        until an example shows it. The sending side joins when it ships."""
        verbs, events, statements, roots, functions_used, targets = set(), set(), set(), set(), set(), set()
        headers = set()
        for path in EXAMPLES:
            sc, _ = scenario.check(path.read_text(encoding="utf-8"))
            headers |= {"seed"} if sc.seed is not None else set()
            headers |= {"on error"} if sc.on_error == "continue" else set()
            for block in sc.blocks:
                for st in nodes.walk(block.body):
                    statements.add(type(st).__name__)
                    if isinstance(st, If):
                        statements |= {"else"} if st.orelse else set()
                        statements |= {"else if"} if len(st.branches) > 1 else set()
                    if isinstance(st, Action):
                        verbs.add(st.verb)
                        targets |= {st.target.which} if st.target else set()
                        statements |= {"using"} if st.template else set()
                    if isinstance(st, (Wait, Expect, When)):
                        events |= set(st.events)
                    if isinstance(st, After) and st.jitter:
                        statements.add("jitter")
                    for e in nodes.expressions(st):
                        roots |= expr.field_refs(e.node)
                        functions_used |= expr.function_refs(e.node)
                        value = getattr(e.node, "value", None)
                        if e.template:
                            for kind, part in expr.compile_template(value, functions.ENV).parts:
                                roots |= part.field_refs if kind == "expr" else set()
        market_verbs = {v.name for v in vocab.VERBS.values() if vocab.MARKET in v.sides}
        market_events = {e.name for e in vocab.EVENTS.values() if vocab.MARKET in e.sides}
        assert market_verbs - verbs == set()
        assert market_events - events <= {"message", "manual"}, "the two a script seldom needs"
        assert {"After", "Wait", "When", "If", "else", "else if", "While", "Let", "Stop", "Finish", "Log",
                "using", "jitter"} - statements == set()
        assert {"last", "first", "where"} - targets == set()
        assert {"seed", "on error"} - headers == set()
        assert {"order", "trade", "trades", "history", "event"} - roots == set()
        assert {"TICK", "RANDOM", "COUNT", "MIN"} - functions_used == set()
