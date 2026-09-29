"""Macros and lists (0.78): `new list` and its `order` lines, an order's own
macro under its line, `add order`, the list requests and the market's
answers, a list sent order by order offered once it goes quiet, and the
recorder writing a list worked by hand. The examples list-trader,
drip-basket and list-desk play each other over the linked pair of
tests/test_macro_sending.py."""

import json

import pytest

from mkfix import macro
from mkfix.fix.actions import UNSCRIPTED
from mkfix.macro import vocab
from mkfix.macro.instance import COMPLETED, FAILED, PASSED

from tests.test_engine import _fetch_all, stack  # noqa: F401
from tests.test_macro_recorder import body, hand  # noqa: F401
from tests.test_macro_sending import pair  # noqa: F401

BASKET = """run
    new list mode: list, tot_orders: yes
        order symbol: 'IBM', side: buy, qty: 100, price: 10
        order symbol: 'MSFT', side: sell, qty: 50, price: 20
            expect filled within 10s
            pass 'MSFT filled'
    expect done within 10s
    pass '${list.order_count} done'
"""
DESK = "on list\n    accept list\n    fill all price: 11\n"


def _messages(diags):
    return [d.message for d in diags]


async def _lists(pair, direction):
    return await _fetch_all(pair.db, f"SELECT * FROM fix_lists WHERE direction = '{direction}' ORDER BY id")


def _types(session):
    return [m["35"] for m in session.sent]


class TestTheWords:
    def test_a_list_checks_clean(self):
        sc, diags = macro.check(BASKET)
        assert diags == [] and sc.blocks[0].subject == vocab.LIST
        _, diags = macro.check(DESK)
        assert diags == []

    def test_every_list_op_has_its_verb(self):
        assert UNSCRIPTED == frozenset()
        ops = {v.op for v in vocab.VERBS.values()}
        assert {"send_new_list", "add_list_order", "execute_list", "cancel_list", "request_list_status",
                "accept_list", "reject_list", "send_list_status", "fill_list", "cancel_list_orders"} <= ops

    @pytest.mark.parametrize("text, message", [
        ("run\n    new symbol: 'IBM', side: buy, qty: 1\n    order symbol: 'IBM', side: buy, qty: 1\n",
         "belongs under a `new list`"),
        ("run\n    new list mode: list\n    execute list\n", "`new list` needs its orders"),
        ("run\n    new list mode: list\n        order symbol: 'IBM', side: buy\n", "qty"),
        ("run\n    new list mode: list\n        order symbol: 'IBM', side: buy, qty: 1\n            fill qty: 1\n",
         "`fill`"),
        ("on list\n    execute list\n", "`execute list`"),
        ("run\n    new list mode: sideways\n        order symbol: 'IBM', side: buy, qty: 1\n", "sideways"),
    ])
    def test_what_is_refused(self, text, message):
        _, diags = macro.check(text)
        assert any(message in m for m in _messages(macro.errors(diags))), _messages(diags)

    def test_the_order_line_is_the_statement_only_leading_its_line(self):
        assert "order" in vocab.STATEMENTS and "order" in vocab.CONTEXT_DOCS


