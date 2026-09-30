"""Macros and multileg orders (0.79): `new multileg` with its `leg` lines or
a strategy — declared with `leg` lines at the top, or saved with legs —
`replace` sending an AC, `fill leg:` and `report_legs:`, `event.leg` and
`legs`; the recorder and a macro from history writing them. The examples
calendar-spread and spread-desk play each other over the linked pair of
tests/test_macro_sending.py, a FIX 4.2 pair: 4.2 carries the multileg
subset of 4.4."""

import json

import pytest

from mkfix import macro
from mkfix.macro.instance import FAILED, PASSED

from tests.test_engine import _fetch_all, stack  # noqa: F401
from tests.test_macro_history import hand, same, written  # noqa: F401
from tests.test_macro_recorder import body
from tests.test_macro_sending import pair  # noqa: F401

LEGS = ("        leg symbol: 'ES', sec_type: future, maturity: '202612', side: sell, price: 5200\n"
        "        leg symbol: 'ES', sec_type: future, maturity: '202703', side: buy, price: 5210\n")
SPREAD = "run\n    new multileg side: buy, qty: 5, price: 10\n" + LEGS
STRATEGY = ("instrument 'ES Dec/Mar' symbol: 'ES'\n"
            "    leg symbol: 'ES', sec_type: future, maturity: '202612', side: sell\n"
            "    leg symbol: 'ES', sec_type: future, maturity: '202703', side: buy, open_close: open\n")
CALENDAR = [{"symbol": "ES", "security_type": "FUT", "maturity": "202612", "side": "2", "leg_price": 5200},
            {"symbol": "ES", "security_type": "FUT", "maturity": "202703", "side": "1", "leg_price": 5210}]


def _errors(text, **kw):
    return [d.message for d in macro.errors(macro.check(text, **kw)[1])]


async def _legs(db, direction):
    return await _fetch_all(db, f"SELECT * FROM fix_order_legs WHERE direction = '{direction}' ORDER BY seq")


class TestTheWords:
    def test_a_spread_and_a_strategy_check_clean(self):
        assert _errors(SPREAD) == []
        assert _errors(STRATEGY + "run\n    new multileg instrument: 'ES Dec/Mar', side: buy, qty: 1\n") == []
        assert _errors("on order where leg_count > 0\n    fill leg: 1, qty: 1\n"
                       "    fill qty: 1, price: 2, report_legs: yes\n") == []

    @pytest.mark.parametrize("text, message", [
        ("run\n    new multileg side: buy, qty: 1\n        leg symbol: 'ES', side: buy\n", "needs its legs"),
        ("run\n    new multileg side: buy, qty: 1\n", "needs its legs"),
        ("run\n    new symbol: 'ES', side: buy, qty: 1\n    leg symbol: 'ES', side: buy\n", "belongs under `new multileg`"),
        ("run\n    new multileg side: buy, qty: 1\n        leg symbol: 'ES'\n        leg symbol: 'ES', side: sell\n",
         "A leg needs side"),
        ("run\n    new multileg side: buy, qty: 1\n        leg side: buy\n        leg symbol: 'ES', side: sell\n",
         "A leg needs symbol"),
        ("run\n    new multileg side: buy, qty: 1\n        leg symbol: 'ES', side: buy, strike_px: 1\n"
         "        leg symbol: 'ES', side: sell\n", "A leg has no term 'strike_px'"),
        ("run\n    new multileg side: buy, qty: 1\n        order symbol: 'ES', side: buy, qty: 1\n", "Only `leg` lines"),
        (STRATEGY + "run\n    new multileg instrument: 'ES Dec/Mar', side: buy, qty: 1\n" + LEGS,
         "from the strategy or from its `leg` lines, not both"),
        (STRATEGY + "run\n    new instrument: 'ES Dec/Mar', side: buy, qty: 1\n", "is a strategy"),
        ("instrument 'ESZ6' symbol: 'ES', sec_type: future\nrun\n    new multileg instrument: 'ESZ6', side: buy, qty: 1\n",
         "is no strategy"),
        ("instrument 'X' symbol: 'ES'\n    leg symbol: 'ES', side: buy\nrun\n    new multileg instrument: 'X', side: buy, qty: 1\n",
         "has one leg"),
        ("instrument 'X' symbol: 'ES'\n    leg symbol: 'ES', side: buy, ratio: 1 + 1\n    leg symbol: 'ES', side: sell\n"
         "run\n    new multileg instrument: 'X', side: buy, qty: 1\n", "written out"),
        ("run\n    new symbol: 'ES', side: buy, qty: 1\n    replace qty: 2\n        leg symbol: 'ES', side: buy\n",
         "two legs or more"),
        ("on order\n    fill qty: 1, report_legs: maybe\n", "maybe"),
    ])
    def test_what_is_refused(self, text, message):
        found = _errors(text) + [d.message for d in macro.check(text)[1]]
        assert any(message in m for m in found), found

    def test_a_saved_strategy_is_known(self):
        saved = {"S": {"symbol": "ES", "security_type": "MLEG", "legs": CALENDAR},
                 "ESZ6": {"symbol": "ES", "security_type": "FUT"}}
        assert _errors("run\n    new multileg instrument: 'S', side: buy, qty: 1\n", instruments=saved) == []
        assert any("is no strategy" in m for m in
                   _errors("run\n    new multileg instrument: 'ESZ6', side: buy, qty: 1\n", instruments=saved))
        assert any("is a strategy" in m for m in
                   _errors("run\n    new instrument: 'S', side: buy, qty: 1\n", instruments=saved))


