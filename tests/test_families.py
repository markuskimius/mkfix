"""IOIs, adverts and allocations: the chains the engine keeps on
fix_iois/fix_adverts/fix_allocations, the messages it sends and answers, and
the pure part in mkfix/fix/families.py."""

import json

import pytest

from mkfix.fix.dictionary import FixDictionary, STANDARD_VERSIONS
from mkfix.fix.families import (ALLOC_GROUPS, allocation_columns, format_lines, group_instances,
                                group_pairs, ioi_columns, parse_lines)
from mkfix.fix.message import FixMessageFactory, parse_fix

from tests.test_engine import RecordingStub, StubSession, _fetch_all, stack  # noqa: F401

IOI_RX = "8=FIX.4.2|35=6|23=I1|28=N|55=AAPL|54=1|27=L|44=150.5|62=20260926-20:00:00.000|25=H|130=Y|199=2|104=A|104=X|58=hi|5001=x"
ADV_RX = "8=FIX.4.2|35=7|2=D1|5=N|55=AAPL|4=B|53=500|44=150.5|15=USD|75=20260926|30=XNAS|58=adv|5001=y"
ALLOC_RX = ("8=FIX.4.2|35=J|70=A1|71=0|73=1|11=C1|37=O1|124=1|17=E1|32=100|31=150.5|55=AAPL|54=1|53=100|6=150.5"
            "|75=20260926|78=2|79=ACC1|80=60|366=150.5|79=ACC2|80=40|58=please|5001=z")


async def _rows(db, table, direction=None):
    where = f" WHERE direction = '{direction}'" if direction else ""
    return await _fetch_all(db, f"SELECT * FROM {table}{where} ORDER BY id")


async def _versions(db, table, row_id):
    return await _fetch_all(db, f"SELECT * FROM {table}__history WHERE id = {row_id} ORDER BY _mkio_version")


def _stub(engine, version="FIX.4.2"):
    stub = RecordingStub(engine)
    stub.dictionary = FixDictionary(version)
    stub.factory = FixMessageFactory(stub.dictionary, "MKT", "CLIENT")
    engine.sessions["S1"] = stub
    return stub


# ── The pure part ──────────────────────────────────────────────────────

class TestFamilies:
    def test_groups_are_read_by_their_members_and_written_back(self):
        d = FixDictionary("FIX.4.2")
        msg = parse_fix(ALLOC_RX)
        counter, members = ALLOC_GROUPS["allocs"]
        instances = group_instances(msg, counter, members, d)
        assert instances == [{"79": "ACC1", "80": "60", "366": "150.5"}, {"79": "ACC2", "80": "40"}]
        assert format_lines(instances, members) == "ACC1 60 150.5; ACC2 40"
        assert parse_lines("ACC1 60 150.5\nACC2, 40", members) == instances
        assert group_pairs(counter, members, instances, d) == [
            ("78", "2"), ("79", "ACC1"), ("80", "60"), ("366", "150.5"), ("79", "ACC2"), ("80", "40")]
        # a member the row does not keep (ProcessCode 81) does not end the group
        msg = parse_fix("8=FIX.4.2|35=J|78=2|79=ACC1|80=60|81=0|79=ACC2|80=40|58=x")
        assert [i["79"] for i in group_instances(msg, counter, members, d)] == ["ACC1", "ACC2"]
        assert group_pairs(counter, members, [], d) == []
        assert group_pairs("78", ("79", "80", "366"), instances, FixDictionary("FIX.4.0")) == [
            ("78", "2"), ("79", "ACC1"), ("80", "60"), ("79", "ACC2"), ("80", "40")], "366 is not a 4.0 tag"

    def test_lines_refuse_too_many_values(self):
        with pytest.raises(ValueError, match="at most 2"):
            parse_lines("C1 O1 extra", ALLOC_GROUPS["orders"][1])
        assert parse_lines("", ALLOC_GROUPS["orders"][1]) == []
        assert parse_lines(" ; \n", ALLOC_GROUPS["orders"][1]) == []

    def test_columns_of_an_allocation(self):
        cols = allocation_columns(parse_fix(ALLOC_RX), FixDictionary("FIX.4.2"))
        assert cols["orders"] == "C1 O1" and cols["execs"] == "E1 100 150.5"
        assert cols["allocs"] == "ACC1 60 150.5; ACC2 40" and cols["num_allocs"] == 2
        assert cols["alloc_trans_type"] == "New" and cols["side"] == "Buy" and cols["quantity"] == 100.0

    def test_columns_of_an_ioi(self):
        cols = ioi_columns(parse_fix(IOI_RX), FixDictionary("FIX.4.2"))
        assert cols["ioi_qty"] == "L" and cols["qualifiers"] == "A,X" and cols["qlty_ind"] == "High"
        assert cols["natural_flag"] == "Y" and cols["valid_until"] == "20260926-20:00:00.000"


