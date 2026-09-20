"""The sending side of scenarios — `run on` and `on sent order` — over two
linked sessions of one engine: what one end puts on the wire the other end
receives, so a script that sends faces a script that answers."""

from pathlib import Path

import pytest
import pytest_asyncio

from mkfix import scenario
from mkfix.fix.message import parse_fix
from mkfix.scenario.clock import Scheduler, VirtualClock
from mkfix.scenario.instance import COMPLETED, FAILED, LISTENING, PASSED, STOPPED
from mkfix.scenario.runner import ScenarioRunner

from tests.test_engine import StubSession, _fetch_all, stack  # noqa: F401

EXAMPLES = Path(__file__).parent.parent / "mkfix" / "scenario" / "examples"


class LinkedSession(StubSession):
    """Whatever this end sends, the other end's engine handler receives —
    inside the send, the way a fast counterparty answers."""

    def __init__(self, engine, session_id):
        super().__init__(session_id)
        self.engine, self.peer = engine, None

    async def send_message(self, msg):
        await super().send_message(msg)
        wire = self.engine._as_sent(self, msg)
        await self.engine.on_app_message(self.peer, wire["35"], parse_fix(wire.to_pipe_string()))
        return msg


class Pair:
    def __init__(self, db, engine):
        self.db, self.engine = db, engine
        self.cli, self.mkt = LinkedSession(engine, "LOOP-CLI"), LinkedSession(engine, "LOOP-MKT")
        self.cli.peer, self.mkt.peer = self.mkt, self.cli
        engine.sessions.update({"LOOP-CLI": self.cli, "LOOP-MKT": self.mkt})
        self.clock = VirtualClock(Scheduler())
        self.runner = ScenarioRunner(engine, self.clock)

    def arm(self, text, **kw):
        if "\n" not in text:
            text = (EXAMPLES / f"{text}.scenario").read_text(encoding="utf-8")
        sc, diags = scenario.check(text)
        assert scenario.errors(diags) == [], [str(d) for d in diags]
        if sc.needs_session:
            kw.setdefault("session", "LOOP-CLI")        # what Run… would have been given
        return self.runner.arm(sc, **kw)

    async def advance(self, seconds):
        await self.clock.advance(seconds)

    async def orders(self, direction):
        return await _fetch_all(self.db, f"SELECT * FROM fix_orders WHERE direction = '{direction}' ORDER BY id")

    async def trades(self, direction):
        return await _fetch_all(self.db, f"SELECT * FROM fix_executions WHERE direction = '{direction}' ORDER BY id")


@pytest_asyncio.fixture
async def pair(stack):
    db, writer, engine = stack
    p = Pair(db, engine)
    yield p
    p.runner.stop_all()
    await p.runner.settle()


MARKET = """scenario venue
on order
    after 100ms
    accept
    when cancel
        accept
        stop
    when replace
        accept
    while order.leaves_qty > 0
        after 1s
        fill qty: MIN(100, order.leaves_qty), price: order.price
"""


