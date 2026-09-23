"""The recorder: orders worked by hand, written out as the script that would
have done it — and that script, run, does it again."""

import pytest
import pytest_asyncio

from mkfix import macro
from mkfix.macro.instance import PASSED
from mkfix.macro.recorder import Recorder, _duration, _number, _quote, _value

from tests.test_engine import _fetch_all, stack  # noqa: F401
from tests.test_macro_sending import Pair


class Hand:
    """Both ends of the loopback worked by hand, on a clock the test moves."""

    def __init__(self, pair):
        self.pair, self.engine, self.now = pair, pair.engine, 100.0

    def clock(self):
        return self.now

    def recorder(self, side, session="", **kw):
        return Recorder(self.engine, side, session, clock=self.clock, **kw)

    async def do(self, op, wait=0.0, **data):
        self.now += wait
        return await self.engine.perform(op, {k: str(v) for k, v in data.items()})

    async def new(self, wait=0.0, symbol="IBM", qty=100, price=10, **more):
        result = await self.do("send_new_order", wait, session_id="LOOP-CLI", symbol=symbol, side="1", qty=qty,
                               price=price, **more)
        return result["cl_ord_id"]

    async def market(self, op, cl, wait=0.0, **data):
        return await self.do(op, wait, session_id="LOOP-MKT", cl_ord_id=cl, **data)

    async def replace(self, cl, wait=0.0, qty=200, price=10, **more):
        result = await self.do("send_cancel_replace", wait, session_id="LOOP-CLI", orig_cl_ord_id=cl, symbol="IBM",
                               side="1", qty=qty, price=price, **more)
        return result["cl_ord_id"]

    async def cancel(self, cl, wait=0.0):
        result = await self.do("send_cancel", wait, session_id="LOOP-CLI", orig_cl_ord_id=cl, symbol="IBM", side="1", qty=0)
        return result["cl_ord_id"]


@pytest_asyncio.fixture
async def hand(stack):
    db, writer, engine = stack
    pair = Pair(db, engine)
    yield Hand(pair)
    pair.runner.stop_all()
    await pair.runner.settle()


def body(source):
    """The script without its header comments and blank lines."""
    return [line for line in source.splitlines() if line.strip() and not line.startswith("#")]


async def lifecycle(hand):
    """One order through accept, a fill, a replace and a cancel, both ends by hand."""
    cl = await hand.new()
    await hand.market("accept_request", cl, wait=1.2, text="working")
    await hand.market("fill_order", cl, wait=2, qty=50, price=10)
    # A request is answered on the order's row, under the ClOrdID it holds: the chain moves on an accept.
    cl2 = await hand.replace(cl, wait=1)
    await hand.market("accept_request", cl, wait=0.5)
    await hand.cancel(cl2, wait=1)
    await hand.market("accept_request", cl2, wait=0.3)


