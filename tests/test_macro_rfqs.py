"""Macros about RFQs, quotes and RFQ requests (0.73): the client asks from a
`run` block and takes, counters or passes the quote; the market answers in
an `on rfq` block and quotes unasked from a `run` block; an RFQ request is
sent by the market and answered by the client's `on rfq request`. Run over
the linked pair of tests/test_macro_sending.py with both ends on FIX 4.4,
so the bundled examples play against each other; recorded by hand through
tests/test_macro_recorder's Hand, and written from history."""

import pytest
import pytest_asyncio

from mkfix import macro
from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.message import FixMessageFactory
from mkfix.macro import vocab
from mkfix.macro.history import FromHistory
from mkfix.macro.instance import COMPLETED, LISTENING, PASSED, STOPPED
from mkfix.macro.recorder import Recorder

from tests.test_engine import _fetch_all, stack  # noqa: F401
from tests.test_macro_history import KeptPair


@pytest_asyncio.fixture
async def pair(stack):
    """The linked pair, on FIX 4.4, keeping its messages as a real session
    does — what a macro from history is written from."""
    db, writer, engine = stack
    p = KeptPair(db, engine)
    for session, sender, target in ((p.cli, "CLI", "MKT"), (p.mkt, "MKT", "CLI")):
        session.dictionary = FixDictionary("FIX.4.4")
        session.factory = FixMessageFactory(session.dictionary, sender, target)
    yield p
    p.runner.stop_all()
    await p.runner.settle()


async def rows(pair, table, where=""):
    return await _fetch_all(pair.db, f"SELECT * FROM {table} {where} ORDER BY id")


class TestTheWords:
    def test_pass_quote_is_a_verb_and_pass_a_verdict(self):
        sc, diags = macro.check("run\n    rfq symbol: 'IBM'\n    pass quote text: 'no'\n    pass 'done'\n")
        assert diags == []
        body = sc.blocks[0].body
        assert type(body[1]).__name__ == "Action" and body[1].verb == "pass quote"
        assert type(body[2]).__name__ == "Finish" and body[2].verdict == "pass"
        assert sc.blocks[0].subject == vocab.RFQ and sc.side == "client"

    @pytest.mark.parametrize("text, side, subject", [
        ("on rfq\n    quote bid: 1\n", "market", vocab.RFQ),
        ("on quote\n    hit side: buy\n", "client", vocab.QUOTE),
        ("run\n    new quote symbol: 'IBM', bid: 1\n", "market", vocab.QUOTE),
        ("run\n    rfq request symbols: 'IBM'\n", "market", vocab.RFQ_REQUEST),
        ("on rfq request\n    rfq symbol: 'IBM'\n", "client", vocab.RFQ_REQUEST),
        ("on sent rfq\n    counter offer: 1\n", "client", vocab.RFQ),
        ("on sent quote\n    requote bid: 1\n", "market", vocab.QUOTE),
        ("on sent rfq request\n    unsubscribe\n", "market", vocab.RFQ_REQUEST),
    ])
    def test_blocks_and_their_sides(self, text, side, subject):
        sc, diags = macro.check(text)
        assert diags == [] and sc.side == side and sc.blocks[0].subject == subject

    @pytest.mark.parametrize("text, message", [
        ("on rfq\n    hit\n", "`hit` belongs in"),
        ("run\n    rfq symbol: 'IBM'\n    quote bid: 1\n", "`quote` belongs in an `on rfq` block"),
        ("on quote\n    wait quoted\n", "`quoted` never happens in an `on quote` block"),
        ("on rfq\n    reject rfq\n", "`reject rfq` needs reason"),
        ("run\n    rfq request symbols: 'A'\n    rfq symbol: 'A'\n", "belongs in a `run` block of its own"),
    ])
    def test_misplaced_words_are_named(self, text, message):
        _, diags = macro.check(text)
        assert any(message in d.message for d in macro.errors(diags)), [d.message for d in diags]

    def test_new_takes_a_quote_from_an_rfq_block(self):
        _, diags = macro.check("run\n    rfq symbol: 'IBM', side: buy, qty: 5\n    expect quoted within 5s\n"
                               "    new symbol: rfq.symbol, side: buy, qty: 5, type: previously_quoted\n")
        assert diags == []