class TestSendingOneNewOrderList:
    @pytest.mark.asyncio
    async def test_the_lines_go_as_one_message_and_an_order_keeps_its_own_macro(self, pair):
        desk = pair.arm(DESK, session="LOOP-MKT")
        client = pair.arm(BASKET)
        await pair.advance(5)
        assert _types(pair.cli).count("E") == 1 and "D" not in _types(pair.cli)
        (lst,) = [m for m in pair.cli.sent if m["35"] == "E"]
        assert lst.get("68") == "2"
        assert sorted((i.status, i.message) for i in client.instances) == [(PASSED, "2 done"), (PASSED, "MSFT filled")]
        assert [(i.status, i.message) for i in desk.instances] == [(COMPLETED, "")]
        sent = await pair.orders("TX")
        assert [(o["symbol"], o["list_seq_no"], o["status"]) for o in sent] == [("IBM", 1, "Filled"), ("MSFT", 2, "Filled")]

    @pytest.mark.asyncio
    async def test_a_list_macros_orders_are_no_on_sent_order_blocks(self, pair):
        pair.arm(DESK, session="LOOP-MKT")
        minder = pair.arm("on sent order\n    log 'minding ${order.symbol}'\n", session="LOOP-CLI")
        pair.arm(BASKET)
        await pair.advance(5)
        assert minder.instances == [], "the list's orders are its macro's"

    @pytest.mark.asyncio
    async def test_the_requests_and_their_answers(self, pair):
        pair.arm("on list\n    when execute\n        accept list\n        fill all price: 5\n"
                 "    when status request\n        list status status_type: response, text: 'fine'\n"
                 "    accept list\n", session="LOOP-MKT")
        client = pair.arm("run\n    new list mode: list, execution: wait\n"
                          "        order symbol: 'IBM', side: buy, qty: 10, price: 5\n"
                          "    expect accepted within 2s\n    request list status\n    expect status within 2s\n"
                          "    log '${list.text}'\n    execute list\n    expect executing within 2s\n"
                          "    expect done within 2s\n    pass\n")
        await pair.advance(5)
        (inst,) = client.instances
        assert inst.status == PASSED, inst.message
        assert [t for t in _types(pair.cli) if t in "EKLM"] == ["E", "M", "L"]
        # The engine answers a ListStatusRequest from the tables at once; the desk's `list status` is a second.
        statuses = [pair.engine._as_sent(pair.mkt, m).to_pipe_string() for m in pair.mkt.sent if m["35"] == "N"]
        assert len(statuses) == 4 and sum("=fine|" in m for m in statuses) == 1, statuses
        (rx,) = await _lists(pair, "RX")
        assert rx["pending_action"] == ""

    @pytest.mark.asyncio
    async def test_add_order_joins_the_list_and_runs_its_block(self, pair):
        # The rule first: the late order can arrive while `fill all` is under way.
        desk = pair.arm("on list\n    when joined\n        fill all price: 12\n" + DESK.split("\n", 1)[1],
                        session="LOOP-MKT")
        client = pair.arm("run\n    new list mode: list\n        order symbol: 'IBM', side: buy, qty: 10, price: 5\n"
                          "    expect accepted within 2s\n"
                          "    add order symbol: 'MSFT', side: buy, qty: 5, price: 7\n"
                          "        expect filled within 5s\n        pass 'late ${order.avg_price}'\n"
                          "    expect done within 5s\n    pass\n")
        await pair.advance(10)
        assert sorted(i.message for i in client.instances) == ["", "late 12"]
        (msft,) = [o for o in await pair.orders("TX") if o["symbol"] == "MSFT"]
        assert (msft["list_id"], msft["list_seq_no"]) == ((await _lists(pair, "TX"))[0]["list_id"], 2)
        assert desk.instances[0].status != FAILED

    @pytest.mark.asyncio
    async def test_a_refused_list(self, pair):
        pair.arm("on list\n    reject list text: 'no'\n", session="LOOP-MKT")
        client = pair.arm("run\n    new list mode: list\n        order symbol: 'IBM', side: buy, qty: 10, price: 5\n"
                          "    expect rejected within 2s\n    pass '${list.text}'\n")
        await pair.advance(3)
        assert [(i.status, i.message) for i in client.instances] == [(PASSED, "no")]


class TestSendingOrderByOrder:
    DRIP = ("run\n    new list mode: orders, tot_orders: 3\n"
            "        repeat 3 every 1s with sym = ['IBM', 'MSFT', 'AAPL']\n"
            "            order symbol: sym, side: buy, qty: 10, price: 5\n"
            "    expect done within 30s\n    pass '${list.order_count}'\n")

    @pytest.mark.asyncio
    async def test_each_order_goes_as_its_line_is_reached_with_the_count_to_come(self, pair):
        pair.arm(self.DRIP)
        await pair.advance(0.5)
        assert _types(pair.cli) == ["D"]
        await pair.advance(2)
        sent = [pair.engine._as_sent(pair.cli, m) for m in pair.cli.sent if m["35"] == "D"]
        assert len(sent) == 3 and "E" not in _types(pair.cli)
        assert {m["66"] for m in sent} == {(await _lists(pair, "TX"))[0]["list_id"]}
        assert [m.get("68") for m in sent] == ["3", "3", "3"] and all("67" not in m.fields for m in sent)

    @pytest.mark.asyncio
    async def test_the_market_takes_it_once_it_goes_quiet(self, pair):
        desk = pair.arm(DESK, session="LOOP-MKT")
        client = pair.arm(self.DRIP)
        await pair.advance(6.9)             # the last order went at 2s: quiet until 7s
        assert desk.instances == []
        await pair.advance(0.2)
        assert len(desk.instances) == 1
        await pair.advance(5)
        assert [(i.status, i.message) for i in client.instances] == [(PASSED, "3")]
        (rx,) = await _lists(pair, "RX")
        assert (rx["order_count"], rx["tot_no_orders"]) == (3, 3)

    @pytest.mark.asyncio
    async def test_a_late_order_is_joined(self, pair):
        pair.arm("on list\n    accept list\n    wait joined\n    pass '${list.order_count}'\n", session="LOOP-MKT")
        pair.arm("run\n    new list mode: orders\n        order symbol: 'IBM', side: buy, qty: 10, price: 5\n"
                 "        after 10s\n        order symbol: 'MSFT', side: buy, qty: 10, price: 5\n")
        await pair.advance(12)
        (desk,) = pair.runner.runs[0].instances
        assert (desk.status, desk.message) == (PASSED, "2")

    @pytest.mark.asyncio
    async def test_nobody_answering_cancels_the_rest(self, pair):
        client = pair.arm("drip-basket")
        await pair.advance(40)
        (inst,) = client.instances
        assert (inst.status, inst.message) == (FAILED, "the basket was not filled")
        assert _types(pair.cli)[-1] == "K"


