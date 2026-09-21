"""The engine groundwork scripted macros stand on: `FixEngine.perform`
(one way in for every order and trade action), the event bus (what happened,
announced after its writes), and the market-side order lock."""

import asyncio

import pytest

from mkfix.fix.actions import ACTIONS, ORDER_KEY, TRADE_KEY
from mkfix.fix.events import EngineEvent, EventBus, report_kinds
from mkfix.fix.message import parse_fix
from mkfix.services.fix_command import TEMPLATE_TERMS

from tests.test_engine import (  # noqa: F401  (stack is a fixture)
    CLIENT_FILL_RX, NEW_ORDER_RX, StubSession, _cancel_reject, _er, _fetch_all, _order, stack,
)


def _listen(engine):
    seen = []
    engine.events.subscribe(seen.append)
    return seen


def _kinds(seen):
    return [e.kinds for e in seen]


async def _received_order(engine, stub):
    engine.sessions["S1"] = stub
    await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))


# -- the bus ------------------------------------------------------------------

class TestEventBus:
    def test_idle_until_someone_listens(self):
        bus = EventBus()
        assert not bus.active
        unsubscribe = bus.subscribe(lambda e: None)
        assert bus.active
        unsubscribe()
        unsubscribe()
        assert not bus.active

    def test_a_failing_listener_cannot_fail_the_engine(self, caplog):
        bus, seen = EventBus(), []
        bus.subscribe(lambda e: 1 / 0)
        bus.subscribe(seen.append)
        bus.emit(EngineEvent(("order", "message"), "S1"))
        assert [e.kind for e in seen] == ["order"], "the listeners after it still hear"
        assert "event listener failed on order" in caplog.text

    def test_event_names_and_key(self):
        e = EngineEvent(("fill", "filled", "er"), "S1", order={"id": 7, "cl_ord_id": "C1"})
        assert (e.kind, e.order_key, e.source) == ("fill", 7, "wire")
        assert EngineEvent(("message",), "S1").order_key is None

    @pytest.mark.parametrize("tags, kinds", [
        ("150=0|39=0", ("ack", "er")),
        ("150=8|39=8", ("rejected", "er")),
        ("150=4|39=4", ("canceled", "er")),
        ("150=5|39=5", ("replaced", "er")),
        ("150=5|39=0", ("replaced", "er")),                 # FIX 4.4: no OrdStatus Replaced
        ("150=6|39=6", ("pending", "er")),
        ("150=E|39=E", ("pending", "er")),
        ("150=A|39=A", ("pending", "er")),
        ("150=C|39=C", ("expired", "er")),
        ("150=3|39=3", ("done for day", "er")),
        ("150=D|39=1", ("restated", "er")),
        ("150=1|39=1", ("fill", "er")),
        ("150=2|39=2", ("fill", "filled", "er")),
        ("150=F|39=1", ("fill", "er")),                     # 4.3+: Trade
        ("150=F|39=2", ("fill", "filled", "er")),
        ("150=G|39=2", ("corrected", "er")),
        ("150=H|39=1", ("busted", "er")),
        ("20=2|150=2|39=2", ("corrected", "er")),           # through 4.2: ExecTransType
        ("20=1|150=1|39=1", ("busted", "er")),
        ("20=0|39=2", ("fill", "filled", "er")),            # 4.0/4.1: no ExecType at all
        ("20=0|39=5", ("replaced", "er")),
        ("150=Z|39=Z", ("er",)),                            # unknown: still a report
    ])
    def test_report_kinds(self, tags, kinds):
        assert report_kinds(parse_fix(f"8=FIX.4.2|35=8|11=C1|{tags}")) == kinds


# -- what the engine announces --------------------------------------------------