class TestTheExamplesPlayEachOther:
    @pytest.mark.asyncio
    async def test_the_desk_and_the_taker(self, pair):
        """rfq-taker asks for IBM and MSFT; rfq-desk quotes each 20 cents
        wide, the taker counters five cents under the offer, the desk meets
        it with a requote, the taker lifts it, and the desk fills the order
        the hit made."""
        desk = pair.arm("rfq-desk", session="LOOP-MKT")
        taker = pair.arm("rfq-taker")
        await pair.advance(5)
        sent = await rows(pair, "fix_rfqs", "WHERE session_id = 'LOOP-CLI'")
        received = await rows(pair, "fix_rfqs", "WHERE session_id = 'LOOP-MKT'")
        assert [(r["symbol"], r["status"], r["offer_px"]) for r in sent] == [("IBM", "Hit", 150.05), ("MSFT", "Hit", 250.05)]
        assert [(r["symbol"], r["status"], r["sent_text"]) for r in received] == [("IBM", "Hit", "met"), ("MSFT", "Hit", "met")]
        assert all(r["quote_ref_id"] and r["pending_action"] == "" for r in received), "a requote answered the counter"
        orders = await rows(pair, "fix_orders", "WHERE direction = 'RX'")
        assert [(o["symbol"], o["status"], o["cum_qty"], o["price"], o["quote_id"] != "") for o in orders] == [
            ("IBM", "Filled", 500.0, 150.05, True), ("MSFT", "Filled", 500.0, 250.05, True)]
        assert sorted(i.message for i in taker.instances) == [
            "lifted the requote at 150.05, 1 hit so far", "lifted the requote at 250.05, 2 hit so far"]
        assert all(i.status == PASSED for i in taker.instances)
        assert [(i.kind, i.status) for i in desk.instances] == [
            ("rfq", STOPPED), ("order", COMPLETED), ("rfq", STOPPED), ("order", COMPLETED)]
        assert any(line[3].startswith("hit QTMA") for line in desk.log)

    @pytest.mark.asyncio
    async def test_a_wide_counter_is_refused_and_the_taker_hears_it(self, pair):
        pair.arm("rfq-desk", session="LOOP-MKT")
        taker = pair.arm("run\n    rfq symbol: 'IBM', side: buy, qty: 100\n    expect quoted within 5s\n"
                         "    counter offer: 149\n    expect rejected within 5s\n    pass '${rfq.rej_reason}'\n")
        await pair.advance(3)
        assert [(i.status, i.message) for i in taker.instances] == [(PASSED, "Other")]
        (received,) = await rows(pair, "fix_rfqs", "WHERE session_id = 'LOOP-MKT'")
        assert (received["status"], received["sent_text"]) == ("Rejected", "too far from 150.1")

    @pytest.mark.asyncio
    async def test_the_stream_and_the_quote_taker(self, pair):
        """quote-stream quotes IBM unasked; quote-taker counters ten cents
        under, the stream meets the counter, and the taker takes the requote
        with an order naming the quote in tag 117 — so the quote is Hit on
        both sides."""
        taker = pair.arm("quote-taker")
        stream = pair.arm("quote-stream", session="LOOP-MKT")
        await pair.advance(3)
        (received,) = await rows(pair, "fix_rfqs", "WHERE session_id = 'LOOP-CLI'")
        (sent,) = await rows(pair, "fix_rfqs", "WHERE session_id = 'LOOP-MKT'")
        assert (received["origin"], received["status"], sent["status"]) == ("quote", "Hit", "Hit")
        (order,) = await rows(pair, "fix_orders", "WHERE direction = 'TX'")
        assert (order["quote_id"], order["ord_type"], order["price"], order["macro"]) == (
            received["quote_id"], "PreviouslyQuoted", 150.0, ""), "an order nobody's macro owns"
        assert f"117={received['quote_id']}" in order["extra_tags"]
        assert [(i.kind, i.status) for i in taker.instances] == [("quote", STOPPED)]
        assert [(i.kind, i.status) for i in stream.instances] == [("quote", STOPPED)]
        assert any(line[3] == f"taken: {order['cl_ord_id']}" for line in stream.log)

    @pytest.mark.asyncio
    async def test_the_stream_ticks_and_closes_when_nobody_answers(self, pair):
        stream = pair.arm("quote-stream", session="LOOP-MKT")
        await pair.advance(12)
        (sent,) = await rows(pair, "fix_rfqs", "WHERE session_id = 'LOOP-MKT'")
        assert sent["status"] == "Canceled" and sent["sent_text"] == "stream closed"
        versions = await _fetch_all(pair.db, f"SELECT quote_id FROM fix_rfqs__history WHERE id = {sent['id']}")
        assert len({v["quote_id"] for v in versions}) == 6, "the quote and five requotes"
        (inst,) = stream.instances
        assert inst.status == PASSED and inst.message.endswith("0 still standing")
        (received,) = await rows(pair, "fix_rfqs", "WHERE session_id = 'LOOP-CLI'")
        assert received["status"] == "Canceled"

    @pytest.mark.asyncio
    async def test_the_subscriber_and_the_responder(self, pair):
        """rfq-subscriber asks for IBM and MSFT; rfq-responder sends an RFQ
        for each, naming the request; both sides count them, and the
        unsubscribe a minute on ends the responder's macro."""
        responder = pair.arm("rfq-responder")
        subscriber = pair.arm("rfq-subscriber", session="LOOP-MKT")
        await pair.advance(2)
        rfqs = await rows(pair, "fix_rfqs", "WHERE session_id = 'LOOP-MKT'")
        (request,) = await rows(pair, "fix_rfq_requests", "WHERE session_id = 'LOOP-MKT'")
        assert [(r["symbol"], r["rfq_req_id"]) for r in rfqs] == [("IBM", request["rfq_req_id"]), ("MSFT", request["rfq_req_id"])]
        assert request["quote_requests"] == 2
        await pair.advance(60)
        assert [(i.status, i.message) for i in subscriber.instances] == [(PASSED, "2 RFQs for IBM; MSFT")]
        assert [(i.status, i.message) for i in responder.instances] == [(PASSED, "unsubscribed after 2 RFQs")]
        assert any("1 requests answered so far" in line[3] for line in responder.log)
        rows_ = await rows(pair, "fix_rfq_requests")
        assert {r["status"] for r in rows_} == {"Unsubscribed"}

    @pytest.mark.asyncio
    async def test_end_to_end(self, pair):
        text = (pair.runner and __import__("pathlib").Path(macro.__file__).parent / "examples" / "end-to-end-rfq.macro"
                ).read_text(encoding="utf-8")
        sc, diags = macro.check(text)
        assert diags == [] and sc.side == vocab.E2E
        run = pair.runner.arm(sc, session="LOOP-CLI", market_session="LOOP-MKT")
        await pair.advance(3)
        assert sorted((i.kind, i.status) for i in run.instances) == [("order", PASSED), ("rfq", PASSED), ("rfq", PASSED)]
        assert run.status == "finished" and run.verdict == "passed"
        (order,) = await rows(pair, "fix_orders", "WHERE direction = 'RX'")
        assert (order["status"], order["cum_qty"], order["price"]) == ("Filled", 200.0, 100.1)


