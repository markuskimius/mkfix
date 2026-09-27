"""RFQs and quotes: the negotiations the engine keeps on fix_rfqs — a
QuoteRequest and the quotes answering it, or a stream of unsolicited quotes
— the messages each side sends and answers, per version, and the pure part
in mkfix/fix/families.py."""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from mkfix.fix.dictionary import FixDictionary, STANDARD_VERSIONS
from mkfix.fix.families import parse_stamp, quote_columns, quote_side, rfq_columns
from mkfix.fix.message import FixMessageFactory, parse_fix

from tests.test_engine import RecordingStub, _fetch_all, stack  # noqa: F401
from tests.test_replay_e2e import LinkedSession

R_42 = "8=FIX.4.2|35=R|131=Q1|146=1|55=AAPL|54=1|38=500|303=1|60=20260927-10:00:00.000|58=please|5001=r"
S_44 = ("8=FIX.4.4|35=S|131={req}|117={qid}|537=1|55=AAPL|132=150.1|133=150.3|134=500|135=400"
        "|62=20990101-00:00:00.000|58=firm|5001=s")


async def _rows(db, where=""):
    return await _fetch_all(db, f"SELECT * FROM fix_rfqs {where} ORDER BY id")


async def _versions(db, row_id):
    return await _fetch_all(db, f"SELECT * FROM fix_rfqs__history WHERE id = {row_id} ORDER BY _mkio_version")


def _stub(engine, version="FIX.4.4"):
    stub = RecordingStub(engine)
    stub.dictionary = FixDictionary(version)
    stub.factory = FixMessageFactory(stub.dictionary, "CLIENT", "MKT")
    engine.sessions["S1"] = stub
    return stub


@pytest_asyncio.fixture
async def linked(stack):
    """A client session and a market session on one engine, each end's
    sends handled by the other's."""
    db, writer, engine = stack
    cli = LinkedSession(engine, "Client", "Client", "Server")
    mkt = LinkedSession(engine, "Server", "Server", "Client")
    cli.peer, mkt.peer = mkt, cli
    engine.sessions.update({"Client": cli, "Server": mkt})
    yield db, engine, cli, mkt


# ── The pure part ──────────────────────────────────────────────────────

class TestColumns:
    def test_a_request_reads_its_instrument_group(self):
        cols = rfq_columns(parse_fix(R_42), FixDictionary("FIX.4.2"))
        assert (cols["quote_req_id"], cols["symbol"], cols["side"], cols["order_qty"]) == ("Q1", "AAPL", "Buy", 500)
        assert cols["quote_request_type"] == "Manual" and cols["text"] == "please"

    def test_a_flat_request_and_a_two_way_one(self):
        cols = rfq_columns(parse_fix("8=FIX.4.0|35=R|131=Q2|55=IBM"), FixDictionary("FIX.4.0"))
        assert (cols["symbol"], cols["side"], cols["side_code"], cols["order_qty"]) == ("IBM", "", "", 0)

    def test_a_quote(self):
        cols = quote_columns(parse_fix(S_44.format(req="Q1", qid="Z1")), FixDictionary("FIX.4.4"))
        assert (cols["quote_id"], cols["bid_px"], cols["offer_px"], cols["bid_size"], cols["offer_size"]) == \
            ("Z1", 150.1, 150.3, 500, 400)
        assert cols["quote_type"] == "Tradeable" and cols["valid_until"] == "20990101-00:00:00.000"
        one_sided = quote_columns(parse_fix("8=FIX.4.2|35=S|117=Z2|55=X|132=1"), FixDictionary("FIX.4.2"))
        assert one_sided["offer_px"] is None and one_sided["bid_px"] == 1.0

    def test_sides_and_stamps(self):
        assert quote_side({"origin": "rfq", "direction": "TX"}) == "client"
        assert quote_side({"origin": "quote", "direction": "RX"}) == "client"
        assert quote_side({"origin": "rfq", "direction": "RX"}) == "market"
        assert quote_side({"origin": "quote", "direction": "TX"}) == "market"
        assert parse_stamp("20260927-10:00:01.5") == datetime(2026, 9, 27, 10, 0, 1, 500000, tzinfo=timezone.utc)
        assert parse_stamp("20260927-10:00:01") == datetime(2026, 9, 27, 10, 0, 1, tzinfo=timezone.utc)
        assert parse_stamp("") is None and parse_stamp("tomorrow") is None


