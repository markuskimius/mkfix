"""Macros of one run working together: a signal one says and the others
hear, values they share, the rows the others hold, and blocks a signal
starts."""

import pytest

from mkfix import macro
from mkfix.macro import vocab
from mkfix.macro.instance import COMPLETED, FAILED, LISTENING, PASSED, RUNNING
from mkfix.macro.nodes import Share, Signal
from mkfix.macro.runner import MacroError

from tests.test_engine import _fetch_all, stack  # noqa: F401
from tests.test_macro_sending import MARKET, pair  # noqa: F401

NEW = "new symbol: 'IBM', side: buy, qty: 100, price: 10"


def problems(text, **kw):
    return [(d.severity, d.line, d.message) for d in macro.check(text, **kw)[1]]


def clean(text, **kw):
    parsed, found = macro.check(text, **kw)
    assert found == [], [str(d) for d in found]
    return parsed


class TestTheWords:
    def test_signal_and_share_are_statements_and_a_signal_is_an_event(self):
        parsed = clean("share done = 0\n"
                       f"run\n    {NEW}\n    expect filled within 30s\n    share done = shared.done + 1\n"
                       "    signal 'parent filled' with order.cum_qty\n    pass\n"
                       "run\n    new symbol: 'IBM', side: sell, qty: 100, price: 11\n"
                       "    when signal 'parent filled' or cancel rejected and event.value >= 100\n        cancel\n"
                       "    wait signal where event.name == 'x' or timeout 5s\n"
                       "    expect signal \"it's done\" or signal 'parent filled' within 2s\n"
                       "    signal \"it's done\"\n")
        assert [(s.name, s.value.source) for s in parsed.shares] == [("done", "0")]
        first, second = parsed.blocks
        share, signal = first.body[2], first.body[3]
        assert isinstance(share, Share) and (share.name, share.value.source) == ("done", "shared.done + 1")
        assert isinstance(signal, Signal) and (signal.name, signal.value.source) == ("parent filled", "order.cum_qty")
        assert second.body[1].events == ["signal:parent filled", "cancel rejected"]
        assert second.body[1].guard.source == "event.value >= 100"
        assert second.body[2].events == ["signal"] and second.body[2].where.source == "event.name == 'x'"
        assert second.body[3].events == ["signal:it's done", "signal:parent filled"]
        assert second.body[4].value is None
        assert vocab.signal_event("a b") == "signal:a b" and vocab.event_of("signal:a b") is vocab.EVENTS["signal"]
        assert vocab.event_of("fill") is vocab.EVENTS["fill"] and vocab.event_of("nothing") is None

    def test_a_signal_is_heard_in_every_kind_of_block(self):
        assert vocab.EVENTS["signal"].places == frozenset(
            (subject, kind) for subject in vocab.SUBJECTS for kind in vocab.SIDES)
        clean("on order\n    accept\n    signal 'taken'\n    when signal 'taken'\n        log event.sender.cl_ord_id\n")
        clean("on allocation\n    when signal 'go'\n        accept allocation\n    signal 'go'\n", side="client")
        clean("on sent ioi\n    wait signal 'pull' or timeout 5s\n    cancel ioi\n    signal 'pull'\n", side="market")

    def test_on_signal_is_a_block_that_sends(self):
        parsed = clean("on order\n    accept\n    fill qty: order.leaves_qty, price: order.price\n"
                       "    signal 'filled' with order.cum_qty\n"
                       "on signal 'filled' where event.value > 0 and shared.open < 3\n"
                       "    share open = (shared.open ?? 0) + 1\n"
                       "    allocate symbol: event.sender.symbol, side: event.sender.side_code, qty: event.value, "
                       "avg_price: event.sender.avg_price, accounts: 'ACC1 ${event.value}'\n")
        taker, started = parsed.blocks
        assert (started.kind, started.subject, started.signal, started.session) == ("client", "allocation", "filled", None)
        assert started.where.source == "event.value > 0 and shared.open < 3"
        assert (taker.signal, parsed.side) == (None, "market")
        # it sends nothing until something signals, and it sends where the macro that signalled is
        assert (parsed.sends, parsed.needs_session) == (False, False)
        assert vocab.BLOCK_HEADERS["on signal"] == (vocab.CLIENT, None)
        both = clean(f"run\n    {NEW}\n    signal 'go'\non signal 'go'\n    {NEW}\n")
        assert (both.sends, both.needs_session) == (True, True), "its `run` block is what is run"

    @pytest.mark.parametrize("text, line, message", [
        ("run\n    signal filled\n", 2, "A signal is named in quotes: signal 'filled'"),
        ("run\n    signal ''\n", 2, "A signal needs a name: signal 'filled'"),
        ("run\n    signal 'leg ${n}'\n", 2, "A signal's name is the name itself, without ${…}: say what differs `with` a value"),
        ("run\n    signal 'a' order.qty\n", 2, "Unexpected 'order.qty'"),
        ("run\n    share = 3\n", 2, "Expected a name"),
        ("run\n    share total 3\n", 2, "Expected `=`"),
        ("run\n    share total =\n", 2, "Expected a value"),
        ("on signal\n    log 1\n", 1, "A signal is named in quotes: on signal 'filled'"),
        ("on signal 'a' on LOOP\n    log 1\n", 1, "Unexpected 'on'"),
        (f"run\n    {NEW}\nshare total = 1\n", 3,
         "`share` at the start of a line sets what the run starts with: it belongs before the first block"),
    ])
    def test_problems_of_form(self, text, line, message):
        found = [(d.line, d.message) for d in macro.parse(text)[1]]
        assert (line, message) in found, found

    def test_problems_of_meaning(self):
        found = problems("share a = order.qty\n"
                         f"run\n    {NEW}\n    when signal 'parent filed'\n        log 1\n"
                         "    log shared.nope\n    log orders[0].leave_qty\n    log event.sender.anything\n"
                         "    signal 'parent filled'\n"
                         "on signal 'x' where symbol == 'IBM'\n    log 1\n"
                         "on signal 'parent filled'\n    accept\n")
        assert ("error", 1, "Unknown field: 'order'. Available fields: shared") in found
        assert ("warning", 4, "Nothing in this macro signals 'parent filed', so this never happens: a signal is heard by "
                              "the macros of its own run. Signals sent here: 'parent filled' — did you mean "
                              "'parent filled'?") in found
        assert ("error", 6, "Unknown field: 'shared.nope'. shared has: a") in found
        assert any(sev == "error" and line == 7 and m.startswith("Unknown field: 'orders.*.leave_qty'") for sev, line, m in found)
        assert not any(line == 8 for _, line, _ in found), "the sender's row is whatever it is a row of"
        needs = ("An `on signal` block sends something of its own: it needs a `new`, `ioi`, `advert`, `allocate`, `rfq`, `new quote` or `rfq request`. To "
                 "react to a signal in a macro that already has its order, write `when signal 'NAME'` inside that block")
        assert ("error", 10, needs) in found and ("error", 12, needs) in found
        assert any(sev == "warning" and line == 10 and "Nothing in this macro signals 'x'" in m for sev, line, m in found)
        assert any(line == 10 and m.startswith("Unknown field: 'symbol'. Available fields: ") for _, line, m in found), \
            "an `on signal` block's `where` sees the signal, not a row: its subject is still to be sent"

    def test_a_trade_is_not_in_hand_under_a_signal(self):
        found = problems("on order\n    accept\n    signal 'go'\n    when signal 'go'\n        correct qty: 1\n")
        assert [m for _, line, m in found if line == 5][0].startswith("Which trade?")
        clean("on order\n    accept\n    signal 'go'\n    when signal 'go' or dk\n        renotify last trade\n")

    def test_a_block_of_the_other_side_is_still_refused(self):
        found = problems(f"on order\n    accept\n    signal 'go'\non signal 'go'\n    {NEW}\n", side="market")
        assert any(line == 4 and "this block belongs in a client macro" in m for _, line, m in found)
        # together they are an end-to-end macro, and what the order's macro says starts the block of the other side
        assert clean(f"on order\n    accept\n    signal 'go'\non signal 'go'\n    {NEW}\n").side == "end-to-end"

    def test_names_a_macro_already_used_stay_its_own(self):
        """`orders`, `shared` and the rest came with 0.68; a macro written
        before that calls something by one of them keeps working."""
        clean(f"run\n    let orders = 3\n    let shared = 'x'\n    repeat orders with iois = [1, 2]\n        {NEW}\n"
              "        log orders + iois\n        log shared\n")
        assert {"order", "trade", "event", "n"} <= vocab.RESERVED
        assert not {"shared", "orders", "iois", "adverts", "allocations"} & vocab.RESERVED
        assert set(vocab.PEERS.values()) | {"shared"} <= set(vocab.CONTEXT_DOCS)
        assert ("error", 2, "'order' is the macro's own name for something") in problems(f"run\n    let order = 1\n    {NEW}\n")

    def test_the_editor_is_told(self):
        v = macro.vocabulary()
        assert {"signal", "share", "on signal"} <= set(v["statements"])
        assert v["blocks"]["on signal"] == {"kind": "client", "subject": None, "side": None}
        assert "signal" in v["events"] and {"name", "value", "sender", "subject"} <= set(v["fields"]["event"])
        assert {"shared", "orders", "iois", "adverts", "allocations"} <= set(v["context"])