class TestMarketRecording:
    @pytest.mark.asyncio
    async def test_a_worked_order_becomes_its_script(self, hand):
        recorder = hand.recorder("market")
        await lifecycle(hand)
        assert recorder.status() == {"recording": True, "side": "market", "session": "", "orders": 1, "actions": 4,
                                     "since": recorder.started_at}
        result = await recorder.stop("venue")
        assert (result["orders"], result["actions"]) == (1, 4) and not recorder.recording
        assert body(result["source"]) == [
            "on order where symbol == 'IBM'",
            "    when replace",                 # a request answered the same way every time is a rule
            "        accept",
            "    when cancel",
            "        accept",
            "        stop",                     # nothing was done after it: the order was over
            "    accept text: 'working'",       # the arrival is the event: no delay
            "    after 2s",                     # nothing came between the accept and the fill: only time
            "    fill qty: 50, price: 10"]
        assert "# Each action runs the moment what it answered comes" in result["source"]
        assert "two.\n\non order where" in result["source"], "a blank line between the header comment and the first block"
        assert "macro venue" not in result["source"], "the text carries no name: it is named where it is saved"
        assert result["source"].startswith("# Recorded 20") and " on LOOP-MKT: 1 order, 4 actions." in result["source"]

    @pytest.mark.asyncio
    async def test_the_recording_checks_clean_and_does_it_again(self, hand):
        recorder = hand.recorder("market")
        await lifecycle(hand)
        source = (await recorder.stop("venue"))["source"]
        sc, diags = macro.check(source, side="market")
        assert diags == [] and sc.side == "market"
        before = len(hand.pair.mkt.sent)
        pair = hand.pair
        pair.arm(source)
        run = pair.arm("run\n    new symbol: 'IBM', side: buy, qty: 100, price: 10\n"
                       "    wait fill\n    after 1s\n    replace qty: 200\n    wait replaced\n    after 1s\n    cancel\n"
                       "    expect canceled within 2s\n    pass\n")
        await pair.advance(10)
        assert [i.status for i in run.instances] == [PASSED]
        wire = [pair.engine._as_sent(pair.mkt, m) for m in pair.mkt.sent[before:]]
        assert [(m.get("150"), m.get("58")) for m in wire] == [("0", "working"), ("1", None), ("5", None), ("4", None)]
        assert wire[1].get("32") == "50" and wire[2].get("38") == "200"

    @pytest.mark.asyncio
    async def test_orders_worked_the_same_way_share_a_block_and_others_do_not(self, hand):
        recorder = hand.recorder("market")
        ibm = await hand.new(symbol="IBM")
        msft = await hand.new(symbol="MSFT")
        zzzz = await hand.new(symbol="ZZZZ")
        await hand.market("accept_request", ibm, wait=1)
        await hand.market("accept_request", msft, wait=0.4)
        await hand.market("reject_request", zzzz, wait=2, text="Unknown symbol")
        assert body((await recorder.stop())["source"]) == [
            "on order where symbol in ['IBM', 'MSFT']", "    accept",
            "on order where symbol == 'ZZZZ'", "    reject text: 'Unknown symbol'"]

    @pytest.mark.asyncio
    async def test_a_symbol_worked_two_ways_is_told_apart_by_quantity_narrowest_first(self, hand):
        recorder = hand.recorder("market")
        small = await hand.new(qty=100)
        big = await hand.new(qty=50000)
        await hand.market("accept_request", small, wait=1)
        await hand.market("reject_request", big, wait=1, text="too big")
        source = (await recorder.stop())["source"]
        assert [line for line in body(source) if line.startswith("on order")] == [
            "on order where symbol == 'IBM' and order_qty == 100", "on order where symbol == 'IBM' and order_qty == 50000"]
        assert macro.check(source, side="market")[1] == []

    @pytest.mark.asyncio
    async def test_trades_are_named_the_way_a_script_names_them(self, hand):
        recorder = hand.recorder("market")
        cl = await hand.new(qty=300)
        await hand.market("accept_request", cl, wait=1)
        for qty in (100, 50, 25):
            await hand.market("fill_order", cl, wait=1, qty=qty, price=10)
        first, middle, last = [t["exec_id"] for t in await hand.pair.trades("TX")]
        await hand.do("correct_trade", 1, session_id="LOOP-MKT", exec_id=last, qty=20, price=10)
        await hand.do("bust_trade", 1, session_id="LOOP-MKT", exec_id=first, text="in error")
        await hand.do("correct_trade", 1, session_id="LOOP-MKT", exec_id=middle, qty=60, price=10.5)
        source = (await recorder.stop())["source"]
        assert body(source)[-5:] == [
            "    correct last trade qty: 20, price: 10", "    after 1s",
            "    bust first trade text: 'in error'", "    after 1s",
            "    correct trade where trade.last_qty == 50 and trade.last_price == 10, qty: 60, price: 10.5"]
        assert macro.check(source, side="market")[1] == []

        # and run, it does to a new order's trades what was done by hand to this one's
        pair = hand.pair
        by_hand = [(t["last_qty"], t["last_price"], t["exec_type"]) for t in await pair.trades("TX")]
        pair.arm(source)
        await hand.new(qty=300)
        await pair.advance(10)
        again = [(t["last_qty"], t["last_price"], t["exec_type"]) for t in (await pair.trades("TX"))[3:]]
        assert again == by_hand == [(100.0, 10.0, "Cancel"), (60.0, 10.5, "Correct"), (20.0, 10.0, "Correct")]

    @pytest.mark.asyncio
    async def test_an_order_worked_from_both_blotters_is_one_macro(self, hand):
        """Received Orders and Sent Trades in one recording: the recorder
        follows orders, not blotters — a fill, the client's dispute, and the
        re-notification that answers it."""
        market, client = hand.recorder("market"), hand.recorder("client")
        cl = await hand.new(qty=100)
        await hand.market("accept_request", cl, wait=1)
        await hand.market("fill_order", cl, wait=1, qty=100, price=10)
        (received,) = await hand.pair.trades("RX")
        await hand.do("dk_trade", 2, session_id="LOOP-CLI", exec_id=received["exec_id"], dk_reason="F")
        (sent,) = await hand.pair.trades("TX")
        await hand.do("renotify_trade", 0.5, session_id="LOOP-MKT", exec_id=sent["exec_id"], text="as booked")
        venue, disputer = (await market.stop("venue"))["source"], (await client.stop("disputer"))["source"]
        assert body(venue)[1:] == ["    when dk", "        renotify last trade text: 'as booked'",
                                   "    accept", "    after 1s", "    fill qty: 100, price: 10"]
        assert body(disputer)[2:] == ["    expect ack within 5s", "    expect filled within 5s",
                                      "    dk last trade reason: calculation_difference",
                                      "    expect filled within 5s", "    pass"], "the re-notification is heard as the fill it restates"
        assert macro.check(venue, side="market")[1] == [] and macro.check(disputer, side="client")[1] == []
        # and the two recordings, run against each other, do it again
        pair = hand.pair
        before = len(await pair.trades("TX"))
        pair.arm(venue)
        run = pair.arm(disputer)
        await pair.advance(10)
        assert [i.status for i in run.instances] == [PASSED]
        (again,) = (await pair.trades("TX"))[before:]
        assert again["dk_reason"] == "" and again["exec_id"] != again["exec_ref_id"], "disputed, then re-notified under a fresh ExecID"

    @pytest.mark.asyncio
    async def test_what_is_not_recorded(self, hand):
        early = await hand.new(symbol="EARLY")
        recorder = hand.recorder("market", session="LOOP-MKT")
        await hand.market("accept_request", early, wait=1)             # under way before the recording began
        untouched = await hand.new(symbol="IDLE")                       # arrived, never worked
        hand.pair.arm("on order where symbol == 'AUTO'\n    accept\n")
        await hand.new(symbol="AUTO")                                   # a macro's order
        await hand.pair.runner.settle()
        result = await recorder.stop()
        assert (result["orders"], result["actions"]) == (0, 0) and untouched
        assert "# Nothing was recorded: no order arrived and was worked while recording." in result["source"]
        assert macro.check(result["source"], side="market")[0].blocks == []

    @pytest.mark.asyncio
    async def test_one_session_only_when_asked(self, hand):
        elsewhere = hand.recorder("market", session="LOOP-CLI")
        here = hand.recorder("market", session="LOOP-MKT")
        cl = await hand.new()
        await hand.market("accept_request", cl, wait=1)
        assert (await elsewhere.stop())["orders"] == 0 and (await here.stop())["orders"] == 1