class TestSending:
    @pytest.mark.asyncio
    async def test_one_order_from_new_to_filled(self, pair):
        pair.arm(MARKET)
        run = pair.arm("scenario one\nrun on LOOP-CLI\n"
                       "    new symbol: 'IBM', side: buy, qty: 250, type: limit, price: 100.5, client: 'ACME', text: 'hello'\n"
                       "    expect ack within 1s\n"
                       "    log 'acked ${order.cl_ord_id} as ${order.status}'\n"
                       "    wait filled or timeout 10s\n"
                       "    pass 'cum ${order.cum_qty} in ${COUNT(trades, t -> t.last_qty > 0)} fills'\n")
        await pair.advance(5)
        (inst,) = run.instances
        assert (inst.status, inst.message) == (PASSED, "cum 250 in 3 fills")
        (sent,) = await pair.orders("TX")
        assert (sent["status"], sent["symbol"], sent["client"], sent["sent_text"]) == ("Filled", "IBM", "ACME", "hello")
        assert run.log[0][3] == f"acked {sent['cl_ord_id']} as New"
        assert run.status == "finished", "a run that only sends is over when its last script is"

    @pytest.mark.asyncio
    async def test_the_acknowledgement_inside_the_send_is_heard(self, pair):
        # The venue accepts at once, so the ER reaches the engine inside send_new_order.
        pair.arm("scenario instant\non order\n    accept\n")
        run = pair.arm("scenario one\nrun on LOOP-CLI\n    new symbol: 'IBM', side: sell, qty: 10, price: 5\n"
                       "    expect ack within 1s else fail 'the ack was lost'\n    pass\n")
        await pair.runner.settle()
        assert [i.status for i in run.instances] == [PASSED]

    @pytest.mark.asyncio
    async def test_a_burst_is_one_script_per_order_paced(self, pair):
        pair.arm(MARKET)
        run = pair.arm("scenario burst\nseed 3\nrun on LOOP-CLI\n    let lot = 100\n"
                       "    repeat 6 at 2/s with sym = ['IBM', 'MSFT', 'AAPL']\n"
                       "        new symbol: sym, side: buy, qty: lot * (n + 1), price: 10\n"
                       "        expect ack within 1s\n        pass '${sym} #${n}'\n")
        await pair.runner.settle()
        assert len(await pair.orders("TX")) == 1, "the first goes out at once"
        await pair.advance(1)
        assert len(await pair.orders("TX")) == 3
        await pair.advance(3)
        sent = await pair.orders("TX")
        assert [(o["symbol"], o["order_qty"]) for o in sent] == [
            ("IBM", 100.0), ("MSFT", 200.0), ("AAPL", 300.0), ("IBM", 400.0), ("MSFT", 500.0), ("AAPL", 600.0)]
        assert [(i.status, i.message) for i in run.instances][:2] == [(PASSED, "IBM #0"), (PASSED, "MSFT #1")]
        assert run.status == "finished" and run.verdict == PASSED and run.counts() == {PASSED: 6}

    @pytest.mark.asyncio
    async def test_replace_keeps_the_terms_left_out_and_cancel_is_answered(self, pair):
        pair.arm(MARKET)
        run = pair.arm("scenario chase\nrun on LOOP-CLI\n"
                       "    new symbol: 'IBM', side: buy, qty: 1000, price: 100, extra: '9001=x'\n"
                       "    expect ack within 1s\n"
                       "    replace price: TICK(order.price + 0.05, 0.01)\n"
                       "    expect replaced within 1s\n"
                       "    log 'now ${order.price} x ${order.order_qty} as ${order.cl_ord_id}'\n"
                       "    cancel text: 'enough'\n"
                       "    expect canceled within 1s\n    pass\n")
        await pair.advance(2)
        (inst,) = run.instances
        assert inst.status == PASSED, inst.message
        (sent,) = await pair.orders("TX")
        assert run.log[0][3].startswith("now 100.05 x 1000 as RT")
        assert (sent["status"], sent["entered_price"], sent["entered_qty"]) == ("Canceled", 100.05, 1000.0)
        replace = next(m for m in pair.cli.sent if m.get("35") == "G")
        wire = pair.engine._as_sent(pair.cli, replace)
        assert (wire["38"], wire["44"], wire["55"], wire["54"], wire["9001"]) == ("1000", "100.05", "IBM", "1", "x")

    @pytest.mark.asyncio
    async def test_cancel_rejected_and_the_pending_guard(self, pair):
        pair.arm("scenario strict\non order\n    accept\n    when cancel or replace\n        reject text: 'not today'\n")
        run = pair.arm("scenario ask\nrun on LOOP-CLI\n    new symbol: 'IBM', side: buy, qty: 10, price: 1\n"
                       "    expect ack within 1s\n    replace qty: 20\n"
                       "    wait replaced or cancel rejected or timeout 1s\n"
                       "    pass '${event.kind}/${event.response_to}/${event.text} pending=${order.pending_action} qty=${order.entered_qty}'\n")
        await pair.runner.settle()
        assert run.instances[0].message == "cancel rejected/replace/not today pending= qty=10"

    @pytest.mark.asyncio
    async def test_dk_a_fill_and_tell_a_renotification_from_a_fill(self, pair):
        pair.arm("scenario desk\non order\n    accept\n    fill qty: 10, price: order.price + 1\n"
                 "    when dk\n        renotify\n")
        run = pair.arm("scenario policy\nrun on LOOP-CLI\n    new symbol: 'IBM', side: buy, qty: 10, price: 5\n"
                       "    let real = 0\n    let restated = 0\n"
                       "    when fill and event.prev.cum_qty < order.cum_qty\n"
                       "        let real = real + 1\n        dk reason: 'Price exceeds limit', text: 'through ${order.price}'\n"
                       "    when fill\n        let restated = restated + 1\n"
                       "    after 1s\n    pass 'real ${real} restated ${restated}'\n")
        await pair.advance(2)
        assert run.instances[0].message == "real 1 restated 1", "the re-notification was not disputed again"
        dk = next(m for m in pair.cli.sent if m.get("35") == "Q")
        assert (dk.get("127"), dk.get("58")) == ("E", "through 5")
        received = await pair.trades("RX")
        assert len(received) == 2 and received[0]["dk_reason"] == "PriceExceedsLimit"

    @pytest.mark.asyncio
    async def test_what_a_sending_script_may_not_do(self, pair):
        run = pair.arm("scenario twice\nrun on LOOP-CLI\n    new symbol: 'A', side: buy, qty: 1, price: 1\n"
                       "    new symbol: 'B', side: buy, qty: 1, price: 1\n")
        await pair.runner.settle()
        assert "line 4: this script has already sent its order" in run.instances[0].message
        early = pair.arm("scenario early\nrun on LOOP-CLI\n    if n == 0\n        cancel\n"
                         "    new symbol: 'A', side: buy, qty: 1, price: 1\n")
        await pair.runner.settle()
        assert "line 4: `cancel` before `new`" in early.instances[0].message

    @pytest.mark.asyncio
    async def test_a_send_that_fails_fails_the_script(self, pair):
        run = pair.arm("scenario down\nrun on LOOP-CLI\n    after 1s\n    new symbol: 'A', side: buy, qty: 1, price: 1\n")
        pair.cli.is_active = False                     # the session drops while the script waits
        await pair.advance(2)
        assert run.instances[0].status == FAILED and "not active" in run.instances[0].message
        assert run.status == "finished" and run.verdict == FAILED

    @pytest.mark.asyncio
    async def test_a_session_that_is_down_is_refused_before_anything_starts(self, pair):
        from mkfix.scenario.runner import ScenarioError
        pair.cli.is_active = False
        sc, _ = scenario.check("scenario down\nrun on LOOP-CLI\n    new symbol: 'A', side: buy, qty: 1, price: 1\n")
        with pytest.raises(ScenarioError, match="`run` on LOOP-CLI: the session is not active"):
            pair.runner.arm(sc)
        assert pair.runner.runs == [] and not pair.engine.events.active

    @pytest.mark.asyncio
    async def test_a_run_without_a_session_sends_where_it_is_told(self, pair):
        """`run` leaves the session to Run…: one script, a run a session."""
        engine = pair.engine
        cli2, mkt2 = LinkedSession(engine, "CLI-2"), LinkedSession(engine, "MKT-2")
        cli2.peer, mkt2.peer = mkt2, cli2
        engine.sessions.update({"CLI-2": cli2, "MKT-2": mkt2})
        text = "scenario open\nrun\n    new symbol: 'IBM', side: buy, qty: 10, price: 5\n    expect ack within 1s\n    pass\n"
        pair.arm("scenario instant\non order\n    accept\n")
        sc, _ = scenario.check(text)
        runs = [pair.runner.arm(sc, session="LOOP-CLI"), pair.runner.arm(sc, session="CLI-2"),
                pair.runner.arm(sc, session="CLI-2")]
        await pair.runner.settle()
        assert [r.status for r in runs] == ["finished"] * 3 and [r.verdict for r in runs] == [PASSED] * 3
        assert [o["session_id"] for o in await pair.orders("TX")] == ["LOOP-CLI", "CLI-2", "CLI-2"]
        assert (len(pair.cli.sent), len(cli2.sent)) == (1, 2)

    @pytest.mark.asyncio
    async def test_the_session_chosen_at_run_wins_over_the_one_written(self, pair):
        engine = pair.engine
        cli2, mkt2 = LinkedSession(engine, "CLI-2"), LinkedSession(engine, "MKT-2")
        cli2.peer, mkt2.peer = mkt2, cli2
        engine.sessions.update({"CLI-2": cli2, "MKT-2": mkt2})
        sc, _ = scenario.check("scenario named\nrun on LOOP-CLI\n    new symbol: 'IBM', side: buy, qty: 10, price: 5\n")
        pair.runner.arm(sc)
        pair.runner.arm(sc, session="CLI-2")
        await pair.runner.settle()
        assert [o["session_id"] for o in await pair.orders("TX")] == ["LOOP-CLI", "CLI-2"]
        pair.cli.is_active = False
        pair.runner.arm(sc, session="CLI-2")            # the session written is not looked at
        from mkfix.scenario.runner import ScenarioError
        with pytest.raises(ScenarioError, match="`run` on LOOP-CLI: the session is not active"):
            pair.runner.arm(sc)

    @pytest.mark.asyncio
    async def test_many_runs_of_one_script_at_once_each_with_its_own_orders(self, pair):
        pair.arm(MARKET)
        text = ("scenario many\nrun\n    repeat 3 at 10/s\n        new symbol: 'IBM', side: buy, qty: 100, price: 10\n"
                "        wait filled or timeout 5s\n        pass '${order.cum_qty}'\n")
        runs = [pair.arm(text, seed=n) for n in range(4)]
        await pair.advance(6)
        assert [r.counts() for r in runs] == [{PASSED: 3}] * 4
        assert len({i.order["id"] for r in runs for i in r.instances}) == 12 == len(await pair.orders("TX"))
        assert [r.status for r in runs] == ["finished"] * 4

    @pytest.mark.asyncio
    async def test_armed_but_not_started_until_told(self, pair):
        sc, _ = scenario.check("scenario later\nrun on LOOP-CLI\n    new symbol: 'A', side: buy, qty: 1, price: 1\n")
        run = pair.runner.arm(sc, start=False)
        await pair.runner.settle()
        assert run.instances == [] and pair.cli.sent == [] and run.status == "armed"
        pair.runner.start(run)
        await pair.runner.settle()
        assert len(pair.cli.sent) == 1

    @pytest.mark.asyncio
    async def test_stopping_a_burst_stops_the_orders_to_come(self, pair):
        pair.arm(MARKET)
        run = pair.arm("scenario burst\nrun on LOOP-CLI\n    repeat 100 every 1s\n"
                       "        new symbol: 'IBM', side: buy, qty: 1, price: 1\n        wait filled\n")
        await pair.advance(2.5)
        pair.runner.stop(run)
        await pair.advance(10)
        assert len(await pair.orders("TX")) == 3 and {i.status for i in run.instances} <= {STOPPED, COMPLETED}


