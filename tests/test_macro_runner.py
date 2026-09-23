"""Scripts running: the interpreter and the market-side runner, on a virtual
clock over a real engine and database. The bundled examples are run to the
outcome their headers state."""

from pathlib import Path

import pytest
import pytest_asyncio

from mkfix import macro
from mkfix.fix.message import parse_fix
from mkfix.macro.clock import Scheduler, VirtualClock
from mkfix.macro.instance import COMPLETED, DETACHED, FAILED, LISTENING, PASSED, RUNNING, STOPPED
from mkfix.macro.runner import MacroError, MacroRunner

from tests.test_engine import StubSession, _fetch_all, stack  # noqa: F401  (stack is a fixture)

EXAMPLES = Path(__file__).parent.parent / "mkfix" / "macro" / "examples"


class Desk:
    """An engine with one session, a runner on a virtual clock, and the
    counterparty's side of the wire."""

    def __init__(self, db, engine):
        self.db, self.engine = db, engine
        self.stub = StubSession()
        engine.sessions["S1"] = self.stub
        self.clock = VirtualClock(Scheduler())
        self.runner = MacroRunner(engine, self.clock)

    def arm(self, text, **kw):
        if "\n" not in text:
            text = (EXAMPLES / f"{text}.macro").read_text(encoding="utf-8")
        sc, diags = macro.check(text)
        assert macro.errors(diags) == [], [str(d) for d in diags]
        return self.runner.arm(sc, **kw)

    async def inbound(self, msg_type, text, session=None):
        await self.engine.on_app_message(session or self.stub, msg_type, parse_fix("8=FIX.4.2|35=" + msg_type + "|" + text))
        await self.runner.settle()

    async def order(self, cl="C100", qty=100, px="150.25", sym="AAPL", extra="", session=None):
        await self.inbound("D", f"11={cl}|55={sym}|54=1|38={qty}|40=2|44={px}|59=0{extra}", session)

    async def replace(self, new, orig, qty, px="151"):
        await self.inbound("G", f"11={new}|41={orig}|55=AAPL|54=1|38={qty}|40=2|44={px}")

    async def cancel(self, new, orig):
        await self.inbound("F", f"11={new}|41={orig}|55=AAPL|54=1|38=100")

    async def advance(self, seconds):
        await self.clock.advance(seconds)

    async def row(self, cl=None):
        where = f"WHERE cl_ord_id = '{cl}'" if cl else ""
        return (await _fetch_all(self.db, f"SELECT * FROM fix_orders {where} ORDER BY id"))[-1]

    async def trades(self):
        return await _fetch_all(self.db, "SELECT * FROM fix_executions ORDER BY id")

    def sent(self, tag=None):
        """What went out, as it went out: extras are applied when a message is sent."""
        wire = [self.engine._as_sent(self.stub, m) for m in self.stub.sent]
        return [m if tag is None else m.get(tag) for m in wire]


@pytest_asyncio.fixture
async def desk(stack):
    db, writer, engine = stack
    d = Desk(db, engine)
    yield d
    d.runner.stop_all()
    await d.runner.settle()


def script(body, header="on order", top=""):
    lines = "\n".join("    " + l if l.strip() else l for l in body.strip("\n").splitlines())
    return f"{top}{header}\n{lines}\n"


# -- the examples, to their stated outcomes ----------------------------------------------