class TestWhatTriggersAnAction:
    """An action runs when what it answered comes; the clock only decides
    where nothing came between two actions."""

    @pytest.mark.asyncio
    async def test_a_request_answered_two_ways_is_no_rule_and_stays_in_order(self, hand):
        recorder = hand.recorder("market")
        cl = await hand.new()
        await hand.market("accept_request", cl, wait=1)
        cl2 = await hand.replace(cl, wait=1, qty=200)
        await hand.market("accept_request", cl, wait=0.5)
        await hand.replace(cl2, wait=1, qty=300)
        await hand.market("reject_request", cl2, wait=0.5, text="one replace per order")
        source = (await recorder.stop())["source"]
        assert body(source)[1:] == ["    accept", "    wait replace", "    accept", "    wait replace",
                                    "    reject text: 'one replace per order'"]
        assert macro.check(source, side="market")[1] == []

    @pytest.mark.asyncio
    async def test_a_request_left_unanswered_is_waited_for_by_what_came_next(self, hand):
        recorder = hand.recorder("market")
        cl = await hand.new(qty=300)
        await hand.market("accept_request", cl, wait=1)
        await hand.cancel(cl, wait=1)                                   # heard, and ignored
        await hand.market("fill_order", cl, wait=3, qty=300, price=10)
        await hand.cancel(cl, wait=1)                                   # heard after the last thing done
        assert body((await recorder.stop())["source"])[1:] == ["    accept", "    wait cancel", "    fill qty: 300, price: 10"], \
            "two cancels, one answered by nothing: no rule — and a wait nothing follows is not written"

    @pytest.mark.asyncio
    async def test_an_accepted_cancel_stops_the_macro_only_when_nothing_was_done_after_it(self, hand):
        recorder = hand.recorder("market")
        cl = await hand.new(qty=300)
        await hand.market("accept_request", cl, wait=1)
        await hand.cancel(cl, wait=1)
        await hand.market("reject_request", cl, wait=0.2, text="too late")
        await hand.market("fill_order", cl, wait=2, qty=300, price=10)
        assert body((await recorder.stop())["source"])[1:] == [
            "    when cancel", "        reject text: 'too late'", "    accept", "    after 3.2s", "    fill qty: 300, price: 10"]

    @pytest.mark.asyncio
    async def test_orders_worked_by_the_same_rule_share_a_block_whatever_the_timing(self, hand):
        recorder = hand.recorder("market")
        for symbol, think in (("IBM", 0.4), ("MSFT", 3.0)):
            cl = await hand.new(symbol=symbol)
            await hand.market("accept_request", cl, wait=think)
            await hand.cancel(cl, wait=1)
            await hand.market("accept_request", cl, wait=think)
        assert body((await recorder.stop())["source"]) == [
            "on order where symbol in ['IBM', 'MSFT']", "    when cancel", "        accept", "        stop", "    accept"]

    @pytest.mark.asyncio
    async def test_every_event_heard_is_expected_in_the_order_it_came(self, hand):
        recorder = hand.recorder("client")
        cl = await hand.new(qty=300)
        await hand.market("accept_request", cl, wait=0.5)
        for wait, qty in ((1, 100), (4, 100), (1, 100)):
            await hand.market("fill_order", cl, wait=wait, qty=qty, price=10)
        source = (await recorder.stop())["source"]
        assert body(source)[2:] == ["    expect ack within 5s", "    expect fill within 5s", "    expect fill within 12s",
                                    "    expect filled within 5s", "    pass"]
        assert macro.check(source, side="client")[1] == []

    @pytest.mark.asyncio
    async def test_keeping_the_delays_adds_the_time_taken_to_answer(self, hand):
        market, client = hand.recorder("market"), hand.recorder("client", delays=True)
        await lifecycle(hand)
        venue = await market.stop("venue", delays=True)                 # chosen at the end: the timeline has it either way
        assert body(venue["source"])[1:] == [
            "    when replace", "        after 500ms", "        accept",
            "    when cancel", "        after 300ms", "        accept", "        stop",
            "    after 1.2s", "    accept text: 'working'", "    after 2s", "    fill qty: 50, price: 10"]
        assert "after the time you took to answer it" in venue["source"]
        chase = body((await client.stop("chase"))["source"])
        assert chase[2:] == ["    expect ack within 5s", "    expect fill within 6s", "    after 1s", "    replace qty: 200",
                             "    expect replaced within 5s", "    after 1s", "    cancel", "    expect canceled within 5s", "    pass"]
        assert macro.check(venue["source"], side="market")[1] == []

    @pytest.mark.asyncio
    async def test_the_delayed_recordings_run_against_each_other_too(self, hand):
        market, client = hand.recorder("market"), hand.recorder("client")
        await lifecycle(hand)
        venue, chase = (await market.stop("venue", delays=True))["source"], (await client.stop("chase", delays=True))["source"]
        pair = hand.pair
        pair.arm(venue)
        run = pair.arm(chase)
        await pair.advance(15)
        assert [(i.status, i.message) for i in run.instances] == [(PASSED, "")]
        sent = (await pair.orders("TX"))[-1]
        assert (sent["status"], sent["order_qty"], sent["cum_qty"]) == ("Canceled", 200.0, 50.0)

    @pytest.mark.asyncio
    async def test_a_recorded_venue_answers_a_cancel_that_comes_sooner_than_it_did(self, hand):
        """Why the accepted cancel says `stop`: replayed against a client that
        cancels at once, the fill still to come is not done to a cancelled order."""
        recorder = hand.recorder("market")
        cl = await hand.new(qty=300)
        await hand.market("accept_request", cl, wait=1)
        await hand.market("fill_order", cl, wait=5, qty=100, price=10)
        await hand.cancel(cl, wait=1)
        await hand.market("accept_request", cl, wait=0.5)
        source = (await recorder.stop("venue"))["source"]
        assert body(source)[1:] == ["    when cancel", "        accept", "        stop", "    accept", "    after 5s",
                                    "    fill qty: 100, price: 10"]
        pair = hand.pair
        before = len(await pair.trades("TX"))
        pair.arm(source)
        run = pair.arm("run\n    new symbol: 'IBM', side: buy, qty: 300, price: 10\n    expect ack within 5s\n"
                       "    cancel\n    expect canceled within 5s\n    pass\n")
        await pair.advance(10)
        assert [i.status for i in run.instances] == [PASSED]
        assert len(await pair.trades("TX")) == before, "the order was cancelled first: it is not filled afterwards"