HEDGE = (f"run\n    {NEW}\n    expect filled within 30s\n    signal 'parent filled' with order.cum_qty\n    pass\n"
         "run\n    new symbol: 'IBM', side: sell, qty: 100, price: 99\n    expect ack within 5s\n"
         "    wait signal 'parent filled' or timeout 60s\n"
         "    if event.kind == 'timeout'\n        fail 'the parent never filled'\n"
         "    log 'parent ${event.sender.cl_ord_id} filled ${event.value}; pass ${event.n} of ${event.subject}'\n"
         "    cancel\n    expect canceled within 5s\n    pass\n")
# A venue that fills only what buys: the hedge, a sell, rests until it is cancelled.
BUYS_ONLY = ("on order\n    after 100ms\n    accept\n    when cancel\n        accept\n        stop\n"
             "    if order.side_code == '1'\n        after 1s\n        fill qty: order.leaves_qty, price: order.price\n")


class TestSignals:
    @pytest.mark.asyncio
    async def test_the_hedge_is_cancelled_when_the_parent_fills(self, pair):
        pair.arm(BUYS_ONLY)
        run = pair.arm(HEDGE, session="LOOP-CLI")
        await pair.advance(10)
        assert [(i.status, i.message) for i in run.instances] == [(PASSED, ""), (PASSED, "")]
        parent, hedge = await pair.orders("TX")
        assert (parent["status"], hedge["status"], hedge["cum_qty"]) == ("Filled", "Canceled", 0.0)
        texts = [text for _, _, _, text in run.log]
        assert texts == ["signal 'parent filled' with 100",
                         f"parent {parent['cl_ord_id']} filled 100; pass 0 of order"]
        assert run.log[0][1] == parent["id"], "the signal is in the log under the macro that said it"
        assert run.status == "finished" and run.verdict == PASSED

    @pytest.mark.asyncio
    async def test_a_macro_does_not_hear_itself_and_a_bare_signal_is_any(self, pair):
        pair.arm(BUYS_ONLY)
        run = pair.arm(f"run\n    {NEW}\n    signal 'one'\n    wait signal or timeout 2s\n"
                       "    pass IF(event.kind == 'timeout', 'heard nothing', CONCAT('heard ', event.name))\n"
                       f"run\n    {NEW}\n    wait signal\n    log 'heard ${{event.name}} as ${{event.kind}}'\n"
                       "    after 1s\n    signal 'two'\n    pass\n", session="LOOP-CLI")
        await pair.advance(5)
        first, second = run.instances
        assert (first.status, first.message) == (PASSED, "heard two"), "its own `one` is not what it heard"
        assert second.status == PASSED and "heard one as signal" in [t for *_, t in run.log]

    @pytest.mark.asyncio
    async def test_a_signal_said_before_the_wait_is_not_missed_and_where_chooses(self, pair):
        pair.arm(BUYS_ONLY)
        run = pair.arm(f"run\n    {NEW}\n    signal 'step' with 1\n    signal 'step' with 2\n    signal 'step' with 3\n    pass\n"
                       f"run\n    {NEW}\n    after 5s\n    wait signal 'step' where event.value == 2\n"
                       "    let first = event.value\n    wait signal 'step'\n    pass 'took ${first} then ${event.value}'\n",
                       session="LOOP-CLI")
        await pair.advance(10)
        assert [(i.status, i.message) for i in run.instances] == [(PASSED, ""), (PASSED, "took 2 then 3")]

    @pytest.mark.asyncio
    async def test_expect_fails_the_macro_when_no_signal_comes(self, pair):
        pair.arm(BUYS_ONLY)
        run = pair.arm(f"run\n    {NEW}\n    expect signal 'never' within 2s\n    pass\n"
                       f"run\n    {NEW}\n    after 10s\n    signal 'never'\n", session="LOOP-CLI")
        await pair.advance(5)
        assert run.instances[0].status == FAILED and "expected signal 'never' within 2s" in run.instances[0].message
        assert run.instances[1].waiting_for == "after 10s"

    @pytest.mark.asyncio
    async def test_what_a_macro_waits_for_is_said_as_it_was_written(self, pair):
        pair.arm(BUYS_ONLY)
        run = pair.arm(f"run\n    {NEW}\n    wait signal 'go' or signal or filled\n", session="LOOP-CLI")
        await pair.runner.settle()
        assert run.instances[0].waiting_for == "wait signal 'go' or signal or filled"

    @pytest.mark.asyncio
    async def test_handlers_take_signals_beside_the_main_flow(self, pair):
        """Question and answer between a venue's orders: a new one asks
        whether anything is being worked, the one that is says so, and the
        new one refuses itself."""
        pair.arm("on order\n    signal 'anyone working?'\n    wait signal 'busy' or timeout 200ms\n"
                 "    if event.kind != 'timeout'\n"
                 "        reject text: 'one at a time: ${event.sender.cl_ord_id} is working'\n        stop\n"
                 "    when signal 'anyone working?'\n        signal 'busy'\n    accept\n")
        run = pair.arm(f"run\n    repeat 3 every 1s\n        {NEW}\n        wait ack or rejected or timeout 5s\n"
                       "        pass event.kind\n", session="LOOP-CLI")
        await pair.advance(10)
        assert [i.message for i in run.instances] == ["ack", "rejected", "rejected"]
        first, second, third = await pair.orders("RX")
        assert [o["status"] for o in (first, second, third)] == ["New", "Rejected", "Rejected"]
        assert second["sent_text"] == third["sent_text"] == f"one at a time: {first['cl_ord_id']} is working"
        venue = pair.runner.runs[0]
        assert [i.status for i in venue.instances] == [LISTENING, "stopped", "stopped"], \
            "the first goes on answering after its last line"

    @pytest.mark.asyncio
    async def test_signals_stay_within_the_run(self, pair):
        pair.arm(BUYS_ONLY)
        text = (f"run\n    {NEW}\n    after 1s\n    signal 'go'\n    pass\n"
                f"run\n    {NEW}\n    wait signal 'go' or timeout 3s\n    pass event.kind\n")
        a = pair.arm(text, session="LOOP-CLI")
        await pair.advance(0.5)
        b = pair.arm(text, session="LOOP-CLI")
        await pair.advance(0.8)
        assert a.instances[1].message == "signal" and b.instances[1].status == RUNNING, "b has not heard a's"
        await pair.advance(5)
        assert [i.message for i in b.instances] == ["", "signal"], "a signal is a `signal` by kind: its name is event.name"
        said = lambda run: [t for *_, t in run.log if t.startswith("signal ")]  # noqa: E731
        assert said(a) == ["signal 'go'"] and said(b) == ["signal 'go'"]

    @pytest.mark.asyncio
    async def test_a_run_that_signals_for_ever_is_stopped(self, pair):
        pair.arm(BUYS_ONLY)
        pair.runner.max_signals = 50
        run = pair.arm(f"run\n    {NEW}\n    when signal 'ping'\n        signal 'pong'\n    after 60s\n"
                       f"run\n    {NEW}\n    when signal 'pong'\n        signal 'ping'\n    signal 'ping'\n    after 60s\n",
                       session="LOOP-CLI")
        await pair.advance(5)
        assert FAILED in {i.status for i in run.instances}
        assert any("more than 50 signals in one run" in i.message for i in run.instances)


