"""A macro from history: what an order has been through, read back from the
messages kept, is the macro a recording of it would have been — and that
macro, run, does it again."""

import pytest
import pytest_asyncio

from mkfix import macro
from mkfix.fix.message import parse_fix
from mkfix.macro.history import FromHistory, _seconds, _stamp
from mkfix.macro.instance import PASSED
from mkfix.macro.store import MacroManager

from tests.test_engine import StubSession, _fetch_all, stack  # noqa: F401
from tests.test_macro_recorder import Hand, body, lifecycle
from tests.test_macro_sending import LinkedSession, Pair

EPOCH = 1_790_000_000.0          # the hand's clock counts from here: 2026-09-21 14:13:20 UTC


class KeptSession(LinkedSession):
    """A linked session that keeps its messages the way a real one does:
    each is recorded as it leaves, and as it arrives at the other end."""

    async def send_message(self, msg):
        await StubSession.send_message(self, msg)
        wire = self.engine._as_sent(self, msg)
        await self.engine.record_message(self.session_id, "TX", wire)
        received = parse_fix(wire.to_wire_string())
        await self.engine.record_message(self.peer.session_id, "RX", received)
        await self.engine.on_app_message(self.peer, wire["35"], received)
        return msg


class KeptPair(Pair):
    def __init__(self, db, engine):
        super().__init__(db, engine)
        self.cli, self.mkt = KeptSession(engine, "LOOP-CLI"), KeptSession(engine, "LOOP-MKT")
        self.cli.peer, self.mkt.peer = self.mkt, self.cli
        engine.sessions.update({"LOOP-CLI": self.cli, "LOOP-MKT": self.mkt})


@pytest_asyncio.fixture
async def hand(stack, monkeypatch):
    """Both ends worked by hand on a clock the test moves — the clock the
    messages and the rows are stamped by, too."""
    db, writer, engine = stack
    kept = Hand(KeptPair(db, engine))
    monkeypatch.setattr("mkfix.fix.engine._fix_timestamp", lambda precision="millisecond": _stamp(EPOCH + kept.now))
    yield kept
    kept.pair.runner.stop_all()
    await kept.pair.runner.settle()


async def ids(hand, table, direction):
    return [r["id"] for r in await _fetch_all(hand.pair.db, f"SELECT id FROM {table} WHERE direction = '{direction}' ORDER BY id")]


async def written(hand, side, subject="order", delays=False, rows=None):
    table = {"order": "fix_orders", "ioi": "fix_iois", "advert": "fix_adverts", "allocation": "fix_allocations"}[subject]
    sends = (subject == "order") == (side == "client")
    rows = rows if rows is not None else await ids(hand, table, "TX" if sends else "RX")
    return await FromHistory(hand.engine, side, delays=delays).write(subject, rows)


async def same(hand, side, work, subject="order", delays=False):
    """Work by hand while a recorder listens; the macro from history is the
    recording. Answers the text both gave."""
    recorder = hand.recorder(side)
    await work()
    recorded = await recorder.stop("recorded", delays=delays)
    result = await written(hand, side, subject, delays)
    assert body(result["source"]) == body(recorded["source"])
    assert (result["orders"], result["actions"]) == (recorded["orders"], recorded["actions"])
    assert macro.check(result["source"], side=side)[1] == []
    return result


class TestStamps:
    def test_a_stamp_is_seconds_and_back(self):
        assert _stamp(EPOCH + 100.25) == "20260921-14:15:00.250"
        assert _seconds("20260921-14:15:00.250") == EPOCH + 100.25
        assert _seconds("20260921-14:15:00") == EPOCH + 100 and _seconds("") == 0.0 and _seconds("yesterday") == 0.0


