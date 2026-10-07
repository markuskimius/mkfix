"""Lists (0.77): a basket of orders under one ListID, sent as one
NewOrderList (E) or as orders each carrying the ListID (D), received as a
list either way, and answered order by order with ListStatus speaking for
the list. Run over the linked pair of tests/test_instruments.py."""

import json

import pytest

from mkfix.fix import lists
from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.message import FixMessageFactory, parse_fix

from tests.test_engine import _fetch_all, stack  # noqa: F401
from tests.test_instruments import FUTURE, linked  # noqa: F401

BASKET = [
    {"symbol": "IBM", "side": "1", "qty": "100", "ord_type": "2", "price": "10"},
    {"symbol": "MSFT", "side": "2", "qty": "200", "ord_type": "2", "price": "20"},
]


async def _lists(db, direction=None):
    where = f"WHERE direction = '{direction}'" if direction else ""
    return await _fetch_all(db, f"SELECT * FROM fix_lists {where} ORDER BY id")


async def _orders(db, direction):
    return await _fetch_all(db, f"SELECT * FROM fix_orders WHERE direction = '{direction}' ORDER BY list_seq_no, id")


def _sent(end, msg_type):
    return [w for w in end.wire if f"|35={msg_type}|" in w]


# ── The pure part ──────────────────────────────────────────────────────

class TestMessages:
    def test_members_of_a_new_order_list(self):
        msg = parse_fix("8=FIX.4.4|35=E|66=L1|394=3|68=2|73=2|11=C1|67=1|55=IBM|54=1|38=100|40=2|44=10"
                        "|11=C2|67=2|55=MSFT|167=OPT|54=2|38=5|40=1|")
        members = lists.list_members(msg)
        assert [dict(m)["11"] for m in members] == ["C1", "C2"]
        assert dict(members[1])["167"] == "OPT"
        d = lists.member_message(msg, members[0])
        assert (d["35"], d["66"], d["11"], d["55"], d["67"]) == ("D", "L1", "C1", "IBM", "1")

    def test_one_order_in_the_body_before_42(self):
        msg = parse_fix("8=FIX.4.1|35=E|66=L1|67=1|68=3|11=C1|55=IBM|54=1|38=100|40=1|")
        (member,) = lists.list_members(msg)
        assert dict(member)["11"] == "C1" and dict(member)["68"] == "3"

    def test_the_factory_orders_each_instance_clordid_first(self):
        f = FixMessageFactory(FixDictionary("FIX.4.2"), "A", "B")
        msg = f.new_order_list("L1", [[("55", "IBM"), ("11", "C1"), ("67", "1"), ("54", "1")]], exec_inst_type="2")
        msg.sendprep(f.dictionary, "A", "B", 1)
        assert "|66=L1|394=3|433=2|68=1|73=1|11=C1|67=1|55=IBM|54=1|" in msg.to_pipe_string()

    def test_list_status_by_version(self):
        report = [{"11": "C1", "14": "0", "39": "0", "151": "100", "84": "0", "6": "0"}]
        for version, has in (("FIX.4.0", False), ("FIX.4.2", True)):
            f = FixMessageFactory(FixDictionary(version), "A", "B")
            msg = f.list_status("L1", "1", "3", report, text="ok")
            msg.sendprep(f.dictionary, "A", "B", 1)
            wire = msg.to_pipe_string()
            assert ("|429=1|" in wire) == has and ("|431=3|" in wire) == has and ("|444=ok|" in wire) == has
            assert "|66=L1|" in wire and "|73=1|11=C1|14=0|" in wire
            assert ("|39=0|151=100|" in wire) == has, "OrdStatus and LeavesQty join the report in 4.1/4.2"


# ── Sent as one NewOrderList ───────────────────────────────────────────