class TestShared:
    @pytest.mark.asyncio
    async def test_a_value_every_macro_of_the_run_reads_and_sets(self, pair):
        pair.arm(BUYS_ONLY)
        run = pair.arm("share filled = 0\nshare target = shared.filled + 3\n"
                       f"run\n    repeat 3 every 1s\n        {NEW}\n        expect filled within 10s\n"
                       "        share filled = shared.filled + order.cum_qty\n        share last = order.cl_ord_id\n"
                       "        pass '${shared.filled} of ${shared.target * 100}'\n", session="LOOP-CLI")
        assert run.shared == {"filled": 0, "target": 3, "last": None}, "every name the macro shares, NULL until set"
        await pair.advance(10)
        assert [i.message for i in run.instances] == ["100 of 300", "200 of 300", "300 of 300"]
        assert run.shared["filled"] == 300 and run.shared["last"] == (await pair.orders("TX"))[-1]["cl_ord_id"]

    @pytest.mark.asyncio
    async def test_each_run_has_its_own(self, pair):
        pair.arm(BUYS_ONLY)
        text = f"share seen = 0\nrun\n    {NEW}\n    share seen = shared.seen + 1\n    pass shared.seen\n"
        a, b = pair.arm(text, session="LOOP-CLI"), pair.arm(text, session="LOOP-CLI")
        await pair.advance(2)
        assert (a.instances[0].message, b.instances[0].message) == ("1", "1") and a.shared is not b.shared

    @pytest.mark.asyncio
    async def test_the_lines_around_a_repeat_may_share_and_signal(self, pair):
        pair.arm(BUYS_ONLY)
        run = pair.arm("run\n    share lot = 50\n    signal 'started'\n"
                       "    repeat 2\n        new symbol: 'IBM', side: buy, qty: shared.lot, price: 10\n        pass\n",
                       session="LOOP-CLI")
        await pair.advance(2)
        assert [o["order_qty"] for o in await pair.orders("TX")] == [50.0, 50.0]
        assert [t for *_, t in run.log] == ["signal 'started'"]

    @pytest.mark.asyncio
    async def test_a_starting_value_that_cannot_be_worked_out_refuses_the_run(self, pair):
        parsed = clean(f"share a = 1 / 0\nrun on LOOP-CLI\n    {NEW}\n")
        with pytest.raises(MacroError, match="line 1: .* — in `1 / 0`"):
            pair.runner.arm(parsed)
        assert pair.runner.runs == []