class TestPlaying:
    @pytest.mark.asyncio
    async def test_the_calendar_and_the_desk(self, pair):
        desk = pair.arm("spread-desk", session="LOOP-MKT")
        client = pair.arm("calendar-spread")
        await pair.advance(40)
        (inst,) = client.instances
        assert (inst.status, inst.message) == (PASSED, "-1 ES Dec26 / +1 ES Jun27 filled at 20"), inst.message
        assert [m for _, _, _, m in client.log if m.startswith("leg ")] == ["leg 1: 5 at 5200", "leg 2: 5 at 5220"]
        sent = [pair.engine._as_sent(pair.cli, m) for m in pair.cli.sent]
        assert [m["35"] for m in sent] == ["AB", "AC"] and sent[0]["563"] == "1"
        for side in ("TX", "RX"):
            assert [(l["maturity"], l["cum_qty"], l["avg_price"]) for l in await _legs(pair.db, side)] == [
                ("202612", 5.0, 5200.0), ("202706", 5.0, 5220.0)]
        assert desk.instances and desk.instances[0].status != FAILED

    @pytest.mark.asyncio
    async def test_legs_only_fills_leg_by_leg(self, pair):
        pair.arm("spread-desk", session="LOOP-MKT")
        client = pair.arm("run\n    new multileg side: buy, qty: 3, price: 10, report: legs_only\n" + LEGS +
                          "    expect ack within 5s\n    after 5s\n"
                          "    pass '${COUNT(legs, l -> l.cum_qty == 3)} ${order.cum_qty}'\n")
        await pair.advance(10)
        assert [i.message for i in client.instances] == ["2 0"], "the legs filled, the order not"
        leg_trades = await _fetch_all(pair.db, "SELECT leg_ref_id, last_qty, last_price FROM fix_executions "
                                               "WHERE direction = 'RX' ORDER BY id")
        assert leg_trades == [{"leg_ref_id": "1", "last_qty": 3.0, "last_price": 5200.0},
                              {"leg_ref_id": "2", "last_qty": 3.0, "last_price": 5210.0}]

    @pytest.mark.asyncio
    async def test_a_saved_strategy_sends_its_legs(self, pair):
        await pair.engine.save_instrument("Cal", legs=CALENDAR)
        client = pair.arm("run\n    new multileg instrument: 'Cal', side: sell, qty: 2, price: -3\n"
                          "    pass '${order.symbol} ${order.legs}'\n")
        await pair.advance(1)
        assert [i.message for i in client.instances] == ["ES -1 ES Dec26 / +1 ES Mar27"]
        assert "|44=-3.0|" in pair.engine._as_sent(pair.cli, pair.cli.sent[-1]).to_pipe_string()

    @pytest.mark.asyncio
    async def test_a_strategy_nobody_has_fails_the_macro(self, pair):
        client = pair.arm("run\n    let s = 'Nope'\n    new multileg instrument: s, side: buy, qty: 1\n")
        await pair.advance(1)
        (inst,) = client.instances
        assert inst.status == FAILED and "no instrument named 'Nope'" in inst.message

    @pytest.mark.asyncio
    async def test_a_leg_fill_is_never_the_orders_filled(self, pair):
        pair.arm("on order\n    accept\n    fill leg: 1, qty: 5\n    fill leg: 2, qty: 5\n", session="LOOP-MKT")
        client = pair.arm(SPREAD + "    wait filled or timeout 3s\n    pass '${event.kind}'\n")
        await pair.advance(5)
        assert [i.message for i in client.instances] == ["timeout"]

    @pytest.mark.asyncio
    async def test_a_macro_that_ends_on_a_fill_its_handler_also_heard_settles(self, pair):
        """The event that ends the macro can wake a `when` handler too: the
        handler, cancelled before it ran, must not keep the run busy."""
        pair.arm("on order\n    accept\n    fill qty: 5, price: 10\n", session="LOOP-MKT")
        client = pair.arm(SPREAD + "    when fill\n        log 'heard'\n    expect filled within 5s\n    pass\n")
        await pair.advance(2)
        await pair.runner.settle()
        assert [i.status for i in client.instances] == [PASSED]