class TestClientRecording:
    @pytest.mark.asyncio
    async def test_a_sent_order_becomes_a_run_block_that_checks_what_it_heard(self, hand):
        recorder = hand.recorder("client")
        await lifecycle(hand)
        source = (await recorder.stop("chase"))["source"]
        assert body(source) == [
            "run",
            "    new symbol: 'IBM', side: buy, qty: 100, type: limit, price: 10, tif: day",
            "    expect ack within 5s",          # every event heard, in order, each with a generous bound
            "    expect fill within 6s",         # three times the 2 s it took
            "    replace qty: 200",              # triggered by the fill, not by a clock
            "    expect replaced within 5s",
            "    cancel",
            "    expect canceled within 5s",
            "    pass"]
        sc, diags = macro.check(source, side="client")
        assert diags == [] and sc.needs_session, "the session is chosen at Run…, like any client script"

    @pytest.mark.asyncio
    async def test_the_recording_runs_against_the_market_recording(self, hand):
        """Both ends recorded at once, then both recordings run against each other."""
        market, client = hand.recorder("market"), hand.recorder("client")
        await lifecycle(hand)
        venue, chase = (await market.stop("venue"))["source"], (await client.stop("chase"))["source"]
        pair = hand.pair
        pair.arm(venue)
        run = pair.arm(chase)
        await pair.advance(12)
        assert [(i.status, i.message) for i in run.instances] == [(PASSED, "")]
        sent = (await pair.orders("TX"))[-1]
        assert (sent["status"], sent["order_qty"], sent["cum_qty"]) == ("Canceled", 200.0, 50.0)

    @pytest.mark.asyncio
    async def test_several_orders_keep_their_spacing_and_their_terms(self, hand):
        recorder = hand.recorder("client")
        await hand.new(symbol="IBM", qty=100, price=10, client="ACME", text="first", extra_tags="9001=x")
        await hand.new(wait=2.5, symbol="MSFT", qty=250, price=20.25, ord_type="2", tif="1", handl_inst="3")
        await hand.new(wait=0.5, symbol="AAPL", qty=10, price="", ord_type="1")
        source = (await recorder.stop())["source"]
        assert body(source) == [
            "run",
            "    new symbol: 'IBM', side: buy, qty: 100, type: limit, price: 10, tif: day, client: 'ACME', text: 'first', extra: '9001=x'",
            "run", "    after 2.5s",
            "    new symbol: 'MSFT', side: buy, qty: 250, type: limit, price: 20.25, tif: gtc, handl_inst: manual",
            "run", "    after 3s",
            "    new symbol: 'AAPL', side: buy, qty: 10, type: market, tif: day"]
        assert macro.check(source, side="client")[1] == []

    @pytest.mark.asyncio
    async def test_a_refused_request_and_a_dk(self, hand):
        recorder = hand.recorder("client")
        cl = await hand.new()
        await hand.market("accept_request", cl, wait=0.5)
        await hand.market("fill_order", cl, wait=0.5, qty=100, price=10)
        await hand.cancel(cl, wait=1)
        await hand.market("reject_request", cl, wait=0.2, text="too late")
        (trade,) = await hand.pair.trades("RX")
        await hand.do("dk_trade", 2, session_id="LOOP-CLI", exec_id=trade["exec_id"], dk_reason="E", text="through the limit")
        assert body((await recorder.stop())["source"])[2:] == [
            "    expect ack within 5s", "    expect filled within 5s", "    cancel", "    expect cancel rejected within 5s",
            "    dk last trade reason: price_exceeds_limit, text: 'through the limit'"]

    @pytest.mark.asyncio
    async def test_a_scripts_own_orders_and_the_other_side_are_not_recorded(self, hand):
        recorder = hand.recorder("client")
        run = hand.pair.arm("run\n    new symbol: 'OWN', side: buy, qty: 1, price: 1\n")
        await hand.pair.runner.settle()
        assert len(run.instances) == 1
        result = await recorder.stop()
        assert result["orders"] == 0 and "no order was sent by hand" in result["source"]