class TestTheRunsRows:
    @pytest.mark.asyncio
    async def test_a_venue_that_works_one_order_a_client_at_a_time(self, pair):
        pair.arm("on order\n"
                 "    if COUNT(orders, o -> o.client == order.client and o.id != order.id and o.leaves_qty > 0 "
                 "and o.status != 'Rejected') > 0\n"
                 "        reject text: 'one at a time'\n        stop\n"
                 "    accept\n    after 3s\n    fill qty: order.leaves_qty, price: order.price\n")
        run = pair.arm("run\n    repeat 4 every 1s with who = ['ACME', 'ACME', 'ZED', 'ACME']\n"
                       "        new symbol: 'IBM', side: buy, qty: 100, price: 10, client: who\n"
                       "        wait ack or rejected\n        let first = event.kind\n"
                       "        wait filled or timeout 10s\n        pass '${who} ${first} ${order.status}'\n"
                       "    ", session="LOOP-CLI")
        await pair.advance(20)
        assert [i.message for i in run.instances] == [
            "ACME ack Filled", "ACME rejected Rejected", "ZED ack Filled", "ACME ack Filled"], \
            "the last ACME order came after the first had filled"

    @pytest.mark.asyncio
    async def test_the_rows_are_as_they_stand_and_only_this_runs(self, pair):
        pair.arm(BUYS_ONLY)
        other = pair.arm(f"run\n    {NEW}\n    pass\n", session="LOOP-CLI")
        run = pair.arm(f"run\n    repeat 3\n        {NEW}\n        expect filled within 10s\n"
                       "        pass '${LEN(orders)} orders, ${SUM(orders, o -> o.cum_qty)} filled, "
                       "${LEN(iois)} IOIs, ${LEN(trades)} trade'\n", session="LOOP-CLI")
        await pair.advance(10)
        assert len(other.instances) == 1
        assert {i.message for i in run.instances} <= {"3 orders, 100 filled, 0 IOIs, 1 trade",
                                                      "3 orders, 200 filled, 0 IOIs, 1 trade",
                                                      "3 orders, 300 filled, 0 IOIs, 1 trade"}
        assert run.instances[-1].message.startswith("3 orders, 300 filled")


