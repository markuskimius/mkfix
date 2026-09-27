"""Macros about IOIs, adverts and allocations (0.64): the market side sends
them from `run` blocks and minds hand-sent ones with `on sent …`, the client
side receives them in `on ioi`/`on advert`/`on allocation` blocks. Run over
the linked pair of tests/test_macro_sending.py, so a script that sends faces
a script that answers; recorded by hand through tests/test_macro_recorder's
Hand."""

import pytest

from mkfix import macro
from mkfix.macro.instance import COMPLETED, LISTENING, PASSED, STOPPED, FAILED
from mkfix.macro.runner import MacroRunner

from tests.test_engine import _fetch_all, stack  # noqa: F401
from tests.test_macro_recorder import Hand, body, hand  # noqa: F401
from tests.test_macro_sending import EXAMPLES, Pair, pair  # noqa: F401


async def rows(pair, table, direction=None):
    where = f" WHERE direction = '{direction}'" if direction else ""
    return await _fetch_all(pair.db, f"SELECT * FROM {table}{where} ORDER BY id")


class TestIois:
    @pytest.mark.asyncio
    async def test_the_desk_and_the_taker(self, pair):
        """ioi-desk sends three IOIs from LOOP-MKT and an advert; ioi-taker on
        the other end orders against each large IBM/MSFT indication, naming
        it in tag 23, and hears the replace and the cancel that follow."""
        taker = pair.arm("ioi-taker")
        desk = pair.arm("ioi-desk", session="LOOP-MKT")
        await pair.advance(8)
        sent, received = await rows(pair, "fix_iois", "TX"), await rows(pair, "fix_iois", "RX")
        assert [(i["symbol"], i["status"], i["ioi_trans_type"]) for i in sent] == [
            ("IBM", "Canceled", "Cancel"), ("MSFT", "Canceled", "Cancel"), ("AAPL", "Canceled", "Cancel")]
        assert [(i["symbol"], i["status"]) for i in received] == [(i["symbol"], i["status"]) for i in sent]
        assert all(i["ioi_qty"] == "M" and i["qlty_ind"] == "High" and i["natural_flag"] == "Y" for i in sent)
        orders = await rows(pair, "fix_orders", "TX")
        assert [(o["symbol"], o["order_qty"], o["ioi_id"] != "") for o in orders] == [("IBM", 100.0, True), ("MSFT", 100.0, True)]
        assert [o["ioi_id"] for o in orders] == ["IOMA00000001", "IOMA00000002"], "the IOIs as first sent"
        assert all(o["macro"] == "" for o in orders), "an order sent from an `on ioi` block is nobody's macro's"
        assert [i["order_cl_ord_id"] != "" for i in received] == [True, True, False], "AAPL was not taken"
        adverts = await rows(pair, "fix_adverts")
        assert [(a["direction"], a["status"], a["quantity"]) for a in adverts] == [("TX", "Canceled", 6000.0), ("RX", "Canceled", 6000.0)]
        # the advert block, one macro among its own lines, starts at arm; the IOIs' passes follow
        assert [(i.kind, i.status) for i in desk.instances] == [("advert", PASSED)] + [("ioi", PASSED)] * 3
        assert desk.instances[1].message.startswith("IOI chain ended at IO")
        assert sorted((i.kind, i.status) for i in taker.instances) == [("advert", COMPLETED), ("ioi", COMPLETED), ("ioi", COMPLETED)]
        texts = [line[3] for line in taker.log]
        assert any(t.startswith("ordered against IO") for t in texts)
        assert sum("now M at" in t for t in texts) == 2, "each replace came before its cancel: the wait took the replace"
        assert any(t.startswith("advert: Trade 5000 IBM at 20.5") for t in texts), "logged as it arrived, before its replace"
        assert desk.status == "armed", "its `on sent ioi` block waits for IOIs sent by hand"
        assert taker.status == "armed"

    @pytest.mark.asyncio
    async def test_a_hand_sent_ioi_is_minded_and_the_macros_own_is_not_offered(self, pair):
        run = pair.arm("on sent ioi where symbol == 'IBM'\n    after 1s\n    cancel ioi text: 'stale'\n",
                       session="LOOP-MKT")
        own = pair.arm("run on LOOP-MKT\n    ioi symbol: 'IBM', side: buy, qty: 'S'\n    after 5s\n    pass\n")
        await pair.engine.perform("send_ioi", {"session_id": "LOOP-MKT", "symbol": "IBM", "side": "1", "qty": "L"})
        await pair.engine.perform("send_ioi", {"session_id": "LOOP-MKT", "symbol": "MSFT", "side": "1", "qty": "L"})
        await pair.runner.settle()
        assert [(i.kind, i.subject_id[:2]) for i in run.instances] == [("ioi", "IO")]
        assert len(own.instances) == 1 and run.instances[0].key != own.instances[0].key
        await pair.advance(1.5)
        sent = await rows(pair, "fix_iois", "TX")
        assert sorted((i["symbol"], i["ioi_qty"], i["status"], i["text"]) for i in sent) == [
            ("IBM", "L", "Canceled", "stale"), ("IBM", "S", "Active", ""), ("MSFT", "L", "Active", "")]
        assert run.instances[0].status == COMPLETED

    @pytest.mark.asyncio
    async def test_replace_ioi_keeps_the_values_it_does_not_name(self, pair):
        run = pair.arm("run on LOOP-MKT\n"
                       "    ioi symbol: 'IBM', side: cross, qty: 'L', price: 10.5, quality: low, currency: 'USD'\n"
                       "    replace ioi price: 11\n"
                       "    pass '${ioi.ioi_qty} ${ioi.ioi_ref_id != \"\"}'\n")
        await pair.runner.settle()
        (ioi,) = await rows(pair, "fix_iois", "TX")
        assert (ioi["side"], ioi["ioi_qty"], ioi["price"], ioi["qlty_ind_code"], ioi["currency"]) == ("Cross", "L", 11.0, "L", "USD")
        assert ioi["ioi_ref_id"] and ioi["ioi_id"] != ioi["ioi_ref_id"]
        assert [(i.status, i.message) for i in run.instances] == [(PASSED, "L true")]

    @pytest.mark.asyncio
    async def test_new_from_an_on_ioi_block_names_the_ioi_and_may_be_taken_up(self, pair):
        pair.arm("on order\n    accept\n", session="LOOP-MKT")
        minder = pair.arm("on sent order\n    expect ack within 1s\n    pass 'minded ${order.ioi_id}'\n")
        taker = pair.arm("on ioi\n    new symbol: ioi.symbol, side: buy, qty: 5, extra: '9001=x'\n"
                         "    new symbol: ioi.symbol, side: sell, qty: 5, extra: '23=CUSTOM'\n")
        await pair.engine.perform("send_ioi", {"session_id": "LOOP-MKT", "symbol": "IBM", "side": "1", "qty": "L"})
        await pair.runner.settle()
        orders = await rows(pair, "fix_orders", "TX")
        (ioi,) = await rows(pair, "fix_iois", "RX")
        assert [(o["ioi_id"], o["extra_tags"]) for o in orders] == [
            (ioi["ioi_id"], f"23={ioi['ioi_id']}|9001=x"), ("CUSTOM", "23=CUSTOM")]
        assert [(i.kind, i.status) for i in taker.instances] == [("ioi", COMPLETED)]
        assert [(i.kind, i.status, i.message) for i in minder.instances] == [
            ("order", PASSED, f"minded {ioi['ioi_id']}"), ("order", PASSED, "minded CUSTOM")]