class TestFactoryByVersion:
    @pytest.mark.parametrize("version", STANDARD_VERSIONS)
    def test_ioi_withholds_what_the_version_lacks(self, version):
        f = FixMessageFactory(FixDictionary(version), "A", "B")
        msg = f.ioi("I1", "N", "AAPL", "1", "L", 10.5, qlty_ind="H", natural_flag="Y", qualifiers=["A"],
                    valid_until="20260926-20:00:00")
        msg.sendprep(f.dictionary, "A", "B", 1)
        wire = msg.to_pipe_string()
        assert "|23=I1|28=N|55=AAPL|54=1|27=L|44=10.5|" in wire
        assert ("|60=" in wire) == (version not in ("FIX.4.0", "FIX.4.1", "FIX.4.2")), "TransactTime joined in 4.3"
        assert ("|199=1|104=A|" in wire) == (version != "FIX.4.0"), "the qualifier group joined in 4.1"
        assert "|25=H|130=Y|" in wire

    @pytest.mark.parametrize("version", STANDARD_VERSIONS)
    def test_allocation_withholds_what_the_version_lacks(self, version):
        f = FixMessageFactory(FixDictionary(version), "A", "B")
        msg = f.allocation_instruction("A1", "0", "AAPL", "1", 100, 10.5, alloc_type="2",
                                       allocs=[{"79": "X", "80": "100", "366": "10.5"}])
        msg.sendprep(f.dictionary, "A", "B", 1)
        wire = msg.to_pipe_string()
        assert ("|626=2|" in wire) == (version not in ("FIX.4.0", "FIX.4.1", "FIX.4.2")), "AllocType joined in 4.3"
        assert ("|366=10.5|" in wire) == (version not in ("FIX.4.0", "FIX.4.1")), "AllocPrice joined in 4.2"
        assert "|78=1|79=X|80=100|" in wire and "|53=100|6=10.5|" in wire
        ack = f.allocation_ack("A1", "1", rej_code="7", text="no")
        ack.sendprep(f.dictionary, "A", "B", 2)
        assert "|35=P|" in ack.to_pipe_string() and "|87=1|88=7|58=no|" in ack.to_pipe_string()

    def test_advert_and_dates(self):
        f = FixMessageFactory(FixDictionary("FIX.4.4"), "A", "B")
        msg = f.advertisement("D1", "R", "AAPL", "B", 100.5, 10, ref_id="D0", trade_date="2026-09-26", last_mkt="XNAS")
        msg.sendprep(f.dictionary, "A", "B", 1)
        assert "|2=D1|5=R|3=D0|55=AAPL|4=B|53=100.5|44=10|75=20260926|60=" in msg.to_pipe_string()
        assert msg.to_pipe_string().endswith("|30=XNAS|10=" + msg["10"])


# ── IOIs ───────────────────────────────────────────────────────────────