class TestStartedByASignal:
    @pytest.mark.asyncio
    async def test_the_hedge_goes_out_when_the_parent_fills(self, pair):
        pair.arm("on order\n    after 100ms\n    accept\n    if order.side_code == '1'\n        after 1s\n"
                 "        fill qty: order.leaves_qty, price: order.price\n")
        run = pair.arm("run\n    repeat 2 every 3s with sym = ['IBM', 'MSFT']\n"
                       "        new symbol: sym, side: buy, qty: 100 * (n + 1), price: 10\n"
                       "        expect filled within 5s\n        signal 'filled' with order.cum_qty\n        pass\n"
                       "on signal 'filled' where event.value >= 100\n"
                       "    new symbol: event.sender.symbol, side: sell, qty: event.value, price: 12, "
                       "text: 'hedge ${n} of ${event.sender.cl_ord_id}'\n"
                       "    expect ack within 5s\n    pass 'hedged ${order.order_qty}'\n", session="LOOP-CLI")
        await pair.advance(15)
        assert [(i.status, i.message) for i in run.instances] == [
            (PASSED, ""), (PASSED, "hedged 100.0"), (PASSED, ""), (PASSED, "hedged 200.0")] or \
            sorted(i.message for i in run.instances) == ["", "", "hedged 100", "hedged 200"]
        sent = await pair.orders("TX")
        parents = [o for o in sent if o["side_code"] == "1"]
        hedges = [o for o in sent if o["side_code"] == "2"]
        assert [(o["symbol"], o["order_qty"]) for o in hedges] == [("IBM", 100.0), ("MSFT", 200.0)]
        assert [o["sent_text"] for o in hedges] == [f"hedge {n} of {p['cl_ord_id']}" for n, p in enumerate(parents)]
        assert run.status == "finished" and run.verdict == PASSED

    @pytest.mark.asyncio
    async def test_a_venue_allocates_what_it_filled_where_it_filled_it(self, pair):
        """An order macro and an allocation macro in one market macro: the
        allocation goes out on the session of the order that signalled."""
        run = pair.arm("on order\n    accept\n    fill qty: order.order_qty, price: order.price\n"
                       "    signal 'filled'\n"
                       "on signal 'filled'\n"
                       "    allocate symbol: event.sender.symbol, side: event.sender.side_code, qty: event.sender.cum_qty, "
                       "avg_price: event.sender.avg_price, orders: event.sender.cl_ord_id, "
                       "accounts: 'ACC1 ${event.sender.cum_qty}'\n"
                       "    wait accepted or rejected or timeout 5s\n    pass event.kind\n")
        assert (run.macro.side, run.macro.sends, run.waits) == ("market", False, True)
        await pair.engine.perform("send_new_order", {"session_id": "LOOP-CLI", "symbol": "IBM", "side": "1",
                                                     "qty": "300", "price": "10"})
        await pair.advance(1)
        (sent,) = await _fetch_all(pair.db, "SELECT * FROM fix_allocations WHERE direction = 'TX'")
        (order,) = await pair.orders("RX")
        assert (sent["session_id"], sent["symbol"], sent["quantity"], sent["orders"], sent["allocs"]) == (
            "LOOP-MKT", "IBM", 300.0, order["cl_ord_id"], "ACC1 300")
        taker, allocator = run.instances
        assert (taker.kind, allocator.kind, allocator.status) == ("order", "allocation", RUNNING)
        (received,) = await _fetch_all(pair.db, "SELECT * FROM fix_allocations WHERE direction = 'RX'")
        await pair.engine.perform("accept_allocation", {"session_id": "LOOP-CLI", "alloc_id": received["alloc_id"],
                                                        "alloc_status": "0"})
        await pair.runner.settle()
        assert (allocator.status, allocator.message) == (PASSED, "accepted")
        assert run.status == "armed", "it still waits for orders"

    @pytest.mark.asyncio
    async def test_where_is_asked_of_every_signal_and_may_read_what_is_shared(self, pair):
        pair.arm(BUYS_ONLY)
        run = pair.arm("share hedges = 0\n"
                       f"run\n    repeat 4 every 1s\n        {NEW}\n        signal 'sent' with n\n        pass\n"
                       "on signal 'sent' where shared.hedges < 2 and LEN(orders) >= 1\n"
                       "    share hedges = shared.hedges + 1\n"
                       "    new symbol: 'IBM', side: sell, qty: 10, price: 99, text: 'after ${event.value}'\n"
                       "    pass\n", session="LOOP-CLI")
        await pair.advance(10)
        sells = [o["sent_text"] for o in await pair.orders("TX") if o["side_code"] == "2"]
        assert sells == ["after 0", "after 1"] and run.shared["hedges"] == 2

    @pytest.mark.asyncio
    async def test_a_repeat_in_it_sends_one_for_each_pass(self, pair):
        """The sending verb inside a `repeat`: each pass is a macro of its
        own, as in a `run` block, and each has the signal that started them
        and sends where the block was started to send."""
        pair.arm(BUYS_ONLY)
        run = pair.arm(f"run\n    {NEW}\n    expect ack within 2s\n    signal 'slice' with 3\n    pass\n"
                       "on signal 'slice'\n    let lot = 10\n"
                       "    repeat event.value every 1s with px = [9, 8, 7]\n"
                       "        new symbol: event.sender.symbol, side: sell, qty: lot * (n + 1), price: px, "
                       "text: 'slice ${n} of ${event.sender.cl_ord_id}'\n"
                       "        expect ack within 2s\n        pass\n", session="LOOP-CLI")
        await pair.advance(10)
        parent, *slices = await pair.orders("TX")
        assert [(o["order_qty"], o["price"], o["sent_text"], o["session_id"]) for o in slices] == [
            (10.0 * (n + 1), px, f"slice {n} of {parent['cl_ord_id']}", "LOOP-CLI") for n, px in enumerate((9.0, 8.0, 7.0))]
        assert len(run.generators) == 1 and len(run.instances) == 4 and run.counts() == {PASSED: 4}
        assert run.status == "finished"

    @pytest.mark.asyncio
    async def test_a_macro_it_started_is_paused_stopped_and_counted_like_any_other(self, pair):
        pair.arm(BUYS_ONLY)
        run = pair.arm(f"run\n    {NEW}\n    signal 'go'\n    after 2s\n    signal 'go'\n    after 60s\n"
                       "on signal 'go'\n    new symbol: 'IBM', side: sell, qty: 1, price: 99\n    after 60s\n",
                       session="LOOP-CLI")
        await pair.advance(1)
        assert [i.status for i in run.instances] == [RUNNING, RUNNING]
        pair.runner.pause(run)
        await pair.advance(5)
        assert len(run.instances) == 2, "the first macro is parked before its second signal"
        pair.runner.resume(run)
        await pair.advance(1)
        assert len(run.instances) == 3
        pair.runner.stop(run)
        await pair.runner.settle()
        venue = pair.runner.runs[0]
        assert {i.status for i in run.instances} == {"stopped"}
        assert pair.runner.live == len(venue.instances) == 3, "the venue's macros, one an order, are all that live"
        # and nothing is started in a run that is over
        assert not pair.runner._start_block(run, run.macro.blocks[1])

    @pytest.mark.asyncio
    async def test_nothing_starts_it_but_its_signal(self, pair):
        pair.arm(BUYS_ONLY)
        run = pair.arm(f"run\n    {NEW}\n    signal 'other'\n    pass\n"
                       f"run\n    {NEW}\n    wait signal 'go' or timeout 1s\n    pass\n"
                       f"on signal 'go'\n    {NEW}\n    pass\n"
                       f"run\n    {NEW}\n    after 2s\n    signal 'go'\n    signal 'go'\n    pass\n", session="LOOP-CLI")
        await pair.runner.settle()
        assert len(run.instances) == 3, "the three `run` blocks; the `on signal` block waits"
        await pair.advance(5)
        assert len(run.instances) == 5 and len(await pair.orders("TX")) == 5
        assert [i.main.n for i in run.instances[3:]] == [0, 1]
        assert run.status == "finished"