class TestExamples:
    @pytest.mark.asyncio
    async def test_auto_ack(self, desk):
        run = desk.arm("auto-ack")
        await desk.order()
        assert desk.sent() == [] and (await desk.row())["pending_action"] == "New"
        await desk.advance(0.25)
        assert desk.sent("150") == ["0"] and (await desk.row())["status"] == "New"
        assert [i.status for i in run.instances] == [COMPLETED]

    @pytest.mark.asyncio
    async def test_reject_by_rule(self, desk):
        await desk.engine.save_template("reject", "client-blocked", text="Client is blocked")
        desk.arm("reject-by-rule")
        await desk.order("OK1", qty=500, sym="IBM")
        await desk.order("BIG", qty=20000, sym="IBM")
        await desk.order("ODD", qty=150, sym="IBM")
        await desk.order("SYM", qty=100, sym="ZZZZ")
        await desk.order("BLK", qty=100, sym="IBM", extra="|109=BLOCKED")
        await desk.inbound("D", "11=MKT|55=IBM|54=1|38=100|40=1|59=0")
        await desk.advance(0.1)
        got = {m.get("11"): (m.get("150"), m.get("58"), m.get("103")) for m in desk.sent()}
        assert got == {
            "OK1": ("0", "Working", None),
            "BIG": ("8", "Order of 20000 exceeds the 10000 limit", None),
            "ODD": ("8", "Round lots only", "99"),
            "SYM": ("8", "Unknown symbol ZZZZ", "1"),
            "BLK": ("8", "Client is blocked", None),
        }, "the market order matched no block and stays pending for a person"
        assert (await desk.row("MKT"))["pending_action"] == "New"

    @pytest.mark.asyncio
    async def test_slow_fill_is_the_same_every_run(self, desk):
        async def one(cl):
            desk.stub.sent.clear()
            run = desk.arm("slow-fill")
            await desk.order(cl, qty=250)
            times = []
            for _ in range(60):
                before = len(desk.stub.sent)
                await desk.advance(0.1)
                if len(desk.stub.sent) > before:
                    times.append(round(desk.clock.now() % 1000, 1))
            desk.runner.stop(run)
            return [(m.get("32"), m.get("31")) for m in desk.sent() if float(m.get("32") or 0) > 0], run
        fills, run = await one("A1")
        assert [q for q, _ in fills] == ["100", "100", "50"] and (await desk.row("A1"))["status"] == "Filled"
        assert all(150.23 <= float(px) <= 150.25 and len(px.split(".")[1]) <= 2 for _, px in fills), fills
        assert run.log[-1][3].startswith("done: A1 avg 150.2")
        again, _ = await one("A2")
        assert again == fills, "seed 7: the same jitter, the same prices"

    @pytest.mark.asyncio
    async def test_correct_and_bust(self, desk):
        run = desk.arm("correct-and-bust")
        await desk.order(qty=400, px="150")
        await desk.advance(10)
        trades = await desk.trades()
        assert [(t["exec_type"], t["last_qty"], t["last_price"]) for t in trades] == [
            ("Cancel", 100.0, 150.0),        # first trade: busted
            ("Cancel", 100.0, 150.01),       # `trade where trade.last_price > 150`: the next one above it
            ("Correct", 100.0, 150.0),       # last trade: repriced to the limit
        ]
        assert [i.status for i in run.instances] == [COMPLETED] and (await desk.row())["cum_qty"] == 100.0

    @pytest.mark.asyncio
    async def test_cancel_replace_desk(self, desk):
        run = desk.arm("cancel-replace-desk")
        await desk.order("C1", qty=300)
        await desk.advance(0.2)
        await desk.replace("C2", "C1", 400)
        await desk.advance(0.4)
        assert (await desk.row())["cl_ord_id"] == "C2" and (await desk.row())["order_qty"] == 400.0
        await desk.replace("C3", "C2", 500)
        assert desk.sent()[-1].get("35") == "9" and desk.sent()[-1].get("58") == "one replace per order"
        await desk.advance(2)
        await desk.replace("C4", "C2", 50)          # below the 100 already filled: still the second replace
        assert desk.sent()[-1].get("58") == "one replace per order"
        await desk.advance(8)
        row = await desk.row()
        assert (row["status"], row["cum_qty"]) == ("Filled", 400.0) and run.instances[0].status == LISTENING
        await desk.cancel("C5", "C2")
        assert (desk.sent()[-1].get("35"), desk.sent()[-1].get("58")) == ("9", "too late to cancel")

    @pytest.mark.asyncio
    async def test_cancel_replace_desk_accepts_a_cancel_in_time(self, desk):
        run = desk.arm("cancel-replace-desk")
        await desk.order("C1", qty=1000)
        await desk.advance(2.5)
        await desk.cancel("C2", "C1")
        await desk.advance(0.5)
        row = await desk.row()
        assert (row["status"], row["cl_ord_id"], row["cum_qty"]) == ("Canceled", "C2", 100.0)
        assert run.instances[0].status == STOPPED
        sent = len(desk.stub.sent)
        await desk.advance(10)
        assert len(desk.stub.sent) == sent, "`stop` ended the fills too"

    @pytest.mark.asyncio
    async def test_unsolicited(self, desk):
        desk.arm("unsolicited")
        await desk.order("DAY", qty=200)
        await desk.inbound("D", "11=GTC|55=AAPL|54=1|38=200|40=2|44=100|59=1")
        await desk.advance(3)
        day, gtc = await desk.row("DAY"), await desk.row("GTC")
        assert (day["order_qty"], gtc["order_qty"], gtc["price"]) == (100.0, 200.0, 99.0)
        restates = [m for m in desk.sent() if m.get("150") == "D"]
        assert sorted((m.get("11"), m.get("378"), m.get("58")) for m in restates) == [
            ("DAY", "5", "half declined"), ("GTC", "3", None)]
        await desk.advance(7)
        assert (await desk.row("DAY"))["status"] == "Canceled" and (await desk.row("GTC"))["status"] != "Canceled"
        await desk.cancel("X1", "DAY")
        assert (desk.sent()[-1].get("35"), desk.sent()[-1].get("58")) == ("9", "order already canceled")

    @pytest.mark.asyncio
    async def test_dispute_desk(self, desk):
        desk.arm("dispute-desk")
        await desk.order("C1", qty=100)
        await desk.order("C2", qty=100)
        first, second = [t["exec_id"] for t in await desk.trades()]
        await desk.inbound("Q", f"37=X|17={first}|127=D|55=AAPL|54=1|38=100")
        await desk.advance(1)
        renotified = (await desk.trades())[0]
        assert renotified["exec_id"] != first and renotified["dk_reason"] == "" and desk.sent()[-1].get("58") == "as reported"
        await desk.inbound("Q", f"37=X|17={renotified['exec_id']}|127=D|55=AAPL|54=1|38=100")
        assert (await desk.trades())[0]["exec_type"] == "Cancel" and desk.sent()[-1].get("58") == "disputed twice"
        await desk.inbound("Q", f"37=X|17={second}|127=E|55=AAPL|54=1|38=100|58=way off")
        await desk.advance(0.5)
        assert (await desk.trades())[1]["exec_type"] == "Cancel" and desk.sent()[-1].get("58") == "busted: way off"

    @pytest.mark.asyncio
    async def test_misbehaving_counterparty(self, desk):
        run = desk.arm("misbehaving-counterparty")
        await desk.order(qty=100)
        await desk.advance(1)
        wire = desk.sent()
        assert wire[0].get("9001") == "venue-A"
        assert [m.get("32") for m in wire[1:]] == ["100", "10", "1", "1"] and (await desk.row())["cum_qty"] == 112.0
        assert "60" not in wire[3].fields and "60" in wire[2].fields and wire[4].get("10") == "000"
        assert run.instances[0].status == LISTENING

    @pytest.mark.asyncio
    async def test_session_aware(self, desk):
        run = desk.arm("session-aware")
        await desk.order("C1", qty=150)
        await desk.advance(1)
        await desk.engine.update_session_state("S1", {"status": "ACTIVE"})
        desk.stub.is_active = False
        await desk.engine.update_session_state("S1", {"status": "DOWN"})
        await desk.runner.settle()
        assert "paused: C1" in [entry[3] for entry in run.log]
        await desk.advance(61)
        inst = run.instances[0]
        assert inst.status == FAILED and "session did not come back" in inst.message and run.verdict == FAILED

    @pytest.mark.asyncio
    async def test_session_aware_passes_when_nothing_goes_wrong(self, desk):
        run = desk.arm("session-aware")
        await desk.order("C1", qty=150)
        await desk.advance(3)
        assert (run.instances[0].status, run.instances[0].message, run.verdict) == (PASSED, "filled 150", PASSED)