class TestWritingItDown:
    @pytest.mark.asyncio
    async def test_a_multileg_order_worked_by_hand(self, hand):
        async def work():
            result = await hand.do("send_new_multileg", session_id="LOOP-CLI", legs=json.dumps(CALENDAR), side="1",
                                   qty=5, price=10, rpt_type="1")
            cl = result["cl_ord_id"]
            await hand.market("accept_request", cl, wait=1)
            new_legs = [CALENDAR[0], {**CALENDAR[1], "maturity": "202706"}]
            await hand.do("send_cancel_replace", 1, session_id="LOOP-CLI", orig_cl_ord_id=cl, symbol="ES", side="1",
                          qty=5, price=12, legs=json.dumps(new_legs))
            await hand.market("accept_request", cl, wait=0.5)
            renamed = (await hand.pair.orders("RX"))[0]["cl_ord_id"]
            await hand.market("fill_order", renamed, wait=1, qty=5, price=12)
            await hand.market("fill_order", renamed, wait=0.5, qty=5, leg="1")
        client = await same(hand, "client", work)
        lines = body(client["source"])
        assert lines[:4] == [
            "run",
            "    new multileg symbol: 'ES', side: buy, qty: 5, type: limit, price: 10, tif: day, report: multileg_and_legs",
            "        leg symbol: 'ES', sec_type: future, maturity: '202612', side: sell, price: 5200",
            "        leg symbol: 'ES', sec_type: future, maturity: '202703', side: buy, price: 5210"], lines
        at = lines.index("    replace price: 12")
        assert lines[at + 1:at + 3] == [
            "        leg symbol: 'ES', sec_type: future, maturity: '202612', side: sell, price: 5200",
            "        leg symbol: 'ES', sec_type: future, maturity: '202706', side: buy, price: 5210"]
        assert "filled" in " ".join(lines) and lines.count("    expect fill within 5s") >= 1

    @pytest.mark.asyncio
    async def test_the_market_writes_its_leg_fills(self, hand):
        result = await hand.do("send_new_multileg", session_id="LOOP-CLI", legs=json.dumps(CALENDAR), side="1",
                               qty=5, price=10)
        cl = result["cl_ord_id"]
        await hand.market("accept_request", cl, wait=1)
        await hand.market("fill_order", cl, wait=1, qty=5, price=10, report_legs="Y")
        recorded = await written(hand, "market")
        lines = body(recorded["source"])
        assert lines[:2] == ["on order where symbol == 'ES'", "    accept"]
        assert "    fill leg: 1, qty: 5, price: 5200" in lines and "    fill leg: 2, qty: 5, price: 5210" in lines
        assert macro.check(recorded["source"], side="market")[1] == []

    @pytest.mark.asyncio
    async def test_legs_that_are_a_saved_strategy_name_it(self, hand):
        await hand.engine.save_instrument("Cal", legs=CALENDAR)

        async def work():
            await hand.do("send_new_multileg", session_id="LOOP-CLI", legs=json.dumps(CALENDAR), side="1", qty=1,
                          price=10)
        result = await same(hand, "client", work)
        lines = result["source"].splitlines()
        assert "instrument 'Cal' symbol: 'ES'" in lines
        assert "    leg symbol: 'ES', sec_type: future, maturity: '202612', side: sell, price: 5200" in lines
        new = next(line.strip() for line in lines if line.strip().startswith("new multileg"))
        assert new.startswith("new multileg instrument: 'Cal', side: buy, qty: 1")
        assert macro.check(result["source"], side="client")[1] == []