class TestTheManager:
    @pytest.mark.asyncio
    async def test_check_says_what_play_should_open(self, stack):
        from mkfix.macro.store import MacroManager
        db, writer, engine = stack
        manager = MacroManager(engine)
        waits = await manager.check("on order\n    accept\n    signal 'taken'\non signal 'taken'\n"
                                    "    ioi symbol: 'IBM', side: buy, qty: 'L'\n", "market")
        assert (waits["errors"], waits["sends"], waits["needs_session"], waits["session"]) == (0, False, False, "")
        runs = await manager.check(f"run on S1\n    {NEW}\n    signal 'go'\non signal 'go'\n    {NEW}\n", "client")
        assert (runs["sends"], runs["needs_session"], runs["session"]) == (True, False, "S1")


class TestTheExamples:
    """The three bundled examples that show it, run as they ship."""

    @pytest.mark.asyncio
    async def test_hedge_pairs(self, pair):
        pair.arm("slow-fill")
        run = pair.arm("hedge-pairs")
        await pair.advance(40)
        assert run.status == "finished" and run.verdict == PASSED and run.counts() == {PASSED: 6}
        sent = await pair.orders("TX")
        buys, sells = [o for o in sent if o["side_code"] == "1"], [o for o in sent if o["side_code"] == "2"]
        assert [(o["symbol"], o["order_qty"]) for o in buys] == [("IBM", 100.0), ("MSFT", 200.0), ("AAPL", 300.0)]
        assert sorted((o["symbol"], o["order_qty"]) for o in sells) == sorted((o["symbol"], o["order_qty"]) for o in buys)
        assert {o["sent_text"] for o in sells} == {f"hedge of {o['cl_ord_id']}" for o in buys}
        assert run.shared == {"hedged": 600.0}
        hedged_by = {i.message for i in run.instances if i.message.startswith("hedged by ")}
        assert hedged_by == {f"hedged by {o['cl_ord_id']}" for o in sells}

    @pytest.mark.asyncio
    async def test_one_at_a_time(self, pair):
        venue = pair.arm("one-at-a-time")
        run = pair.arm("run\n    repeat 3 every 1s with who = ['ACME', 'ACME', 'ZED']\n"
                       "        new symbol: 'IBM', side: buy, qty: 100, price: 10, client: who\n"
                       "        wait filled or rejected or timeout 10s\n        pass '${who} ${order.status}: ${order.text}'\n",
                       session="LOOP-CLI")
        await pair.advance(15)
        first = (await pair.orders("RX"))[0]
        assert [i.message for i in run.instances] == [
            "ACME Filled: ", f"ACME Rejected: one working order per client: {first['cl_ord_id']} is open", "ZED Filled: "]
        assert venue.shared == {"taken": 2}
        assert [t for *_, t in venue.log if t.startswith("order ")] == [
            "order 1 taken, 1 working", "order 2 taken, 2 working"]

    @pytest.mark.asyncio
    async def test_fill_and_allocate(self, pair):
        venue = pair.arm("fill-and-allocate")
        pair.arm("allocation-check")
        run = pair.arm("run\n    repeat 2 every 2s with sym = ['IBM', 'MSFT']\n"
                       "        new symbol: sym, side: buy, qty: 100 * (n + 1), price: 10\n"
                       "        expect filled within 5s\n        pass\n", session="LOOP-CLI")
        await pair.advance(20)
        assert run.verdict == PASSED
        sent = await _fetch_all(pair.db, "SELECT * FROM fix_allocations WHERE direction = 'TX' ORDER BY id")
        orders = await pair.orders("RX")
        assert [(a["session_id"], a["symbol"], a["quantity"], a["orders"], a["allocs"]) for a in sent] == [
            ("LOOP-MKT", "IBM", 100.0, orders[0]["cl_ord_id"], "ACC1 100"),
            ("LOOP-MKT", "MSFT", 200.0, orders[1]["cl_ord_id"], "ACC1 200")]
        assert [(i.kind, i.status) for i in venue.instances] == [
            ("order", "stopped"), ("allocation", COMPLETED), ("order", "stopped"), ("allocation", COMPLETED)]
        logged = [t for *_, t in venue.log]
        assert [t for t in logged if t.startswith("allocated as ")] == [
            f"allocated as {a['alloc_id']}: {a['status']}" for a in sent]
        assert "1 allocations for 1 orders so far" in logged and "2 allocations for 2 orders so far" in logged
        assert venue.status == "armed"