class TestLateAnswers:
    @pytest.mark.asyncio
    async def test_a_request_naming_a_clordid_the_venue_has_moved_past_leaves_our_status_alone(self, pair):
        """Found by running the loopback tour over real TCP at 20x: the client
        gave up waiting for `replaced`, then cancelled under the ClOrdID the
        venue had already replaced away. The venue's automatic reject says
        UnknownOrder, so its 39=8 is about no order of ours."""
        pair.arm("scenario v\non order\n    accept\n")
        run = pair.arm("scenario c\nrun on LOOP-CLI\n    new symbol: 'IBM', side: buy, qty: 10, price: 1\n"
                       "    expect ack within 1s\n    wait cancel rejected or timeout 5s\n"
                       "    pass '${event.reason} status=${order.status}'\n")
        await pair.runner.settle()
        (sent,) = await pair.orders("TX")
        await pair.engine.on_app_message(pair.mkt, "F", parse_fix(
            f"8=FIX.4.2|35=F|11=LATE1|41=GONE|55=IBM|54=1|38=10"))
        reject = pair.engine._as_sent(pair.mkt, pair.mkt.sent[-1])
        await pair.engine.on_app_message(pair.cli, "9", parse_fix(
            reject.to_pipe_string().replace("41=GONE", f"41={sent['cl_ord_id']}")))
        await pair.runner.settle()
        assert run.instances[0].message == "UnknownOrder status=New"