class TestInboundEvents:
    @pytest.mark.asyncio
    async def test_nothing_is_read_or_said_without_a_listener(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        loads = []
        real = engine._find_order
        async def counting(*a):
            loads.append(a)
            return await real(*a)
        engine._find_order = counting
        await _received_order(engine, stub)
        assert loads == [], "an event's row reads are only paid for when someone listens"

    @pytest.mark.asyncio
    async def test_received_order_then_its_requests(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        seen = _listen(engine)
        await _received_order(engine, stub)
        await engine.on_app_message(
            stub, "G", parse_fix("8=FIX.4.2|35=G|11=C101|41=C100|55=AAPL|54=1|38=300|40=2|44=151"))
        await engine.on_app_message(stub, "F", parse_fix("8=FIX.4.2|35=F|11=C102|41=C100|55=AAPL|54=1|38=100"))
        assert _kinds(seen) == [("order", "message"), ("replace", "message"), ("cancel", "message")]
        new, replace, cancel = seen
        assert new.prev is None and new.order["pending_action"] == "New" and new.request == "C100"
        assert (replace.prev["pending_action"], replace.order["pending_action"]) == ("New", "Replace")
        assert replace.request == "C101" and replace.order["pending_qty"] == 300.0
        assert cancel.order["pending_cl_ord_id"] == "C102"
        assert len({e.order_key for e in seen}) == 1, "one order, whatever its requests are called"
        assert all(e.source == "wire" and e.msg is not None for e in seen)

    @pytest.mark.asyncio
    async def test_request_for_an_unknown_order_is_only_a_message(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        seen = _listen(engine)
        await engine.on_app_message(stub, "F", parse_fix("8=FIX.4.2|35=F|11=C9|41=GHOST|55=AAPL|54=1|38=1"))
        assert _kinds(seen) == [("message",)]
        assert seen[0].order is None and seen[0].detail == {"unknown_order": "GHOST", "request": "cancel"}
        assert stub.sent[-1]["35"] == "9", "and it is still auto-rejected"

    @pytest.mark.asyncio
    async def test_reports_carry_the_order_before_and_after(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        c1 = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
        seen = _listen(engine)
        await engine.on_app_message(stub, "8", _er(c1, "0", "0"))
        await engine.on_app_message(stub, "8", _er(c1, "1", "1", cum=40, extra="|32=40|31=150"))
        await engine.on_app_message(stub, "8", _er(c1, "2", "2", cum=100, extra="|32=60|31=150"))
        assert _kinds(seen) == [("ack", "er"), ("fill", "er"), ("fill", "filled", "er")]
        ack, part, full = seen
        assert ack.prev["status"] == "PendingNew" and ack.order["status"] == "New" and ack.trade is None
        assert (part.prev["cum_qty"], part.order["cum_qty"], part.trade["last_qty"]) == (0.0, 40.0, 40.0)
        assert (full.prev["cum_qty"], full.order["cum_qty"]) == (40.0, 100.0)
        assert full.trade["exec_id"] != part.trade["exec_id"]

    @pytest.mark.asyncio
    async def test_a_restated_fill_is_tellable_from_a_real_one(self, stack):
        # A re-notified fill is recorded as a new trade; prev is what lets a
        # script see that CumQty did not move.
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "8", parse_fix(CLIENT_FILL_RX))
        seen = _listen(engine)
        await engine.on_app_message(stub, "8", parse_fix(CLIENT_FILL_RX.replace("17=E1", "17=E9")))
        (again,) = seen
        assert again.kind == "fill" and again.prev["cum_qty"] == again.order["cum_qty"]

    @pytest.mark.asyncio
    async def test_accept_and_reject_of_a_request(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        c1 = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
        await engine.on_app_message(stub, "8", _er(c1, "0", "0"))
        r1 = await engine.send_cancel_replace("S1", c1, symbol="AAPL", side="1", qty=200, price=151.0)
        seen = _listen(engine)
        await engine.on_app_message(stub, "8", _er(r1, "E", "E", orig=c1))
        await engine.on_app_message(stub, "9", _cancel_reject(r1, c1, status="0"))
        r2 = await engine.send_cancel_replace("S1", c1, symbol="AAPL", side="1", qty=300, price=152.0)
        await engine.on_app_message(stub, "8", _er(r2, "5", "5", orig=c1, qty=300))
        assert _kinds(seen) == [("pending", "er"), ("cancel rejected", "message"), ("replaced", "er")]
        pending, rejected, replaced = seen
        assert pending.order["cl_ord_id"] == c1 and pending.request == r1
        assert rejected.detail == {"response_to": "replace", "reason": "TooLateToCancel"}
        assert rejected.request == r1 and rejected.prev["pending_action"] == "Replace"
        assert rejected.order["pending_action"] == "" and rejected.order["status"] == "New"
        assert replaced.request == r2 and replaced.order["cl_ord_id"] == r2 and replaced.prev["cl_ord_id"] == c1
        assert pending.order_key == rejected.order_key == replaced.order_key

    @pytest.mark.asyncio
    async def test_a_report_that_creates_its_order_announces_the_order_first(self, stack):
        # Message Replay sends around send_new_order: the row first exists
        # when the counterparty answers, which is when a script can take it.
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        seen = _listen(engine)
        await engine.on_app_message(stub, "8", _er("REPLAYED1", "0", "0"))
        await engine.on_app_message(stub, "8", _er("REPLAYED1", "1", "1", cum=10, extra="|32=10|31=150"))
        assert _kinds(seen) == [("sent order",), ("ack", "er"), ("fill", "er")]
        assert seen[0].order["cl_ord_id"] == "REPLAYED1" and seen[0].order["direction"] == "TX"
        assert seen[0].order_key == seen[1].order_key == seen[2].order_key

    @pytest.mark.asyncio
    async def test_inbound_dispute(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        await _received_order(engine, stub)
        await engine.accept_order("S1", "C100")
        exec_id = await engine.fill_order("S1", "C100", 100, 150.25)
        seen = _listen(engine)
        await engine.on_app_message(
            stub, "Q", parse_fix(f"8=FIX.4.2|35=Q|37=X|17={exec_id}|127=D|55=AAPL|54=1|38=100|58=who?"))
        await engine.on_app_message(stub, "Q", parse_fix("8=FIX.4.2|35=Q|37=X|17=NOPE|127=D|55=AAPL|54=1"))
        assert _kinds(seen) == [("dk", "message")], "a dispute naming nothing is no event"
        (dk,) = seen
        assert dk.trade["dk_reason"] == "NoMatchingOrder" and dk.detail == {"reason": "NoMatchingOrder", "text": "who?"}
        assert dk.order["cl_ord_id"] == "C100"

    @pytest.mark.asyncio
    async def test_event_rows_are_what_a_read_returns(self, stack):
        # Announced after the write commits: what the event says, a query sees.
        db, writer, engine = stack
        stub = StubSession()
        checked = []
        def check(event):
            checked.append(event.order)
        engine.events.subscribe(check)
        await _received_order(engine, stub)
        assert checked == [await _order(db)]


# -- perform ----------------------------------------------------------------------

class TestPerform:
    def test_the_table_covers_every_dialog_and_names_its_subject(self):
        assert set(TEMPLATE_TERMS) <= set(ACTIONS), "every op a dialog submits is an action"
        assert set(ACTIONS) == set(ORDER_KEY) | set(TRADE_KEY) | {"send_new_order"}
        assert not set(ORDER_KEY) & set(TRADE_KEY)

    @pytest.mark.asyncio
    async def test_unknown_action(self, stack):
        db, writer, engine = stack
        with pytest.raises(ValueError, match="Unknown command: nope"):
            await engine.perform("nope", {})

    @pytest.mark.asyncio
    async def test_strings_from_a_dialog_become_terms(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        await _received_order(engine, stub)
        assert await engine.perform("accept_request", {"session_id": "S1", "cl_ord_id": "C100"}) \
            == {"exec_id": (await _order(db))["order_id"]}
        result = await engine.perform("fill_order", {
            "session_id": "S1", "cl_ord_id": "C100", "qty": "40", "price": "150.5", "text": "partial"})
        assert set(result) == {"exec_id"}
        msg = stub.sent[-1]
        assert (msg["32"], msg["31"], msg["58"]) == ("40", "150.5", "partial")
        assert (await _order(db))["cum_qty"] == 40.0

    @pytest.mark.asyncio
    async def test_refusals_are_the_engines_own(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        await _received_order(engine, stub)
        with pytest.raises(ValueError, match="No pending"):
            await engine.perform("accept_cancel", {"session_id": "S1", "cl_ord_id": "C100"})
        stub.is_active = False
        with pytest.raises(ValueError, match="not active"):
            await engine.perform("fill_order", {"session_id": "S1", "cl_ord_id": "C100", "qty": 1, "price": 1})

    @pytest.mark.asyncio
    async def test_a_sent_order_is_announced_before_it_goes_out(self, stack):
        # The counterparty may acknowledge inside the send: whoever takes the
        # order must already have been told about it.
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        seen, on_the_wire = _listen(engine), []
        plain = stub.send_message
        async def send(msg):
            on_the_wire.append([e.kinds for e in seen])
            return await plain(msg)
        stub.send_message = send
        result = await engine.perform("send_new_order", {
            "session_id": "S1", "symbol": "AAPL", "side": "1", "qty": "100", "price": "150", "_tag": "t7"},
            source="macro")
        assert on_the_wire == [[("sent order",)]]
        sent, acted = seen
        assert sent.source == "macro" and sent.detail == {"tag": "t7"} and sent.prev is None
        assert sent.order["cl_ord_id"] == result["cl_ord_id"] == sent.request and sent.order["status"] == "PendingNew"
        # the terms as given, for whoever writes down what was done (the recorder); nothing internal
        assert acted.kinds == ("action",) and acted.detail == {
            "op": "send_new_order", "result": result, "trade_before": None,
            "data": {"session_id": "S1", "symbol": "AAPL", "side": "1", "qty": "100", "price": "150"}}
        assert acted.order_key == sent.order_key

    @pytest.mark.asyncio
    async def test_an_order_sent_by_hand_says_so(self, stack):
        db, writer, engine = stack
        engine.sessions["S1"] = StubSession()
        seen = _listen(engine)
        await engine.perform("send_new_order", {"session_id": "S1", "symbol": "AAPL", "side": "1", "qty": 1, "price": 1})
        await engine.send_new_order("S1", symbol="AAPL", side="1", qty=1, price=1)
        assert [(e.kind, e.source, e.detail.get("tag")) for e in seen if e.kind == "sent order"] == [
            ("sent order", "manual", ""), ("sent order", "manual", "")]

    @pytest.mark.asyncio
    async def test_an_action_is_announced_on_the_order_it_renamed(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        await _received_order(engine, stub)
        await engine.perform("accept_request", {"session_id": "S1", "cl_ord_id": "C100"})
        await engine.on_app_message(
            stub, "G", parse_fix("8=FIX.4.2|35=G|11=C101|41=C100|55=AAPL|54=1|38=300|40=2|44=151"))
        seen = _listen(engine)
        await engine.perform("accept_request", {"session_id": "S1", "cl_ord_id": "C100"})
        (event,) = seen
        assert event.kinds == ("action",) and event.source == "manual" and event.detail["op"] == "accept_request"
        assert (event.prev["cl_ord_id"], event.order["cl_ord_id"]) == ("C100", "C101"), \
            "found before the rename, re-read after it by row id"
        assert event.order["order_qty"] == 300.0

    @pytest.mark.asyncio
    async def test_trade_actions_name_their_trade_and_order(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        await _received_order(engine, stub)
        await engine.accept_order("S1", "C100")
        exec_id = await engine.fill_order("S1", "C100", 100, 150.25)
        seen = _listen(engine)
        result = await engine.perform("bust_trade", {"session_id": "S1", "exec_id": exec_id})
        (event,) = seen
        assert event.trade["exec_id"] == result["exec_id"] and event.trade["exec_type"] == "Cancel"
        assert event.order["cl_ord_id"] == "C100" and event.order["cum_qty"] == 0.0 and event.prev["cum_qty"] == 100.0

    @pytest.mark.asyncio
    async def test_a_refused_action_announces_nothing(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        await _received_order(engine, stub)
        seen = _listen(engine)
        with pytest.raises(ValueError):
            await engine.perform("accept_cancel", {"session_id": "S1", "cl_ord_id": "C100"})
        assert seen == []


# -- the order lock -----------------------------------------------------------------

class TestOrderLock:
    """A market-side action writes ORDER_UPDATE_COLS back from the snapshot it
    loaded. A request the read loop parks between that load and that write
    used to lose its pending_* columns; the session's order lock makes the
    handler wait for the action's writes."""

    async def _racing(self, engine, stub, request):
        """Make the next order load start `request` on the read loop's side
        and give it every chance to finish before the action goes on."""
        real, started = engine._load_order, []
        async def load(session_id, cl_ord_id):
            row = await real(session_id, cl_ord_id)
            if not started:
                started.append(asyncio.create_task(
                    engine.on_app_message(stub, request["35"], request)))
                await asyncio.wait(started, timeout=0.2)
            return row
        engine._load_order = load
        return started

    @pytest.mark.asyncio
    @pytest.mark.parametrize("act", [
        lambda e: e.fill_order("S1", "C100", 40, 150.25),
        lambda e: e.restate_order("S1", "C100", 80, 150.25, "5"),
        lambda e: e.perform("fill_order", {"session_id": "S1", "cl_ord_id": "C100", "qty": 40, "price": 150.25}),
    ])
    async def test_request_arriving_mid_action_keeps_its_place(self, stack, act):
        db, writer, engine = stack
        stub = StubSession()
        await _received_order(engine, stub)
        await engine.accept_order("S1", "C100")
        request = parse_fix("8=FIX.4.2|35=F|11=C200|41=C100|55=AAPL|54=1|38=100")
        started = await self._racing(engine, stub, request)
        await act(engine)
        await started[0]
        row = await _order(db)
        assert (row["pending_action"], row["pending_cl_ord_id"]) == ("Cancel", "C200")
        assert stub.sent[-1]["35"] == "8", "and the action went through"

    @pytest.mark.asyncio
    async def test_the_lock_is_not_held_across_the_send(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        await _received_order(engine, stub)
        held = []
        plain = stub.send_message
        async def send(msg):
            held.append(engine._order_lock("S1").locked())
            return await plain(msg)
        stub.send_message = send
        await engine.accept_order("S1", "C100")
        await engine.fill_order("S1", "C100", 10, 150.25)
        assert held == [False, False], "the counterparty's answer, handled inside a send, needs this lock"

    @pytest.mark.asyncio
    async def test_a_refused_action_releases_the_lock(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        await _received_order(engine, stub)
        with pytest.raises(ValueError):
            await engine.accept_cancel("S1", "C100")
        assert not engine._order_lock("S1").locked()
        await engine.accept_order("S1", "C100")

    @pytest.mark.asyncio
    async def test_sessions_do_not_wait_on_each_other(self, stack):
        db, writer, engine = stack
        assert engine._order_lock("S1") is engine._order_lock("S1")
        assert engine._order_lock("S1") is not engine._order_lock("S2")