class TestWriting:
    def test_durations_read_like_a_person_wrote_them(self):
        assert [_duration(s) for s in (0.012, 0.249, 0.5, 0.999, 1.0, 1.26, 59.96, 119.9, 120, 600)] == [
            "10ms", "250ms", "500ms", "1000ms", "1s", "1.3s", "60s", "119.9s", "2m", "10m"]
        assert [_duration(s, coarse=True) for s in (2.0, 2.1, 0.4)] == ["2s", "3s", "1s"]

    def test_values(self):
        assert [_number(v) for v in (100.0, "50", 20.25, 1e6)] == ["100", "50", "20.25", "1000000"]
        assert _quote("it's") == "'it\\'s'" and _quote("a\\b") == "'a\\\\b'"
        assert _value("new", "side", "2") == "sell" and _value("new", "side", "9") == "'9'", "a code with no word stays a code"
        assert _value("dk", "reason", "Z") == "other" and _value("restate", "reason", "3") == "repricing"
        assert _value("fill", "qty", "abc") == "'abc'"

    @pytest.mark.parametrize("text", ["it's a 'test'", "back\\slash", "50% # not a comment"])
    def test_any_text_survives_the_round_trip(self, text):
        source = f"on order\n    accept text: {_quote(text)}\n"
        sc, diags = macro.check(source)
        assert diags == [], [str(d) for d in diags]
        assert sc.blocks[0].body[0].terms[0].value.node.value == text

    def test_every_verb_is_recorded_under_its_op(self):
        from mkfix.fix.actions import ACTIONS
        from mkfix.macro.recorder import _VERB_OF
        from mkfix.macro import vocab
        assert set(_VERB_OF) == set(ACTIONS), "an action the recorder cannot name would be dropped silently"
        assert set(_VERB_OF.values()) == set(vocab.VERBS)