class TestAllocations:
    @pytest.mark.asyncio
    async def test_the_desk_and_the_check(self, pair):
        check = pair.arm("allocation-check")
        desk = pair.arm("allocation-desk", session="LOOP-MKT")
        await pair.advance(12)
        sent = await rows(pair, "fix_allocations", "TX")
        assert [(a["status"], a["allocs"], a["alloc_rej_reason"]) for a in sent] == [
            ("Accepted", "ACC1 300; ACC2 200", ""), ("Accepted", "ACC1 300; ACC2 200", "")]
        assert sent[1]["ref_alloc_id"] and sent[1]["alloc_trans_type"] == "Replace", "the refused one was replaced"
        received = await rows(pair, "fix_allocations", "RX")
        assert [(a["status"], a["pending_action"], a["sent_text"]) for a in received] == [
            ("Accepted", "", "booked"), ("Accepted", "", "")]
        assert [(i.kind, i.status) for i in desk.instances] == [("allocation", PASSED)] * 2
        assert desk.instances[1].message == "settled as ACC1 300; ACC2 200"
        assert any("refused: IncorrectQuantity" in line[3] for line in desk.log)
        assert [(i.kind, i.status) for i in check.instances] == [("allocation", LISTENING)] * 2
        assert desk.status == "armed", "its `on sent allocation` block waits"

    @pytest.mark.asyncio
    async def test_a_cancel_after_acceptance_and_a_hand_sent_one(self, pair):
        pair.arm("allocation-check")
        minder = pair.arm("on sent allocation\n    wait acked or timeout 1s\n"
                          "    pass '${allocation.status} after ${event.kind}'\n", session="LOOP-MKT")
        result = await pair.engine.perform("send_allocation", {
            "session_id": "LOOP-MKT", "symbol": "IBM", "side": "1", "qty": "100", "avg_price": "1", "allocs": "A 100"})
        await pair.runner.settle()
        assert [(i.status, i.message) for i in minder.instances] == [(PASSED, "Accepted after accepted")]
        await pair.engine.perform("cancel_allocation", {"session_id": "LOOP-MKT", "alloc_id": result["alloc_id"]})
        await pair.runner.settle()
        (sent,) = await rows(pair, "fix_allocations", "TX")
        (received,) = await rows(pair, "fix_allocations", "RX")
        assert sent["status"] == received["status"] == "Canceled" and "canceled after" in received["sent_text"]

    @pytest.mark.asyncio
    async def test_a_refused_ack_is_rejected_and_the_status_words_are_codes(self, pair):
        pair.arm("on allocation\n    reject allocation status: block_level_reject, reason: other, text: 'no'\n")
        run = pair.arm("run on LOOP-MKT\n    allocate symbol: 'IBM', side: buy, qty: 10, accounts: 'A 10'\n"
                       "    expect rejected within 1s\n    fail 'refused: ${event.tag[\"88\"]} ${allocation.alloc_status}'\n")
        await pair.runner.settle()
        assert [(i.status, i.message) for i in run.instances] == [(FAILED, "refused: 7 BlockLevelReject")]
        (sent,) = await rows(pair, "fix_allocations", "TX")
        assert (sent["alloc_status_code"], sent["alloc_rej_code"], sent["text"]) == ("1", "7", "no")


