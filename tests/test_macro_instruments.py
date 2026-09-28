"""Macros and instruments (0.75): the instrument terms on `new`, a named
instrument — declared at the top of the macro or saved in Config ›
Instruments — and what the recorder and a macro from history write for an
order in one. The examples futures-roll and derivatives-desk play each
other over the linked pair of tests/test_macro_sending.py."""

import pytest

from mkfix import macro
from mkfix.macro.instance import FAILED, PASSED
from mkfix.macro.store import EXAMPLES

from tests.test_engine import _fetch_all, stack  # noqa: F401
from tests.test_macro_history import hand, same, written  # noqa: F401
from tests.test_macro_sending import pair  # noqa: F401

ESZ6 = "instrument 'ESZ6' symbol: 'ES', sec_type: future, maturity: '202612', multiplier: 50\n"


def _messages(diags):
    return [d.message for d in diags]


class TestTheWords:
    def test_a_declaration_and_a_named_instrument_check_clean(self):
        sc, diags = macro.check(ESZ6 + "run\n    new instrument: 'ESZ6', side: buy, qty: 2, open_close: open\n")
        assert diags == [], "the instrument brings the symbol `new` needs"
        (decl,) = sc.instruments
        assert decl.name == "ESZ6" and [t.name for t in decl.terms] == ["symbol", "sec_type", "maturity", "multiplier"]

    def test_the_instrument_terms_inline(self):
        _, diags = macro.check("run\n    new symbol: 'AAPL', sec_type: option, maturity: '20261218', strike: 250, "
                               "put_call: call, covered: covered, id_source: isin, side: buy, qty: 1\n")
        assert diags == []

    @pytest.mark.parametrize("text, message", [
        ("run\n    new instrument: 'NOPE', side: buy, qty: 1\n", "No instrument named 'NOPE'"),
        ("run\n    new instrument: 7, side: buy, qty: 1\n", "An instrument is named in quotes"),
        (ESZ6 + ESZ6 + "run\n    new instrument: 'ESZ6', side: buy, qty: 1\n", "declared twice"),
        ("instrument 'X' sec_type: future\nrun\n    new instrument: 'X', side: buy, qty: 1\n", "needs its symbol"),
        ("instrument 'X' symbol: 'ES', strike: 1 + 2\nrun\n    new instrument: 'X', side: buy, qty: 1\n",
         "written out"),
        ("instrument 'X' symbol: 'ES', open_close: open\nrun\n    new instrument: 'X', side: buy, qty: 1\n",
         "An instrument has no term 'open_close'"),
        ("run\n    new symbol: 'ES', side: buy, qty: 1\n" + ESZ6, "belongs before the first block"),
        ("run\n    new symbol: 'ES', side: buy, qty: 1\n    replace sec_type: option\n", "`replace` has no term 'sec_type'"),
    ])
    def test_what_is_refused(self, text, message):
        _, diags = macro.check(text, instruments={})
        assert any(message in m for m in _messages(macro.errors(diags))), _messages(diags)

    def test_a_saved_name_is_known_and_a_different_one_warned_about(self):
        _, diags = macro.check("run\n    new instrument: 'SAVED', side: buy, qty: 1\n",
                               instruments={"SAVED": {"symbol": "ES", "security_type": "FUT"}})
        assert diags == []
        _, diags = macro.check(ESZ6 + "run\n    new instrument: 'ESZ6', side: buy, qty: 1\n",
                               instruments={"ESZ6": {"symbol": "ES", "security_type": "FUT", "maturity": "202703"}})
        assert [(d.severity, d.message) for d in diags] == [
            ("warning", "Config › Instruments saves a different 'ESZ6'; this macro uses its own")]
        _, diags = macro.check(ESZ6 + "run\n    new instrument: 'ESZ6', side: buy, qty: 1\n", instruments={
            "ESZ6": {"symbol": "ES", "security_type": "FUT", "maturity": "202612", "multiplier": 50.0}})
        assert diags == [], "the same one is no news"

    def test_a_computed_name_is_left_to_the_run(self):
        _, diags = macro.check("run\n    let which = 'ESZ6'\n    new instrument: which, side: buy, qty: 1\n",
                               instruments={})
        assert diags == []