class TestFactoryByVersion:
    @pytest.mark.parametrize("version", STANDARD_VERSIONS)
    def test_quote_request(self, version):
        f = FixMessageFactory(FixDictionary(version), "A", "B")
        msg = f.quote_request("Q1", "AAPL", side="1", qty=500, quote_request_type="1", quote_type="1",
                              currency="USD", text="hi")
        msg.sendprep(f.dictionary, "A", "B", 1)
        wire = msg.to_pipe_string()
        if version in ("FIX.4.0", "FIX.4.1"):
            assert "|131=Q1|58=hi|55=AAPL|54=1|38=500|" in wire and "|146=" not in wire, "flat through 4.1"
            assert "|303=" not in wire and "|537=" not in wire
        else:
            assert "|131=Q1|58=hi|146=1|55=AAPL|303=1|" in wire, "one NoRelatedSym instance from 4.2"
            assert ("|537=1|" in wire) == (version not in ("FIX.4.2",)), "QuoteType joined in 4.3"
            assert "|54=1|38=500|15=USD|60=" in wire

    @pytest.mark.parametrize("version", STANDARD_VERSIONS)
    def test_quote(self, version):
        f = FixMessageFactory(FixDictionary(version), "A", "B")
        msg = f.quote("Z1", "AAPL", quote_req_id="Q1", bid_px=10.5, offer_px=10.7, bid_size=100, offer_size=200,
                      valid_until="20260927-10:00:00", quote_type="1", side="1", qty=100, currency="USD")
        msg.sendprep(f.dictionary, "A", "B", 1)
        wire = msg.to_pipe_string()
        assert "|131=Q1|117=Z1|" in wire and "|132=10.5|133=10.7|134=100|135=200|62=20260927-10:00:00|" in wire
        legacy = version in ("FIX.4.0", "FIX.4.1")
        assert ("|537=1|" in wire) == (version not in ("FIX.4.0", "FIX.4.1", "FIX.4.2")), "QuoteType joined in 4.3"
        assert ("|54=1|38=100|" in wire) == (version not in ("FIX.4.0", "FIX.4.1", "FIX.4.2", "FIX.4.3")), \
            "Side and OrderQty joined the Quote in 4.4"
        assert ("|60=" in wire) == (not legacy) and ("|15=USD|" in wire) == (not legacy)

    def test_cancel_reject_and_response(self):
        f = FixMessageFactory(FixDictionary("FIX.4.4"), "A", "B")
        z = f.quote_cancel("Z1", "AAPL", quote_req_id="Q1", text="gone")
        z.sendprep(f.dictionary, "A", "B", 1)
        assert "|35=Z|" in z.to_pipe_string() and "|131=Q1|117=Z1|298=1|58=gone|295=1|55=AAPL|" in z.to_pipe_string()
        ag = f.quote_request_reject("Q1", "AAPL", "3")
        ag.sendprep(f.dictionary, "A", "B", 2)
        assert "|35=AG|" in ag.to_pipe_string() and "|131=Q1|658=3|146=1|55=AAPL|" in ag.to_pipe_string()
        aj = f.quote_response("R1", "Z1", "1", "AAPL", cl_ord_id="C1", side="1", qty=100, ord_type="D", price=10.7)
        aj.sendprep(f.dictionary, "A", "B", 3)
        assert "|35=AJ|" in aj.to_pipe_string()
        assert "|693=R1|117=Z1|694=1|11=C1|55=AAPL|54=1|38=100|60=" in aj.to_pipe_string()
        assert "|40=D|44=10.7|" in aj.to_pipe_string()


# ── The client side: sent RFQs, received quotes ────────────────────────