class TestNewOrderList:
    @pytest.mark.asyncio
    async def test_both_sides_of_a_new_order_list(self, linked):
        db, engine, cli, mkt = linked
        list_id = await engine.send_new_list("Client", BASKET, mode="E", exec_inst_type="2", text="basket")
        (wire,) = _sent(cli, "E")
        assert f"|66={list_id}|394=3|433=2|68=2|73=2|" in wire and "|58=basket|" in wire
        assert _sent(cli, "D") == [], "one message holds the list"
        (sent,) = await _lists(db, "TX")
        assert (sent["mode"], sent["status"], sent["tot_no_orders"], sent["exec_inst_type"]) == (
            "E", "Sent", 2, "WaitForInstruction")
        (received,) = await _lists(db, "RX")
        assert (received["list_id"], received["mode"], received["pending_action"], received["tot_no_orders"]) == (
            list_id, "E", "New", 2)
        for direction in ("TX", "RX"):
            orders = await _orders(db, direction)
            assert [(o["symbol"], o["list_id"], o["list_seq_no"]) for o in orders] == [
                ("IBM", list_id, 1), ("MSFT", list_id, 2)], direction
        assert all(o["pending_action"] == "New" for o in await _orders(db, "RX"))

        await engine.accept_list("Server", list_id)
        assert len(_sent(mkt, "8")) == 2 and all(f"|66={list_id}|" in w for w in _sent(mkt, "8"))
        (status,) = _sent(mkt, "N")
        assert "|429=1|" in status and "|431=2|" in status, "acknowledged, waiting for the ListExecute"
        (sent,) = await _lists(db, "TX")
        assert (sent["status"], sent["status_type"]) == ("ReceivedForExecution", "Ack")
        assert {o["status"] for o in await _orders(db, "TX")} == {"New"}

        await engine.execute_list("Client", list_id)
        assert (await _lists(db, "TX"))[0]["pending_action"] == "Execute"
        assert (await _lists(db, "RX"))[0]["pending_action"] == "Execute"
        await engine.accept_list("Server", list_id)
        assert "|429=4|" in _sent(mkt, "N")[-1] and "|431=3|" in _sent(mkt, "N")[-1]
        sent = (await _lists(db, "TX"))[0]
        assert (sent["pending_action"], sent["status"]) == ("", "Executing"), "the execute is answered"

        await engine.fill_list("Server", list_id)
        assert {(o["status"], o["cum_qty"]) for o in await _orders(db, "TX")} == {("Filled", 100.0), ("Filled", 200.0)}

    @pytest.mark.asyncio
    async def test_reject_a_new_order_list(self, linked):
        db, engine, cli, mkt = linked
        list_id = await engine.send_new_list("Client", BASKET)
        await engine.reject_list("Server", list_id, text="no baskets today")
        assert "|431=7|" in _sent(mkt, "N")[-1] and "|444=no baskets today|" in _sent(mkt, "N")[-1]
        assert {o["status"] for o in await _orders(db, "TX")} == {"Rejected"}
        assert (await _lists(db, "TX"))[0]["status"] == "Reject"
        assert (await _lists(db, "RX"))[0]["pending_action"] == ""

    @pytest.mark.asyncio
    async def test_cancel_a_list_by_list_cancel_request(self, linked):
        db, engine, cli, mkt = linked
        list_id = await engine.send_new_list("Client", BASKET)
        await engine.accept_list("Server", list_id)
        await engine.cancel_list("Client", list_id)
        assert _sent(cli, "K") and _sent(cli, "F") == [], "an E list is canceled as a list"
        await engine.accept_list("Server", list_id)
        assert {o["status"] for o in await _orders(db, "TX")} == {"Canceled"}
        sent = (await _lists(db, "TX"))[0]
        assert (sent["status"], sent["pending_action"]) == ("AllDone", "")

    @pytest.mark.asyncio
    async def test_a_refused_execute_leaves_the_list(self, linked):
        db, engine, cli, mkt = linked
        list_id = await engine.send_new_list("Client", BASKET, exec_inst_type="2")
        await engine.accept_list("Server", list_id)
        await engine.execute_list("Client", list_id)
        await engine.reject_list("Server", list_id, text="market closed")
        assert "|429=2|" in _sent(mkt, "N")[-1] and "|444=market closed|" in _sent(mkt, "N")[-1]
        assert (await _lists(db, "RX"))[0]["pending_action"] == ""

    @pytest.mark.asyncio
    async def test_status_request_is_answered_from_the_tables(self, linked):
        db, engine, cli, mkt = linked
        list_id = await engine.send_new_list("Client", BASKET)
        await engine.accept_list("Server", list_id)
        orders = await _orders(db, "RX")
        await engine.fill_order("Server", orders[0]["cl_ord_id"], 40, 10)
        await engine.request_list_status("Client", list_id)
        reply = _sent(mkt, "N")[-1]
        assert "|429=2|" in reply and f"|11={orders[0]['cl_ord_id']}|14=40|39=1|151=60|" in reply

    @pytest.mark.asyncio
    async def test_a_trade_carries_its_orders_list(self, linked):
        """executions_query's `list_id`, which the trade blotters follow a
        selected list by: the trade's order's, on both sides, blank for an
        order outside any list."""
        import tomllib
        from pathlib import Path
        import mkfix
        config = tomllib.loads((Path(mkfix.__file__).parent / "mkfix.toml").read_text(encoding="utf-8"))
        db, engine, cli, mkt = linked
        list_id = await engine.send_new_list("Client", BASKET)
        await engine.accept_list("Server", list_id)
        await engine.fill_order("Server", (await _orders(db, "RX"))[0]["cl_ord_id"], 40, 10)
        await engine.send_new_order("Client", "ORCL", "1", 10, "2", 5)
        loose = (await _orders(db, "RX"))[0]
        assert loose["list_id"] == ""
        await engine.fill_order("Server", loose["cl_ord_id"], 10, 5)
        trades = await _fetch_all(db, config["services"]["executions_query"]["sql"])
        assert sorted((t["direction"], t["symbol"], t["list_id"]) for t in trades) == [
            ("RX", "IBM", list_id), ("RX", "ORCL", ""), ("TX", "IBM", list_id), ("TX", "ORCL", "")]

    @pytest.mark.asyncio
    async def test_unknown_lists_are_answered(self, linked):
        db, engine, cli, mkt = linked
        await engine.on_app_message(mkt, "L", parse_fix("8=FIX.4.4|35=L|66=NOPE|"))
        await engine.on_app_message(mkt, "M", parse_fix("8=FIX.4.4|35=M|66=NOPE|"))
        assert all("|431=7|" in w and "|444=Unknown list: NOPE|" in w for w in _sent(mkt, "N"))
        assert len(_sent(mkt, "N")) == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("linked", ["FIX.4.1"], indirect=True)
    async def test_before_42_a_new_order_list_holds_one_order(self, linked):
        db, engine, cli, mkt = linked
        list_id = await engine.send_new_list("Client", BASKET)
        wires = _sent(cli, "E")
        assert len(wires) == 2 and "|73=" not in wires[0]
        assert "|66=" in wires[0] and "|67=1|" in wires[0] and "|68=2|" in wires[0] and "|67=2|" in wires[1]
        (received,) = await _lists(db, "RX")
        assert (received["list_id"], received["mode"], received["tot_no_orders"]) == (list_id, "E", 2)
        assert [o["list_seq_no"] for o in await _orders(db, "RX")] == [1, 2]

    @pytest.mark.asyncio
    async def test_members_carry_their_instrument(self, linked):
        db, engine, cli, mkt = linked
        await engine.send_new_list("Client", [{**BASKET[0], "symbol": "ES", **FUTURE}])
        assert "|73=1|" in _sent(cli, "E")[0] and "|167=FUT|200=202612|" in _sent(cli, "E")[0]
        assert {o["instrument"] for o in await _fetch_all(db, "SELECT instrument FROM fix_orders")} == {"ES Dec26"}


