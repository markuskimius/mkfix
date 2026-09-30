"""Multileg orders (0.79): NewOrderMultileg (35=AB) with its legs in
NoLegs(555), MultilegOrderCancelReplace (35=AC), and the two kinds of
report — the whole order's (442=3) and a leg's (442=2) — on FIX 4.1, 4.2
(which carry the multileg subset of 4.4, tools/overlays) and 4.4, over the
linked pair of tests/test_instruments.py."""

import json

import pytest

from mkfix.fix import multileg
from mkfix.fix.dictionary import FixDictionary

from tests.test_engine import _fetch_all, stack  # noqa: F401
from tests.test_instruments import linked  # noqa: F401

CALENDAR = [{"symbol": "ES", "security_type": "FUT", "maturity": "202612", "side": "2", "ratio": 1,
             "position_effect": "C"},
            {"symbol": "ES", "security_type": "FUT", "maturity": "202703", "side": "1", "ratio": 1,
             "position_effect": "O"}]
CALL_SPREAD = [{"symbol": "AAPL", "security_type": "OPT", "maturity": "20261218", "strike_price": 250,
                "put_or_call": "Call", "side": "1", "leg_price": 12.5},
               {"symbol": "AAPL", "security_type": "OPT", "maturity": "20261218", "strike_price": 260,
                "put_or_call": "Call", "side": "2", "leg_price": 8.25}]
VERSIONS = ["FIX.4.1", "FIX.4.2", "FIX.4.4"]


async def _orders(db):
    return {o["direction"]: o for o in await _fetch_all(db, "SELECT * FROM fix_orders")}


async def _legs(db, direction):
    return await _fetch_all(db, f"SELECT * FROM fix_order_legs WHERE direction = '{direction}' ORDER BY seq")


async def _trades(db, direction):
    return await _fetch_all(db, f"SELECT * FROM fix_executions WHERE direction = '{direction}' ORDER BY id")


async def _spread(engine, legs=CALL_SPREAD, **kw):
    return await engine.send_new_multileg("Client", legs, "1", 10, price=kw.pop("price", 4.25), **kw)


class TestPure:
    def test_a_leg_as_typed(self):
        leg = multileg.normalize_leg({"symbol": " AAPL ", "security_type": "opt", "put_or_call": "c",
                                      "strike_price": "250", "side": "Sell", "ratio": "2",
                                      "open_close": "Open"}, 3)
        assert (leg["symbol"], leg["security_type"], leg["put_or_call"], leg["strike_price"], leg["side_code"],
                leg["side"], leg["ratio"], leg["position_effect"], leg["leg_ref_id"]) == \
            ("AAPL", "OPT", "Call", 250.0, "2", "Sell", 2.0, "Open", "3")

    @pytest.mark.parametrize("legs, message", [
        ([CALENDAR[0]], "two legs or more"),
        ([CALENDAR[0], {**CALENDAR[1], "symbol": ""}], "Leg 2 has no symbol"),
        ([{**CALENDAR[0], "leg_ref_id": "A"}, {**CALENDAR[1], "leg_ref_id": "A"}], "its own LegRefID"),
        ([CALENDAR[0], {**CALENDAR[1], "ratio": -1}], "positive"),
    ])
    def test_what_is_refused(self, legs, message):
        with pytest.raises(ValueError, match=message):
            multileg.check_legs(multileg.normalize_legs(legs))

    def test_the_grid_gives_json_and_empty_rows_go(self):
        grid = json.dumps([CALENDAR[0], {"symbol": "", "side": ""}, CALENDAR[1]])
        assert [leg["maturity"] for leg in multileg.normalize_legs(grid)] == ["202612", "202703"]

    def test_the_display_signs_each_ratio_by_side(self):
        assert multileg.legs_text(multileg.normalize_legs(CALENDAR)) == "-1 ES Dec26 / +1 ES Mar27"

    @pytest.mark.parametrize("version", ["FIX.4.2", "FIX.4.4", "FIX.5.0SP2"])
    def test_a_legs_put_or_call(self, version):
        """No LegPutOrCall before 5.0 SP1: the CFI code carries it."""
        pairs = multileg.leg_pairs(FixDictionary(version), multileg.normalize_leg(CALL_SPREAD[0], 1))
        if version == "FIX.5.0SP2":
            assert ("1358", "1") in pairs and not any(t == "608" for t, _ in pairs)
        else:
            assert ("608", "OCXXXX") in pairs and not any(t == "1358" for t, _ in pairs)

    @pytest.mark.parametrize("version", VERSIONS)
    def test_the_wire_reads_back(self, version):
        d = FixDictionary(version)
        legs = multileg.normalize_legs(CALL_SPREAD)
        from mkfix.fix.message import parse_fix
        pairs = multileg.group_pairs(d, legs)
        msg = parse_fix("8=FIX.4.2|35=AB|11=X|" + "|".join(f"{t}={v}" for t, v in pairs) + "|10=000")
        assert [{k: v for k, v in leg.items() if k != "leg_last_px"} for leg in multileg.legs_of(msg, d)] == legs

    def test_only_where_ab_is(self):
        assert [v for v in ("FIX.4.0", "FIX.4.1", "FIX.4.2", "FIX.4.3", "FIX.4.4", "FIX.5.0SP2")
                if multileg.supports(FixDictionary(v))] == ["FIX.4.1", "FIX.4.2", "FIX.4.3", "FIX.4.4",
                                                             "FIX.5.0SP2"]