class TestAttached:
    @pytest.mark.asyncio
    async def test_an_order_sent_by_hand_is_taken_over(self, pair):
        pair.arm(MARKET)
        run = pair.arm("scenario minder\non sent order where symbol == 'IBM'\n"
                       "    expect ack within 1s else fail 'no ack'\n"
                       "    after 2s\n    if order.leaves_qty > 0\n        cancel\n"
                       "        expect canceled or filled within 2s\n    pass 'left ${order.leaves_qty}'\n")
        await pair.engine.perform("send_new_order", {"session_id": "LOOP-CLI", "symbol": "IBM", "side": "1",
                                                     "qty": 1000, "price": 10})
        await pair.engine.perform("send_new_order", {"session_id": "LOOP-CLI", "symbol": "MSFT", "side": "1",
                                                     "qty": 1000, "price": 10})
        await pair.advance(5)
        assert [(i.order["symbol"], i.status) for i in run.instances] == [("IBM", PASSED)]
        ibm, msft = await pair.orders("TX")
        assert ibm["status"] == "Canceled" and ibm["cum_qty"] == 100.0 and msft["status"] != "Canceled"
        assert run.status == "armed", "a run that waits for orders stays armed"

    @pytest.mark.asyncio
    async def test_a_scripts_own_order_is_not_taken_by_an_attached_block(self, pair):
        pair.arm("scenario instant\non order\n    accept\n")
        both = pair.arm("scenario both\non sent order\n    fail 'took an order that had a script'\n"
                        "run on LOOP-CLI\n    new symbol: 'IBM', side: buy, qty: 1, price: 1\n    expect ack within 1s\n    pass\n")
        await pair.runner.settle()
        assert [(i.block.kind, i.status) for i in both.instances] == [("client", PASSED)]

    @pytest.mark.asyncio
    async def test_a_replayed_order_is_taken_at_its_first_report(self, pair):
        run = pair.arm("scenario replayed\non sent order\n    wait fill or timeout 5s\n    pass '${event.kind} ${order.cl_ord_id}'\n")
        report = "8=FIX.4.2|35=8|11=R1|37=M1|17=E1|20=0|150=0|39=0|55=IBM|54=1|38=10|14=0|6=0|151=10"
        await pair.engine.on_app_message(pair.cli, "8", parse_fix(report))
        await pair.engine.on_app_message(pair.cli, "8", parse_fix(
            report.replace("17=E1", "17=E2").replace("150=0|39=0", "150=1|39=1") + "|32=4|31=9"))
        await pair.runner.settle()
        assert run.instances[0].message == "fill R1"