class TestRequotesKeepWhatTheyDoNotName:
    @pytest.mark.asyncio
    async def test_terms_left_out_keep_the_standing_quote(self, pair):
        run = pair.arm("run\n    new quote symbol: 'IBM', bid: 10, offer: 11, bid_size: 100, offer_size: 200\n"
                       "    requote offer: 10.9\n    pass '${quote.bid_px} ${quote.offer_px} ${quote.offer_size}'\n",
                       session="LOOP-MKT")
        await pair.runner.settle()
        assert [(i.status, i.message) for i in run.instances] == [(PASSED, "10 10.9 200")]

    @pytest.mark.asyncio
    async def test_a_quote_is_for_the_size_asked(self, pair):
        pair.arm("on rfq\n    quote bid: 1, offer: 2\n", session="LOOP-MKT")
        await pair.engine.send_rfq("LOOP-CLI", "IBM", side="1", qty=300)
        await pair.runner.settle()
        (sent,) = await rows(pair, "fix_rfqs", "WHERE session_id = 'LOOP-CLI'")
        assert (sent["bid_size"], sent["offer_size"], sent["status"]) == (300.0, 300.0, "Quoted")

    @pytest.mark.asyncio
    async def test_an_rfq_sent_by_hand_is_minded_and_a_macros_own_is_not_offered(self, pair):
        pair.arm("on rfq\n    quote bid: 1, offer: 2\n", session="LOOP-MKT")
        minder = pair.arm("on sent rfq\n    expect quoted within 1s\n    pass quote\n    pass\n")
        own = pair.arm("run\n    rfq symbol: 'MSFT'\n    expect quoted within 1s\n    pass\n")
        await pair.engine.perform("send_rfq", {"session_id": "LOOP-CLI", "symbol": "IBM", "qty": "5"})
        await pair.runner.settle()
        assert [(i.kind, i.status, i.row["symbol"]) for i in minder.instances] == [("rfq", PASSED, "IBM")]
        assert [(i.kind, i.status) for i in own.instances] == [("rfq", PASSED)]
        rfqs = {r["symbol"]: r for r in await rows(pair, "fix_rfqs", "WHERE session_id = 'LOOP-CLI'")}
        assert (rfqs["IBM"]["status"], rfqs["MSFT"]["status"]) == ("Passed", "Quoted")
        assert own.instances[0].row["id"] == rfqs["MSFT"]["id"], "the macro's own RFQ is its own"