# -- how a script runs ---------------------------------------------------------------------

class TestSemantics:
    @pytest.mark.asyncio
    async def test_first_matching_when_wins_and_runs_beside_the_main_flow(self, desk):
        desk.arm(script("accept\n"
                        "when cancel and order.cum_qty > 0\n    reject text: 'partly done'\n"
                        "when cancel\n    after 2s\n    accept\n"
                        "after 1s\nfill qty: 10, price: 1\nafter 5s\nfill qty: 10, price: 1\n"))
        await desk.order()
        await desk.cancel("X1", "C100")             # cum 0: the second `when`, which takes 2 s
        await desk.advance(1.5)
        assert (await desk.row())["cum_qty"] == 10.0, "the main flow's fill was not held up by the handler"
        await desk.advance(1)
        assert (await desk.row())["status"] == "Canceled"

    @pytest.mark.asyncio
    async def test_a_when_lives_as_long_as_its_block(self, desk):
        desk.arm(script("accept\nif order.order_qty > 50\n    when cancel\n        reject text: 'inner'\n    after 1s\n"
                        "after 1s\nwhen cancel\n    reject text: 'outer'\n"))
        await desk.order()
        await desk.cancel("X1", "C100")
        assert desk.sent()[-1].get("58") == "inner"
        await desk.advance(1.5)
        await desk.cancel("X2", "C100")
        assert desk.sent()[-1].get("58") == "inner", "X1's reject; nothing answers X2: the `if` is over"
        await desk.advance(1)
        await desk.cancel("X3", "C100")
        assert desk.sent()[-1].get("58") == "outer"

    @pytest.mark.asyncio
    async def test_an_event_before_the_wait_is_not_missed(self, desk):
        run = desk.arm(script("accept\nafter 5s\nwait cancel or timeout 1s\n"
                              "if event.kind == 'cancel'\n    pass 'saw ${event.request}'\nfail 'missed it'\n"))
        await desk.order()
        await desk.cancel("X1", "C100")
        await desk.advance(5)
        assert (run.instances[0].status, run.instances[0].message) == (PASSED, "saw X1")

    @pytest.mark.asyncio
    async def test_wait_where_timeout_and_since(self, desk):
        run = desk.arm(script("accept\nwait replace where order.pending_qty > 500 or timeout 10s\n"
                              "log 'got ${event.kind} ${event.request} after ${since}'\n"
                              "wait cancel or timeout 2s\nlog 'then ${event.kind}, ${ROUND(elapsed)}s in'\n"))
        await desk.order()
        await desk.advance(1)
        await desk.replace("R1", "C100", 300)
        await desk.advance(1)
        await desk.replace("R2", "C100", 900)
        await desk.advance(5)
        assert [e[3] for e in run.log] == ["got replace R2 after 0", "then timeout, 4s in"]

    @pytest.mark.asyncio
    async def test_expect_fails_the_script_with_its_reason(self, desk):
        run = desk.arm(script("accept\nexpect cancel within 2s else fail 'no cancel for ${order.cl_ord_id}'\n"))
        await desk.order()
        await desk.advance(3)
        assert (run.instances[0].status, run.instances[0].message) == (FAILED, "line 3: no cancel for C100")
        other = desk.arm(script("accept\nexpect cancel within 2s\npass\n"))
        desk.runner.stop(run)
        await desk.order("C2")
        await desk.cancel("X", "C2")
        assert other.instances[0].status == PASSED

    @pytest.mark.asyncio
    async def test_a_refused_action_fails_the_script_unless_told_to_continue(self, desk):
        strict = desk.arm(script("accept\nbust last trade\n"))
        await desk.order("C1")
        inst = strict.instances[0]
        assert inst.status == FAILED and "line 3: `bust last trade`: this order has no live trade" in inst.message
        desk.runner.stop(strict)
        lenient = desk.arm(script("when error\n    log 'heard ${event.op}: ${event.text}'\n"
                                  "accept\nrestate qty: 1, price: 1\nfill qty: 5, price: 1\n"
                                  "restate qty: 2, price: 1\nbust trade where trade.last_qty > 99\nfill qty: 1, price: 1\n",
                                  top="on error continue\n"))
        await desk.order("C2")
        assert lenient.instances[0].status == LISTENING and (await desk.row("C2"))["cum_qty"] == 6.0, \
            "both refusals were heard and the script went on"
        heard = [e[3] for e in lenient.log if e[3].startswith("heard")]
        assert len(heard) == 2 and "heard restate_order: `restate` was refused:" in heard[0]
        assert "heard bust_trade: no live trade of this order matches" in heard[1]

    @pytest.mark.asyncio
    async def test_an_expression_that_fails_names_its_line(self, desk):
        run = desk.arm(script("accept\nfill qty: 1 / order.cum_qty, price: 1\n"))
        await desk.order()
        assert run.instances[0].message == "line 3: Division by zero — in `1 / order.cum_qty`"

    @pytest.mark.asyncio
    async def test_let_is_one_flat_scope_and_repeat_counts(self, desk):
        run = desk.arm(script("accept\nlet total = 0\nrepeat 3 every 1s with px = [10, 20]\n"
                              "    let total = total + px\n    fill qty: n + 1, price: px\n"
                              "log 'total ${total}, n back to ${n}'\n"))
        await desk.order()
        await desk.advance(3)
        assert [(t["last_qty"], t["last_price"]) for t in await desk.trades()] == [(1.0, 10.0), (2.0, 20.0), (3.0, 10.0)]
        assert run.log[-1][3] == "total 40, n back to 0"

    @pytest.mark.asyncio
    async def test_a_loop_that_never_waits_is_stopped(self, desk):
        run = desk.arm(script("accept\nwhile order.leaves_qty > 0\n    let x = 1\n"))
        await desk.order()
        assert run.instances[0].status == FAILED and "1000 times without once waiting" in run.instances[0].message

    @pytest.mark.asyncio
    async def test_an_order_that_answers_itself_is_stopped(self, desk):
        desk.runner.max_actions = 5
        run = desk.arm(script("accept\nwhile TRUE\n    fill qty: 1, price: 1\n    after 1ms\n"))
        await desk.order(qty=1000)
        await desk.advance(1)
        assert run.instances[0].status == FAILED and "more than 5 actions on one order" in run.instances[0].message

    @pytest.mark.asyncio
    async def test_a_hand_on_the_order_is_a_manual_event(self, desk):
        run = desk.arm(script("when manual\n    log 'by hand: ${event.op}'\n    stop\nafter 60s\naccept\n"))
        await desk.order()
        await desk.engine.perform("accept_request", {"session_id": "S1", "cl_ord_id": "C100"})
        await desk.runner.settle()
        assert [e[3] for e in run.log] == ["by hand: accept_request"] and run.instances[0].status == STOPPED
        await desk.advance(61)
        assert len(desk.stub.sent) == 1, "the script's own accept never went out"

    @pytest.mark.asyncio
    async def test_its_own_actions_are_not_events_but_do_refresh_the_order(self, desk):
        run = desk.arm(script("when manual\n    fail 'heard itself'\naccept\nfill qty: 40, price: 1\n"
                              "log 'cum ${order.cum_qty} leaves ${order.leaves_qty}'\n"))
        await desk.order()
        assert run.log[-1][3] == "cum 40 leaves 60" and run.instances[0].status == LISTENING