class TestSending:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("linked", VERSIONS, indirect=True)
    async def test_an_ab_and_both_sides_rows(self, linked):
        db, engine, cli, mkt = linked
        cl = await _spread(engine, rpt_type="1", extra_tags="9001=x")
        wire = cli.wire[-1]
        assert "|35=AB|" in wire and "|55=AAPL|167=MLEG|54=1|" in wire and "|563=1|" in wire
        assert "|555=2|600=AAPL|608=OCXXXX|609=OPT|610=202612|611=20261218|612=250|623=1|624=1|654=1|566=12.5|" \
            "600=AAPL|608=OCXXXX|609=OPT|610=202612|611=20261218|612=260|623=1|624=2|654=2|566=8.25|9001=x|" in wire
        orders = await _orders(db)
        for side in ("TX", "RX"):
            o = orders[side]
            assert (o["cl_ord_id"], o["leg_count"], o["security_type"], o["multileg_rpt_type"]) == (cl, 2, "MLEG", "1")
            assert o["legs"] == "+1 AAPL 18Dec26 250 C / -1 AAPL 18Dec26 260 C"
            legs = await _legs(db, side)
            assert [(l["seq"], l["leg_ref_id"], l["strike_price"], l["put_or_call"], l["side"], l["leg_qty"],
                     l["leg_price"], l["order_id"]) for l in legs] == [
                (1, "1", 250.0, "Call", "Buy", 10.0, 12.5, o["order_id"]),
                (2, "2", 260.0, "Call", "Sell", 10.0, 8.25, o["order_id"])]
        assert orders["RX"]["extra_tags"] == "9001=x", "the legs are columns, not echoed extras"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("linked", ["FIX.4.0"], indirect=True)
    async def test_refused_where_there_is_no_ab(self, linked):
        db, engine, cli, mkt = linked
        with pytest.raises(ValueError, match="no NewOrderMultileg"):
            await _spread(engine)
        assert cli.wire == [] and await _fetch_all(db, "SELECT * FROM fix_orders") == []

    @pytest.mark.asyncio
    async def test_the_ratio_sets_each_legs_quantity(self, linked):
        db, engine, cli, mkt = linked
        await engine.send_new_multileg("Client", [CALENDAR[0], {**CALENDAR[1], "ratio": 2}], "1", 5, price=1)
        assert [l["leg_qty"] for l in await _legs(db, "TX")] == [5.0, 10.0]
        assert "|623=2|" in cli.wire[-1]

    @pytest.mark.asyncio
    async def test_a_negative_net_price(self, linked):
        db, engine, cli, mkt = linked
        await _spread(engine, price=-1.5)
        assert "|44=-1.5|" in cli.wire[-1]
        assert (await _orders(db))["RX"]["price"] == -1.5

    @pytest.mark.asyncio
    async def test_a_list_order(self, linked):
        db, engine, cli, mkt = linked
        await _spread(engine, list_id="LI1")
        assert "|66=LI1|" in cli.wire[-1]
        assert {(o["list_id"], o["direction"]) for o in await _fetch_all(db, "SELECT * FROM fix_orders")} == {
            ("LI1", "TX"), ("LI1", "RX")}