class TestRecordingAndHistory:
    @pytest.mark.asyncio
    async def test_a_market_recording_of_quoting(self, pair):
        recorder = Recorder(pair.engine, "market", session="LOOP-MKT")
        await pair.engine.send_rfq("LOOP-CLI", "IBM", side="1", qty=100)
        await pair.engine.perform("quote_rfq", {"session_id": "LOOP-MKT", "quote_req_id": "RQMA00000001",
                                                "bid_px": "9.9", "offer_px": "10.1", "valid_for": "30"}, source="manual")
        (rfq,) = await rows(pair, "fix_rfqs", "WHERE session_id = 'LOOP-CLI'")
        await pair.engine.counter_quote("LOOP-CLI", rfq["quote_id"], offer_px=10.0)
        await pair.engine.perform("quote_rfq", {"session_id": "LOOP-MKT", "quote_req_id": "RQMA00000001",
                                                "bid_px": "9.9", "offer_px": "10.0"}, source="manual")
        result = await recorder.stop()
        source = result["source"]
        assert "on rfq where symbol == 'IBM'" in source
        assert "quote bid: 9.9, offer: 10.1, valid: 30s" in source
        assert "when countered\n        quote bid: 9.9, offer: 10\n" in source, "answered the same way each time: a rule"
        sc, diags = macro.check(source, side="market")
        assert macro.errors(diags) == [], [str(d) for d in diags]

    @pytest.mark.asyncio
    async def test_a_client_recording_of_an_rfq_taken(self, pair):
        pair.arm("on rfq\n    quote bid: 1, offer: 2\n", session="LOOP-MKT")
        recorder = Recorder(pair.engine, "client", session="LOOP-CLI")
        await pair.engine.perform("send_rfq", {"session_id": "LOOP-CLI", "symbol": "IBM", "side": "1", "qty": "50"})
        await pair.runner.settle()
        (rfq,) = await rows(pair, "fix_rfqs", "WHERE session_id = 'LOOP-CLI'")
        await pair.engine.perform("hit_quote", {"session_id": "LOOP-CLI", "quote_id": rfq["quote_id"]})
        source = (await recorder.stop())["source"]
        assert "rfq symbol: 'IBM', side: buy, qty: 50" in source and "hit" in source
        assert "expect quoted within" in source
        assert macro.errors(macro.check(source, side="client")[1]) == []

    @pytest.mark.asyncio
    async def test_from_history_both_sides_of_the_desk_and_the_taker(self, pair):
        pair.arm("rfq-desk", session="LOOP-MKT")
        pair.arm("rfq-taker")
        await pair.advance(5)
        pair.runner.stop_all()
        await pair.runner.settle()
        received = await rows(pair, "fix_rfqs", "WHERE session_id = 'LOOP-MKT'")
        market = await FromHistory(pair.engine, "market").write(vocab.RFQ, [r["id"] for r in received])
        assert "on rfq where symbol in ['IBM', 'MSFT']" in market["source"] or "on rfq where symbol == 'IBM'" in market["source"]
        assert "quote bid: 149.9, offer: 150.1, bid_size: 500, offer_size: 500, valid: 30s, quote_type: tradeable" \
            in market["source"], "the sizes and type the quote carried on the wire"
        assert "wait countered" in market["source"] or "when countered" in market["source"]
        assert market["left_out"] == [] and market["actions"] == 4
        assert macro.errors(macro.check(market["source"], side="market")[1]) == []
        sent = await rows(pair, "fix_rfqs", "WHERE session_id = 'LOOP-CLI'")
        client = await FromHistory(pair.engine, "client").write(vocab.RFQ, [r["id"] for r in sent])
        assert "rfq symbol: 'IBM', side: buy, qty: 500, request_type: manual, quote_type: tradeable" in client["source"]
        assert "    expect quoted within 5s\n    counter bid: 149.9, offer: 150.05, bid_size: 500, offer_size: 500\n" \
            "    expect requoted within 5s\n    hit side: buy, qty: 500, price: 150.05\n" in client["source"], \
            "the counter as it went out, the quote's other terms with it"
        assert client["source"].count("\n    hit") == 2
        assert macro.errors(macro.check(client["source"], side="client")[1]) == []

    @pytest.mark.asyncio
    async def test_from_history_of_a_stream_and_a_request(self, pair):
        pair.arm("quote-taker")
        pair.arm("quote-stream", session="LOOP-MKT")
        pair.arm("rfq-responder")
        pair.arm("rfq-subscriber", session="LOOP-MKT")
        await pair.advance(62)
        sent_quotes = await rows(pair, "fix_rfqs", "WHERE session_id = 'LOOP-MKT' AND origin = 'quote'")
        stream = await FromHistory(pair.engine, "market").write(vocab.QUOTE, [r["id"] for r in sent_quotes])
        assert "    new quote symbol: 'IBM', side: sell, qty: 1000, bid: 149.9, offer: 150.1\n" \
               "    expect countered within 5s\n" \
               "    requote bid: 149.9, offer: 150, bid_size: 1000, offer_size: 1000, text: 'met'\n" \
               "    expect hit within 5s\n    pass\n" in stream["source"]
        assert macro.errors(macro.check(stream["source"], side="market")[1]) == []
        taken = await rows(pair, "fix_rfqs", "WHERE session_id = 'LOOP-CLI' AND origin = 'quote'")
        answered = await FromHistory(pair.engine, "client").write(vocab.QUOTE, [r["id"] for r in taken])
        assert "on quote where symbol == 'IBM'" in answered["source"]
        assert "    counter bid: 149.9, offer: 150, bid_size: 1000, offer_size: 1000\n    wait requoted\n" \
               "    new symbol: 'IBM', side: buy, qty: 100, type: previously_quoted, price: 150" in answered["source"]
        assert macro.errors(macro.check(answered["source"], side="client")[1]) == []
        sent_requests = await rows(pair, "fix_rfq_requests", "WHERE session_id = 'LOOP-MKT'")
        subscriber = await FromHistory(pair.engine, "market").write(vocab.RFQ_REQUEST, [r["id"] for r in sent_requests])
        assert "rfq request symbols: 'IBM; MSFT', subscription: subscribe, request_type: automatic" in subscriber["source"]
        assert "unsubscribe" in subscriber["source"]
        received_requests = await rows(pair, "fix_rfq_requests", "WHERE session_id = 'LOOP-CLI'")
        responder = await FromHistory(pair.engine, "client").write(vocab.RFQ_REQUEST, [r["id"] for r in received_requests])
        assert "on rfq request where symbols == 'IBM; MSFT'" in responder["source"]
        assert responder["source"].count("    rfq symbol:") == 2
        for result, side in ((subscriber, "market"), (responder, "client")):
            assert macro.errors(macro.check(result["source"], side=side)[1]) == [], result["source"]