class TestExamples:
    """The sending examples, run against the bundled venues their headers name."""

    @pytest.mark.asyncio
    async def test_single_order_lifecycle(self, pair):
        pair.arm("slow-fill")
        run = pair.arm("single-order-lifecycle")
        await pair.advance(8)
        assert [(i.status, i.message) for i in run.instances] == [(PASSED, "done 300 of 300")]

    @pytest.mark.asyncio
    async def test_single_order_lifecycle_cancels_what_is_left(self, pair):
        pair.arm("scenario slow\non order\n    accept\n    when cancel\n        accept\n    after 1s\n    fill qty: 100, price: order.price\n")
        run = pair.arm("single-order-lifecycle")
        await pair.advance(8)
        assert run.instances[0].message == "done 100 of 300" and (await pair.orders("TX"))[0]["status"] == "Canceled"

    @pytest.mark.asyncio
    async def test_replace_chase(self, pair):
        pair.arm("cancel-replace-desk")
        run = pair.arm("replace-chase")
        await pair.advance(12)
        (inst,) = run.instances
        assert (inst.status, inst.message) == (PASSED, "chased to 50.01"), "one replace accepted, two refused"
        assert [e[3] for e in run.log if e[3].startswith("refused")] == [
            "refused (): one replace per order", "refused (): one replace per order"]
        (sent,) = await pair.orders("TX")
        assert (sent["status"], sent["price"], sent["cxl_rej_reason"]) == ("Canceled", 50.01, ""), \
            "the cancel that followed retired the last reject note"

    @pytest.mark.asyncio
    async def test_order_burst_is_the_same_twenty_orders_every_run(self, pair):
        pair.arm(MARKET)
        async def once():
            before = len(await pair.orders("TX"))
            run = pair.arm("order-burst")
            await pair.advance(6)
            sent = (await pair.orders("TX"))[before:]
            return run, [(o["symbol"], o["side"], o["order_qty"], o["price"], o["client"]) for o in sent]
        run, first = await once()
        assert len(first) == 20 and run.counts() == {PASSED: 20} and run.status == "finished"
        assert [o[0] for o in first[:4]] == ["IBM", "MSFT", "AAPL", "IBM"] and [o[1] for o in first[:2]] == ["Buy", "Sell"]
        assert {o[4] for o in first} == {"ACME", "GLOBEX"} and all(o[2] % 100 == 0 and 100 <= o[2] <= 500 for o in first)
        assert all(20 <= o[3] <= 25 and round(o[3] * 20) == o[3] * 20 for o in first), "prices on a 0.05 tick"
        assert run.log[0][3] == "sending 20 orders"
        _, second = await once()
        assert second == first

    @pytest.mark.asyncio
    async def test_dk_policy_against_the_dispute_desk(self, pair):
        pair.arm("dispute-desk")
        run = pair.arm("dk-policy")
        await pair.advance(11)
        (inst,) = run.instances
        assert (inst.status, inst.message) == (PASSED, "1 disputed")
        assert [e[3] for e in run.log if "re-notified" in e[3]] == ["re-notified: 100 @ 150"]
        assert len(await pair.trades("RX")) == 2 and len(await pair.trades("TX")) == 1

    @pytest.mark.asyncio
    async def test_regression_suite_passes_against_the_desk_and_fails_against_auto_ack(self, pair):
        desk = pair.arm("cancel-replace-desk")
        run = pair.arm("regression-suite")
        await pair.advance(15)
        assert run.counts() == {PASSED: 3} and run.verdict == PASSED, [i.message for i in run.instances]
        pair.runner.stop(desk)
        pair.arm("auto-ack")
        failing = pair.arm("regression-suite")
        await pair.advance(15)
        assert failing.verdict == FAILED and failing.counts() == {FAILED: 3}
        assert failing.instances[0].message == "line 20: only 0 of 100 after 10s"

    @pytest.mark.asyncio
    async def test_take_over(self, pair):
        pair.arm(MARKET)
        run = pair.arm("take-over")
        for sym, qty in (("IBM", 5000), ("MSFT", 100)):
            await pair.engine.perform("send_new_order", {"session_id": "LOOP-CLI", "symbol": sym, "side": "1",
                                                         "qty": qty, "price": 10})
        await pair.advance(2)
        ibm = (await pair.orders("TX"))[0]
        await pair.engine.perform("send_cancel_replace", {
            "session_id": "LOOP-CLI", "orig_cl_ord_id": ibm["cl_ord_id"], "symbol": "IBM", "side": "1", "qty": 6000, "price": 10})
        await pair.advance(40)
        (inst,) = run.instances
        assert (inst.order["symbol"], inst.status, inst.message) == ("IBM", PASSED, "left 0")
        assert "by hand: send_cancel_replace" in [e[3] for e in run.log]
        assert (await pair.orders("TX"))[0]["status"] == "Canceled"

    @pytest.mark.asyncio
    async def test_loopback_tour(self, pair):
        venue = pair.arm("loopback-venue", session="LOOP-MKT")
        run = pair.arm("loopback-client")
        await pair.advance(12)
        assert [i.block.kind for i in venue.instances] == ["market"] * 5
        assert [i.block.kind for i in run.instances] == ["client"] * 5, "its own orders are not minded"
        clients = run.instances
        assert all(i.status == PASSED for i in clients), [i.message for i in clients]
        # 100: half filled, replaced up to 200, half of what was left filled -> 125
        assert [i.message for i in clients][:2] == ["IBM: 125 done", "MSFT: 200 done"]
        assert all(o["status"] == "Canceled" and o["order_qty"] == 100 * (n + 1) + 100
                   for n, o in enumerate(await pair.orders("TX")))
        assert run.status == "armed" and run.verdict == PASSED

    def test_the_whole_language_is_covered(self):
        """Every verb, event and statement the language has appears in some
        bundled example, so a new word fails here until an example shows it."""
        from mkio import expr
        from mkfix.scenario import functions, nodes, vocab
        from mkfix.scenario.nodes import Action, After, Expect, If, Repeat, Wait, When
        verbs, events, kinds, shapes, roots = set(), set(), set(), set(), set()
        for path in EXAMPLES.glob("*.scenario"):
            sc, diags = scenario.check(path.read_text(encoding="utf-8"))
            assert diags == [], (path.name, [str(d) for d in diags])
            for block in sc.blocks:
                kinds.add(block.kind)
                for st in nodes.walk(block.body):
                    shapes.add(type(st).__name__)
                    if isinstance(st, Action):
                        verbs.add(st.verb)
                    if isinstance(st, (Wait, Expect, When)):
                        events |= set(st.events)
                    if isinstance(st, Repeat):
                        shapes |= {"repeat at"} if st.interval else set()
                        shapes |= {"repeat every"} if st.every else set()
                        shapes |= {"repeat with"} if st.var else set()
                    if isinstance(st, Expect) and st.message is not None:
                        shapes.add("else fail")
                    if isinstance(st, Wait) and st.timeout is not None:
                        shapes.add("or timeout")
                    for e in nodes.expressions(st):
                        roots |= expr.field_refs(e.node)
                        if e.template:
                            for kind, part in expr.compile_template(e.node.value, functions.ENV).parts:
                                roots |= part.field_refs if kind == "expr" else set()
        assert set(vocab.VERBS) - verbs == set()
        assert set(vocab.EVENTS) - events <= {"message", "er", "pending", "restated", "expired", "done for day",
                                              "corrected", "busted"}, "reports a script seldom needs to name"
        assert kinds == set(vocab.SIDES)
        sides = {scenario.check(p.read_text(encoding="utf-8"))[0].side for p in EXAMPLES.glob("*.scenario")}
        assert sides == set(vocab.SCENARIO_SIDES)
        assert {"Repeat", "repeat at", "repeat every", "repeat with", "else fail", "or timeout", "Expect", "Wait"} <= shapes
        assert set(vocab.CONTEXT_DOCS) - roots == set(), "every name an expression can see is used somewhere"