class TestMarketSide:
    @pytest.mark.asyncio
    async def test_a_worked_order_is_the_macro_a_recording_would_be(self, hand):
        result = await same(hand, "market", lambda: lifecycle(hand))
        assert body(result["source"]) == [
            "on order where symbol == 'IBM'",
            "    when replace", "        accept",
            "    when cancel", "        accept", "        stop",
            "    accept text: 'working'",
            "    after 2s",
            "    fill qty: 50, price: 10"]
        (order,) = await hand.pair.orders("RX")
        head = result["source"].splitlines()[0]
        assert head == f"# Market side, from the history of {order['cl_ord_id']} on LOOP-MKT: 1 order, 4 actions."
        assert result["left_out"] == []

    @pytest.mark.asyncio
    async def test_with_the_delays_it_took(self, hand):
        result = await same(hand, "market", lambda: lifecycle(hand), delays=True)
        assert body(result["source"])[1:] == [
            "    when replace", "        after 500ms", "        accept",
            "    when cancel", "        after 300ms", "        accept", "        stop",
            "    after 1.2s", "    accept text: 'working'", "    after 2s", "    fill qty: 50, price: 10"]
        assert "after the time it took to answer it" in result["source"]

    @pytest.mark.asyncio
    async def test_it_does_it_again(self, hand):
        await lifecycle(hand)
        source = (await written(hand, "market"))["source"]
        pair = hand.pair
        before = len(pair.mkt.sent)
        pair.arm(source)
        run = pair.arm("run\n    new symbol: 'IBM', side: buy, qty: 100, price: 10\n"
                       "    wait fill\n    after 1s\n    replace qty: 200\n    wait replaced\n    after 1s\n    cancel\n"
                       "    expect canceled within 2s\n    pass\n")
        await pair.advance(10)
        assert [i.status for i in run.instances] == [PASSED]
        wire = [pair.engine._as_sent(pair.mkt, m) for m in pair.mkt.sent[before:]]
        assert [(m.get("150"), m.get("58")) for m in wire] == [("0", "working"), ("1", None), ("5", None), ("4", None)]

    @pytest.mark.asyncio
    async def test_trades_are_named_as_they_stood(self, hand):
        cl = await hand.new(qty=300)
        await hand.market("accept_request", cl, wait=1)
        for qty in (100, 50, 25):
            await hand.market("fill_order", cl, wait=1, qty=qty, price=10)
        first, middle, last = [t["exec_id"] for t in await hand.pair.trades("TX")]
        await hand.do("correct_trade", 1, session_id="LOOP-MKT", exec_id=last, qty=20, price=10)
        await hand.do("bust_trade", 1, session_id="LOOP-MKT", exec_id=first, text="in error")
        await hand.do("correct_trade", 1, session_id="LOOP-MKT", exec_id=middle, qty=60, price=10.5)
        source = (await written(hand, "market"))["source"]
        assert body(source)[-5:] == [
            "    correct last trade qty: 20, price: 10", "    after 1s",
            "    bust first trade text: 'in error'", "    after 1s",
            # among the live trades, as a macro looks: with the first one busted, the middle one is the first
            "    correct first trade qty: 60, price: 10.5"]
        assert macro.check(source, side="market")[1] == []
        # and run, it does to a new order's trades what was done to this one's
        pair = hand.pair
        by_hand = [(t["last_qty"], t["last_price"], t["exec_type"]) for t in await pair.trades("TX")]
        pair.arm(source)
        await hand.new(qty=300)
        await pair.advance(10)
        again = [(t["last_qty"], t["last_price"], t["exec_type"]) for t in (await pair.trades("TX"))[3:]]
        assert again == by_hand == [(100.0, 10.0, "Cancel"), (60.0, 10.5, "Correct"), (20.0, 10.0, "Correct")]

    @pytest.mark.asyncio
    async def test_a_trade_in_the_middle_is_named_by_its_terms(self, hand):
        cl = await hand.new(qty=300)
        await hand.market("accept_request", cl, wait=1)
        for qty in (100, 50, 25):
            await hand.market("fill_order", cl, wait=1, qty=qty, price=10)
        _, middle, _ = [t["exec_id"] for t in await hand.pair.trades("TX")]
        await hand.do("correct_trade", 1, session_id="LOOP-MKT", exec_id=middle, qty=60, price=10.5)
        (corrected,) = [t["exec_id"] for t in await hand.pair.trades("TX") if t["exec_type"] == "Correct"]
        await hand.do("bust_trade", 1, session_id="LOOP-MKT", exec_id=corrected)
        assert body((await written(hand, "market"))["source"])[-3:] == [
            "    correct trade where trade.last_qty == 50 and trade.last_price == 10, qty: 60, price: 10.5",
            "    after 1s",
            "    bust trade where trade.last_qty == 60 and trade.last_price == 10.5"], "by the terms it had when it was named"

    @pytest.mark.asyncio
    async def test_a_dispute_and_the_re_notification_that_answered_it(self, hand):
        async def work():
            cl = await hand.new(qty=100)
            await hand.market("accept_request", cl, wait=1)
            await hand.market("fill_order", cl, wait=1, qty=100, price=10)
            (received,) = await hand.pair.trades("RX")
            await hand.do("dk_trade", 2, session_id="LOOP-CLI", exec_id=received["exec_id"], dk_reason="F")
            (sent,) = await hand.pair.trades("TX")
            await hand.do("renotify_trade", 0.5, session_id="LOOP-MKT", exec_id=sent["exec_id"], text="as booked")
        venue = await same(hand, "market", work)
        assert body(venue["source"])[1:] == ["    when dk", "        renotify last trade text: 'as booked'",
                                             "    accept", "    after 1s", "    fill qty: 100, price: 10"]

    @pytest.mark.asyncio
    async def test_a_request_answered_two_ways_stays_in_order(self, hand):
        async def work():
            cl = await hand.new()
            await hand.market("accept_request", cl, wait=1)
            cl2 = await hand.replace(cl, wait=1, qty=200)
            await hand.market("accept_request", cl, wait=0.5)
            await hand.replace(cl2, wait=1, qty=300)
            await hand.market("reject_request", cl2, wait=0.5, text="one replace per order")
        result = await same(hand, "market", work)
        assert body(result["source"])[1:] == ["    accept", "    wait replace", "    accept", "    wait replace",
                                              "    reject text: 'one replace per order'"]

    @pytest.mark.asyncio
    async def test_an_unsolicited_cancel_a_restatement_and_a_reject(self, hand):
        async def work():
            a = await hand.new(symbol="IBM", qty=300)
            await hand.market("accept_request", a, wait=1)
            await hand.market("restate_order", a, wait=1, qty=200, price=9.5, restate_reason="5")
            await hand.market("unsolicited_cancel", a, wait=1, text="halted")
            b = await hand.new(symbol="ZZZZ")
            await hand.market("reject_request", b, wait=1, text="Unknown symbol")
        result = await same(hand, "market", work)
        lines = body(result["source"])
        assert "    unsol cxl text: 'halted'" in lines and "    reject text: 'Unknown symbol'" in lines
        assert any(line.startswith("    restate qty: 200, price: 9.5, reason: ") for line in lines)

    @pytest.mark.asyncio
    async def test_several_orders_share_a_block_or_are_told_apart(self, hand):
        async def work():
            ibm, msft, zzzz = [await hand.new(symbol=s) for s in ("IBM", "MSFT", "ZZZZ")]
            await hand.market("accept_request", ibm, wait=1)
            await hand.market("accept_request", msft, wait=0.4)
            await hand.market("reject_request", zzzz, wait=2, text="Unknown symbol")
        result = await same(hand, "market", work)
        assert body(result["source"]) == [
            "on order where symbol in ['IBM', 'MSFT']", "    accept",
            "on order where symbol == 'ZZZZ'", "    reject text: 'Unknown symbol'"]
        assert "from the history of " in result["source"] and ": 3 orders, 3 actions." in result["source"]

    @pytest.mark.asyncio
    async def test_only_the_orders_asked_for(self, hand):
        ibm, msft = await hand.new(symbol="IBM"), await hand.new(symbol="MSFT")
        await hand.market("accept_request", ibm, wait=1)
        await hand.market("reject_request", msft, wait=1, text="no")
        first, second = await ids(hand, "fix_orders", "RX")
        assert body((await written(hand, "market", rows=[second]))["source"]) == [
            "on order where symbol == 'MSFT'", "    reject text: 'no'"]
        assert body((await written(hand, "market", rows=[first, first]))["source"]) == [
            "on order where symbol == 'IBM'", "    accept"]

    @pytest.mark.asyncio
    async def test_custom_tags_are_extra_tags_and_the_client_is_not(self, hand):
        hand.pair.mkt.config = hand.pair.cli.config = {"client_tags": "109"}
        cl = await hand.new(client="ACME", extra_tags="9001=x")
        await hand.market("accept_request", cl, wait=1, extra_tags="9001=x|9002=y")
        await hand.market("fill_order", cl, wait=1, qty=100, price=10, extra_tags="9003=z")
        (answered,) = [m for m in hand.pair.mkt.sent if hand.engine._as_sent(hand.pair.mkt, m).get("150") == "0"]
        assert hand.engine._as_sent(hand.pair.mkt, answered).get("109") == "ACME", "the engine stamps the order's client"
        assert body((await written(hand, "market"))["source"])[1:] == [
            "    accept extra: '9001=x|9002=y'", "    after 1s", "    fill qty: 100, price: 10, extra: '9003=z'"]
        assert body((await written(hand, "client"))["source"])[1] == \
            "    new symbol: 'IBM', side: buy, qty: 100, type: limit, price: 10, tif: day, client: 'ACME', extra: '9001=x'"

    @pytest.mark.asyncio
    async def test_a_report_the_language_cannot_send_is_left_out_and_said(self, hand):
        cl = await hand.new()
        await hand.market("accept_request", cl, wait=1)
        await hand.cancel(cl, wait=1)
        # A PendingCancel, as Message Replay would have sent one: no button does
        hand.now += 0.5
        await hand.engine.record_message("LOOP-MKT", "TX", parse_fix(f"8=FIX.4.2|35=8|11={cl}|17=P1|150=6|39=6|55=IBM|54=1"))
        await hand.market("accept_request", cl, wait=1)
        result = await written(hand, "market")
        assert body(result["source"])[1:] == ["    when cancel", "        accept", "        stop", "    accept"]
        (order,) = await hand.pair.orders("RX")
        assert len(result["left_out"]) == 1 and "PendingCancel" in result["left_out"][0]
        assert "# Left out — " in result["source"] and "an ExecutionReport the language has no action for (PendingCancel)." in result["source"]
        assert macro.check(result["source"], side="market")[1] == []

    @pytest.mark.asyncio
    async def test_an_order_nothing_was_done_to_is_no_block(self, hand):
        await hand.new()
        result = await written(hand, "market")
        assert result["orders"] == 0 and "# Nothing to write: none of them was answered by this side." in result["source"]
        assert result["left_out"] and result["left_out"][0].endswith("nothing was done to it")