class TestReports:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("linked", VERSIONS, indirect=True)
    async def test_the_orders_reports_say_442_3(self, linked):
        db, engine, cli, mkt = linked
        cl = await _spread(engine)
        await engine.accept_order("Server", cl)
        assert "|442=3|" in mkt.wire[-1] and "|167=MLEG|" in mkt.wire[-1]
        await engine.fill_order("Server", cl, 4, 4.25)
        assert "|442=3|" in mkt.wire[-1]
        orders = await _orders(db)
        assert (orders["TX"]["status"], orders["TX"]["cum_qty"]) == ("PartiallyFilled", 4.0)
        assert all(l["cum_qty"] == 0 for l in await _legs(db, "TX")), "the order's fill is not the legs'"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("linked", VERSIONS, indirect=True)
    async def test_the_order_filled_with_its_legs_reported(self, linked):
        db, engine, cli, mkt = linked
        cl = await _spread(engine)
        await engine.accept_order("Server", cl)
        await engine.fill_multileg("Server", cl, 10, 4.25, report_legs=True)
        whole, first, second = mkt.wire[-3:]
        assert "|442=3|" in whole and "|32=10|31=4.25|" in whole
        for report, (strike, price, ref) in ((first, ("250", "12.5", "1")), (second, ("260", "8.25", "2"))):
            assert "|442=2|" in report and f"|202={strike}|" in report
            assert f"|555=1|600=AAPL|654={ref}|637={price}|" in report and f"|31={price}|" in report
        orders = await _orders(db)
        assert (orders["TX"]["status"], orders["TX"]["cum_qty"], orders["TX"]["last_qty"]) == ("Filled", 10.0, 10.0)
        for side in ("TX", "RX"):
            assert [(l["cum_qty"], l["avg_price"], l["last_price"]) for l in await _legs(db, side)] == [
                (10.0, 12.5, 12.5), (10.0, 8.25, 8.25)]
        received = await _trades(db, "RX")
        assert [(t["leg_ref_id"], t["symbol"], t["last_qty"], t["last_price"], t["security_type"]) for t in received] == [
            ("", "AAPL", 10.0, 4.25, "MLEG"), ("1", "AAPL", 10.0, 12.5, "OPT"), ("2", "AAPL", 10.0, 8.25, "OPT")]
        assert [(t["leg_ref_id"], t["strike_price"]) for t in await _trades(db, "TX")] == [
            ("", None), ("1", 250.0), ("2", 260.0)]

    @pytest.mark.asyncio
    async def test_one_leg_alone(self, linked):
        db, engine, cli, mkt = linked
        cl = await _spread(engine, CALENDAR)
        seen = []
        engine.events.subscribe(lambda ev: seen.append((ev.kinds, ev.detail.get("leg"))) if ev.session_id == "Client" else None)
        await engine.fill_multileg("Server", cl, 3, 5200.25, leg="2")
        assert "|442=2|" in mkt.wire[-1] and "|54=1|" in mkt.wire[-1] and "|200=202703|" in mkt.wire[-1]
        assert [(l["seq"], l["cum_qty"]) for l in await _legs(db, "TX")] == [(1, 0.0), (2, 3.0)]
        orders = await _orders(db)
        assert (orders["TX"]["cum_qty"], orders["RX"]["cum_qty"]) == (0.0, 0.0), "the order does not move"
        assert orders["RX"]["pending_action"] == "", "the leg's report acknowledged the order"
        assert (("fill", "er"), "2") in seen and not any("filled" in k for k, _ in seen)

    @pytest.mark.asyncio
    async def test_a_leg_by_its_place_and_its_leg_price(self, linked):
        db, engine, cli, mkt = linked
        cl = await _spread(engine)
        await engine.fill_leg("Server", cl, 1, 2)
        assert "|31=12.5|" in mkt.wire[-1]
        with pytest.raises(ValueError, match="has no leg 3"):
            await engine.fill_leg("Server", cl, 3, 1, 1)
        plain = await engine.send_new_order("Client", "IBM", "1", 1, price=1)
        with pytest.raises(ValueError, match="not a multileg order"):
            await engine.fill_leg("Server", plain, 1, 1, 1)

    @pytest.mark.asyncio
    async def test_a_leg_report_for_nothing_we_know(self, linked):
        db, engine, cli, mkt = linked
        from mkfix.fix.message import parse_fix
        await engine.on_app_message(cli, "8", parse_fix(
            "8=FIX.4.4|35=8|37=X|11=NOPE|17=E1|150=F|39=1|55=AAPL|54=1|38=1|32=1|31=1|14=1|6=1|151=0|442=2|10=000"))
        assert await _trades(db, "RX") == []