class TestInTheTables:
    """What the Macro Runs window shows of it: a macro a signal started is
    a row under its run like any other, and a signal is a line of the log."""

    @pytest.mark.asyncio
    async def test_a_run_with_its_signals_and_the_macros_they_started(self, pair):
        from mkfix.macro.store import MacroManager
        manager = MacroManager(pair.engine, pair.runner)
        pair.engine.macros = manager
        await manager.start()
        try:
            for name, side in (("fill-and-allocate", "market"), ("allocation-check", "client")):
                await manager.save(name, manager.example(name)["source"], side)
                started = await manager.arm(name, side=side)
            assert started["side"] == "client"
            await pair.engine.perform("send_new_order", {"session_id": "LOOP-CLI", "symbol": "IBM", "side": "1",
                                                         "qty": "300", "price": "10"})
            await pair.advance(10)
            await manager.flush()
            (run,) = await _fetch_all(pair.db, "SELECT * FROM fix_macro_runs WHERE macro = 'fill-and-allocate'")
            assert (run["status"], run["side"], run["orders"]) == ("armed", "market", 2)
            held = await _fetch_all(pair.db, f"SELECT * FROM fix_macro_orders WHERE run_id = {run['id']} ORDER BY id")
            (order,), (alloc,) = await pair.orders("RX"), await _fetch_all(
                pair.db, "SELECT * FROM fix_allocations WHERE direction = 'TX'")
            assert [(h["subject"], h["cl_ord_id"], h["status"]) for h in held] == [
                ("order", order["cl_ord_id"], "stopped"), ("allocation", alloc["alloc_id"], "completed")]
            assert alloc["macro"] == f"fill-and-allocate #{run['id']}" == order["macro"]
            lines = await _fetch_all(pair.db, f"SELECT * FROM fix_macro_log WHERE run_id = {run['id']} ORDER BY id")
            assert [(line["cl_ord_id"], line["text"]) for line in lines] == [
                (order["cl_ord_id"], "signal 'filled'"),
                (alloc["alloc_id"], "1 allocations for 1 orders so far"),
                (alloc["alloc_id"], f"signal 'allocated' with {order['cl_ord_id']}"),
                (order["cl_ord_id"], f"allocated as {alloc['alloc_id']}: Accepted")]
            report = await manager.report(run["id"])
            assert (report["sends"], report["sending"]) == (False, False), "it waits: nothing of it is a `run`"
        finally:
            await manager.stop()

    @pytest.mark.asyncio
    async def test_a_starting_value_that_fails_is_said_and_arms_nothing(self, pair):
        from mkfix.macro.store import MacroManager
        manager = MacroManager(pair.engine, pair.runner)
        await manager.start()
        try:
            await manager.save("bad start", f"share a = NUM_OF('x') + 1\nrun on LOOP-CLI\n    {NEW}\n", "client")
            with pytest.raises(MacroError, match="line 1: "):
                await manager.arm("bad start", side="client")
            assert await _fetch_all(pair.db, "SELECT * FROM fix_macro_runs") == []
        finally:
            await manager.stop()