class TestPlaying:
    @pytest.mark.asyncio
    async def test_the_roll_and_the_desk(self, pair):
        desk = pair.arm("derivatives-desk", session="LOOP-MKT")
        roll = pair.arm("futures-roll")
        await pair.advance(5)
        sent = await pair.orders("TX")
        assert [(o["instrument"], o["side"], o["open_close"], o["status"], o["cum_qty"]) for o in sent] == [
            ("ES Dec26", "Sell", "Close", "Filled", 5.0), ("ES Mar27", "Buy", "Open", "Filled", 5.0)]
        received = await pair.orders("RX")
        assert [(o["security_type"], o["maturity"], o["multiplier"]) for o in received] == [
            ("FUT", "202612", 50.0), ("FUT", "202703", 50.0)]
        assert [(i.status, i.message) for i in roll.instances] == [
            (PASSED, "closed 5 ES Dec26"), (PASSED, "opened 5 ES Mar27")]
        assert [i.message for i in desk.instances] == ["filled 5 ES Dec26", "filled 5 ES Mar27"]
        trades = await pair.trades("RX")
        assert [t["instrument"] for t in trades] == ["ES Dec26", "ES Mar27"]

    @pytest.mark.asyncio
    async def test_the_desk_writes_no_uncovered_puts(self, pair):
        desk = pair.arm("derivatives-desk", session="LOOP-MKT")
        client = pair.arm("run\n    new symbol: 'AAPL', sec_type: option, maturity: '20261218', strike: 250, "
                          "put_call: put, covered: uncovered, side: sell, qty: 1, price: 3\n"
                          "    expect rejected within 2s\n    pass '${order.text}'\n")
        await pair.advance(2)
        assert [(i.status, i.message) for i in client.instances] == [(PASSED, "this desk writes no uncovered puts")]
        assert [i.message for i in desk.instances] == ["rejected AAPL 18Dec26 250 P: uncovered put"]

    @pytest.mark.asyncio
    async def test_a_saved_instrument_and_terms_beside_it(self, pair):
        await pair.engine.save_instrument("ESZ6", symbol="ES", security_type="FUT", maturity="202612")
        client = pair.arm("run\n    new instrument: 'ESZ6', maturity: '202703', side: buy, qty: 1, price: 5\n"
                          "    pass '${order.instrument}'\n")
        await pair.advance(1)
        assert [i.message for i in client.instances] == ["ES Mar27"], "the term written out wins"

    @pytest.mark.asyncio
    async def test_an_instrument_nobody_has_fails_the_macro(self, pair):
        client = pair.arm("run\n    let which = 'GONE'\n    new instrument: which, side: buy, qty: 1, price: 5\n")
        await pair.advance(1)
        (instance,) = client.instances
        assert instance.status == FAILED and "no instrument named 'GONE'" in instance.message
        assert await pair.orders("TX") == []

    @pytest.mark.asyncio
    async def test_option_on_future_is_refused_before_50(self, pair):
        client = pair.arm("run\n    new symbol: 'ES', sec_type: option_on_future, side: buy, qty: 1, price: 5\n")
        await pair.advance(1)
        (instance,) = client.instances
        assert instance.status == FAILED and "OOF is not defined by FIX.4.2" in instance.message