# ── Sent as orders carrying the ListID ─────────────────────────────────

class TestOrdersCarryingTheListId:
    @pytest.mark.asyncio
    async def test_a_basket_of_orders_and_one_more_later(self, linked):
        db, engine, cli, mkt = linked
        list_id = await engine.send_new_list("Client", json.dumps(BASKET), mode="D", tot_orders=True)
        wires = _sent(cli, "D")
        assert len(wires) == 2 and all(f"|66={list_id}|" in w and "|68=2|" in w and "|67=" not in w for w in wires)
        assert _sent(cli, "E") == []
        (received,) = await _lists(db, "RX")
        assert (received["mode"], received["pending_action"]) == ("D", "New")
        await engine.accept_list("Server", list_id)
        assert _sent(mkt, "N") == [], "orders are answered as orders"
        assert (await _lists(db, "RX"))[0]["pending_action"] == ""

        cl = await engine.send_new_order("Client", "ORCL", "1", 5, price=1, list_id=list_id)
        assert f"|66={list_id}|" in _sent(cli, "D")[-1] and "|67=" not in _sent(cli, "D")[-1], "ListID, no ListSeqNo"
        received = await _orders(db, "RX")
        assert [(o["symbol"], o["list_seq_no"]) for o in received] == [("IBM", 1), ("MSFT", 2), ("ORCL", 3)]
        assert (await _lists(db, "RX"))[0]["pending_action"] == "New", "a late order is pending on its list"
        assert [o["list_id"] for o in await _orders(db, "TX")] == [list_id] * 3 and cl

    @pytest.mark.asyncio
    async def test_a_hand_sent_order_with_a_new_list_id_makes_the_list(self, linked):
        db, engine, cli, mkt = linked
        await engine.send_new_order("Client", "IBM", "1", 5, price=1, extra_tags="66=MYLIST")
        for direction in ("TX", "RX"):
            (row,) = await _lists(db, direction)
            assert (row["list_id"], row["mode"]) == ("MYLIST", "D"), direction

    @pytest.mark.asyncio
    async def test_cancel_as_orders_is_the_default_for_d(self, linked):
        db, engine, cli, mkt = linked
        list_id = await engine.send_new_list("Client", BASKET, mode="D")
        await engine.accept_list("Server", list_id)
        sent = await engine.cancel_list("Client", list_id)
        assert len(sent) == 2 and len(_sent(cli, "F")) == 2 and _sent(cli, "K") == []
        assert {o["pending_action"] for o in await _orders(db, "RX")} == {"Cancel"}

    @pytest.mark.asyncio
    async def test_a_d_joining_an_e_list_makes_it_both(self, linked):
        db, engine, cli, mkt = linked
        list_id = await engine.send_new_list("Client", BASKET)
        await engine.send_new_order("Client", "ORCL", "1", 5, price=1, list_id=list_id)
        assert {r["mode"] for r in await _lists(db)} == {"E+D"}

    @pytest.mark.asyncio
    async def test_fill_all_needs_a_price_for_a_market_order(self, linked):
        db, engine, cli, mkt = linked
        list_id = await engine.send_new_list("Client", [{**BASKET[0], "ord_type": "1", "price": ""}], mode="D")
        with pytest.raises(ValueError, match="No price to fill"):
            await engine.fill_list("Server", list_id)
        await engine.fill_list("Server", list_id, price="9.5")
        (order,) = await _orders(db, "TX")
        assert (order["status"], order["avg_price"]) == ("Filled", 9.5)

    @pytest.mark.asyncio
    async def test_unsolicited_cancel_of_every_working_order(self, linked):
        db, engine, cli, mkt = linked
        list_id = await engine.send_new_list("Client", BASKET, mode="D")
        await engine.accept_list("Server", list_id)
        await engine.fill_order("Server", (await _orders(db, "RX"))[0]["cl_ord_id"], 100, 10)
        cancels = await engine.cancel_list_orders("Server", list_id)
        assert len(cancels) == 1
        assert [o["status"] for o in await _orders(db, "TX")] == ["Filled", "Canceled"]