class TestClientSide:
    @pytest.mark.asyncio
    async def test_a_sent_order_is_a_run_block_that_checks_what_it_heard(self, hand):
        result = await same(hand, "client", lambda: lifecycle(hand))
        assert body(result["source"]) == [
            "run",
            "    new symbol: 'IBM', side: buy, qty: 100, type: limit, price: 10, tif: day",
            "    expect ack within 5s", "    expect fill within 6s",
            "    replace qty: 200",
            "    expect replaced within 5s",
            "    cancel",
            "    expect canceled within 5s",
            "    pass"]
        assert macro.check(result["source"], side="client")[0].needs_session

    @pytest.mark.asyncio
    async def test_both_ends_from_history_run_against_each_other(self, hand):
        await lifecycle(hand)
        venue, chase = (await written(hand, "market"))["source"], (await written(hand, "client", delays=True))["source"]
        pair = hand.pair
        pair.arm(venue)
        run = pair.arm(chase)
        await pair.advance(15)
        assert [(i.status, i.message) for i in run.instances] == [(PASSED, "")]
        sent = (await pair.orders("TX"))[-1]
        assert (sent["status"], sent["order_qty"], sent["cum_qty"]) == ("Canceled", 200.0, 50.0)

    @pytest.mark.asyncio
    async def test_several_orders_keep_their_spacing_and_their_terms(self, hand):
        async def work():
            await hand.new(symbol="IBM", qty=100, price=10, client="ACME", text="first", extra_tags="9001=x")
            await hand.new(wait=2.5, symbol="MSFT", qty=250, price=20.25, ord_type="2", tif="1", handl_inst="3")
            await hand.new(wait=0.5, symbol="AAPL", qty=10, price="", ord_type="1")
        result = await same(hand, "client", work)
        assert body(result["source"])[3:5] == [
            "    after 2.5s",
            "    new symbol: 'MSFT', side: buy, qty: 250, type: limit, price: 20.25, tif: gtc, handl_inst: manual"]

    @pytest.mark.asyncio
    async def test_a_refused_request_and_a_dk(self, hand):
        async def work():
            cl = await hand.new()
            await hand.market("accept_request", cl, wait=0.5)
            await hand.market("fill_order", cl, wait=0.5, qty=100, price=10)
            await hand.cancel(cl, wait=1)
            await hand.market("reject_request", cl, wait=0.2, text="too late")
            (trade,) = await hand.pair.trades("RX")
            await hand.do("dk_trade", 2, session_id="LOOP-CLI", exec_id=trade["exec_id"], dk_reason="E", text="through the limit")
        result = await same(hand, "client", work)
        assert body(result["source"])[2:] == [
            "    expect ack within 5s", "    expect filled within 5s", "    cancel", "    expect cancel rejected within 5s",
            "    dk last trade reason: price_exceeds_limit, text: 'through the limit'"]

    @pytest.mark.asyncio
    async def test_a_replace_names_what_it_changed_from_the_terms_last_accepted(self, hand):
        async def work():
            cl = await hand.new(qty=100, price=10)
            await hand.market("accept_request", cl, wait=0.5)
            refused = await hand.replace(cl, wait=1, qty=500, price=10)
            await hand.market("reject_request", cl, wait=0.5, text="too big")
            taken = await hand.replace(cl, wait=1, qty=200, price=10)
            await hand.market("accept_request", cl, wait=0.5)
            await hand.replace(taken, wait=1, qty=200, price=11)
            await hand.market("accept_request", taken, wait=0.5)
            assert refused != taken
        result = await same(hand, "client", work)
        assert [line.strip() for line in body(result["source"]) if "replace" in line and "expect" not in line] == [
            "replace qty: 500", "replace qty: 200", "replace price: 11"]