class TestTheExamples:
    @pytest.mark.asyncio
    async def test_the_trader_and_the_desk(self, pair):
        desk = pair.arm("list-desk", session="LOOP-MKT")
        trader = pair.arm("list-trader")
        await pair.advance(30)
        assert sorted((i.status, i.message) for i in trader.instances) == [
            (COMPLETED, ""), (PASSED, "4 orders done"), (PASSED, "the late order filled")]
        assert [o["status"] for o in await pair.orders("TX")] == ["Filled"] * 4
        assert desk.instances and all(i.status != FAILED for i in desk.instances)

    @pytest.mark.asyncio
    async def test_the_drip_and_the_desk(self, pair):
        pair.arm("list-desk", session="LOOP-MKT")
        drip = pair.arm("drip-basket")
        await pair.advance(20)
        assert [(i.status, i.message) for i in drip.instances] == [(PASSED, "5 orders filled")]

    @pytest.mark.asyncio
    async def test_the_desk_refuses_a_big_list(self, pair):
        pair.arm("list-desk", session="LOOP-MKT")
        orders = "\n".join("        order symbol: 'IBM', side: buy, qty: 1, price: 1" for _ in range(21))
        client = pair.arm(f"run\n    new list mode: list\n{orders}\n    expect rejected within 5s\n    pass '${{list.text}}'\n")
        await pair.advance(5)
        assert [i.message for i in client.instances] == ["at most 20 orders a list"]


class TestRecording:
    @pytest.mark.asyncio
    async def test_a_list_sent_by_hand_is_new_list_with_its_orders(self, hand):
        recorder = hand.recorder("client")
        orders = [{"symbol": "IBM", "side": "1", "qty": "100", "ord_type": "2", "price": "10", "tif": "0"},
                  {"symbol": "MSFT", "side": "2", "qty": "50", "ord_type": "2", "price": "20", "tif": "0"}]
        result = await hand.do("send_new_list", session_id="LOOP-CLI", list_orders=json.dumps(orders), mode="E",
                               tot_orders="1")
        list_id = result["list_id"]
        await hand.do("accept_list", 1, session_id="LOOP-MKT", list_id=list_id)
        await hand.do("fill_list", 1, session_id="LOOP-MKT", list_id=list_id, price="11")
        source = (await recorder.stop("rec"))["source"]
        lines = body(source)
        assert lines[:4] == ["run", "    new list mode: list, bid_type: no_bidding, tot_orders: yes",
                             "        order symbol: 'IBM', side: buy, qty: 100, type: limit, price: 10, tif: day",
                             "            expect ack within 5s"], source
        assert not any(line.startswith("    new symbol") for line in lines), "its orders are its lines"
        assert macro.check(source, side="client")[1] == []

    @pytest.mark.asyncio
    async def test_a_list_worked_by_hand_is_an_on_list_block(self, hand):
        recorder = hand.recorder("market")
        orders = [{"symbol": "IBM", "side": "1", "qty": "100", "price": "10"}]
        list_id = (await hand.do("send_new_list", session_id="LOOP-CLI", list_orders=json.dumps(orders)))["list_id"]
        await hand.do("accept_list", 1, session_id="LOOP-MKT", list_id=list_id)
        await hand.do("fill_list", 1, session_id="LOOP-MKT", list_id=list_id, price="11")
        source = (await recorder.stop("rec"))["source"]
        assert body(source)[:4] == ["on list where order_count == 1", "    accept list", "    after 1s",
                                    "    fill all price: 11"], source
        assert macro.check(source, side="market")[1] == []