class TestRefusals:
    @pytest.mark.asyncio
    async def test_what_a_list_needs(self, linked):
        db, engine, cli, mkt = linked
        with pytest.raises(ValueError, match="at least one order"):
            await engine.send_new_list("Client", "[]")
        with pytest.raises(ValueError, match="Order 2 of the list needs qty"):
            await engine.send_new_list("Client", [BASKET[0], {"symbol": "X", "side": "1"}])
        with pytest.raises(ValueError, match="not 'X'"):
            await engine.send_new_list("Client", BASKET, mode="X")
        with pytest.raises(ValueError, match="nothing pending"):
            list_id = await engine.send_new_list("Client", BASKET)
            await engine.accept_list("Server", list_id)
            await engine.accept_list("Server", list_id)
        assert cli.wire and all("|35=E|" in w for w in cli.wire), "nothing sent for a refused list"

    @pytest.mark.asyncio
    async def test_a_send_that_fails_rejects_what_did_not_go(self, linked):
        db, engine, cli, mkt = linked
        calls = []
        real = cli.send_message

        async def flaky(msg):
            calls.append(msg["35"])
            if len(calls) == 2:
                raise ConnectionError("gone")
            return await real(msg)
        cli.send_message = flaky
        with pytest.raises(ConnectionError):
            await engine.send_new_list("Client", BASKET + [{**BASKET[0], "symbol": "ORCL"}], mode="D")
        assert [(o["symbol"], o["status"]) for o in await _orders(db, "TX")] == [
            ("IBM", "PendingNew"), ("MSFT", "Rejected"), ("ORCL", "Rejected")]
        assert (await _lists(db, "TX"))[0]["status"] == "Sent", "one order went: the list stands"