class TestWritingItDown:
    @pytest.mark.asyncio
    async def test_an_order_in_a_saved_instrument_names_it(self, hand):
        await hand.engine.save_instrument("ESZ6", symbol="ES", security_type="FUT", maturity="202612",
                                          multiplier="50")
        cl = await hand.new(symbol="ES", qty=2, price=5200, security_type="FUT", maturity="202612",
                            multiplier="50", open_close="C")

        result = await written(hand, "client")
        lines = result["source"].splitlines()
        assert "instrument 'ESZ6' symbol: 'ES', sec_type: future, maturity: '202612', multiplier: 50" in lines
        new = next(line.strip() for line in lines if line.strip().startswith("new "))
        assert new == "new instrument: 'ESZ6', side: buy, qty: 2, type: limit, price: 5200, tif: day, open_close: close"
        assert macro.check(result["source"], side="client")[1] == []
        assert cl

    @pytest.mark.asyncio
    async def test_one_saved_nowhere_is_written_out(self, hand):
        async def work():
            await hand.new(symbol="AAPL", qty=1, price=4, security_type="OPT", maturity="20261218",
                           strike_price="250", put_or_call="1")
        result = await same(hand, "client", work)
        new = next(line.strip() for line in result["source"].splitlines() if line.strip().startswith("new "))
        assert new == ("new symbol: 'AAPL', side: buy, qty: 1, type: limit, price: 4, tif: day, sec_type: option, "
                       "maturity: '20261218', strike: 250, put_call: call")
        assert "instrument '" not in result["source"] and "extra" not in new, "nothing left over as extra tags"


class TestTheFamilies:
    """0.76: the sending verbs of the families take the instrument too."""

    @pytest.mark.asyncio
    async def test_option_quotes_plays_both_sides(self, pair):
        text = (EXAMPLES / "option-quotes.macro").read_text(encoding="utf-8")
        run = pair.runner.arm(macro.check(text)[0], session="LOOP-CLI", market_session="LOOP-MKT")
        await pair.advance(5)
        messages = sorted(i.message for i in run.instances)
        assert messages == sorted([f"the call was taken: {(await pair.orders('TX'))[0]['cl_ord_id']}",
                                   "the put was withdrawn", "took AAPL 18Dec26 250 C at 4.3"]), messages
        assert all(i.status == PASSED for i in run.instances)
        quotes = await _fetch_all(pair.db, "SELECT instrument, status FROM fix_rfqs WHERE direction = 'TX' ORDER BY id")
        assert [(q["instrument"], q["status"]) for q in quotes] == [
            ("AAPL 18Dec26 250 C", "Hit"), ("AAPL 18Dec26 240 P", "Canceled")], "one row a series"
        (order,) = await pair.orders("RX")
        assert (order["instrument"], order["ord_type"]) == ("AAPL 18Dec26 250 C", "PreviouslyQuoted")

    @pytest.mark.asyncio
    async def test_an_ioi_in_a_named_instrument(self, pair):
        client = pair.arm(ESZ6 + "run\n    ioi instrument: 'ESZ6', side: buy, qty: 'L'\n    pass '${ioi.instrument}'\n",
                          session="LOOP-MKT")
        await pair.advance(1)
        assert [i.message for i in client.instances] == ["ES Dec26"]
        received = await _fetch_all(pair.db, "SELECT instrument FROM fix_iois WHERE direction = 'RX'")
        assert [r["instrument"] for r in received] == ["ES Dec26"]

    def test_replacing_a_family_keeps_its_instrument(self):
        _, diags = macro.check("run\n    ioi symbol: 'ES', side: buy, qty: 'L'\n    replace ioi strike: 1\n")
        assert any("`replace ioi` has no term 'strike'" in d.message for d in macro.errors(diags))

    @pytest.mark.asyncio
    async def test_history_names_a_saved_instrument_on_an_ioi(self, hand):
        await hand.engine.save_instrument("ESZ6", symbol="ES", security_type="FUT", maturity="202612")
        await hand.do("send_ioi", session_id="LOOP-MKT", symbol="ES", side="1", qty="L", security_type="FUT",
                      maturity="202612")
        result = await written(hand, "market", "ioi")
        lines = result["source"].splitlines()
        assert "instrument 'ESZ6' symbol: 'ES', sec_type: future, maturity: '202612'" in lines
        ioi = next(line.strip() for line in lines if line.strip().startswith("ioi "))
        assert ioi.startswith("ioi instrument: 'ESZ6', side: buy, qty: 'L'"), ioi
        assert macro.check(result["source"], side="market")[1] == []