async def _status(ask, side):
    return {k: v for k, v in (await ask("record_status", {"side": side})).items() if k != "ok"}


class TestThroughTheManager:
    @pytest.mark.asyncio
    async def test_fix_cmd_starts_and_stops_a_recording_a_side(self, stack):
        from tests.test_macro_store import _ask, _manager
        db, writer, engine = stack
        pair = Pair(db, engine)
        manager, _ = await _manager(engine)
        try:
            ask = _ask(engine)
            assert (await ask("record_status", {"side": "market"}))["recording"] is False
            started = await ask("record_start", {"side": "market", "session": "LOOP-MKT"})
            assert started["recording"] and started["session"] == "LOOP-MKT"
            await ask("record_start", {"side": "client"})
            for data, why in (({"side": "market"}, "A market recording is already under way"),
                              ({"side": "both"}, "client side or the market side"), ({}, "Record the client side"),
                              ({"side": "client", "session": "NOPE"}, "already under way")):
                with pytest.raises(ValueError, match=why):
                    await ask("record_start", data)
            cl = (await engine.perform("send_new_order", {"session_id": "LOOP-CLI", "symbol": "IBM", "side": "1",
                                                          "qty": "100", "price": "10"}))["cl_ord_id"]
            await ask("accept_request", {"session_id": "LOOP-MKT", "cl_ord_id": cl})
            assert (await ask("record_status", {"side": "market"}))["actions"] == 1
            venue = await ask("record_stop", {"side": "market", "name": "  my   venue "})
            assert venue["orders"] == 1 and venue["name"] == "my venue" and "macro my venue" not in venue["source"]
            saved = await ask("save_macro", {"name": "my venue", "source": venue["source"], "side": "market"})
            assert saved["errors"] == 0
            with pytest.raises(ValueError, match="No market recording is under way"):
                await ask("record_stop", {"side": "market"})
            # from an order blotter: stop and save in one step, the name checked before anything is lost
            assert (await ask("macro_status", {}))["client"]["recording"] is True
            await engine.perform("send_new_order", {"session_id": "LOOP-CLI", "symbol": "MSFT", "side": "1",
                                                    "qty": "5", "price": "1"})
            with pytest.raises(ValueError, match="A macro is already called 'my venue': choose another name. Still recording"):
                await ask("record_stop", {"side": "client", "name": "my venue", "save": "1"})
            assert (await ask("record_status", {"side": "client"}))["recording"] is True
            kept = await ask("record_stop", {"side": "client", "name": "my chase", "save": "1", "delays": "1"})
            assert "after the time you took to answer it" in kept["source"]
            assert (kept["saved"], kept["name"], kept["side"], kept["orders"]) == (True, "my chase", "client", 2)
            row = await manager.load("my chase")
            assert (row["side"], row["problems"], row["needs_session"]) == ("client", 0, 1) and row["source"] == kept["source"]
            await ask("record_start", {"side": "client"})
            nothing = await ask("record_stop", {"side": "client", "name": "empty", "save": "1"})
            assert (nothing["saved"], nothing["orders"]) == (False, 0)
            with pytest.raises(ValueError, match="No macro named 'empty'"):
                await manager.load("empty")
            assert await ask("macro_status", {}) == {"ok": True, "client": await _status(ask, "client"), "market": await _status(ask, "market")}
            assert not engine.events.active or manager.recorders.keys() == {"client"}
        finally:
            await manager.stop()
        assert manager.recorders == {} and not engine.events.active, "a recording does not outlive the server's stop"