class TestAllDone:
    """A list whose every order is finished is AllDone on both sides, sent
    as one NewOrderList or as orders, whether or not a ListStatus says so."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["E", "D"])
    async def test_the_last_order_finished_finishes_the_list(self, linked, mode):
        db, engine, cli, mkt = linked
        seen = []
        engine.events.subscribe(lambda ev: seen.append(ev.kinds[0]) if ev.table == "fix_lists" else None)
        list_id = await engine.send_new_list("Client", BASKET, mode=mode)
        await engine.accept_list("Server", list_id)
        first, second = await _orders(db, "RX")
        await engine.fill_order("Server", first["cl_ord_id"], 100, 10)
        assert {r["status"] for r in await _lists(db)} != {"AllDone"}, "one order still works"
        await engine.unsolicited_cancel("Server", second["cl_ord_id"])
        rows = await _lists(db)
        assert [(r["direction"], r["status"], r["list_status_code"]) for r in rows] == [
            ("TX", "AllDone", "6"), ("RX", "AllDone", "6")]
        assert seen.count("list done") == 1

    @pytest.mark.asyncio
    async def test_not_while_orders_it_announced_are_still_to_come(self, linked):
        db, engine, cli, mkt = linked
        list_id = await engine.send_new_list("Client", BASKET[:1], mode="D", tot_count=2)
        (order,) = await _orders(db, "RX")
        await engine.fill_order("Server", order["cl_ord_id"], 100, 10)
        assert {r["status"] for r in await _lists(db)} != {"AllDone"}, "TotNoOrders says one more is coming"
        cl = await engine.send_new_order("Client", "MSFT", "1", 5, price=1, list_id=list_id)
        received = next(o for o in await _orders(db, "RX") if o["cl_ord_id"] == cl)
        await engine.fill_order("Server", received["cl_ord_id"], 5, 1)
        assert {r["status"] for r in await _lists(db)} == {"AllDone"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("linked", ["FIX.4.1"], indirect=True)
    async def test_named_all_done_where_the_version_has_no_name(self, linked):
        db, engine, cli, mkt = linked
        list_id = await engine.send_new_list("Client", BASKET[:1], mode="D")
        (order,) = await _orders(db, "RX")
        await engine.fill_order("Server", order["cl_ord_id"], 100, 10)
        assert {r["status"] for r in await _lists(db)} == {"AllDone"} and list_id


def test_all_done_name():
    assert lists.all_done_name(FixDictionary("FIX.4.2")) == "AllDone"
    assert lists.all_done_name(FixDictionary("FIX.4.0")) == "AllDone"