class TestReceivedIois:
    @pytest.mark.asyncio
    async def test_new_replace_and_cancel_are_one_chain(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive(IOI_RX)
        (row,) = await _rows(db, "fix_iois")
        assert row["direction"] == "RX" and row["status"] == "Active" and row["ioi_id"] == "I1"
        assert row["ioi_trans_type"] == "New" and row["ioi_qty"] == "L" and row["qualifiers"] == "A,X"
        assert row["extra_tags"] == "5001=x" and row["text"] == "hi" and parse_fix(row["raw_message"])["23"] == "I1"

        await stub.receive("8=FIX.4.2|35=6|23=I2|28=R|26=I1|55=AAPL|54=1|27=M|44=151|58=less")
        (row,) = await _rows(db, "fix_iois")
        assert (row["ioi_id"], row["ioi_ref_id"], row["ioi_trans_type"]) == ("I2", "I1", "Replace")
        assert row["ioi_qty"] == "M" and row["price"] == 151 and row["text"] == "less"
        assert row["qualifiers"] == "" and row["status"] == "Active", "a replace is the message's terms"

        await stub.receive("8=FIX.4.2|35=6|23=I3|28=C|26=I2|55=AAPL|54=1|27=M|58=gone")
        (row,) = await _rows(db, "fix_iois")
        assert (row["ioi_id"], row["ioi_ref_id"], row["status"], row["ioi_trans_type"]) == ("I3", "I2", "Canceled", "Cancel")
        assert row["price"] == 151 and row["text"] == "gone", "a cancel keeps the terms"
        assert [v["ioi_id"] for v in await _versions(db, "fix_iois", row["id"])] == ["I1", "I2", "I3"]

    @pytest.mark.asyncio
    async def test_a_reference_to_nothing_starts_a_row(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive("8=FIX.4.2|35=6|23=I9|28=C|26=NOPE|55=AAPL|54=1|27=S")
        (row,) = await _rows(db, "fix_iois")
        assert row["ioi_id"] == "I9" and row["ioi_ref_id"] == "NOPE" and row["status"] == "Canceled"

    @pytest.mark.asyncio
    async def test_an_order_naming_the_ioi_is_linked_both_ways(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive(IOI_RX)
        await stub.receive("8=FIX.4.2|35=D|11=C7|23=I1|55=AAPL|54=1|38=100|40=2|44=150.5|59=0")
        (ioi,) = await _rows(db, "fix_iois")
        (order,) = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert ioi["order_cl_ord_id"] == "C7" and order["ioi_id"] == "I1"
        assert order["pending_extra_tags"] == "", "tag 23 is consumed, not echoed"


class TestSentIois:
    @pytest.mark.asyncio
    async def test_send_replace_cancel(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        ioi_id = await engine.send_ioi("S1", "AAPL", "1", "1000", price=150.5, valid_until="2026-09-26T20:00:00Z",
                                       qlty_ind="H", natural_flag="Y", qualifiers="A, X", currency="USD",
                                       client="ACME", text="hi", extra_tags="5001=x")
        assert ioi_id.startswith("IO")
        (row,) = await _rows(db, "fix_iois")
        assert row["direction"] == "TX" and row["status"] == "Active" and row["ioi_id"] == ioi_id
        assert row["ioi_qty"] == "1000" and row["qualifiers"] == "A,X" and row["client"] == "ACME"
        assert row["valid_until"] == "20260926-20:00:00.000" and row["extra_tags"] == "5001=x"
        wire = stub.sent[-1].to_pipe_string()
        assert f"|23={ioi_id}|28=N|55=AAPL|54=1|27=1000|44=150.5|15=USD|62=20260926-20:00:00.000|25=H|130=Y|" in wire
        assert "|199=2|104=A|104=X|" in wire and "|109=ACME|" in wire and "|5001=x|" in wire

        new_id = await engine.replace_ioi("S1", ioi_id, "AAPL", "2", "M", price=151)
        (row,) = await _rows(db, "fix_iois")
        assert (row["ioi_id"], row["ioi_ref_id"], row["ioi_trans_type"], row["side"]) == (new_id, ioi_id, "Replace", "Sell")
        assert row["client"] == "ACME", "the replace keeps the client"
        assert f"|23={new_id}|28=R|26={ioi_id}|" in stub.sent[-1].to_pipe_string()

        last = await engine.cancel_ioi("S1", new_id, text="bye")
        (row,) = await _rows(db, "fix_iois")
        assert (row["ioi_id"], row["ioi_ref_id"], row["status"], row["text"]) == (last, new_id, "Canceled", "bye")
        assert row["ioi_qty"] == "M" and row["price"] == 151
        assert f"|23={last}|28=C|26={new_id}|55=AAPL|54=2|27=M|44=151.0|" in stub.sent[-1].to_pipe_string()
        assert len(await _versions(db, "fix_iois", row["id"])) == 3

    @pytest.mark.asyncio
    async def test_a_sent_order_answering_it_is_linked(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive(IOI_RX)
        cl_ord_id = await engine.send_new_order("S1", "AAPL", "1", 100, price=150.5, extra_tags="23=I1")
        (ioi,) = await _rows(db, "fix_iois")
        (order,) = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert ioi["order_cl_ord_id"] == cl_ord_id and order["ioi_id"] == "I1"

    @pytest.mark.asyncio
    async def test_refusals(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        with pytest.raises(ValueError, match="Unknown IOI"):
            await engine.cancel_ioi("S1", "NOPE")
        stub.is_active = False
        with pytest.raises(ValueError, match="not active"):
            await engine.send_ioi("S1", "AAPL", "1", "L")

    @pytest.mark.asyncio
    async def test_a_failed_send_is_recorded(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        ioi_id = await engine.send_ioi("S1", "AAPL", "1", "L")

        async def boom(msg):
            raise ConnectionError("gone")
        stub.send_message = boom
        with pytest.raises(ConnectionError):
            await engine.replace_ioi("S1", ioi_id, "AAPL", "1", "M")
        (row,) = await _rows(db, "fix_iois")
        assert row["ioi_id"] == ioi_id and row["ioi_qty"] == "L", "a replace that never went out is undone"
        with pytest.raises(ConnectionError):
            await engine.send_ioi("S1", "IBM", "1", "S")
        failed = (await _rows(db, "fix_iois"))[-1]
        assert failed["status"] == "Failed" and failed["text"].startswith("Send failed")


# ── Adverts ────────────────────────────────────────────────────────────

class TestAdverts:
    @pytest.mark.asyncio
    async def test_received_chain(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive(ADV_RX)
        (row,) = await _rows(db, "fix_adverts")
        assert row["adv_id"] == "D1" and row["side"] == "Buy" and row["quantity"] == 500 and row["last_mkt"] == "XNAS"
        assert row["extra_tags"] == "5001=y" and row["status"] == "Active" and row["trade_date"] == "20260926"
        await stub.receive("8=FIX.4.2|35=7|2=D2|5=R|3=D1|55=AAPL|4=S|53=400|44=150")
        await stub.receive("8=FIX.4.2|35=7|2=D3|5=C|3=D2|55=AAPL|4=S|53=400")
        (row,) = await _rows(db, "fix_adverts")
        assert (row["adv_id"], row["adv_ref_id"], row["side"], row["status"]) == ("D3", "D2", "Sell", "Canceled")

    @pytest.mark.asyncio
    async def test_sent_chain(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        adv_id = await engine.send_advert("S1", "AAPL", "T", 500, price=150.5, currency="USD",
                                          trade_date="2026-09-26", last_mkt="XNAS", text="done")
        assert adv_id.startswith("AD")
        assert f"|2={adv_id}|5=N|55=AAPL|4=T|53=500|44=150.5|15=USD|75=20260926|60=" in stub.sent[-1].to_pipe_string()
        new_id = await engine.replace_advert("S1", adv_id, "AAPL", "T", 600, price=151)
        last = await engine.cancel_advert("S1", new_id)
        (row,) = await _rows(db, "fix_adverts")
        assert (row["adv_id"], row["adv_ref_id"], row["status"], row["quantity"]) == (last, new_id, "Canceled", 600)
        assert f"|2={last}|5=C|3={new_id}|" in stub.sent[-1].to_pipe_string()
        assert row["direction"] == "TX" and row["side"] == "Trade"


# ── Allocations ────────────────────────────────────────────────────────

class TestReceivedAllocations:
    @pytest.mark.asyncio
    async def test_new_parks_and_accept_answers(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive(ALLOC_RX)
        (row,) = await _rows(db, "fix_allocations")
        assert row["status"] == "PendingNew" and row["pending_action"] == "New" and row["direction"] == "RX"
        assert row["allocs"] == "ACC1 60 150.5; ACC2 40" and row["num_allocs"] == 2 and row["orders"] == "C1 O1"
        assert row["pending_extra_tags"] == "5001=z" and row["text"] == "please"

        answered = await engine.accept_allocation("S1", "A1", alloc_status="0", text="ok", extra_tags="5001=z")
        assert answered == "A1"
        wire = stub.sent[-1].to_pipe_string()
        assert "|35=P|" in wire and "|70=A1|75=20260926|" in wire and "|87=0|58=ok|5001=z|" in wire
        (row,) = await _rows(db, "fix_allocations")
        assert row["status"] == "Accepted" and row["alloc_status"] == "Accepted" and row["alloc_status_code"] == "0"
        assert row["pending_action"] == "" and row["pending_extra_tags"] == "" and row["sent_text"] == "ok"
        assert row["text"] == "please", "their text stays theirs"

    @pytest.mark.asyncio
    async def test_reject_a_new(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive(ALLOC_RX)
        await engine.reject_allocation("S1", "A1", alloc_status="2", alloc_rej_code="1", text="qty")
        assert "|87=2|88=1|58=qty|" in stub.sent[-1].to_pipe_string()
        (row,) = await _rows(db, "fix_allocations")
        assert row["status"] == "Rejected" and row["alloc_status"] == "AccountLevelReject"
        assert row["alloc_rej_reason"] == "IncorrectQuantity" and row["alloc_rej_code"] == "1"

    @pytest.mark.asyncio
    async def test_replace_request_parks_then_accept_renames(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive(ALLOC_RX)
        await engine.accept_allocation("S1", "A1")
        await stub.receive("8=FIX.4.2|35=J|70=A2|71=1|72=A1|55=AAPL|54=1|53=100|6=150.5|75=20260926"
                           "|78=1|79=ACC3|80=100|58=redo|5002=w")
        (row,) = await _rows(db, "fix_allocations")
        assert row["alloc_id"] == "A1" and row["pending_action"] == "Replace" and row["pending_alloc_id"] == "A2"
        assert row["allocs"] == "ACC1 60 150.5; ACC2 40" and row["status"] == "Accepted", "nothing moves until accepted"
        assert row["text"] == "redo" and row["pending_extra_tags"] == "5002=w"
        assert json.loads(row["pending_terms"])["allocs"] == "ACC3 100"

        answered = await engine.accept_allocation("S1", "A1", alloc_status="3")
        assert answered == "A2" and "|70=A2|" in stub.sent[-1].to_pipe_string() and "|87=3|" in stub.sent[-1].to_pipe_string()
        (row,) = await _rows(db, "fix_allocations")
        assert (row["alloc_id"], row["ref_alloc_id"], row["alloc_trans_type"]) == ("A2", "A1", "Replace")
        assert row["allocs"] == "ACC3 100" and row["num_allocs"] == 1 and row["extra_tags"] == "5002=w"
        assert row["status"] == "Received" and row["pending_action"] == "" and row["pending_terms"] == ""
        assert [v["alloc_id"] for v in await _versions(db, "fix_allocations", row["id"])] == ["A1", "A1", "A1", "A2"]

    @pytest.mark.asyncio
    async def test_rejected_request_leaves_the_row(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive(ALLOC_RX)
        await engine.accept_allocation("S1", "A1")
        await stub.receive("8=FIX.4.2|35=J|70=A2|71=2|72=A1|55=AAPL|54=1|53=100|6=150.5|75=20260926|78=1|79=ACC1|80=100")
        (row,) = await _rows(db, "fix_allocations")
        assert row["pending_action"] == "Cancel"
        await engine.reject_allocation("S1", "A1", alloc_status="1", alloc_rej_code="7", text="no")
        assert "|70=A2|" in stub.sent[-1].to_pipe_string()
        (row,) = await _rows(db, "fix_allocations")
        assert row["alloc_id"] == "A1" and row["status"] == "Accepted" and row["pending_action"] == ""
        assert row["alloc_status"] == "BlockLevelReject" and row["alloc_rej_reason"] == "Other"

    @pytest.mark.asyncio
    async def test_accepted_cancel(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive(ALLOC_RX)
        await engine.accept_allocation("S1", "A1")
        await stub.receive("8=FIX.4.2|35=J|70=A2|71=2|72=A1|55=AAPL|54=1|53=100|6=150.5|75=20260926|78=1|79=ACC1|80=100")
        await engine.accept_allocation("S1", "A1")
        (row,) = await _rows(db, "fix_allocations")
        assert (row["alloc_id"], row["ref_alloc_id"], row["status"]) == ("A2", "A1", "Canceled")
        assert row["allocs"] == "ACC1 60 150.5; ACC2 40", "a cancel keeps the terms"
        assert row["alloc_trans_type"] == "Cancel"

    @pytest.mark.asyncio
    async def test_unknown_reference_is_rejected_at_once(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive("8=FIX.4.2|35=J|70=A2|71=1|72=NOPE|55=AAPL|54=1|53=100|6=150.5|75=20260926|78=1|79=ACC1|80=100")
        assert await _rows(db, "fix_allocations") == []
        wire = stub.sent[-1].to_pipe_string()
        assert "|35=P|" in wire and "|70=A2|" in wire and "|87=1|88=7|58=Unknown allocation: NOPE|" in wire

    @pytest.mark.asyncio
    async def test_refusals(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive(ALLOC_RX)
        with pytest.raises(ValueError, match="does not accept"):
            await engine.accept_allocation("S1", "A1", alloc_status="1")
        with pytest.raises(ValueError, match="accepts"):
            await engine.reject_allocation("S1", "A1", alloc_status="0")
        await engine.accept_allocation("S1", "A1")
        with pytest.raises(ValueError, match="Nothing pending"):
            await engine.accept_allocation("S1", "A1")
        with pytest.raises(ValueError, match="Unknown allocation"):
            await engine.reject_allocation("S1", "NOPE")

    @pytest.mark.asyncio
    async def test_legacy_trans_types_are_new(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive("8=FIX.4.2|35=J|70=A5|71=3|55=AAPL|54=1|53=100|6=1|75=20260926|78=1|79=X|80=100")
        (row,) = await _rows(db, "fix_allocations")
        assert row["pending_action"] == "New" and row["alloc_trans_type"] == "Preliminary"


class TestSentAllocations:
    @pytest.mark.asyncio
    async def test_send_and_ack(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        alloc_id = await engine.send_allocation(
            "S1", "AAPL", "1", 100, 150.5, trade_date="2026-09-26", orders="C1,O1\nC2", execs="E1 60 150.5; E2 40 150.5",
            allocs="ACC1 60\nACC2 40 150.5", client="ACME", text="pls", extra_tags="5001=q")
        assert alloc_id.startswith("AL")
        wire = stub.sent[-1].to_pipe_string()
        assert f"|70={alloc_id}|71=0|55=AAPL|54=1|53=100|6=150.5|75=20260926|60=" in wire
        assert "|58=pls|109=ACME|73=2|11=C1|37=O1|11=C2|124=2|17=E1|32=60|31=150.5|17=E2|32=40|31=150.5" \
               "|78=2|79=ACC1|80=60|79=ACC2|80=40|366=150.5|5001=q|" in wire
        (row,) = await _rows(db, "fix_allocations")
        assert row["status"] == "Sent" and row["direction"] == "TX" and row["sent_text"] == "pls" and row["text"] == ""
        assert row["orders"] == "C1 O1; C2" and row["allocs"] == "ACC1 60; ACC2 40 150.5" and row["num_allocs"] == 2
        assert row["client"] == "ACME" and row["extra_tags"] == "5001=q"

        await stub.receive(f"8=FIX.4.2|35=P|70={alloc_id}|75=20260926|87=3")
        (row,) = await _rows(db, "fix_allocations")
        assert row["status"] == "Received" and row["alloc_status"] == "Received"
        await stub.receive(f"8=FIX.4.2|35=P|70={alloc_id}|75=20260926|87=2|88=0|58=who")
        (row,) = await _rows(db, "fix_allocations")
        assert row["status"] == "Rejected" and row["alloc_rej_reason"] == "UnknownAccount" and row["text"] == "who"
        assert row["sent_text"] == "pls"

    @pytest.mark.asyncio
    async def test_replace_waits_for_its_ack(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        alloc_id = await engine.send_allocation("S1", "AAPL", "1", 100, 150.5, allocs="ACC1 100")
        await stub.receive(f"8=FIX.4.2|35=P|70={alloc_id}|75=20260926|87=0")
        new_id = await engine.replace_allocation("S1", alloc_id, "AAPL", "1", 100, 150.5, allocs="ACC2 100", text="redo")
        assert f"|70={new_id}|71=1|72={alloc_id}|" in stub.sent[-1].to_pipe_string()
        (row,) = await _rows(db, "fix_allocations")
        assert row["alloc_id"] == alloc_id and row["allocs"] == "ACC1 100" and row["status"] == "Accepted"
        assert row["pending_action"] == "Replace" and row["pending_alloc_id"] == new_id and row["sent_text"] == "redo"

        await stub.receive(f"8=FIX.4.2|35=P|70={new_id}|75=20260926|87=1|88=7|58=no")
        (row,) = await _rows(db, "fix_allocations")
        assert row["alloc_id"] == alloc_id and row["allocs"] == "ACC1 100" and row["status"] == "Accepted"
        assert row["pending_action"] == "" and row["alloc_status"] == "BlockLevelReject" and row["text"] == "no"

        again = await engine.replace_allocation("S1", alloc_id, "AAPL", "1", 100, 150.5, allocs="ACC3 100")
        await stub.receive(f"8=FIX.4.2|35=P|70={again}|75=20260926|87=0")
        (row,) = await _rows(db, "fix_allocations")
        assert (row["alloc_id"], row["ref_alloc_id"], row["allocs"], row["status"]) == (again, alloc_id, "ACC3 100", "Accepted")
        assert row["alloc_trans_type"] == "Replace" and row["pending_action"] == ""

    @pytest.mark.asyncio
    async def test_cancel_waits_for_its_ack(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        alloc_id = await engine.send_allocation("S1", "AAPL", "1", 100, 150.5, allocs="ACC1 100")
        cancel_id = await engine.cancel_allocation("S1", alloc_id, text="bye")
        wire = stub.sent[-1].to_pipe_string()
        assert f"|70={cancel_id}|71=2|72={alloc_id}|55=AAPL|54=1|53=100|6=150.5|" in wire and "|78=1|79=ACC1|80=100|" in wire
        (row,) = await _rows(db, "fix_allocations")
        assert row["pending_action"] == "Cancel" and row["status"] == "Sent"
        await stub.receive(f"8=FIX.4.2|35=P|70={cancel_id}|75=20260926|87=0")
        (row,) = await _rows(db, "fix_allocations")
        assert (row["alloc_id"], row["ref_alloc_id"], row["status"], row["allocs"]) == (cancel_id, alloc_id, "Canceled", "ACC1 100")

    @pytest.mark.asyncio
    async def test_an_ack_naming_nothing_is_only_a_message(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        await stub.receive("8=FIX.4.2|35=P|70=NOPE|75=20260926|87=0")
        assert await _rows(db, "fix_allocations") == []
        (msg,) = await _fetch_all(db, "SELECT msg_type FROM fix_messages")
        assert msg["msg_type"] == "P"

    @pytest.mark.asyncio
    async def test_a_failed_request_is_not_outstanding(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        alloc_id = await engine.send_allocation("S1", "AAPL", "1", 100, 150.5, allocs="ACC1 100")

        async def boom(msg):
            raise ConnectionError("gone")
        stub.send_message = boom
        with pytest.raises(ConnectionError):
            await engine.cancel_allocation("S1", alloc_id)
        (row,) = await _rows(db, "fix_allocations")
        assert row["pending_action"] == "" and row["status"] == "Sent"


# ── Across the engine ──────────────────────────────────────────────────

class TestAcrossTheEngine:
    @pytest.mark.asyncio
    async def test_every_family_action_writes_before_it_sends(self, stack):
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

        ioi = await traced("send_ioi", engine.send_ioi("S1", "AAPL", "1", "L"))
        ioi = await traced("replace_ioi", engine.replace_ioi("S1", ioi, "AAPL", "1", "M"))
        await traced("cancel_ioi", engine.cancel_ioi("S1", ioi))
        adv = await traced("send_advert", engine.send_advert("S1", "AAPL", "B", 100))
        adv = await traced("replace_advert", engine.replace_advert("S1", adv, "AAPL", "B", 200))
        await traced("cancel_advert", engine.cancel_advert("S1", adv))
        alloc = await traced("send_allocation", engine.send_allocation("S1", "AAPL", "1", 100, 1, allocs="X 100"))
        await traced("replace_allocation", engine.replace_allocation("S1", alloc, "AAPL", "1", 100, 1, allocs="Y 100"))
        await traced("cancel_allocation", engine.cancel_allocation("S1", alloc))
        await stub.receive(ALLOC_RX)
        await traced("accept_allocation", engine.accept_allocation("S1", "A1"))
        await stub.receive(ALLOC_RX.replace("70=A1", "70=A9"))
        await traced("reject_allocation", engine.reject_allocation("S1", "A9"))

    @pytest.mark.asyncio
    async def test_perform_routes_the_ops_and_announces_rows(self, stack):
        db, writer, engine = stack
        stub = _stub(engine)
        seen = []
        engine.events.subscribe(seen.append)
        result = await engine.perform("send_ioi", {"session_id": "S1", "symbol": "AAPL", "side": "1", "qty": "L",
                                                   "price": "150.5", "qualifiers": "A"})
        ioi_id = result["ioi_id"]
        result = await engine.perform("replace_ioi", {"session_id": "S1", "ioi_id": ioi_id, "symbol": "AAPL",
                                                      "side": "1", "qty": "M", "price": ""})
        assert [e.kinds for e in seen] == [("sent ioi",), ("action",), ("action",)]
        assert seen[1].table == "fix_iois" and seen[1].row["ioi_id"] == ioi_id and seen[1].order is None
        assert seen[2].detail["prev_row"]["ioi_id"] == ioi_id and seen[2].row["ioi_id"] == result["ioi_id"]
        await stub.receive(ALLOC_RX)
        assert seen[-1].kinds == ("allocation", "message") and seen[-1].row["alloc_id"] == "A1"
        await engine.perform("accept_allocation", {"session_id": "S1", "alloc_id": "A1", "alloc_status": "0"})
        assert seen[-1].detail["op"] == "accept_allocation" and seen[-1].row["status"] == "Accepted"
        await stub.receive("8=FIX.4.2|35=J|70=A2|71=2|72=A1|55=AAPL|54=1|53=100|6=1|75=20260926|78=1|79=X|80=100")
        assert seen[-1].kinds == ("allocation cancel", "message") and seen[-1].request == "A2"

    @pytest.mark.asyncio
    async def test_fix40_round_trip(self, stack):
        db, writer, engine = stack
        stub = _stub(engine, "FIX.4.0")
        ioi = await engine.send_ioi("S1", "AAPL", "1", "L", qualifiers="A", qlty_ind="H")
        assert "|199=" not in stub.sent[-1].to_pipe_string() and "|25=H|" in stub.sent[-1].to_pipe_string()
        await stub.receive("8=FIX.4.0|35=J|70=A1|71=0|55=AAPL|54=1|53=100|6=1|75=20260926|78=1|79=X|80=100")
        await engine.accept_allocation("S1", "A1")
        assert "|35=P|" in stub.sent[-1].to_pipe_string()
        assert (await _rows(db, "fix_iois"))[0]["ioi_id"] == ioi

    @pytest.mark.asyncio
    async def test_startup_backfill_folds_the_old_viewer_rows(self, stack):
        """Through 0.62 the viewers inserted one bare row per IOI or
        allocation message; the backfill derives the new columns from each
        row's wire message and folds a Replace or Cancel into the row it
        names."""
        db, writer, engine = stack
        conn = db.write_conn
        await (await conn.execute(
            "INSERT INTO fix_sessions (session_id, fix_version, sender_comp_id, target_comp_id) "
            "VALUES ('S1', 'FIX.4.2', 'A', 'B')")).close()
        for ioi_id, raw in (("I1", "8=FIX.4.2|35=6|23=I1|28=N|55=AAPL|54=1|27=L|44=150|58=one"),
                            ("I2", "8=FIX.4.2|35=6|23=I2|28=R|26=I1|55=AAPL|54=1|27=M|44=151"),
                            ("I3", "8=FIX.4.2|35=6|23=I3|28=N|55=IBM|54=2|27=S")):
            await (await conn.execute(
                "INSERT INTO fix_iois (session_id, ioi_id, symbol, timestamp, direction, raw_message) "
                "VALUES ('S1', ?, 'AAPL', '20260926-10:00:00.000', 'RX', ?)", (ioi_id, raw))).close()
        await (await conn.execute(
            "INSERT INTO fix_allocations (session_id, alloc_id, timestamp, direction, raw_message) "
            "VALUES ('S1', 'A1', '20260926-10:00:00.000', 'RX', ?)", (ALLOC_RX,))).close()
        await conn.commit()
        await engine.start()
        rows = await _rows(db, "fix_iois")
        assert [(r["ioi_id"], r["ioi_ref_id"], r["ioi_qty"], r["status"]) for r in rows] == [
            ("I2", "I1", "M", "Active"), ("I3", "", "S", "Active")]
        assert rows[0]["ioi_trans_type"] == "Replace" and rows[0]["price"] == 151 and rows[0]["side"] == "Buy"
        (alloc,) = await _rows(db, "fix_allocations")
        assert alloc["allocs"] == "ACC1 60 150.5; ACC2 40" and alloc["status"] == "Accepted" and alloc["extra_tags"] == "5001=z"
        await engine.stop()
        await engine.start()
        assert len(await _rows(db, "fix_iois")) == 2, "runs once"
        await engine.stop()

    @pytest.mark.asyncio
    async def test_templates_for_the_new_scopes(self, stack):
        db, writer, engine = stack
        await engine.save_template("allocation", "three accounts", allocs="A 50; B 30; C 20", alloc_type="2")
        await engine.save_template("alloc_reject", "bad qty", alloc_status="2", alloc_rej_code="1")
        rows = await _fetch_all(db, "SELECT scope, name, allocs, alloc_type, alloc_status, alloc_rej_code "
                                    "FROM fix_templates ORDER BY scope")
        assert rows == [
            {"scope": "alloc_reject", "name": "bad qty", "allocs": "", "alloc_type": "", "alloc_status": "2",
             "alloc_rej_code": "1"},
            {"scope": "allocation", "name": "three accounts", "allocs": "A 50; B 30; C 20", "alloc_type": "2",
             "alloc_status": "", "alloc_rej_code": ""},
        ]