class TestReplace:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("linked", VERSIONS, indirect=True)
    async def test_an_ac_accepted_moves_the_legs_on_both_sides(self, linked):
        db, engine, cli, mkt = linked
        cl = await _spread(engine)
        await engine.fill_leg("Server", cl, 1, 2)
        new_legs = [CALL_SPREAD[0], {**CALL_SPREAD[1], "strike_price": 270}]
        cl2 = await engine.send_cancel_replace("Client", cl, "AAPL", "1", 12, price=4.0, legs=new_legs)
        assert "|35=AC|" in cli.wire[-1] and f"|41={cl}|" in cli.wire[-1] and "|612=270|" in cli.wire[-1]
        assert "|167=MLEG|" in cli.wire[-1]
        rx = (await _orders(db))["RX"]
        assert rx["pending_action"] == "Replace" and "270" in rx["pending_legs"]
        assert [l["strike_price"] for l in await _legs(db, "RX")] == [250.0, 260.0], "parked until accepted"
        await engine.accept_replace("Server", cl)
        orders = await _orders(db)
        for side in ("TX", "RX"):
            o = orders[side]
            assert (o["cl_ord_id"], o["order_qty"], o["legs"]) == (
                cl2, 12.0, "+1 AAPL 18Dec26 250 C / -1 AAPL 18Dec26 270 C")
            legs = await _legs(db, side)
            assert [(l["strike_price"], l["leg_qty"], l["cum_qty"], l["cl_ord_id"]) for l in legs] == [
                (250.0, 12.0, 2.0, cl2), (270.0, 12.0, 0.0, cl2)], "a leg of the same place keeps its fills"
        assert orders["RX"]["pending_legs"] == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("linked", ["FIX.4.1", "FIX.4.2"], indirect=True)
    async def test_an_ac_refused(self, linked):
        db, engine, cli, mkt = linked
        cl = await _spread(engine)
        await engine.send_cancel_replace("Client", cl, "AAPL", "1", 12, price=4.0,
                                         legs=[CALL_SPREAD[0], {**CALL_SPREAD[1], "strike_price": 270}])
        await engine.reject_cancel("Server", cl, text="no")
        assert "|35=9|" in mkt.wire[-1]
        assert ("|434=2|" in mkt.wire[-1]) == (cli.dictionary.version == "FIX.4.2"), "434 from 4.2"
        for side in ("TX", "RX"):
            assert [l["strike_price"] for l in await _legs(db, side)] == [250.0, 260.0]
        assert (await _orders(db))["TX"]["legs"].endswith("260 C")

    @pytest.mark.asyncio
    async def test_the_legs_it_has_when_none_are_given(self, linked):
        db, engine, cli, mkt = linked
        cl = await _spread(engine)
        await engine.send_cancel_replace("Client", cl, "AAPL", "1", 20, price=4.0)
        assert "|35=AC|" in cli.wire[-1] and "|612=250|" in cli.wire[-1] and "|612=260|" in cli.wire[-1]

    @pytest.mark.asyncio
    async def test_legs_on_a_plain_order_are_refused(self, linked):
        db, engine, cli, mkt = linked
        cl = await engine.send_new_order("Client", "IBM", "1", 1, price=1)
        with pytest.raises(ValueError, match="not a multileg order"):
            await engine.send_cancel_replace("Client", cl, "IBM", "1", 2, price=1, legs=CALENDAR)

    @pytest.mark.asyncio
    async def test_a_cancel_is_an_order_cancel_request(self, linked):
        db, engine, cli, mkt = linked
        cl = await _spread(engine)
        await engine.send_cancel("Client", cl, "AAPL", "1", 10)
        assert "|35=F|" in cli.wire[-1] and "|555=" not in cli.wire[-1]
        await engine.accept_cancel("Server", cl)
        assert {o["status"] for o in (await _orders(db)).values()} == {"Canceled"}


class TestStrategies:
    @pytest.mark.asyncio
    async def test_a_saved_strategy(self, linked):
        db, engine, cli, mkt = linked
        await engine.save_instrument("ES Dec/Mar", legs=CALENDAR, description="calendar")
        (row,) = await _fetch_all(db, "SELECT * FROM fix_instruments")
        assert (row["symbol"], row["security_type"], row["instrument"]) == ("ES", "MLEG", "-1 ES Dec26 / +1 ES Mar27")
        legs = json.loads(row["legs"])
        assert [(l["maturity"], l["side"], l["position_effect"]) for l in legs] == [
            ("202612", "2", "C"), ("202703", "1", "O")], "the New Multileg grid's codes"
        await engine.send_new_multileg("Client", row["legs"], "1", 1, price=2)
        assert [l["maturity"] for l in await _legs(db, "RX")] == ["202612", "202703"], "the saved legs send"

    @pytest.mark.asyncio
    async def test_a_strategy_of_one_leg_is_refused(self, linked):
        db, engine, cli, mkt = linked
        with pytest.raises(ValueError, match="two legs"):
            await engine.save_instrument("X", legs=[CALENDAR[0]])