class TestFamilies:
    @pytest.mark.asyncio
    async def test_a_sent_ioi_with_its_replace_and_cancel(self, hand):
        ioi = (await hand.do("send_ioi", session_id="LOOP-MKT", symbol="IBM", side="1", qty="L", price=10.5,
                             qlty_ind="H", qualifiers="A,X"))["ioi_id"]
        new = (await hand.do("replace_ioi", 2, session_id="LOOP-MKT", ioi_id=ioi, symbol="IBM", side="1", qty="M"))["ioi_id"]
        await hand.do("cancel_ioi", 1, session_id="LOOP-MKT", ioi_id=new, text="done")
        result = await written(hand, "market", "ioi")
        assert body(result["source"]) == [
            "run",
            "    ioi symbol: 'IBM', side: buy, qty: 'L', price: 10.5, quality: high, qualifiers: 'A,X'",
            "    after 2s",
            "    replace ioi symbol: 'IBM', side: buy, qty: 'M'",
            "    after 1s",
            "    cancel ioi text: 'done'"]
        assert result["source"].splitlines()[0].endswith(": 1 IOI, 2 actions.")
        sc, diags = macro.check(result["source"], side="market")
        assert diags == [] and sc.needs_session

    @pytest.mark.asyncio
    async def test_a_sent_advert(self, hand):
        adv = (await hand.do("send_advert", session_id="LOOP-MKT", symbol="IBM", side="B", qty=6000, price=10))["adv_id"]
        await hand.do("cancel_advert", 3, session_id="LOOP-MKT", adv_id=adv)
        result = await written(hand, "market", "advert")
        lines = body(result["source"])
        assert lines[0] == "run" and lines[1].startswith("    advert symbol: 'IBM', side: buy, qty: 6000, price: 10")
        assert lines[2:] == ["    after 3s", "    cancel advert"]
        assert macro.check(result["source"], side="market")[1] == []

    @pytest.mark.asyncio
    async def test_a_sent_allocation_and_the_ack_it_heard(self, hand):
        async def work():
            alloc = (await hand.do("send_allocation", session_id="LOOP-MKT", symbol="IBM", side="1", qty=100, avg_price=10,
                                   allocs="A 60; B 40", orders="C1 O1"))["alloc_id"]
            await hand.do("accept_allocation", 0.5, session_id="LOOP-CLI", alloc_id=alloc, alloc_status="0")
        result = await same(hand, "market", work, "allocation")
        assert body(result["source"])[2:] == ["    expect accepted within 5s", "    pass"]

    @pytest.mark.asyncio
    async def test_a_trade_date_is_written_only_when_it_was_not_the_days_own(self, hand):
        await hand.do("send_allocation", session_id="LOOP-MKT", symbol="IBM", side="1", qty=100, avg_price=10, allocs="A 100")
        await hand.do("send_allocation", 1, session_id="LOOP-MKT", symbol="MSFT", side="1", qty=100, avg_price=10,
                      allocs="A 100", trade_date="20260102")
        lines = body((await written(hand, "market", "allocation"))["source"])
        assert lines[1] == "    allocate symbol: 'IBM', side: buy, qty: 100, avg_price: 10, accounts: 'A 100'"
        assert lines[-1] == ("    allocate symbol: 'MSFT', side: buy, qty: 100, avg_price: 10, trade_date: '20260102', "
                             "accounts: 'A 100'")

    @pytest.mark.asyncio
    async def test_a_received_allocation_and_the_answer_given(self, hand):
        async def work():
            alloc = (await hand.do("send_allocation", session_id="LOOP-MKT", symbol="IBM", side="1", qty=100, avg_price=10,
                                   allocs="A 100"))["alloc_id"]
            await hand.do("reject_allocation", 0.5, session_id="LOOP-CLI", alloc_id=alloc, alloc_status="2",
                          alloc_rej_code="1", text="short")
        result = await same(hand, "client", work, "allocation")
        assert body(result["source"]) == [
            "on allocation where symbol == 'IBM'",
            "    reject allocation status: account_level_reject, reason: incorrect_quantity, text: 'short'"]

    @pytest.mark.asyncio
    async def test_an_allocation_replaced_and_cancelled_from_both_ends(self, hand):
        async def work():
            alloc = (await hand.do("send_allocation", session_id="LOOP-MKT", symbol="IBM", side="1", qty=100, avg_price=10,
                                   allocs="A 100"))["alloc_id"]
            await hand.do("accept_allocation", 0.5, session_id="LOOP-CLI", alloc_id=alloc, alloc_status="0")
            asked = (await hand.do("replace_allocation", 2, session_id="LOOP-MKT", alloc_id=alloc, symbol="IBM", side="1",
                                   qty=100, avg_price=10, allocs="A 60; B 40"))["alloc_id"]
            await hand.do("accept_allocation", 0.5, session_id="LOOP-CLI", alloc_id=alloc, alloc_status="0")
            await hand.do("cancel_allocation", 2, session_id="LOOP-MKT", alloc_id=asked, text="booked twice")
            await hand.do("accept_allocation", 0.5, session_id="LOOP-CLI", alloc_id=asked, alloc_status="0")
        market, client = hand.recorder("market"), hand.recorder("client")
        await work()
        desk, check = await market.stop("desk"), await client.stop("check")
        sent, received = await written(hand, "market", "allocation"), await written(hand, "client", "allocation")
        assert body(sent["source"]) == body(desk["source"]) and body(received["source"]) == body(check["source"])
        lines = body(sent["source"])
        assert lines[0] == "run" and lines[1].startswith("    allocate symbol: 'IBM', side: buy, qty: 100, avg_price: 10")
        assert [line.split(" symbol")[0].split(" text")[0] for line in lines[2:]] == [
            "    expect accepted within 5s", "    replace allocation", "    expect accepted within 5s",
            "    cancel allocation", "    expect accepted within 5s", "    pass"]
        assert "accounts: 'A 60; B 40'" in lines[3] and lines[5] == "    cancel allocation text: 'booked twice'"
        assert body(received["source"])[0] == "on allocation where symbol == 'IBM'"
        assert "    when replace" in body(received["source"]) and "    when cancel" in body(received["source"])
        for source, side in ((sent["source"], "market"), (received["source"], "client")):
            assert macro.check(source, side=side)[1] == []

    @pytest.mark.asyncio
    async def test_a_received_ioi_that_was_replaced_before_it_was_answered(self, hand):
        ioi = (await hand.do("send_ioi", session_id="LOOP-MKT", symbol="IBM", side="1", qty="L", price=10.5))["ioi_id"]
        new = (await hand.do("replace_ioi", 1, session_id="LOOP-MKT", ioi_id=ioi, symbol="IBM", side="1", qty="M", price=10.4))["ioi_id"]
        await hand.do("send_new_order", 0.5, session_id="LOOP-CLI", symbol="IBM", side="2", qty=500, price=10.4,
                      extra_tags=f"23={new}|9001=x")
        await hand.do("cancel_ioi", 1, session_id="LOOP-MKT", ioi_id=new)
        result = await written(hand, "client", "ioi")
        assert body(result["source"]) == [
            "on ioi where symbol == 'IBM'",
            "    wait replaced",                        # heard before the order went: what triggered it
            "    new symbol: 'IBM', side: sell, qty: 500, type: limit, price: 10.4, tif: day, extra: '9001=x'"], \
            "the engine names the IOI on the order itself; the cancel heard after it is a wait nothing follows"
        assert macro.check(result["source"], side="client")[1] == []

    @pytest.mark.asyncio
    async def test_a_session_that_is_gone_still_has_its_messages(self, hand):
        hand.pair.mkt.config = hand.pair.cli.config = {"client_tags": "109"}
        cl = await hand.new(client="ACME")
        await hand.market("accept_request", cl, wait=1, text="working")
        del hand.engine.sessions["LOOP-MKT"]
        lines = body((await written(hand, "market"))["source"])
        assert lines[0] == "on order where symbol == 'IBM'"
        # with no session to say where its client rides, the tag is one more the message carried
        assert lines[1] == "    accept text: 'working', extra: '109=ACME'"

    @pytest.mark.asyncio
    async def test_the_order_that_answered_a_received_ioi(self, hand):
        ioi = (await hand.do("send_ioi", session_id="LOOP-MKT", symbol="IBM", side="1", qty="L", price=10.5))["ioi_id"]
        await hand.do("send_new_order", 1.5, session_id="LOOP-CLI", symbol="IBM", side="2", qty=500, price=10.5,
                      extra_tags=f"23={ioi}")
        await hand.do("send_ioi", 1, session_id="LOOP-MKT", symbol="MSFT", side="2", qty="S")     # nothing done to this one
        result = await written(hand, "client", "ioi")
        assert body(result["source"]) == [
            "on ioi where symbol == 'IBM'",
            "    new symbol: 'IBM', side: sell, qty: 500, type: limit, price: 10.5, tif: day"]
        assert result["orders"] == 1 and len(result["left_out"]) == 1
        assert macro.check(result["source"], side="client")[1] == []