class TestRequestInstruments:
    """0.79.2: `rfq request instruments: 'A, B'` names declared or saved
    instruments, each an instance with its terms; the recorder and a macro
    from history write them back by name."""

    ESZ6 = "instrument 'ESZ6' symbol: 'ES', sec_type: future, maturity: '202612'\n"

    def test_the_words(self):
        assert macro.errors(macro.check(self.ESZ6 + "run\n    rfq request instruments: 'ESZ6'\n")[1]) == []
        found = [d.message for d in macro.errors(macro.check("run\n    rfq request subscription: subscribe\n")[1])]
        assert found == ["`rfq request` needs symbols or instruments"]
        found = [d.message for d in macro.check("run\n    rfq request instruments: 'ESZ6, NOPE'\n",
                                                instruments={"ESZ6": {"symbol": "ES"}})[1]]
        assert any("No instrument named 'NOPE'" in m for m in found), found

    @pytest.mark.asyncio
    async def test_a_declared_instrument_goes_with_its_terms(self, pair):
        run = pair.arm(self.ESZ6 + "run\n    rfq request symbols: 'IBM', instruments: 'ESZ6'\n"
                       "    pass '${rfq_request.instruments}'\n", session="LOOP-MKT")
        await pair.advance(1)
        assert [i.message for i in run.instances] == ["ES Dec26"]
        wire = pair.engine._as_sent(pair.mkt, pair.mkt.sent[-1]).to_pipe_string()
        assert "|146=2|55=IBM|55=ES|167=FUT|200=202612|" in wire
        (received,) = await rows(pair, "fix_rfq_requests", "WHERE session_id = 'LOOP-CLI'")
        assert (received["symbols"], received["instruments"]) == ("IBM", "ES Dec26")

    @pytest.mark.asyncio
    async def test_recorded_and_from_history_by_name(self, pair):
        engine = pair.engine
        await engine.save_instrument("ESZ6", symbol="ES", security_type="FUT", maturity="202612")
        recorder = Recorder(engine, "market", "LOOP-MKT")
        await engine.perform("send_rfq_request", {"session_id": "LOOP-MKT", "symbols": "IBM",
                                                  "instruments": "ESZ6"})
        recorded = await recorder.stop("rec")
        sent = await rows(pair, "fix_rfq_requests", "WHERE session_id = 'LOOP-MKT'")
        written = await FromHistory(engine, "market").write(vocab.RFQ_REQUEST, [r["id"] for r in sent])
        for result in (recorded, written):
            lines = result["source"].splitlines()
            assert "instrument 'ESZ6' symbol: 'ES', sec_type: future, maturity: '202612'" in lines
            assert any(line.strip().startswith("rfq request symbols: 'IBM', instruments: 'ESZ6'") for line in lines), \
                result["source"]
            assert macro.errors(macro.check(result["source"], side="market")[1]) == []