class TestClient:
    @pytest.mark.asyncio
    async def test_an_rfq_is_quoted_requoted_and_hit(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        req = await engine.send_rfq("S1", "AAPL", side="1", qty=500, quote_type="1", client="ACME", text="go",
                                    extra_tags="5001=x")
        assert req.startswith("RQ")
        (row,) = await _rows(db)
        assert (row["origin"], row["direction"], row["status"], row["quote_req_id"]) == ("rfq", "TX", "Open", req)
        assert row["side"] == "Buy" and row["order_qty"] == 500 and row["client"] == "ACME"
        assert row["sent_text"] == "go" and row["text"] == "" and row["extra_tags"] == "5001=x"
        wire = stub.sent[-1].to_pipe_string()
        assert f"|131={req}|58=go|453=1|448=ACME|447=D|452=3|146=1|55=AAPL|537=1|54=1|38=500|60=" in wire
        assert wire.endswith(f"|5001=x|10={stub.sent[-1]['10']}")

        await stub.receive(S_44.format(req=req, qid="Z1"))
        (row,) = await _rows(db)
        assert (row["status"], row["quote_id"], row["quote_ref_id"]) == ("Quoted", "Z1", "")
        assert (row["bid_px"], row["offer_px"], row["offer_size"]) == (150.1, 150.3, 400)
        assert row["text"] == "firm" and row["quote_extra_tags"] == "5001=s" and row["quote_type"] == "Tradeable"
        await stub.receive(S_44.format(req=req, qid="Z2").replace("133=150.3", "133=150.2"))
        (row,) = await _rows(db)
        assert (row["quote_id"], row["quote_ref_id"], row["offer_px"]) == ("Z2", "Z1", 150.2)

        cl_ord_id = await engine.hit_quote("S1", "Z2", text="done")
        (row,) = await _rows(db)
        assert (row["status"], row["order_cl_ord_id"], row["quote_resp_type"]) == ("Hit", cl_ord_id, "Hit")
        wire = stub.sent[-1].to_pipe_string()
        assert f"|35=AJ|" in wire and f"|117=Z2|694=1|11={cl_ord_id}|55=AAPL|54=1|38=400|" in wire, \
            "the side asked for, the offer's size"
        assert "|40=D|44=150.2|58=done|" in wire, "PreviouslyQuoted at the offer"
        (order,) = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert (order["cl_ord_id"], order["direction"], order["quote_id"], order["order_qty"], order["price"]) == \
            (cl_ord_id, "TX", "Z2", 400, 150.2)
        assert order["ord_type"] == "PreviouslyQuoted" and order["status"] == "PendingNew"
        assert [v["status"] for v in await _versions(db, row["id"])] == ["Open", "Quoted", "Quoted", "Hit"]

    @pytest.mark.asyncio
    async def test_counter_then_requote_then_pass(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        req = await engine.send_rfq("S1", "AAPL", qty=100)
        await stub.receive(S_44.format(req=req, qid="Z1"))
        resp = await engine.counter_quote("S1", "Z1", bid_px=150.2, offer_px=150.25, extra_tags="5002=c")
        (row,) = await _rows(db)
        assert (row["status"], row["pending_action"], row["pending_resp_id"]) == ("Countered", "Counter", resp)
        assert (row["pending_bid_px"], row["pending_offer_px"], row["pending_extra_tags"]) == (150.2, 150.25, "5002=c")
        assert f"|693={resp}|117=Z1|694=2|55=AAPL|132=150.2|133=150.25|" in stub.sent[-1].to_pipe_string()
        await stub.receive(S_44.format(req=req, qid="Z2"))
        (row,) = await _rows(db)
        assert (row["status"], row["pending_action"], row["pending_bid_px"]) == ("Quoted", "", None), \
            "a requote answers the counter"
        await engine.pass_quote("S1", "Z2", text="no thanks")
        (row,) = await _rows(db)
        assert row["status"] == "Passed" and row["sent_text"] == "no thanks"
        assert "|117=Z2|694=6|" in stub.sent[-1].to_pipe_string()
        with pytest.raises(ValueError, match="Passed"):
            await engine.hit_quote("S1", "Z2", side="1")

    @pytest.mark.asyncio
    async def test_rejected_canceled_and_reported(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        req = await engine.send_rfq("S1", "AAPL")
        await stub.receive(f"8=FIX.4.4|35=AG|131={req}|658=3|146=1|55=AAPL|58=too big")
        (row,) = await _rows(db)
        assert (row["status"], row["rej_reason"], row["rej_reason_code"], row["text"]) == \
            ("Rejected", "QuoteRequestExceedsLimit", "3", "too big")

        other = await engine.send_rfq("S1", "IBM")
        await stub.receive(S_44.format(req=other, qid="Z9").replace("55=AAPL", "55=IBM"))
        await stub.receive("8=FIX.4.4|35=Z|117=*|298=1|295=1|55=IBM|58=pulled")
        row = (await _rows(db))[-1]
        assert (row["status"], row["text"]) == ("Canceled", "pulled"), "by the instrument group"

        third = await engine.send_rfq("S1", "MSFT")
        await stub.receive(S_44.format(req=third, qid="Z10").replace("55=AAPL", "55=MSFT"))
        await stub.receive("8=FIX.4.4|35=AI|117=Z10|297=7|55=MSFT")
        row = (await _rows(db))[-1]
        assert (row["status"], row["quote_status"]) == ("Expired", "Expired")

    @pytest.mark.asyncio
    async def test_a_two_way_quote_needs_a_side_to_hit(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        req = await engine.send_rfq("S1", "AAPL")
        await stub.receive(S_44.format(req=req, qid="Z1"))
        with pytest.raises(ValueError, match="two-way"):
            await engine.hit_quote("S1", "Z1")
        cl_ord_id = await engine.hit_quote("S1", "Z1", side="2", qty=50)
        wire = stub.sent[-1].to_pipe_string()
        assert "|54=2|38=50|60=" in wire and "|40=D|44=150.1|" in wire, "a sell takes the bid"
        assert (await _rows(db))[0]["order_cl_ord_id"] == cl_ord_id

    @pytest.mark.asyncio
    async def test_an_order_naming_the_quote_takes_it(self, stack):
        db, writer, engine = stack
        stub = _stub(engine, "FIX.4.2")
        await stub.receive("8=FIX.4.2|35=S|117=Z1|55=AAPL|132=10|133=11|134=100|135=100")
        (row,) = await _rows(db)
        assert (row["origin"], row["direction"], row["status"], row["quote_req_id"]) == ("quote", "RX", "Quoted", "")
        cl_ord_id = await engine.send_new_order("S1", "AAPL", "1", 100, ord_type="D", price=11, extra_tags="117=Z1")
        (row,) = await _rows(db)
        (order,) = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert (row["status"], row["order_cl_ord_id"], order["quote_id"]) == ("Hit", cl_ord_id, "Z1")
        assert order["extra_tags"] == "117=Z1"

    @pytest.mark.asyncio
    async def test_unsolicited_quotes_are_one_chain_per_instrument(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive("8=FIX.4.4|35=S|117=U1|55=AAPL|54=1|38=100|132=10|133=11|5003=u")
        await stub.receive("8=FIX.4.4|35=S|117=U2|55=AAPL|132=10.1|133=11.1")
        await stub.receive("8=FIX.4.4|35=S|117=U3|55=IBM|132=1|133=2")
        rows = await _rows(db)
        assert [(r["symbol"], r["quote_id"], r["quote_ref_id"]) for r in rows] == [("AAPL", "U2", "U1"), ("IBM", "U3", "")]
        assert rows[0]["side"] == "Buy" and rows[0]["order_qty"] == 100 and rows[0]["quote_extra_tags"] == ""
        assert len(await _versions(db, rows[0]["id"])) == 2
        await engine.pass_quote("S1", "U2")
        await stub.receive("8=FIX.4.4|35=S|117=U4|55=AAPL|132=9|133=10")
        assert [r["quote_id"] for r in await _rows(db)] == ["U2", "U3", "U4"], "a passed quote ends its chain"

    @pytest.mark.asyncio
    async def test_refusals_by_version(self, stack):
        db, writer, engine = stack
        stub = _stub(engine, "FIX.4.2")
        req = await engine.send_rfq("S1", "AAPL", side="1", qty=100)
        await stub.receive(f"8=FIX.4.2|35=S|131={req}|117=Z1|55=AAPL|132=1|133=2")
        for action in (engine.hit_quote("S1", "Z1"), engine.pass_quote("S1", "Z1"),
                       engine.counter_quote("S1", "Z1", bid_px=1)):
            with pytest.raises(ValueError, match="QuoteResponse .35=AJ. is not a FIX.4.2 message"):
                await action
        with pytest.raises(ValueError, match="Unknown quote"):
            await engine.pass_quote("S1", "NOPE")
        stub.is_active = False
        with pytest.raises(ValueError, match="not active"):
            await engine.send_rfq("S1", "AAPL")


# ── The market side: received RFQs, sent quotes ────────────────────────

class TestMarket:
    @pytest.mark.asyncio
    async def test_an_rfq_is_quoted_countered_requoted_and_hit(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive(R_42.replace("FIX.4.2", "FIX.4.4"))
        (row,) = await _rows(db)
        assert (row["origin"], row["direction"], row["status"], row["symbol"]) == ("rfq", "RX", "Open", "AAPL")
        assert row["extra_tags"] == "5001=r" and row["text"] == "please"

        qid = await engine.quote_rfq("S1", "Q1", bid_px=150.1, offer_px=150.3, bid_size=500, offer_size=500,
                                     valid_for=30, text="firm", extra_tags="5004=q")
        (row,) = await _rows(db)
        assert (row["status"], row["quote_id"], row["offer_px"], row["sent_text"]) == ("Quoted", qid, 150.3, "firm")
        wire = stub.sent[-1].to_pipe_string()
        assert f"|131=Q1|117={qid}|55=AAPL|54=1|38=500|132=150.1|133=150.3|134=500|135=500|62=" in wire
        assert row["valid_until"] > row["timestamp"] and row["quote_extra_tags"] == "5004=q"

        await stub.receive(f"8=FIX.4.4|35=AJ|693=CR1|117={qid}|694=2|55=AAPL|133=150.2|5005=c")
        (row,) = await _rows(db)
        assert (row["status"], row["pending_action"], row["pending_resp_id"], row["pending_offer_px"]) == \
            ("Countered", "Counter", "CR1", 150.2)
        assert row["pending_extra_tags"] == "5005=c" and row["quote_resp_type"] == "Counter"

        second = await engine.quote_rfq("S1", "Q1", bid_px=150.1, offer_px=150.25)
        (row,) = await _rows(db)
        assert (row["quote_id"], row["quote_ref_id"], row["status"], row["pending_action"]) == \
            (second, qid, "Quoted", "")

        await stub.receive(f"8=FIX.4.4|35=AJ|693=CR2|117={second}|694=1|11=C9|55=AAPL|54=1|38=300|58=yes")
        (row,) = await _rows(db)
        assert (row["status"], row["order_cl_ord_id"], row["text"]) == ("Hit", "C9", "yes")
        (order,) = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert (order["direction"], order["cl_ord_id"], order["quote_id"], order["order_qty"]) == ("RX", "C9", second, 300)
        assert (order["price"], order["ord_type_code"], order["pending_action"]) == (150.25, "D", "New"), \
            "the offer taken, as a received order awaiting Accept"
        assert order["extra_tags"] == ""
        with pytest.raises(ValueError, match="Hit"):
            await engine.quote_rfq("S1", "Q1", bid_px=1)

    @pytest.mark.asyncio
    async def test_reject_and_cancel(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive(R_42.replace("FIX.4.2", "FIX.4.4"))
        qid = await engine.quote_rfq("S1", "Q1", offer_px=10)
        await engine.cancel_quote("S1", qid, text="pulled")
        (row,) = await _rows(db)
        assert (row["status"], row["sent_text"]) == ("Canceled", "pulled")
        assert f"|35=Z|" in stub.sent[-1].to_pipe_string() and f"|131=Q1|117={qid}|298=1|" in stub.sent[-1].to_pipe_string()
        with pytest.raises(ValueError, match="nothing to cancel"):
            await engine.cancel_quote("S1", qid)
        await engine.quote_rfq("S1", "Q1", offer_px=11)
        assert (await _rows(db))[0]["status"] == "Quoted", "a canceled quote's RFQ can be quoted again"
        with pytest.raises(ValueError, match="names its reason"):
            await engine.reject_rfq("S1", "Q1")
        await engine.reject_rfq("S1", "Q1", reason="4", text="late")
        (row,) = await _rows(db)
        assert (row["status"], row["rej_reason"], row["sent_text"]) == ("Rejected", "TooLateToEnter", "late")
        assert "|35=AG|" in stub.sent[-1].to_pipe_string() and "|131=Q1|658=4|58=late|146=1|55=AAPL|" in \
            stub.sent[-1].to_pipe_string()

    @pytest.mark.asyncio
    async def test_unsolicited_quotes_stream_on_one_row(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        first = await engine.send_quote("S1", "AAPL", bid_px=10, offer_px=11, bid_size=100, offer_size=100,
                                        side="1", qty=100, client="ACME", extra_tags="5006=a")
        second = await engine.send_quote("S1", "AAPL", bid_px=10.1, offer_px=11.1)
        other = await engine.send_quote("S1", "IBM", offer_px=2)
        rows = await _rows(db)
        assert [(r["origin"], r["direction"], r["symbol"], r["quote_id"], r["quote_ref_id"]) for r in rows] == [
            ("quote", "TX", "AAPL", second, first), ("quote", "TX", "IBM", other, "")]
        assert rows[0]["client"] == "ACME", "the stream keeps its client"
        assert "|131=" not in stub.sent[0].to_pipe_string() and "|448=ACME|447=D|452=3|5006=a|" in stub.sent[0].to_pipe_string()
        third = await engine.requote("S1", second, bid_px=10.2, offer_px=11.2)
        assert (await _rows(db))[0]["quote_id"] == third
        await stub.receive(f"8=FIX.4.4|35=AJ|693=P1|117={third}|694=6|55=AAPL")
        assert (await _rows(db))[0]["status"] == "Passed"

    @pytest.mark.asyncio
    async def test_an_order_naming_our_quote_takes_it(self, stack):
        db, writer, engine = stack
        stub = _stub(engine, "FIX.4.2")
        qid = await engine.send_quote("S1", "AAPL", bid_px=10, offer_px=11)
        await stub.receive(f"8=FIX.4.2|35=D|11=C1|55=AAPL|54=1|38=100|40=D|44=11|59=0|117={qid}")
        (row,) = await _rows(db)
        (order,) = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert (row["status"], row["order_cl_ord_id"], order["quote_id"], order["extra_tags"]) == ("Hit", "C1", qid, "")

    @pytest.mark.asyncio
    async def test_refusals_by_version(self, stack):
        db, writer, engine = stack
        stub = _stub(engine, "FIX.4.1")
        await stub.receive("8=FIX.4.1|35=R|131=Q1|55=AAPL")
        qid = await engine.quote_rfq("S1", "Q1", bid_px=1, offer_px=2)
        assert "|35=S|" in stub.sent[-1].to_pipe_string()
        with pytest.raises(ValueError, match="QuoteCancel .35=Z. is not a FIX.4.1 message"):
            await engine.cancel_quote("S1", qid)
        with pytest.raises(ValueError, match="QuoteRequestReject .35=AG. is not a FIX.4.1 message"):
            await engine.reject_rfq("S1", "Q1", reason="1")
        with pytest.raises(ValueError, match="bid, an offer"):
            await engine.quote_rfq("S1", "Q1")
        with pytest.raises(ValueError, match="Unknown RFQ"):
            await engine.quote_rfq("S1", "NOPE", bid_px=1)

    @pytest.mark.asyncio
    async def test_a_response_naming_nothing_is_only_a_message(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        seen = []
        engine.events.subscribe(seen.append)
        await stub.receive("8=FIX.4.4|35=AJ|693=X|117=NOPE|694=1|11=C1|55=AAPL|54=1|38=1")
        assert await _rows(db) == [] and await _fetch_all(db, "SELECT * FROM fix_orders") == []
        assert seen[-1].kinds == ("message",) and seen[-1].detail["unknown_quote"] == "NOPE"


# ── Across the engine ──────────────────────────────────────────────────

class TestAcrossTheEngine:
    @pytest.mark.asyncio
    async def test_both_ends_of_a_negotiation(self, linked):
        """The client asks, the market quotes, the client counters, the
        market requotes, the client hits: each end's rows follow the other's
        messages, and the hit is an order on both."""
        db, engine, cli, mkt = linked
        req = await engine.send_rfq("Client", "AAPL", side="1", qty=100)
        market_row = (await _rows(db, "WHERE session_id = 'Server'"))[0]
        assert (market_row["direction"], market_row["status"], market_row["quote_req_id"]) == ("RX", "Open", req)
        qid = await engine.quote_rfq("Server", req, bid_px=10, offer_px=11, offer_size=100)
        await engine.counter_quote("Client", qid, offer_px=10.8)
        assert (await _rows(db, "WHERE session_id = 'Server'"))[0]["status"] == "Countered"
        second = await engine.requote("Server", qid, offer_px=10.9, bid_px=10)
        client_row = (await _rows(db, "WHERE session_id = 'Client'"))[0]
        assert (client_row["quote_id"], client_row["status"], client_row["pending_action"]) == (second, "Quoted", "")
        cl_ord_id = await engine.hit_quote("Client", second)
        rows = {r["session_id"]: r for r in await _rows(db)}
        assert rows["Client"]["status"] == rows["Server"]["status"] == "Hit"
        assert rows["Client"]["order_cl_ord_id"] == rows["Server"]["order_cl_ord_id"] == cl_ord_id
        orders = {o["session_id"]: o for o in await _fetch_all(db, "SELECT * FROM fix_orders")}
        assert orders["Client"]["direction"] == "TX" and orders["Server"]["direction"] == "RX"
        assert orders["Server"]["price"] == 10.9 and orders["Server"]["order_qty"] == 100
        await engine.accept_order("Server", cl_ord_id)
        orders = {o["session_id"]: o for o in await _fetch_all(db, "SELECT * FROM fix_orders")}
        assert orders["Client"]["status"] == "New", "and on it goes as an order"

    @pytest.mark.asyncio
    async def test_every_action_writes_before_it_sends(self, stack):
        db, writer, engine = stack
        trace: list[str] = []
        stub = _stub(engine)
        real_send, real_submit = stub.send_message, writer.submit

        async def send(msg):
            trace.append("sent")
            return await real_send(msg)

        async def spy(ops, params_list, data, *a, **kw):
            trace.append("wrote")
            return await real_submit(ops, params_list, data, *a, **kw)

        stub.send_message = send

        async def traced(label, coro):
            trace.clear()
            writer.submit = spy
            try:
                result = await coro
            finally:
                writer.submit = real_submit
            sends = [i for i, t in enumerate(trace) if t == "sent"]
            assert len(sends) == 1 and "wrote" in trace[:sends[0]], f"{label}: {trace}"
            return result

        req = await traced("send_rfq", engine.send_rfq("S1", "AAPL", side="1", qty=10))
        await stub.receive(S_44.format(req=req, qid="Z1"))
        await traced("counter_quote", engine.counter_quote("S1", "Z1", bid_px=1))
        await stub.receive(S_44.format(req=req, qid="Z2"))
        await traced("hit_quote", engine.hit_quote("S1", "Z2"))
        req = await engine.send_rfq("S1", "IBM")
        await stub.receive(S_44.format(req=req, qid="Z3").replace("55=AAPL", "55=IBM"))
        await traced("pass_quote", engine.pass_quote("S1", "Z3"))
        await stub.receive(R_42.replace("FIX.4.2", "FIX.4.4"))
        qid = await traced("quote_rfq", engine.quote_rfq("S1", "Q1", bid_px=1, offer_px=2))
        qid = await traced("requote", engine.requote("S1", qid, bid_px=1, offer_px=3))
        await traced("cancel_quote", engine.cancel_quote("S1", qid))
        await traced("reject_rfq", engine.reject_rfq("S1", "Q1", reason="1"))
        await traced("send_quote", engine.send_quote("S1", "MSFT", bid_px=1))

    @pytest.mark.asyncio
    async def test_a_failed_send_is_undone_or_recorded(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive(R_42.replace("FIX.4.2", "FIX.4.4"))

        async def boom(msg):
            raise ConnectionError("gone")
        real = stub.send_message
        stub.send_message = boom
        with pytest.raises(ConnectionError):
            await engine.send_rfq("S1", "AAPL")
        failed = (await _rows(db))[-1]
        assert failed["status"] == "Failed" and failed["text"].startswith("Send failed")
        with pytest.raises(ConnectionError):
            await engine.send_quote("S1", "IBM", bid_px=1)
        assert (await _rows(db))[-1]["status"] == "Failed"
        stub.send_message = real
        req = await engine.send_rfq("S1", "MSFT", side="1", qty=5)
        await stub.receive(S_44.format(req=req, qid="Z1").replace("55=AAPL", "55=MSFT"))
        stub.send_message = boom
        with pytest.raises(ConnectionError):
            await engine.hit_quote("S1", "Z1")
        row = (await _rows(db))[-1]
        assert row["status"] == "Quoted", "a hit that never went out is undone"
        (order,) = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert order["status"] == "Rejected" and order["text"].startswith("Send failed")

    @pytest.mark.asyncio
    async def test_perform_routes_the_ops_and_announces_rows(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        seen = []
        engine.events.subscribe(seen.append)
        result = await engine.perform("send_rfq", {"session_id": "S1", "symbol": "AAPL", "side": "1", "qty": "100"})
        req = result["quote_req_id"]
        assert [e.kinds for e in seen] == [("sent rfq",), ("action",)]
        assert seen[-1].table == "fix_rfqs" and seen[-1].row["quote_req_id"] == req
        await stub.receive(S_44.format(req=req, qid="Z1"))
        assert seen[-1].kinds == ("rfq quoted", "message") and seen[-1].row["quote_id"] == "Z1"
        await stub.receive(S_44.format(req=req, qid="Z2"))
        assert seen[-1].kinds == ("rfq requoted", "message")
        result = await engine.perform("hit_quote", {"session_id": "S1", "quote_id": "Z2", "qty": "", "price": ""})
        assert [e.kinds for e in seen[-2:]] == [("sent order",), ("action",)]
        assert seen[-1].row["status"] == "Hit" and seen[-1].detail["prev_row"]["status"] == "Quoted"
        await stub.receive(R_42.replace("FIX.4.2", "FIX.4.4"))
        assert seen[-1].kinds == ("rfq", "message")
        result = await engine.perform("quote_rfq", {"session_id": "S1", "quote_req_id": "Q1", "bid_px": "1",
                                                    "offer_px": "", "valid_for": ""})
        assert seen[-1].detail["op"] == "quote_rfq" and seen[-1].row["quote_id"] == result["quote_id"]
        await stub.receive(f"8=FIX.4.4|35=AJ|693=C|117={result['quote_id']}|694=2|55=AAPL|132=0.9")
        assert seen[-1].kinds == ("rfq countered", "message")
        await engine.perform("send_quote", {"session_id": "S1", "symbol": "IBM", "offer_px": "3"})
        assert [e.kinds for e in seen[-2:]] == [("sent quote",), ("action",)]
        assert seen[-1].row["origin"] == "quote"

    @pytest.mark.asyncio
    async def test_quotes_expire_on_both_sides(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        seen = []
        engine.events.subscribe(seen.append)
        req = await engine.send_rfq("S1", "AAPL")
        await stub.receive(S_44.format(req=req, qid="Z1").replace("20990101", "20260927"))
        await stub.receive(R_42.replace("FIX.4.2", "FIX.4.4"))
        await engine.quote_rfq("S1", "Q1", bid_px=1, valid_for=60)
        await engine.send_quote("S1", "IBM", bid_px=1)
        later = datetime.now(timezone.utc) + timedelta(seconds=30)
        following = await engine.expire_due_quotes(later)
        rows = await _rows(db)
        assert [r["status"] for r in rows] == ["Expired", "Quoted", "Quoted"], "the stamp that passed only"
        assert following is not None and following > later
        assert seen[-1].kinds == ("rfq expired",) and seen[-1].source == "engine"
        await engine.expire_due_quotes(later + timedelta(minutes=5))
        assert [r["status"] for r in await _rows(db)] == ["Expired", "Expired", "Quoted"], "no stamp, no expiry"

    @pytest.mark.asyncio
    async def test_the_timer_runs_with_the_engine(self, stack):
        db, writer, engine = stack
        await engine.start()
        try:
            stub = _stub(engine)
            await stub.receive(R_42.replace("FIX.4.2", "FIX.4.4"))
            soon = (datetime.now(timezone.utc) + timedelta(milliseconds=200)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            await engine.quote_rfq("S1", "Q1", bid_px=1, valid_until=soon)
            import asyncio
            for _ in range(50):
                await asyncio.sleep(0.05)
                if (await _rows(db))[0]["status"] == "Expired":
                    break
            assert (await _rows(db))[0]["status"] == "Expired"
        finally:
            engine.sessions.clear()
            await engine.stop()

    @pytest.mark.asyncio
    async def test_templates_for_the_new_scopes(self, stack):
        db, writer, engine = stack
        await engine.save_template("quote", "tight", bid_px="", offer_px="", valid_for="30", quote_type="1")
        await engine.save_template("quote_reject", "no inventory", quote_rej_reason="9")
        rows = await _fetch_all(db, "SELECT scope, name, valid_for, quote_type, quote_rej_reason FROM fix_templates "
                                    "ORDER BY scope")
        assert rows == [
            {"scope": "quote", "name": "tight", "valid_for": "30", "quote_type": "1", "quote_rej_reason": ""},
            {"scope": "quote_reject", "name": "no inventory", "valid_for": "", "quote_type": "",
             "quote_rej_reason": "9"},
        ]