class TestRefusals:
    @pytest.mark.asyncio
    async def test_a_row_of_the_other_side_a_row_that_is_gone_and_none(self, hand):
        await lifecycle(hand)
        (received,), (sent,) = await ids(hand, "fix_orders", "RX"), await ids(hand, "fix_orders", "TX")
        with pytest.raises(ValueError, match="was sent: a macro for it is a client macro"):
            await FromHistory(hand.engine, "market").write("order", [sent])
        with pytest.raises(ValueError, match="was received: a macro for it is a market macro"):
            await FromHistory(hand.engine, "client").write("order", [received])
        with pytest.raises(ValueError, match="No order with row 999: archived, perhaps"):
            await FromHistory(hand.engine, "market").write("order", [received, 999])
        with pytest.raises(ValueError, match="Choose the orders"):
            await FromHistory(hand.engine, "market").write("order", [])
        with pytest.raises(ValueError, match="not 'trade'"):
            await FromHistory(hand.engine, "market").write("trade", [1])
        with pytest.raises(ValueError, match="not 'both'"):
            FromHistory(hand.engine, "both")

    @pytest.mark.asyncio
    async def test_messages_that_are_gone(self, hand):
        await lifecycle(hand)
        await hand.engine.writer.flush() if hasattr(hand.engine.writer, "flush") else None
        await hand.pair.db.write_conn.execute("DELETE FROM fix_messages WHERE msg_type = 'D'")
        await hand.pair.db.write_conn.commit()
        result = await written(hand, "market")
        assert result["orders"] == 0 and "its NewOrderSingle is not among the messages kept" in result["left_out"][0]

    @pytest.mark.asyncio
    async def test_an_id_that_came_round_again_is_not_this_orders(self, hand):
        """A counterparty's ClOrdIDs repeat from day to day: the messages of
        the order that held the ID before are not this one's."""
        stale = parse_fix("8=FIX.4.2|35=8|11=RTMA00000001|17=OLD|150=8|39=8|58=yesterday's")
        hand.now -= 86400
        await hand.engine.record_message("LOOP-MKT", "TX", stale)
        hand.now += 86400
        cl = await hand.new()
        assert cl == "RTMA00000001"
        await hand.market("accept_request", cl, wait=1)
        assert body((await written(hand, "market"))["source"])[1:] == ["    accept"]


    @pytest.mark.asyncio
    async def test_the_next_order_under_the_same_id_ends_this_ones_messages(self, hand):
        cl = await hand.new()
        await hand.market("accept_request", cl, wait=1)
        # tomorrow the counterparty uses the ID again, and that order is refused
        hand.now += 86400
        again = parse_fix(f"8=FIX.4.2|35=D|11={cl}|55=IBM|54=1|38=100|40=2|44=10|59=0")
        await hand.engine.record_message("LOOP-MKT", "RX", again)
        await hand.engine.record_message("LOOP-MKT", "TX", parse_fix(f"8=FIX.4.2|35=8|11={cl}|17=T2|150=8|39=8|58=duplicate"))
        (first,) = await ids(hand, "fix_orders", "RX")
        assert body((await written(hand, "market", rows=[first]))["source"])[1:] == ["    accept"]

    @pytest.mark.asyncio
    async def test_an_order_message_replay_sent_is_written_from_its_own_new_order(self, hand):
        """Replay sends around the engine: the order's row is made by the
        first report on it, which may come long after the order went out."""
        sent = parse_fix("8=FIX.4.2|35=D|11=REPLAY-1|55=IBM|54=1|38=100|40=2|44=10|59=0|21=1")
        await hand.engine.record_message("LOOP-CLI", "TX", sent)
        hand.now += 90
        for wait, text in ((0, "8=FIX.4.2|35=8|11=REPLAY-1|37=V1|17=E1|150=0|39=0|55=IBM|54=1|38=100|14=0|151=100|6=0"),
                           (2, "8=FIX.4.2|35=8|11=REPLAY-1|37=V1|17=E2|150=2|39=2|55=IBM|54=1|38=100|32=100|31=10|14=100|151=0|6=10")):
            hand.now += wait
            report = parse_fix(text)
            await hand.engine.record_message("LOOP-CLI", "RX", report)
            await hand.engine.on_app_message(hand.pair.cli, "8", report)
        (row,) = await ids(hand, "fix_orders", "TX")
        assert body((await written(hand, "client", rows=[row]))["source"]) == [
            "run",
            "    new symbol: 'IBM', side: buy, qty: 100, type: limit, price: 10, tif: day",
            "    expect ack within 4.5m", "    expect filled within 6s", "    pass"]