# -- arming, ownership, control ---------------------------------------------------------------

class TestRunner:
    @pytest.mark.asyncio
    async def test_idle_until_armed_and_again_after(self, desk):
        assert not desk.engine.events.active
        run = desk.arm("auto-ack")
        assert desk.engine.events.active
        desk.runner.stop(run)
        assert not desk.engine.events.active and run.status == "stopped"

    @pytest.mark.asyncio
    async def test_first_armed_match_owns_the_order(self, desk):
        ibm = desk.arm(script("reject text: 'ibm desk'\n", header="on order where symbol == 'IBM'"))
        rest = desk.arm(script("accept\n"))
        await desk.order("A", sym="IBM")
        await desk.order("B", sym="AAPL")
        assert [i.order["cl_ord_id"] for i in ibm.instances] == ["A"]
        assert [i.order["cl_ord_id"] for i in rest.instances] == ["B"]
        assert {m.get("11"): m.get("150") for m in desk.sent()} == {"A": "8", "B": "0"}

    @pytest.mark.asyncio
    async def test_moving_a_run_changes_who_is_offered_an_order_first(self, desk):
        rejecting = desk.arm(script("reject text: 'first'\n"))
        accepting = desk.arm(script("accept\n").replace("macro t", "macro u"))
        third = desk.arm(script("accept\n", header="on order where symbol == 'ZZ'").replace("macro t", "macro v"))
        assert desk.runner.offered() == [rejecting, accepting, third]
        await desk.order("A")
        assert desk.runner.move(accepting, up=True) and desk.runner.offered() == [accepting, rejecting, third]
        await desk.order("B")
        assert {m.get("11"): m.get("150") for m in desk.sent()} == {"A": "8", "B": "0"}
        assert not desk.runner.move(accepting, up=True), "already first"
        assert not desk.runner.move(third, up=False), "already last"
        assert desk.runner.move(accepting, up=False) and desk.runner.move(accepting, up=False)
        assert desk.runner.offered() == [rejecting, third, accepting]
        desk.runner.stop(third)
        assert desk.runner.offered() == [rejecting, accepting] and not desk.runner.move(third, up=True)

    @pytest.mark.asyncio
    async def test_the_same_script_may_be_armed_for_different_sessions(self, desk):
        other = StubSession("S2")
        desk.engine.sessions["S2"] = other
        one = desk.arm(script("accept\n"), session="S1")
        two = desk.arm(script("reject\n"), session="S2")
        await desk.order("A")
        await desk.order("B", session=other)
        assert [i.order["cl_ord_id"] for i in one.instances] == ["A"]
        assert [i.order["cl_ord_id"] for i in two.instances] == ["B"]

    @pytest.mark.asyncio
    async def test_blocks_are_tried_in_order_within_a_script(self, desk):
        run = desk.arm("on order where order_qty >= 1000\n    reject text: 'big'\non order\n    accept\n")
        await desk.order("A", qty=5000)
        await desk.order("B", qty=10)
        assert [m.get("150") for m in desk.sent()] == ["8", "0"] and len(run.instances) == 2

    @pytest.mark.asyncio
    async def test_a_run_may_be_kept_to_one_session(self, desk):
        other = StubSession("S2")
        desk.engine.sessions["S2"] = other
        run = desk.arm("auto-ack", session="S2")
        await desk.order("A")
        await desk.order("B", session=other)
        await desk.advance(1)
        assert [i.order["cl_ord_id"] for i in run.instances] == ["B"] and desk.sent() == [] and len(other.sent) == 1

    @pytest.mark.asyncio
    async def test_orders_that_were_there_before_arming_are_left_alone(self, desk):
        await desk.order("OLD")
        run = desk.arm("auto-ack")
        await desk.cancel("X", "OLD")
        await desk.advance(1)
        assert run.instances == [] and desk.sent() == []

    @pytest.mark.asyncio
    async def test_stop_pause_resume_detach(self, desk):
        run = desk.arm(script("accept\nwhile order.leaves_qty > 0\n    after 1s\n    fill qty: 10, price: 1\n"))
        await desk.order("A", qty=1000)
        await desk.order("B", qty=1000)
        await desk.advance(2)
        assert [(await desk.row(c))["cum_qty"] for c in "AB"] == [20.0, 20.0]
        desk.runner.pause(run)
        await desk.advance(5)
        assert (await desk.row("A"))["cum_qty"] == 20.0, "the wait under way ran out, and the script parked before its next line"
        assert run.instances[0].waiting_for == "paused"
        desk.runner.resume(run)
        await desk.runner.settle()
        assert (await desk.row("A"))["cum_qty"] == 30.0
        await desk.advance(1)
        assert (await desk.row("A"))["cum_qty"] == 40.0
        assert desk.runner.detach(run.instances[1].key) and not desk.runner.detach(999)
        await desk.advance(2)
        assert [(await desk.row(c))["cum_qty"] for c in "AB"] == [60.0, 40.0]
        assert run.instances[1].status == DETACHED and run.counts() == {RUNNING: 1, DETACHED: 1}
        desk.runner.stop(run)
        await desk.advance(5)
        assert (await desk.row("A"))["cum_qty"] == 60.0 and run.instances[0].status == STOPPED

    @pytest.mark.asyncio
    async def test_speed(self, desk):
        desk.arm(script("after 10s\naccept\n"), speed=10)
        await desk.order()
        await desk.advance(0.9)
        assert desk.sent() == []
        await desk.advance(0.2)
        assert desk.sent("150") == ["0"]

    @pytest.mark.asyncio
    async def test_what_cannot_be_armed(self, desk):
        sc, _ = macro.check("run on NOPE\n    new symbol: 'A', side: buy, qty: 1\n")
        with pytest.raises(MacroError, match="`run` on NOPE: no such session"):
            desk.runner.arm(sc)
        open_, _ = macro.check("run\n    new symbol: 'A', side: buy, qty: 1\n")
        with pytest.raises(MacroError, match="line 1: `run` names no session, so choose the one to send on"):
            desk.runner.arm(open_)
        assert desk.runner.runs == [], "a refused run leaves nothing behind"
        with pytest.raises(MacroError, match="has no block to run"):
            desk.runner.arm(macro.check("")[0])
        with pytest.raises(MacroError, match="speed"):
            desk.arm("auto-ack", speed=0)

    @pytest.mark.asyncio
    async def test_status_changes_are_reported(self, desk):
        seen = []
        desk.runner.on_change = lambda i: seen.append((i.status, i.line, i.waiting_for))
        desk.arm(script("after 1s\naccept\n"))
        await desk.order()
        await desk.advance(1)
        assert seen == [(RUNNING, 2, "after 1s"), (COMPLETED, 3, "")]

    @pytest.mark.asyncio
    async def test_too_many_live_scripts_for_the_server(self, desk):
        """The ceiling is on what is live across every run, and a script that ends makes room."""
        desk.runner.max_live = 2
        one = desk.arm(script("wait cancel\n", header="on order where symbol == 'IBM'"))
        two = desk.arm(script("wait cancel\n").replace("macro t", "macro u"))
        await desk.order("A", sym="IBM")
        await desk.order("B")
        await desk.order("C")
        assert (len(one.instances), len(two.instances), desk.runner.live) == (1, 1, 2)
        assert two.log[-1][3] == "order C not taken: 2 macros are live already"
        desk.runner.detach((await desk.row("A"))["id"])
        await desk.order("D")
        assert desk.runner.live == 2 and [i.order["cl_ord_id"] for i in two.instances] == ["B", "D"]
        desk.runner.stop_all()
        assert desk.runner.live == 0

    @pytest.mark.asyncio
    async def test_too_many_orders_for_one_run(self, desk):
        desk.runner.max_instances = 1
        run = desk.arm("auto-ack")
        await desk.order("A")
        await desk.order("B")
        assert len(run.instances) == 1 and "not taken" in run.log[-1][3]