class TestTheRunTables:
    @pytest.mark.asyncio
    async def test_rows_carry_the_subject_and_the_tables_carry_the_macro(self, stack):
        from mkfix.macro.store import MacroManager
        db, writer, engine = stack
        pair = Pair(db, engine)
        manager = MacroManager(engine, pair.runner)
        engine.macros = manager
        await manager.start()
        await manager.save("desk", "run\n    ioi symbol: 'IBM', side: buy, qty: 'L'\n    after 1s\n    pass 'sent'\n",
                           side="market")
        await manager.save("watch", "on ioi\n    log ioi.ioi_id\n    wait canceled or timeout 1m\n", side="client")
        await manager.arm("watch", side="client")
        run_id = (await manager.arm("desk", side="market", session="LOOP-MKT"))["run_id"]
        await manager.flush()
        rows_ = await _fetch_all(db, "SELECT run_id, subject, cl_ord_id, order_id, symbol, status FROM fix_macro_orders ORDER BY id")
        ioi = (await _fetch_all(db, "SELECT * FROM fix_iois WHERE direction = 'TX'"))[0]
        assert [(r["subject"], r["cl_ord_id"], r["order_id"], r["symbol"]) for r in rows_] == [
            ("ioi", ioi["ioi_id"], "", "IBM"), ("ioi", ioi["ioi_id"], "", "IBM")]
        assert ioi["macro"] == f"desk #{run_id}"
        received = (await _fetch_all(db, "SELECT * FROM fix_iois WHERE direction = 'RX'"))[0]
        assert received["macro"].startswith("watch #")
        log = await _fetch_all(db, "SELECT cl_ord_id, text FROM fix_macro_log")
        assert (log[0]["cl_ord_id"], log[0]["text"]) == (ioi["ioi_id"], ioi["ioi_id"])
        # Detach names the subject: the same row id in another table is another thing.
        with pytest.raises(ValueError, match="No live macro owns that ioi"):
            await manager.detach(received["id"] + 100, "ioi")
        with pytest.raises(ValueError, match="No live macro owns that order"):
            await manager.detach(received["id"])
        await manager.detach(received["id"], "ioi")
        await manager.flush()
        detached = await _fetch_all(db, f"SELECT subject, status FROM fix_macro_orders WHERE order_row = {received['id']} "
                                        "AND subject = 'ioi'")
        assert [(r["subject"], r["status"]) for r in detached if r["status"] == "detached"] == [("ioi", "detached")]
        await manager.stop()