class TestThroughTheManager:
    @pytest_asyncio.fixture
    async def manager(self, hand):
        manager = MacroManager(hand.engine)
        manager.runner = hand.pair.runner
        await manager.start()
        yield manager
        await manager.stop()

    @pytest.mark.asyncio
    async def test_it_is_saved_under_the_name_given_on_the_side_it_is_for(self, hand, manager):
        await lifecycle(hand)
        (received,), (sent,) = await ids(hand, "fix_orders", "RX"), await ids(hand, "fix_orders", "TX")
        result = await manager.from_history("market", "order", str(received), name="From history", save=True)
        assert (result["saved"], result["side"], result["orders"], result["actions"]) == (True, "market", 1, 4)
        # without a side it is the side the row is of; rows come as a list, or joined by commas
        result = await manager.from_history("", "order", [sent], name="Chase", save=True, delays=True)
        assert (result["saved"], result["side"]) == (True, "client") and "    after 1s" in result["source"]
        kept = {r["name"]: r for r in await _fetch_all(hand.pair.db, "SELECT name, side, problems, source FROM fix_macros")}
        assert {n: (r["side"], r["problems"]) for n, r in kept.items()} == {"From history": ("market", 0), "Chase": ("client", 0)}
        assert kept["Chase"]["source"] == result["source"]

    @pytest.mark.asyncio
    async def test_a_taken_name_a_bad_one_and_nothing_to_write_save_nothing(self, hand, manager):
        await lifecycle(hand)
        untouched = await hand.new(symbol="MSFT")
        first, second = await ids(hand, "fix_orders", "RX")
        await manager.from_history("market", "order", [first], name="Venue", save=True)
        with pytest.raises(ValueError, match="A macro is already called 'Venue'"):
            await manager.from_history("market", "order", [first], name="Venue", save=True)
        with pytest.raises(ValueError, match="cannot name a macro"):
            await manager.from_history("market", "order", [first], name="a/b", save=True)
        with pytest.raises(ValueError, match=f"Nothing to write a macro from: {untouched}: nothing was done to it"):
            await manager.from_history("market", "order", [second], name="Empty", save=True)
        assert [r["name"] for r in await _fetch_all(hand.pair.db, "SELECT name FROM fix_macros")] == ["Venue"]
        # unsaved it is only read: the text, whatever it holds
        assert (await manager.from_history("market", "order", [second]))["saved"] is False

    @pytest.mark.asyncio
    async def test_fix_cmd_takes_what_the_dialog_submits(self, hand, manager):
        from mkfix.services.fix_command import FixCommandService
        await lifecycle(hand)
        (received,) = await ids(hand, "fix_orders", "RX")
        hand.engine.macros = manager
        service = FixCommandService.__new__(FixCommandService)
        service._engine = hand.engine
        reply = await service._dispatch("macro_from_history", {
            "side": "market", "subject": "order", "row_ids": str(received), "name": "Venue", "save": "1", "delays": "false"})
        assert (reply["ok"], reply["saved"], reply["name"]) == (True, True, "Venue")