class TestRecording:
    @pytest.mark.asyncio
    async def test_a_hand_sent_ioi_and_allocation_become_run_blocks(self, hand):
        recorder = hand.recorder("market")
        ioi = (await hand.do("send_ioi", session_id="LOOP-MKT", symbol="IBM", side="1", qty="L", price=10.5,
                             qlty_ind="H", qualifiers="A,X"))["ioi_id"]
        new = (await hand.do("replace_ioi", 2, session_id="LOOP-MKT", ioi_id=ioi, symbol="IBM", side="1", qty="M"))["ioi_id"]
        await hand.do("cancel_ioi", 1, session_id="LOOP-MKT", ioi_id=new, text="done")
        alloc = (await hand.do("send_allocation", 1, session_id="LOOP-MKT", symbol="IBM", side="1", qty=100, avg_price=10,
                               allocs="A 60; B 40", orders="C1 O1"))["alloc_id"]
        await hand.do("accept_allocation", 0.5, session_id="LOOP-CLI", alloc_id=alloc, alloc_status="0")
        source = (await recorder.stop("desk"))["source"]
        assert body(source) == [
            "run",
            "    ioi symbol: 'IBM', side: buy, qty: 'L', price: 10.5, quality: high, qualifiers: 'A,X'",
            "    after 2s",
            "    replace ioi symbol: 'IBM', side: buy, qty: 'M'",
            "    after 1s",
            "    cancel ioi text: 'done'",
            "run",
            # no trade date was given, so none is written: the day's own would be sent for ever
            "    allocate symbol: 'IBM', side: buy, qty: 100, avg_price: 10, orders: 'C1 O1', accounts: 'A 60; B 40'",
            "    expect accepted within 5s",
            "    pass"]
        sc, diags = macro.check(source, side="market")
        assert diags == [] and sc.needs_session

    @pytest.mark.asyncio
    async def test_a_trade_date_that_was_given_is_written(self, hand):
        recorder = hand.recorder("market")
        await hand.do("send_allocation", session_id="LOOP-MKT", symbol="IBM", side="1", qty=100, avg_price=10,
                      allocs="A 100", trade_date="20260102")
        source = (await recorder.stop("desk"))["source"]
        assert body(source)[1] == ("    allocate symbol: 'IBM', side: buy, qty: 100, avg_price: 10, trade_date: '20260102', "
                                   "accounts: 'A 100'")

    @pytest.mark.asyncio
    async def test_received_ones_become_on_blocks_with_the_answers_given(self, hand):
        recorder = hand.recorder("client")
        alloc = (await hand.do("send_allocation", session_id="LOOP-MKT", symbol="IBM", side="1", qty=100, avg_price=10,
                               allocs="A 100"))["alloc_id"]
        await hand.do("reject_allocation", 0.5, session_id="LOOP-CLI", alloc_id=alloc, alloc_status="2",
                      alloc_rej_code="1", text="short")
        await hand.do("send_ioi", 1, session_id="LOOP-MKT", symbol="MSFT", side="2", qty="S")
        ioi = (await hand.do("send_ioi", 1, session_id="LOOP-MKT", symbol="IBM", side="1", qty="L"))["ioi_id"]
        await hand.do("cancel_ioi", 1, session_id="LOOP-MKT", ioi_id=ioi)
        source = (await recorder.stop("check"))["source"]
        assert body(source) == [
            "on allocation where symbol == 'IBM'",
            "    reject allocation status: account_level_reject, reason: incorrect_quantity, text: 'short'"]
        assert "1 IOI" not in source and "2 IOIs" not in source, "an IOI nothing was done to is no block"
        assert macro.check(source, side="client")[1] == []

    @pytest.mark.asyncio
    async def test_the_two_recordings_run_against_each_other(self, hand):
        market, client = hand.recorder("market"), hand.recorder("client")
        alloc = (await hand.do("send_allocation", session_id="LOOP-MKT", symbol="IBM", side="1", qty=100, avg_price=10,
                               allocs="A 100"))["alloc_id"]
        await hand.do("accept_allocation", 0.5, session_id="LOOP-CLI", alloc_id=alloc, alloc_status="0", text="ok")
        desk, check = (await market.stop("desk"))["source"], (await client.stop("check"))["source"]
        pair = hand.pair
        pair.arm(check)
        run = pair.arm(desk, session="LOOP-MKT")
        await pair.advance(6)
        assert [(i.kind, i.status) for i in run.instances] == [("allocation", PASSED)]
        sent = (await rows(pair, "fix_allocations", "TX"))[-1]
        assert (sent["status"], sent["text"]) == ("Accepted", "ok")
