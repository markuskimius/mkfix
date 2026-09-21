"""Tests for FixEngine's compiled write operations against a real mkio stack."""

import asyncio
import tomllib
from pathlib import Path

import pytest
import pytest_asyncio

from mkio.change_bus import ChangeBus
from mkio.config import load_config
from mkio.database import Database
from mkio.history import history_specs, versioned_tables
from mkio.writer import CompiledOp, WriteBatcher

from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.engine import FixEngine
from mkfix.fix.message import FixMessage, FixMessageFactory, parse_fix, SOH

MKFIX_TOML = tomllib.loads(
    (Path(__file__).parent.parent / "mkfix" / "mkfix.toml").read_text(encoding="utf-8")
)
TABLES = MKFIX_TOML["tables"]
QUERY_SERVICES = ("sessions_query", "orders_query", "executions_query")

# Loaded through mkio so the versioned tables' derived history tables exist
# and the writer records versions, as the real server does.
CONFIG = load_config({
    "db_path": ":memory:", "tables": TABLES,
    "services": {name: MKFIX_TOML["services"][name] for name in QUERY_SERVICES},
})


@pytest_asyncio.fixture
async def stack():
    db = Database(path=":memory:", tables=CONFIG["tables"], config=CONFIG)
    await db.start()
    bus = ChangeBus()
    writer = WriteBatcher(
        db, bus,
        versioned=history_specs(CONFIG),
        versioned_configs=versioned_tables(CONFIG),
    )
    await writer.start()
    engine = FixEngine(db=db, writer=writer)
    engine._compile_ops()
    await engine._ensure_indexes()
    yield db, writer, engine
    await writer.stop()
    await db.stop()


async def _fetch_all(db, sql):
    cur = await db.read_conn.execute(sql)
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    return rows


from mkfix.fix.engine import ORDER_COLS, ORDER_UPDATE_COLS  # noqa: E402


def _order_params(**overrides):
    base = {
        "cl_ord_id": "C1", "session_id": "S1", "order_id": "", "orig_cl_ord_id": "",
        "symbol": "AAPL", "side": "Buy", "side_code": "1",
        "ord_type": "Limit", "ord_type_code": "2", "price": 150.25,
        "stop_price": 0.0, "order_qty": 100.0, "time_in_force": "Day",
        "status": "PendingNew", "cum_qty": 0.0, "avg_price": 0.0,
        "leaves_qty": 100.0, "last_qty": 0.0, "last_price": 0.0, "text": "",
        "transact_time": "", "created_at": "", "updated_at": "", "direction": "TX",
        "pending_action": "", "pending_cl_ord_id": "",
        "pending_qty": 0.0, "pending_price": 0.0, "pending_extra_tags": "",
        "tif_code": "0", "extra_tags": "", "entered_qty": 100.0, "entered_price": 150.25,
        "expire_time": "", "expire_date": "", "client": "",
        "handl_inst": "", "handl_inst_code": "", "sent_text": "",
    }
    base.update(overrides)
    insert = tuple(base[c] for c in ORDER_COLS)
    update = tuple(base[c] for c in ORDER_UPDATE_COLS)
    return insert + (None,) + update + (base["sent_text"],)


class TestInstanceCode:
    @pytest.mark.asyncio
    async def test_engine_passes_instance_code_to_id_generator(self, stack):
        """The CLI's -i reaches the generator through the engine, and every
        ID the engine mints carries it."""
        db, writer, _ = stack
        engine = FixEngine(db=db, writer=writer, instance_code="Q7")
        await engine.start()
        assert engine.ids.instance_id == "Q7"
        assert await engine.ids.next_id("OR") == "ORQ700000001"
        rows = await _fetch_all(db, "SELECT key, value FROM fix_settings WHERE key = 'instance_code'")
        assert rows == [{"key": "instance_code", "value": "Q7"}]
        await engine.stop()

    @pytest.mark.asyncio
    async def test_engine_without_code_reuses_saved_one(self, stack):
        db, writer, _ = stack
        first = FixEngine(db=db, writer=writer, instance_code="Q7")
        await first.start()
        await first.stop()
        second = FixEngine(db=db, writer=writer)
        await second.start()
        assert second.ids.instance_id == "Q7"
        assert second.ids.instance_source == "saved"
        await second.stop()

    def test_engine_rejects_bad_code_before_start(self, stack):
        db, writer, _ = stack
        with pytest.raises(ValueError, match="instance code"):
            FixEngine(db=db, writer=writer, instance_code="bad")


class TestCompiledOps:
    @pytest.mark.asyncio
    async def test_insert_message_writes_ref(self, stack):
        db, writer, engine = stack
        params = ("S1", "20260803-00:00:00.000", "TX", 1, "A", "Logon", "ADMIN",
                  "8=FIX.4.2|", "US", "THEM", "", "", "", "", "", 60, "123", "", None)
        await writer.submit(engine._compiled_ops["insert_message"], (params,), {})
        rows = await _fetch_all(db, "SELECT * FROM fix_messages")
        assert len(rows) == 1
        assert rows[0]["msg_type"] == "A"
        assert rows[0]["_mkio_ref"]

    @pytest.mark.asyncio
    async def test_upsert_state_update_path_keeps_ref(self, stack):
        db, writer, engine = stack
        await engine.update_session_state("S1", {"status": "DOWN"})
        await engine.update_session_state("S1", {"status": "ACTIVE"})
        rows = await _fetch_all(db, "SELECT * FROM fix_session_state")
        assert len(rows) == 1
        assert rows[0]["status"] == "ACTIVE"
        assert rows[0]["_mkio_ref"], "update path must not null out _mkio_ref"

    @pytest.mark.asyncio
    async def test_state_write_is_one_op_on_the_state_table(self, stack):
        """fix_session_state is the only place a session's live state goes:
        the blotters join it, so nothing is mirrored onto fix_sessions,
        fix_orders or fix_executions (through 0.33 every message rewrote
        the session row and every transition every order row)."""
        db, writer, engine = stack
        await _add_session(writer)
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        await engine.fill_order("S1", "C100", qty=40, price=150.0)
        submitted = []
        real_submit = writer.submit

        async def spy(ops, params_list, data):
            submitted.append([(op.table, op.op_type) for op in ops])
            return await real_submit(ops, params_list, data)
        writer.submit = spy

        await engine.update_session_state(
            "S1", {"status": "ACTIVE", "tx_seq_num": 7, "rx_seq_num": 9})
        await engine.update_session_state("S1", {"status": "DOWN"})
        assert submitted == [[("fix_session_state", "upsert")]] * 2
        state = (await _fetch_all(db, "SELECT * FROM fix_session_state"))[0]
        assert (state["status"], state["tx_seq_num"], state["rx_seq_num"]) == ("DOWN", 7, 9)
        gone = {"fix_sessions": {"status", "tx_seq_num", "rx_seq_num"},
                "fix_orders": {"session_status"}, "fix_executions": {"session_status"}}
        for table, columns in gone.items():
            cols = {r["name"] for r in await _fetch_all(db, f"PRAGMA table_info({table})")}
            assert not cols & columns, table

    @pytest.mark.asyncio
    async def test_upsert_order_insert_records_fix_codes(self, stack):
        db, writer, engine = stack
        ops = engine._compiled_ops["upsert_order"]
        await writer.submit(ops, (_order_params(),), {})
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert len(rows) == 1
        assert rows[0]["side"] == "Buy"
        assert rows[0]["side_code"] == "1"
        assert rows[0]["ord_type"] == "Limit"
        assert rows[0]["ord_type_code"] == "2"

    @pytest.mark.asyncio
    async def test_upsert_order_update_path(self, stack):
        db, writer, engine = stack
        ops = engine._compiled_ops["upsert_order"]
        await writer.submit(ops, (_order_params(),), {})
        await writer.submit(ops, (_order_params(
            order_id="X9", status="PartiallyFilled", cum_qty=40.0,
            avg_price=150.25, leaves_qty=60.0, last_qty=40.0, last_price=150.25,
        ),), {})
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert len(rows) == 1, "same (cl_ord_id, session_id) must upsert, not insert"
        assert rows[0]["status"] == "PartiallyFilled"
        assert rows[0]["cum_qty"] == 40.0
        assert rows[0]["order_id"] == "X9", "empty order_id fills in from the update"
        assert rows[0]["_mkio_ref"], "update path must not null out _mkio_ref"

    @pytest.mark.asyncio
    async def test_upsert_order_id_is_write_once(self, stack):
        db, writer, engine = stack
        ops = engine._compiled_ops["upsert_order"]
        await writer.submit(ops, (_order_params(order_id="OR1"),), {})
        await writer.submit(ops, (_order_params(order_id="MKT-37"),), {})
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert rows[0]["order_id"] == "OR1", "an assigned Order ID is immutable"

    @pytest.mark.asyncio
    async def test_orders_distinct_by_session(self, stack):
        db, writer, engine = stack
        ops = engine._compiled_ops["upsert_order"]
        await writer.submit(ops, (_order_params(),), {})
        await writer.submit(ops, (_order_params(session_id="S2"),), {})
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_update_replay(self, stack):
        db, writer, engine = stack
        await db.write_conn.execute(
            "INSERT INTO fix_replay_jobs (name, file_path, status) VALUES ('j', '/f', 'loaded')"
        )
        await db.write_conn.commit()
        params = ("running", 5, "", None, 1)
        await writer.submit(engine._compiled_ops["update_replay"], (params,), {})
        rows = await _fetch_all(db, "SELECT * FROM fix_replay_jobs")
        assert rows[0]["status"] == "running"
        assert rows[0]["sent_messages"] == 5
        assert rows[0]["_mkio_ref"]

    @pytest.mark.asyncio
    async def test_ensure_indexes_idempotent(self, stack):
        db, writer, engine = stack
        await engine._ensure_indexes()
        rows = await _fetch_all(
            db, "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_fix_orders_clord_session'"
        )
        assert len(rows) == 1


class StubSession:
    """Stands in for FixSession: captures sent messages instead of writing to a socket."""

    def __init__(self, session_id="S1"):
        self.session_id = session_id
        self.dictionary = FixDictionary("FIX.4.2")
        self.factory = FixMessageFactory(self.dictionary, "MKT", "CLIENT")
        self.is_active = True
        self.status = "ACTIVE"
        self.sent = []
        self.config = {}

    async def send_message(self, msg):
        self.sent.append(msg)
        return msg


NEW_ORDER_RX = "8=FIX.4.2|35=D|11=C100|55=AAPL|54=1|38=100|40=2|44=150.25|59=0"


class TestWireRecording:
    @pytest.mark.asyncio
    async def test_record_message_stores_wire_bytes(self, stack):
        db, writer, engine = stack
        raw = SOH.join(["8=FIX.4.2", "9=30", "35=D", "11=C1", "58=A|B", "10=000", ""]).encode()
        await engine.record_message("S1", "RX", parse_fix(raw))
        rows = await _fetch_all(db, "SELECT raw_message FROM fix_messages")
        assert rows[0]["raw_message"] == raw.decode("latin-1")
        assert parse_fix(rows[0]["raw_message"])["58"] == "A|B"

    async def _record(self, engine, session_id, direction, seq, msg_type):
        msg = FixMessage({"35": msg_type})
        msg.sendprep(FixDictionary("FIX.4.2"), "US", "THEM", seq)
        await engine.record_message(session_id, direction, msg)

    @pytest.mark.asyncio
    async def test_sent_messages_scoped_by_epoch_and_latest_per_seq(self, stack):
        db, writer, engine = stack
        await self._record(engine, "S1", "TX", 1, "A")
        await self._record(engine, "S1", "TX", 2, "D")
        epoch = await engine.last_message_id()
        await self._record(engine, "S1", "TX", 1, "A")
        await self._record(engine, "S1", "RX", 2, "8")
        await self._record(engine, "S2", "TX", 2, "D")
        await self._record(engine, "S1", "TX", 2, "F")
        await self._record(engine, "S1", "TX", 2, "G")

        rows = await engine.sent_messages("S1", 1, 5, epoch)
        assert [(r["seq_num"], r["msg_type"]) for r in rows] == [(1, "A"), (2, "G")]
        assert epoch == 2
        assert await engine.sent_messages("S1", 3, 5, epoch) == []

    @pytest.mark.asyncio
    async def test_ioi_and_allocation_viewers_store_wire_form(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        ioi = SOH.join(["8=FIX.4.2", "35=6", "23=I1", "28=N", "55=AAPL", "54=1", "27=L",
                        "58=x|y", "10=000", ""]).encode()
        alloc = SOH.join(["8=FIX.4.2", "35=J", "70=A1", "71=0", "55=AAPL", "54=1", "53=100",
                          "6=1.5", "75=20260904", "58=p|q", "10=000", ""]).encode()
        await engine._handle_ioi(stub, parse_fix(ioi), "RX")
        await engine._handle_allocation(stub, parse_fix(alloc), "RX")
        ioi_row, = await _fetch_all(db, "SELECT raw_message FROM fix_iois")
        alloc_row, = await _fetch_all(db, "SELECT raw_message FROM fix_allocations")
        assert parse_fix(ioi_row["raw_message"])["58"] == "x|y"
        assert parse_fix(alloc_row["raw_message"])["58"] == "p|q"

    @pytest.mark.asyncio
    async def test_seq_epoch_persists_in_state(self, stack):
        db, writer, engine = stack
        await engine.update_session_state("S1", {"seq_epoch": 12})
        state = await engine.load_session_state("S1")
        assert state["seq_epoch"] == 12
        await engine.update_session_state("S1", {"status": "ACTIVE"})
        assert (await engine.load_session_state("S1"))["seq_epoch"] == 12


class TestMarketFlow:
    async def _seed(self, engine):
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        return stub

    async def _order(self, db):
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert len(rows) == 1
        return rows[0]

    @pytest.mark.asyncio
    async def test_received_order_recorded(self, stack):
        db, writer, engine = stack
        await self._seed(engine)
        row = await self._order(db)
        assert row["direction"] == "RX"
        assert row["status"] == "PendingNew"
        assert row["side"] == "Buy"
        assert row["ord_type"] == "Limit"
        assert row["order_qty"] == 100.0
        assert row["leaves_qty"] == 100.0
        assert row["pending_action"] == "New", "a new order arrives as a pending request"
        assert row["order_id"].startswith("OR"), "a received order gets its Order ID on arrival"

    @pytest.mark.asyncio
    async def test_accept_order(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        order_id = await engine.accept_order("S1", "C100")
        msg = stub.sent[-1]
        assert msg["35"] == "8"
        assert msg["20"] == "0"
        assert msg["150"] == "0"
        assert msg["39"] == "0"
        assert msg["37"] == order_id
        row = await self._order(db)
        assert row["status"] == "New"
        assert row["order_id"] == order_id
        assert order_id.startswith("OR")
        assert row["direction"] == "RX"
        assert row["pending_action"] == "", "accept consumes the pending request"

    @pytest.mark.asyncio
    async def test_reject_order(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.reject_order("S1", "C100", text="unknown symbol")
        msg = stub.sent[-1]
        assert msg["150"] == "8"
        assert msg["39"] == "8"
        assert msg["58"] == "unknown symbol"
        row = await self._order(db)
        assert row["status"] == "Rejected"
        assert row["leaves_qty"] == 0.0

    @pytest.mark.asyncio
    async def test_fill_order_partial_then_full(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.accept_order("S1", "C100")

        exec_id = await engine.fill_order("S1", "C100", qty=40, price=150.0)
        msg = stub.sent[-1]
        assert msg["150"] == "1"
        assert msg["17"] == exec_id
        assert exec_id.startswith("EX")
        row = await self._order(db)
        assert row["status"] == "PartiallyFilled"
        assert row["cum_qty"] == 40.0
        assert row["leaves_qty"] == 60.0

        await engine.fill_order("S1", "C100", qty=60, price=151.0)
        msg = stub.sent[-1]
        assert msg["150"] == "2"
        row = await self._order(db)
        assert row["status"] == "Filled"
        assert row["cum_qty"] == 100.0
        assert row["leaves_qty"] == 0.0
        assert row["avg_price"] == pytest.approx((40 * 150.0 + 60 * 151.0) / 100)

        execs = await _fetch_all(db, "SELECT * FROM fix_executions ORDER BY id")
        assert [e["exec_type"] for e in execs] == ["PartialFill", "Fill"]
        assert all(e["direction"] == "TX" for e in execs)
        assert all(e["trade_id"].startswith("TR") for e in execs)
        assert execs[0]["trade_id"] != execs[1]["trade_id"], "each fill is its own trade"

    @pytest.mark.asyncio
    async def test_fill_rejects_nonpositive_qty(self, stack):
        db, writer, engine = stack
        await self._seed(engine)
        with pytest.raises(ValueError):
            await engine.fill_order("S1", "C100", qty=0, price=150.0)

    @pytest.mark.asyncio
    async def test_fill_of_filled_order_overfills(self, stack):
        """A fully filled order stays fillable — overfills are a macro the
        engine must be able to produce. Leaves stays 0 and status Filled."""
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.accept_order("S1", "C100")
        await engine.fill_order("S1", "C100", qty=100, price=150.0)

        await engine.fill_order("S1", "C100", qty=20, price=151.0)
        msg = stub.sent[-1]
        assert msg["150"] == "2"
        assert msg["39"] == "2"
        assert msg["14"] == "120"
        row = await self._order(db)
        assert row["status"] == "Filled"
        assert row["cum_qty"] == 120.0
        assert row["leaves_qty"] == 0.0

    @pytest.mark.asyncio
    async def test_fill_before_accept_consumes_pending_new(self, stack):
        """A fill ER implicitly acknowledges a not-yet-accepted order."""
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.fill_order("S1", "C100", qty=40, price=150.0)
        msg = stub.sent[-1]
        assert msg["11"] == "C100"
        assert msg["150"] == "1"
        row = await self._order(db)
        assert row["status"] == "PartiallyFilled"
        assert row["pending_action"] == "", "the fill consumes the pending New"

    @pytest.mark.asyncio
    async def test_correct_trade(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.accept_order("S1", "C100")
        exec_id = await engine.fill_order("S1", "C100", qty=40, price=150.0)

        new_exec_id = await engine.correct_trade("S1", exec_id, qty=50, price=151.0)
        msg = stub.sent[-1]
        assert msg["20"] == "2"
        assert msg["19"] == exec_id
        assert msg["17"] == new_exec_id
        assert new_exec_id != exec_id, "a correction replaces the ExecID"
        assert msg["32"] == "50"

        row = await self._order(db)
        assert row["cum_qty"] == 50.0
        assert row["leaves_qty"] == 50.0
        assert row["avg_price"] == pytest.approx(151.0)
        assert row["status"] == "PartiallyFilled"

        execs = await _fetch_all(db, "SELECT * FROM fix_executions ORDER BY id")
        assert len(execs) == 1, "a correction is a new version of the trade, not a new row"
        assert execs[0]["exec_type"] == "Correct"
        assert execs[0]["direction"] == "TX"
        assert execs[0]["last_qty"] == 50.0
        assert execs[0]["exec_id"] == new_exec_id
        assert execs[0]["exec_ref_id"] == exec_id
        chain = await _versions(db, "fix_executions")
        assert [(v["_mkio_version"], v["exec_id"], v["exec_type"]) for v in chain] == [
            (1, exec_id, "PartialFill"), (2, new_exec_id, "Correct")]
        assert {v["trade_id"] for v in chain} == {execs[0]["trade_id"]}, \
            "the Trade ID survives a correction"

    @pytest.mark.asyncio
    async def test_bust_trade(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.accept_order("S1", "C100")
        exec_id = await engine.fill_order("S1", "C100", qty=40, price=150.0)

        await engine.bust_trade("S1", exec_id)
        msg = stub.sent[-1]
        assert msg["20"] == "1"
        assert msg["19"] == exec_id
        assert msg["39"] == "0"

        row = await self._order(db)
        assert row["cum_qty"] == 0.0
        assert row["leaves_qty"] == 100.0
        assert row["avg_price"] == 0.0
        assert row["status"] == "New"

        execs = await _fetch_all(db, "SELECT * FROM fix_executions ORDER BY id")
        assert len(execs) == 1, "a bust is a new version of the trade, not a new row"
        assert execs[0]["exec_type"] == "Cancel"
        assert execs[0]["last_qty"] == 40.0
        assert execs[0]["exec_ref_id"] == exec_id
        assert execs[0]["exec_id"] != exec_id, "a bust replaces the ExecID"
        chain = await _versions(db, "fix_executions")
        assert [(v["_mkio_version"], v["exec_type"]) for v in chain] == [
            (1, "PartialFill"), (2, "Cancel")]
        assert {v["trade_id"] for v in chain} == {execs[0]["trade_id"]}, \
            "the Trade ID survives a bust"

    @pytest.mark.asyncio
    async def test_busted_trade_takes_no_further_action(self, stack):
        db, writer, engine = stack
        await self._seed(engine)
        await engine.accept_order("S1", "C100")
        exec_id = await engine.fill_order("S1", "C100", qty=40, price=150.0)
        bust_id = await engine.bust_trade("S1", exec_id)
        for ref in (exec_id, bust_id):
            with pytest.raises(ValueError, match="is busted"):
                await engine.correct_trade("S1", ref, qty=10, price=1.0)
            with pytest.raises(ValueError, match="is busted"):
                await engine.bust_trade("S1", ref)

    @pytest.mark.asyncio
    async def test_earlier_exec_id_in_chain_resolves_through_history(self, stack):
        """After a correction the live row carries the correction's ExecID; a
        bust naming the original fill's ExecID still lands on the same trade."""
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.accept_order("S1", "C100")
        fill_id = await engine.fill_order("S1", "C100", qty=40, price=150.0)
        correct_id = await engine.correct_trade("S1", fill_id, qty=50, price=151.0)
        second = await engine.correct_trade("S1", fill_id, qty=60, price=152.0)
        msg = stub.sent[-1]
        assert msg["19"] == fill_id
        assert (await self._order(db))["cum_qty"] == 60.0

        bust_id = await engine.bust_trade("S1", correct_id)
        assert stub.sent[-1]["19"] == correct_id
        execs = await _fetch_all(db, "SELECT * FROM fix_executions")
        assert len(execs) == 1
        assert execs[0]["exec_id"] == bust_id
        assert execs[0]["exec_type"] == "Cancel"
        chain = await _versions(db, "fix_executions")
        assert [v["exec_id"] for v in chain] == [fill_id, correct_id, second, bust_id]
        assert [v["exec_ref_id"] for v in chain] == ["", fill_id, fill_id, correct_id]
        row = await self._order(db)
        assert row["cum_qty"] == 0.0
        assert row["status"] == "New"

    @pytest.mark.asyncio
    async def test_correct_rejects_nonpositive_qty(self, stack):
        db, writer, engine = stack
        await self._seed(engine)
        await engine.accept_order("S1", "C100")
        exec_id = await engine.fill_order("S1", "C100", qty=40, price=150.0)
        with pytest.raises(ValueError):
            await engine.correct_trade("S1", exec_id, qty=0, price=150.0)

    @pytest.mark.asyncio
    async def test_correct_and_bust_require_known_execution(self, stack):
        db, writer, engine = stack
        await self._seed(engine)
        with pytest.raises(ValueError, match="Unknown execution"):
            await engine.correct_trade("S1", "NOPE", qty=10, price=1.0)
        with pytest.raises(ValueError, match="Unknown execution"):
            await engine.bust_trade("S1", "NOPE")

    @pytest.mark.asyncio
    async def test_bust_of_partial_leaves_remainder(self, stack):
        """Busting one of two fills reverses only that fill's quantity."""
        db, writer, engine = stack
        await self._seed(engine)
        await engine.accept_order("S1", "C100")
        first = await engine.fill_order("S1", "C100", qty=40, price=150.0)
        await engine.fill_order("S1", "C100", qty=60, price=151.0)

        await engine.bust_trade("S1", first)
        row = await self._order(db)
        assert row["cum_qty"] == 60.0
        assert row["leaves_qty"] == 40.0
        assert row["status"] == "PartiallyFilled"
        assert row["avg_price"] == pytest.approx(151.0)

    @pytest.mark.asyncio
    async def test_actions_require_active_session(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        stub.is_active = False
        with pytest.raises(ValueError, match="not active"):
            await engine.accept_order("S1", "C100")

    @pytest.mark.asyncio
    async def test_actions_require_known_order(self, stack):
        db, writer, engine = stack
        await self._seed(engine)
        with pytest.raises(ValueError, match="Unknown order"):
            await engine.accept_order("S1", "NOPE")


class LiveQuery:
    """A subscriber to one of mkfix.toml's query services, run against the
    test stack the way the server runs it, collecting what it is sent."""

    def __init__(self, db, writer, name):
        from mkio.services.query import QueryService
        self.svc = QueryService(config=CONFIG["services"][name], db=db,
                                change_bus=writer._bus, writer=writer)
        self.svc.name = name
        self.sent: list[bytes] = []
        self.closed = False
        # Re-queries of the whole sql after a change to a watched table.
        self.requeries = 0
        real = self.svc._on_secondary

        async def counting(event):
            self.requeries += 1
            await real(event)
        self.svc._on_secondary = counting

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def __aenter__(self):
        await self.svc.start()
        await self.svc.on_subscribe(self, {"type": "subscribe"})
        self.snapshot = self.messages()[0]["rows"]
        self.sent.clear()
        return self

    async def __aexit__(self, *exc):
        await self.svc.stop()

    def messages(self):
        from mkio._json import loads
        return [loads(b) for b in self.sent]

    async def updates(self):
        await asyncio.sleep(0.15)
        out = [(m["op"], m["row"]) for m in self.messages() if m.get("type") == "update"]
        self.sent.clear()
        return out


class TestLiveStatusJoin:
    """The blotters read a session's live status through mkio 0.8 joined
    queries on fix_session_state instead of mirror columns: a state write
    reaches the sessions blotter as an update to the session row, and a
    status transition reaches every order and trade of that session as an
    update carrying session_status — while a seq-num-only write, which
    changes nothing the orders query selects, publishes nothing to it."""

    @pytest.mark.asyncio
    async def test_session_row_follows_its_state(self, stack):
        db, writer, engine = stack
        await _add_session(writer)
        async with LiveQuery(db, writer, "sessions_query") as q:
            [row] = q.snapshot
            assert (row["status"], row["tx_seq_num"], row["_mkio_row"]) == ("DOWN", 1, "S1"), \
                "a session without a state row still shows, as DOWN"
            await engine.update_session_state(
                "S1", {"status": "ACTIVE", "tx_seq_num": 7, "rx_seq_num": 9, "error_text": ""})
            [(op, row)] = await q.updates()
            assert op == "update"
            assert (row["status"], row["tx_seq_num"], row["rx_seq_num"], row["_mkio_row"]) == \
                ("ACTIVE", 7, 9, "S1")
            assert row["host"] == "", "the config columns ride along"
            await engine.update_session_state("S1", {"tx_seq_num": 8})
            [(op, row)] = await q.updates()
            assert (op, row["tx_seq_num"], q.requeries) == ("update", 8, 2), \
                "the sessions blotter shows the counters, so every state write re-queries its two rows"

    @pytest.mark.asyncio
    async def test_orders_and_trades_follow_their_sessions_status(self, stack):
        db, writer, engine = stack
        s1, s2 = StubSession("S1"), StubSession("S2")
        engine.sessions["S1"], engine.sessions["S2"] = s1, s2
        await engine.update_session_state("S1", {"status": "ACTIVE"})
        await engine.update_session_state("S2", {"status": "ACTIVE"})
        await engine.on_app_message(s1, "D", parse_fix(NEW_ORDER_RX))
        await engine.on_app_message(s2, "D", parse_fix(NEW_ORDER_RX))
        await engine.fill_order("S1", "C100", qty=40, price=150.0)

        async with LiveQuery(db, writer, "orders_query") as orders, \
                   LiveQuery(db, writer, "executions_query") as trades:
            assert {r["session_id"]: r["session_status"] for r in orders.snapshot} == \
                {"S1": "ACTIVE", "S2": "ACTIVE"}
            [trade] = trades.snapshot
            assert (trade["session_status"], trade["_mkio_row"]) == ("ACTIVE", str(trade["id"]))

            await engine.update_session_state("S1", {"status": "DOWN"})
            [(op, row)] = await orders.updates()
            assert (op, row["session_id"], row["session_status"]) == ("update", "S1", "DOWN")
            assert row["_mkio_row"] == str(row["id"]), \
                "the row identity stays the primary key the history block keys by"
            assert row["cl_ord_id"] == "C100", "the order's own columns ride along"
            [(op, row)] = await trades.updates()
            assert (op, row["session_status"]) == ("update", "DOWN")
            assert (orders.requeries, trades.requeries) == (1, 1)

            for n in range(3):
                await engine.update_session_state("S1", {"tx_seq_num": 5 + n, "rx_seq_num": 7 + n})
            assert await orders.updates() == [], "a seq-num write changes nothing the query shows"
            assert await trades.updates() == []
            assert (orders.requeries, trades.requeries) == (1, 1), \
                "and, status being the only watched column, is dropped before any re-query"

            await engine.update_session_state("S1", {"status": "ACTIVE"})
            assert [(op, r["session_status"]) for op, r in await orders.updates()] == [("update", "ACTIVE")]
            assert (orders.requeries, trades.requeries) == (2, 2)

    @pytest.mark.asyncio
    async def test_a_new_order_carries_its_sessions_status(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        async with LiveQuery(db, writer, "orders_query") as orders:
            await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
            [(op, row)] = await orders.updates()
            assert (op, row["session_status"]) == ("insert", "DOWN"), \
                "no state row yet: the join defaults, the insert still shows"
            await engine.update_session_state("S1", {"status": "ACTIVE"})
            [(op, row)] = await orders.updates()
            assert (op, row["session_status"]) == ("update", "ACTIVE")


CANCEL_REQ_RX = "8=FIX.4.2|35=F|11=C101|41=C100|55=AAPL|54=1|38=100"
REPLACE_REQ_RX = "8=FIX.4.2|35=G|11=C102|41=C100|55=AAPL|54=1|38=200|40=2|44=151.5"
REPLACE_LOW_RX = "8=FIX.4.2|35=G|11=C103|41=C100|55=AAPL|54=1|38=30|40=2|44=151.5"


class TestCancelReplaceRequests:
    async def _seed(self, engine):
        """A received order, accepted, so it is working (status New)."""
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        await engine.accept_order("S1", "C100")
        return stub

    async def _order(self, db):
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert len(rows) == 1
        return rows[0]

    @pytest.mark.asyncio
    async def test_cancel_request_parks_pending(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.on_app_message(stub, "F", parse_fix(CANCEL_REQ_RX))
        row = await self._order(db)
        assert row["pending_action"] == "Cancel"
        assert row["pending_cl_ord_id"] == "C101"
        assert row["status"] == "New", "a request must not disturb the working order"
        assert row["leaves_qty"] == 100.0

    @pytest.mark.asyncio
    async def test_replace_request_parks_pending(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.on_app_message(stub, "G", parse_fix(REPLACE_REQ_RX))
        row = await self._order(db)
        assert row["pending_action"] == "Replace"
        assert row["pending_cl_ord_id"] == "C102"
        assert row["pending_qty"] == 200.0
        assert row["pending_price"] == 151.5
        assert row["order_qty"] == 100.0, "requested terms apply only on accept"
        assert row["price"] == 150.25

    @pytest.mark.asyncio
    async def test_accept_cancel(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.on_app_message(stub, "F", parse_fix(CANCEL_REQ_RX))
        order_id_before = (await self._order(db))["order_id"]
        exec_id = await engine.accept_cancel("S1", "C100")
        msg = stub.sent[-1]
        assert msg["35"] == "8"
        assert msg["150"] == "4"
        assert msg["39"] == "4"
        assert msg["17"] == exec_id
        assert msg["11"] == "C101", "ER answers the cancel request's ClOrdID"
        assert msg["41"] == "C100", "ER references the superseded ClOrdID"
        assert msg["151"] == "0"
        row = await self._order(db)
        assert row["cl_ord_id"] == "C101", "the accepted cancel re-identifies the chain"
        assert row["orig_cl_ord_id"] == "C100"
        assert row["order_id"] == order_id_before, "the Order ID never changes"
        assert row["status"] == "Canceled"
        assert row["leaves_qty"] == 0.0
        assert row["pending_action"] == ""
        assert row["pending_cl_ord_id"] == ""

    @pytest.mark.asyncio
    async def test_accept_replace_after_partial_fill(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.fill_order("S1", "C100", qty=40, price=150.0)
        await engine.on_app_message(stub, "G", parse_fix(REPLACE_REQ_RX))
        order_id_before = (await self._order(db))["order_id"]
        exec_id = await engine.accept_replace("S1", "C100")
        msg = stub.sent[-1]
        assert msg["150"] == "5"
        assert msg["39"] == "5"
        assert msg["17"] == exec_id
        assert msg["11"] == "C102", "ER answers the replace request's ClOrdID"
        assert msg["41"] == "C100", "ER references the superseded ClOrdID"
        assert msg["38"] == "200"
        assert msg["44"] == "151.5"
        assert msg["151"] == "160"
        row = await self._order(db)
        assert row["cl_ord_id"] == "C102", "the accepted replace re-identifies the chain"
        assert row["orig_cl_ord_id"] == "C100"
        assert row["order_id"] == order_id_before, "the Order ID never changes"
        assert row["status"] == "Replaced"
        assert row["order_qty"] == 200.0
        assert row["price"] == 151.5
        assert row["cum_qty"] == 40.0
        assert row["leaves_qty"] == 160.0
        assert row["pending_action"] == ""
        assert row["pending_qty"] == 0.0

    @pytest.mark.asyncio
    async def test_correct_and_bust_after_replace_use_latest_cl_ord_id(self, stack):
        """An accepted replace renames the chain; a later correction or bust of
        a pre-replace fill must still resolve the order (by its immutable
        Order ID) and report under the chain's latest ClOrdID."""
        db, writer, engine = stack
        stub = await self._seed(engine)
        exec_id = await engine.fill_order("S1", "C100", qty=40, price=150.0)
        await engine.on_app_message(stub, "G", parse_fix(REPLACE_REQ_RX))
        await engine.accept_replace("S1", "C100")
        row = await self._order(db)
        assert row["cl_ord_id"] == "C102"

        new_exec_id = await engine.correct_trade("S1", exec_id, qty=50, price=151.0)
        msg = stub.sent[-1]
        assert msg["20"] == "2"
        assert msg["19"] == exec_id
        assert msg["11"] == "C102", "the correction reports the chain's latest ClOrdID"
        assert msg["37"] == row["order_id"]
        execs = await _fetch_all(db, "SELECT * FROM fix_executions ORDER BY id")
        assert [e["cl_ord_id"] for e in execs] == ["C102"], \
            "the trade row now reports under the chain's latest ClOrdID"
        row = await self._order(db)
        assert row["cum_qty"] == 50.0
        assert row["leaves_qty"] == 150.0

        await engine.bust_trade("S1", new_exec_id)
        msg = stub.sent[-1]
        assert msg["20"] == "1"
        assert msg["11"] == "C102", "the bust reports the chain's latest ClOrdID"
        row = await self._order(db)
        assert row["cum_qty"] == 0.0
        assert row["leaves_qty"] == 200.0
        assert row["status"] == "New"

    @pytest.mark.asyncio
    async def test_accept_replace_after_full_fill_revives_order(self, stack):
        """Replacing a filled order up past its executed quantity brings it
        back to working — the Sent Orders Replace-on-Filled flow."""
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.fill_order("S1", "C100", qty=100, price=150.0)
        assert (await self._order(db))["status"] == "Filled"

        await engine.on_app_message(stub, "G", parse_fix(REPLACE_REQ_RX))
        await engine.accept_replace("S1", "C100")
        row = await self._order(db)
        assert row["status"] == "Replaced"
        assert row["order_qty"] == 200.0
        assert row["cum_qty"] == 100.0
        assert row["leaves_qty"] == 100.0

        await engine.fill_order("S1", "C102", qty=100, price=151.0)
        row = await self._order(db)
        assert row["status"] == "Filled"
        assert row["cum_qty"] == 200.0

    @pytest.mark.asyncio
    async def test_fill_while_request_pending_keeps_it_parked(self, stack):
        """Fills stay possible while a cancel/replace awaits action, and answer
        the current (last accepted) ClOrdID, not the request's."""
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.on_app_message(stub, "G", parse_fix(REPLACE_REQ_RX))
        await engine.fill_order("S1", "C100", qty=40, price=150.0)
        msg = stub.sent[-1]
        assert msg["11"] == "C100", "fill uses the last accepted ClOrdID"
        row = await self._order(db)
        assert row["pending_action"] == "Replace", "the request stays parked"
        assert row["pending_cl_ord_id"] == "C102"

    @pytest.mark.asyncio
    async def test_fill_after_accepted_replace(self, stack):
        """An accepted replace re-identifies the chain; later fills answer the
        new ClOrdID and the order stays fillable."""
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.on_app_message(stub, "G", parse_fix(REPLACE_REQ_RX))
        await engine.accept_replace("S1", "C100")
        await engine.fill_order("S1", "C102", qty=50, price=151.0)
        msg = stub.sent[-1]
        assert msg["11"] == "C102", "fill uses the last accepted ClOrdID"
        assert msg["150"] == "1"
        row = await self._order(db)
        assert row["status"] == "PartiallyFilled"
        assert row["cum_qty"] == 50.0
        assert row["leaves_qty"] == 150.0

    @pytest.mark.asyncio
    async def test_accept_replace_below_cum_is_rejected(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.fill_order("S1", "C100", qty=40, price=150.0)
        await engine.on_app_message(stub, "G", parse_fix(REPLACE_LOW_RX))
        with pytest.raises(ValueError, match="below executed"):
            await engine.accept_replace("S1", "C100")
        row = await self._order(db)
        assert row["pending_action"] == "Replace", "failed accept leaves the request pending"
        assert row["order_qty"] == 100.0

    @pytest.mark.asyncio
    async def test_reject_cancel_request(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.on_app_message(stub, "F", parse_fix(CANCEL_REQ_RX))
        await engine.reject_cancel("S1", "C100", text="too late")
        msg = stub.sent[-1]
        assert msg["35"] == "9"
        assert msg["434"] == "1"
        assert msg["11"] == "C101", "reject answers the request's ClOrdID"
        assert msg["41"] == "C100"
        assert msg["39"] == "0", "OrdStatus code for the order's live status (New)"
        assert msg["58"] == "too late"
        row = await self._order(db)
        assert row["status"] == "New", "rejected request leaves the order untouched"
        assert row["pending_action"] == ""

    @pytest.mark.asyncio
    async def test_reject_replace_request_sets_response_to(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.on_app_message(stub, "G", parse_fix(REPLACE_REQ_RX))
        await engine.reject_cancel("S1", "C100")
        msg = stub.sent[-1]
        assert msg["35"] == "9"
        assert msg["434"] == "2"
        row = await self._order(db)
        assert row["pending_action"] == ""
        assert row["order_qty"] == 100.0

    @pytest.mark.asyncio
    async def test_unknown_order_request_auto_rejected(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        unknown = "8=FIX.4.2|35=F|11=C201|41=NOPE|55=AAPL|54=1"
        await engine.on_app_message(stub, "F", parse_fix(unknown))
        msg = stub.sent[-1]
        assert msg["35"] == "9"
        assert msg["11"] == "C201"
        assert msg["41"] == "NOPE"
        assert "Unknown order" in msg["58"]
        assert (msg["39"], msg["102"]) == ("8", "1"), \
            "UnknownOrder: the 39=8 describes no order of the sender's, who then keeps its own status"
        row = await self._order(db)
        assert row["pending_action"] == "", "the known order is untouched"

    @pytest.mark.asyncio
    async def test_actions_require_pending_request(self, stack):
        db, writer, engine = stack
        await self._seed(engine)
        with pytest.raises(ValueError, match="No pending cancel"):
            await engine.accept_cancel("S1", "C100")
        with pytest.raises(ValueError, match="No pending replace"):
            await engine.accept_replace("S1", "C100")
        with pytest.raises(ValueError, match="No pending"):
            await engine.reject_cancel("S1", "C100")

    @pytest.mark.asyncio
    async def test_fill_still_allowed_while_request_pending(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.on_app_message(stub, "F", parse_fix(CANCEL_REQ_RX))
        await engine.fill_order("S1", "C100", qty=40, price=150.0)
        row = await self._order(db)
        assert row["status"] == "PartiallyFilled"
        assert row["pending_action"] == "Cancel", "pending request survives a fill"

    @pytest.mark.asyncio
    async def test_accept_request_routes_by_pending_action(self, stack):
        """Each accepted request moves the chain to its ClOrdID, so the next
        request (and the accept) must address the chain by its current ID."""
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))

        await engine.accept_request("S1", "C100")
        assert stub.sent[-1]["150"] == "0", "pending New accepts the order"

        await engine.on_app_message(stub, "G", parse_fix(REPLACE_REQ_RX))
        await engine.accept_request("S1", "C100")
        assert stub.sent[-1]["150"] == "5", "pending Replace accepts the replace"

        cancel_after_replace = "8=FIX.4.2|35=F|11=C103|41=C102|55=AAPL|54=1|38=200"
        await engine.on_app_message(stub, "F", parse_fix(cancel_after_replace))
        await engine.accept_request("S1", "C102")
        assert stub.sent[-1]["150"] == "4", "pending Cancel accepts the cancel"

        row = await self._order(db)
        assert row["cl_ord_id"] == "C103", "the chain ends on the cancel request's ClOrdID"

        with pytest.raises(ValueError, match="Nothing pending"):
            await engine.accept_request("S1", "C103")

    @pytest.mark.asyncio
    async def test_reject_request_routes_by_pending_action(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))

        await engine.reject_request("S1", "C100", text="no thanks")
        msg = stub.sent[-1]
        assert msg["35"] == "8" and msg["39"] == "8", "pending New rejects the order"
        row = await self._order(db)
        assert row["status"] == "Rejected"
        assert row["pending_action"] == ""

        with pytest.raises(ValueError, match="Nothing pending"):
            await engine.reject_request("S1", "C100")

    @pytest.mark.asyncio
    async def test_reject_request_routes_cancel_to_cancel_reject(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.on_app_message(stub, "F", parse_fix(CANCEL_REQ_RX))
        await engine.reject_request("S1", "C100", text="too late")
        msg = stub.sent[-1]
        assert msg["35"] == "9", "pending Cancel rejects via OrderCancelReject"
        assert msg["434"] == "1"
        row = await self._order(db)
        assert row["status"] == "New"
        assert row["pending_action"] == ""


CLIENT_FILL_RX = (
    "8=FIX.4.2|35=8|11=C1|37=O1|17=E1|20=0|150=2|39=2|55=AAPL|54=1|"
    "38=100|32=100|31=150|14=100|6=150|151=0"
)

CLIENT_BUST_RX = (
    "8=FIX.4.2|35=8|11=C1|37=O1|17=E2|19=E1|20=1|150=1|39=0|55=AAPL|54=1|"
    "38=100|32=100|31=150|14=0|6=0|151=100"
)


class TestUnsolicitedCancel:
    """The market side cancels a received order nobody asked to cancel: an
    ExecutionReport(Canceled) under the order's own ClOrdID, no OrigClOrdID."""

    async def _seed(self, engine, stub=None, order=NEW_ORDER_RX, accept=True):
        stub = stub or StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(order))
        if accept:
            await engine.accept_order("S1", "C100")
        return stub

    async def _order(self, db):
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert len(rows) == 1
        return rows[0]

    @pytest.mark.asyncio
    async def test_cancels_under_the_orders_own_id(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.fill_order("S1", "C100", qty=40, price=150.0)
        before = await self._order(db)
        exec_id = await engine.unsolicited_cancel("S1", "C100", text="halted")
        msg = stub.sent[-1]
        assert (msg["35"], msg["150"], msg["39"], msg["20"]) == ("8", "4", "4", "0")
        assert msg["17"] == exec_id and exec_id.startswith("EX")
        assert msg["11"] == "C100"
        assert msg["41"] is None, "no request is being answered"
        assert msg["37"] == before["order_id"]
        assert float(msg["14"]) == 40.0 and float(msg["6"]) == 150.0
        assert float(msg["151"]) == 0.0
        assert msg["58"] == "halted"
        row = await self._order(db)
        assert row["cl_ord_id"] == "C100", "nothing re-identifies the chain"
        assert row["order_id"] == before["order_id"]
        assert row["status"] == "Canceled"
        assert row["leaves_qty"] == 0.0
        assert row["cum_qty"] == 40.0
        assert row["sent_text"] == "halted"
        execs = await _fetch_all(db, "SELECT * FROM fix_executions")
        assert len(execs) == 1, "a cancel is no trade"

    @pytest.mark.asyncio
    async def test_fix44_withholds_exec_trans_type(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine, stub=_stub44(), order=NEW_ORDER_44_RX)
        await engine.unsolicited_cancel("S1", "C100")
        msg = stub.sent[-1]
        assert (msg["150"], msg["39"]) == ("4", "4")
        assert msg["20"] is None
        assert msg["41"] is None

    @pytest.mark.asyncio
    async def test_consumes_a_pending_new(self, stack):
        db, writer, engine = stack
        await self._seed(engine, accept=False)
        assert (await self._order(db))["pending_action"] == "New"
        await engine.unsolicited_cancel("S1", "C100")
        row = await self._order(db)
        assert row["status"] == "Canceled"
        assert row["pending_action"] == "", "the report implicitly acknowledges the order"
        assert row["pending_extra_tags"] == ""
        assert row["order_id"].startswith("OR")

    @pytest.mark.asyncio
    async def test_pending_request_stays_parked_and_can_be_rejected(self, stack):
        """Too late to cancel: the client's request is still there to answer
        with an OrderCancelReject after the order went away."""
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.on_app_message(stub, "F", parse_fix(CANCEL_REQ_RX))
        await engine.unsolicited_cancel("S1", "C100")
        row = await self._order(db)
        assert row["status"] == "Canceled"
        assert row["pending_action"] == "Cancel"
        assert row["pending_cl_ord_id"] == "C101"
        assert row["cl_ord_id"] == "C100"

        await engine.reject_request("S1", "C100", text="too late")
        reject = stub.sent[-1]
        assert reject["35"] == "9"
        assert (reject["11"], reject["41"]) == ("C101", "C100")
        row = await self._order(db)
        assert row["pending_action"] == ""
        assert row["status"] == "Canceled"

    @pytest.mark.asyncio
    async def test_extra_tags_and_client_ride_on_the_report(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine, order=NEW_ORDER_RX + "|109=ACME")
        await engine.unsolicited_cancel("S1", "C100", extra_tags="378=4|5001=X")
        msg = stub.sent[-1]
        assert msg.extra == [("378", "4"), ("5001", "X")]
        assert msg["109"] == "ACME"

    @pytest.mark.asyncio
    async def test_requires_an_active_session_and_a_known_order(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        with pytest.raises(ValueError):
            await engine.unsolicited_cancel("S1", "NOPE")
        stub.is_active = False
        stub.status = "DOWN"
        with pytest.raises(ValueError):
            await engine.unsolicited_cancel("S1", "C100")

    @pytest.mark.asyncio
    async def test_extra_58_overrides_the_text_recorded(self, stack):
        db, writer, engine = stack
        await self._seed(engine)
        await engine.unsolicited_cancel("S1", "C100", text="typed", extra_tags="58=from extras")
        assert (await self._order(db))["sent_text"] == "from extras"

    @pytest.mark.asyncio
    async def test_fix40_withholds_exec_type_and_leaves(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        stub.dictionary = FixDictionary("FIX.4.0")
        stub.factory = FixMessageFactory(stub.dictionary, "MKT", "CLIENT")
        await self._seed(engine, stub=stub, order=NEW_ORDER_RX.replace("FIX.4.2", "FIX.4.0"))
        await engine.unsolicited_cancel("S1", "C100")
        msg = stub.sent[-1]
        assert msg["39"] == "4" and msg["20"] == "0"
        assert msg["150"] is None and msg["151"] is None
        assert (await self._order(db))["status"] == "Canceled"

    @pytest.mark.asyncio
    async def test_is_one_version_of_the_order(self, stack):
        db, writer, engine = stack
        await self._seed(engine)
        before = len(await _versions(db, "fix_orders"))
        await engine.unsolicited_cancel("S1", "C100")
        chain = await _versions(db, "fix_orders")
        assert len(chain) == before + 1
        assert chain[-1]["status"] == "Canceled"
        assert len({v["id"] for v in chain}) == 1, "same row throughout"

    @pytest.mark.asyncio
    async def test_terms_save_as_an_unsolicited_template(self, stack):
        db, writer, engine = stack
        await engine.save_template("unsolicited", "halt", text="halted", extra_tags="378=4")
        rows = await _fetch_all(db, "SELECT * FROM fix_templates")
        assert [(r["scope"], r["name"], r["text"], r["extra_tags"]) for r in rows] == \
            [("unsolicited", "halt", "halted", "378=4")]

    @pytest.mark.asyncio
    async def test_client_side_takes_it_without_a_rename(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        cl_ord_id = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
        er = (f"8=FIX.4.2|35=8|11={cl_ord_id}|37=MKT1|17=E9|20=0|150=4|39=4|"
              "55=AAPL|54=1|38=100|14=0|6=0|151=0|58=halted")
        await engine.on_app_message(stub, "8", parse_fix(er))
        row = await self._order(db)
        assert row["cl_ord_id"] == cl_ord_id
        assert row["status"] == "Canceled"
        assert row["leaves_qty"] == 0.0
        assert row["text"] == "halted"


class TestRestatement:
    """The market side restates a received order's terms unasked: an
    ExecutionReport(Restated, 150=D) under the order's own ClOrdID with the
    new OrderQty/Price and ExecRestatementReason(378)."""

    async def _seed(self, engine, stub=None, order=NEW_ORDER_RX, accept=True):
        stub = stub or StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(order))
        if accept:
            await engine.accept_order("S1", "C100")
        return stub

    async def _order(self, db):
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert len(rows) == 1
        return rows[0]

    @pytest.mark.asyncio
    async def test_restates_terms_under_the_orders_own_id(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        before = await self._order(db)
        exec_id = await engine.restate_order("S1", "C100", qty=80, price=149.5, reason="3",
                                             text="repriced")
        msg = stub.sent[-1]
        assert (msg["35"], msg["150"], msg["20"], msg["39"]) == ("8", "D", "0", "0")
        assert msg["17"] == exec_id and exec_id.startswith("EX")
        assert msg["11"] == "C100"
        assert msg["41"] is None, "no request is being answered"
        assert msg["37"] == before["order_id"]
        assert (msg["38"], msg["44"], msg["378"]) == ("80", "149.5", "3")
        assert float(msg["151"]) == 80.0 and float(msg["14"]) == 0.0
        assert float(msg["32"]) == 0.0, "a restatement is no fill"
        assert msg["58"] == "repriced"
        row = await self._order(db)
        assert row["cl_ord_id"] == "C100", "nothing re-identifies the chain"
        assert row["order_id"] == before["order_id"]
        assert (row["order_qty"], row["price"], row["leaves_qty"]) == (80.0, 149.5, 80.0)
        assert row["status"] == "New"
        assert row["sent_text"] == "repriced"
        assert await _fetch_all(db, "SELECT * FROM fix_executions") == [], "and no trade"

    @pytest.mark.asyncio
    async def test_status_is_the_working_status(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.fill_order("S1", "C100", qty=40, price=150.0)
        await engine.restate_order("S1", "C100", qty=60, price=150.25, reason="5")
        msg = stub.sent[-1]
        assert (msg["39"], msg["38"], msg["14"], msg["151"]) == ("1", "60", "40", "20")
        assert float(msg["6"]) == 150.0
        row = await self._order(db)
        assert (row["status"], row["cum_qty"], row["leaves_qty"]) == ("PartiallyFilled", 40.0, 20.0)

        await engine.restate_order("S1", "C100", qty=40, price=150.25, reason="5")
        assert stub.sent[-1]["39"] == "2", "restated down to what is done"
        row = await self._order(db)
        assert (row["status"], row["leaves_qty"]) == ("Filled", 0.0)

    @pytest.mark.asyncio
    async def test_quantity_must_be_positive_and_cover_what_is_done(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.fill_order("S1", "C100", qty=40, price=150.0)
        sent = len(stub.sent)
        with pytest.raises(ValueError, match="below executed"):
            await engine.restate_order("S1", "C100", qty=30, price=150.0)
        with pytest.raises(ValueError, match="positive"):
            await engine.restate_order("S1", "C100", qty=0, price=150.0)
        assert len(stub.sent) == sent
        assert (await self._order(db))["order_qty"] == 100.0

    @pytest.mark.asyncio
    async def test_blank_reason_and_price_are_withheld(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.restate_order("S1", "C100", qty=80)
        msg = stub.sent[-1]
        assert msg["378"] is None and msg["44"] is None
        row = await self._order(db)
        assert (row["order_qty"], row["price"]) == (80.0, 150.25), "the row keeps its price"

    @pytest.mark.asyncio
    async def test_fix44_withholds_exec_trans_type(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine, stub=_stub44(), order=NEW_ORDER_44_RX)
        await engine.restate_order("S1", "C100", qty=80, price=149.5, reason="99")
        msg = stub.sent[-1]
        assert (msg["150"], msg["39"], msg["378"]) == ("D", "0", "99")
        assert msg["20"] is None and msg["41"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", ["FIX.4.0", "FIX.4.1"])
    async def test_refused_where_restated_is_undefined(self, stack, version):
        db, writer, engine = stack
        stub = StubSession()
        stub.dictionary = FixDictionary(version)
        stub.factory = FixMessageFactory(stub.dictionary, "MKT", "CLIENT")
        await self._seed(engine, stub=stub, order=NEW_ORDER_RX.replace("FIX.4.2", version))
        sent = len(stub.sent)
        with pytest.raises(ValueError, match="150=D"):
            await engine.restate_order("S1", "C100", qty=80, price=149.5, reason="3")
        assert len(stub.sent) == sent
        assert (await self._order(db))["order_qty"] == 100.0

    @pytest.mark.asyncio
    async def test_consumes_a_pending_new(self, stack):
        db, writer, engine = stack
        await self._seed(engine, accept=False)
        await engine.restate_order("S1", "C100", qty=80, price=149.5, reason="5")
        row = await self._order(db)
        assert row["pending_action"] == "", "the report implicitly acknowledges the order"
        assert row["status"] == "New"
        assert row["order_id"].startswith("OR")

    @pytest.mark.asyncio
    async def test_pending_request_stays_parked(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        await engine.on_app_message(stub, "G", parse_fix(REPLACE_REQ_RX))
        await engine.restate_order("S1", "C100", qty=80, price=149.5, reason="3")
        row = await self._order(db)
        assert (row["pending_action"], row["pending_cl_ord_id"]) == ("Replace", "C102")
        assert (row["pending_qty"], row["pending_price"]) == (200.0, 151.5)
        assert row["order_qty"] == 80.0

        await engine.accept_request("S1", "C100")
        row = await self._order(db)
        assert (row["cl_ord_id"], row["order_qty"], row["price"]) == ("C102", 200.0, 151.5)

    @pytest.mark.asyncio
    async def test_extras_client_and_text_override(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine, order=NEW_ORDER_RX + "|109=ACME")
        await engine.restate_order("S1", "C100", qty=80, price=149.5, reason="3",
                                   text="typed", extra_tags="58=from extras|5001=X|378=4")
        msg = stub.sent[-1]
        assert msg.extra == [("58", "from extras"), ("5001", "X"), ("378", "4")]
        assert msg["109"] == "ACME"
        assert (await self._order(db))["sent_text"] == "from extras"

    @pytest.mark.asyncio
    async def test_is_one_version_of_the_order(self, stack):
        db, writer, engine = stack
        await self._seed(engine)
        before = len(await _versions(db, "fix_orders"))
        await engine.restate_order("S1", "C100", qty=80, price=149.5, reason="3")
        chain = await _versions(db, "fix_orders")
        assert len(chain) == before + 1
        assert (chain[-1]["order_qty"], chain[-1]["price"]) == (80.0, 149.5)
        assert (chain[-2]["order_qty"], chain[-2]["price"]) == (100.0, 150.25)
        assert len({v["id"] for v in chain}) == 1, "same row throughout"

    @pytest.mark.asyncio
    async def test_requires_an_active_session_and_a_known_order(self, stack):
        db, writer, engine = stack
        stub = await self._seed(engine)
        with pytest.raises(ValueError):
            await engine.restate_order("S1", "NOPE", qty=80, price=149.5)
        stub.is_active = False
        stub.status = "DOWN"
        with pytest.raises(ValueError):
            await engine.restate_order("S1", "C100", qty=80, price=149.5)

    @pytest.mark.asyncio
    async def test_terms_save_as_a_restate_template(self, stack):
        db, writer, engine = stack
        await engine.save_template("restate", "reprice", qty="", price="149.5", restate_reason="3",
                                   text="repriced", extra_tags="5001=X")
        rows = await _fetch_all(db, "SELECT * FROM fix_templates")
        assert [(r["scope"], r["name"], r["qty"], r["price"], r["restate_reason"], r["text"])
                for r in rows] == [("restate", "reprice", "", "149.5", "3", "repriced")]

    @pytest.mark.asyncio
    async def test_client_side_takes_the_new_terms(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        cl_ord_id = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
        er = (f"8=FIX.4.2|35=8|11={cl_ord_id}|37=MKT1|17=E9|20=0|150=D|39=0|378=3|"
              "55=AAPL|54=1|38=80|44=149.5|32=0|31=0|14=0|6=0|151=80|58=repriced")
        await engine.on_app_message(stub, "8", parse_fix(er))
        row = await self._order(db)
        assert row["cl_ord_id"] == cl_ord_id
        assert (row["status"], row["order_qty"], row["price"], row["leaves_qty"]) == \
            ("New", 80.0, 149.5, 80.0)
        assert (row["entered_qty"], row["entered_price"]) == (100.0, 150.0), \
            "the as-submitted terms are the client's own"
        assert row["text"] == "repriced"
        assert await _fetch_all(db, "SELECT * FROM fix_executions") == [], "a restatement is no trade"


class TestDkTrade:
    """DK answers a received trade with DontKnowTrade (35=Q) naming the
    counterparty's identifiers; the trade's terms stay as received and the
    row records the dispute — DKReason and Text as sent."""

    async def _fill(self, engine):
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "8", parse_fix(CLIENT_FILL_RX))
        return stub

    @pytest.mark.asyncio
    async def test_dk_sends_q_for_the_received_execution(self, stack):
        db, writer, engine = stack
        stub = await self._fill(engine)
        await engine.dk_trade("S1", "E1", "B", text="wrong side", extra_tags="5001=X")
        msg = stub.sent[-1]
        assert msg["35"] == "Q"
        assert msg["37"] == "O1", "OrderID is the counterparty's, from the ER"
        assert msg["17"] == "E1"
        assert msg["127"] == "B"
        assert msg["55"] == "AAPL"
        assert msg["54"] == "1"
        assert msg["38"] == "100"
        assert msg["32"] == "100"
        assert msg["31"] == "150.0"
        assert msg["58"] == "wrong side"
        assert msg.extra == [("5001", "X")]

    @pytest.mark.asyncio
    async def test_dk_marks_the_trade_and_nothing_else(self, stack):
        db, writer, engine = stack
        await self._fill(engine)
        before = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        orders_before = await _fetch_all(db, "SELECT * FROM fix_orders")
        await engine.dk_trade("S1", "E1", "D", text="not ours")
        after = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert (after["dk_reason"], after["dk_text"]) == ("NoMatchingOrder", "not ours")
        changed = {c for c in after if after[c] != before[c]} - {"_mkio_ref", "_mkio_version"}
        assert changed == {"dk_reason", "dk_text"}, "the trade's terms stay as received"
        assert await _fetch_all(db, "SELECT * FROM fix_orders") == orders_before
        chain = await _versions(db, "fix_executions")
        assert [v["dk_reason"] for v in chain] == ["", "NoMatchingOrder"], "the dispute is a row version"

    @pytest.mark.asyncio
    async def test_dk_is_recorded_as_sent(self, stack):
        # Extras win on the wire, so they win on the row.
        db, writer, engine = stack
        stub = await self._fill(engine)
        await engine.dk_trade("S1", "E1", "D", text="typed", extra_tags="127=B|58=")
        msg = engine._as_sent(stub, stub.sent[-1])
        assert msg["127"] == "B" and "58" not in msg.fields
        row = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert (row["dk_reason"], row["dk_text"]) == ("WrongSide", "")

    @pytest.mark.asyncio
    async def test_second_dk_overwrites_the_first(self, stack):
        db, writer, engine = stack
        await self._fill(engine)
        await engine.dk_trade("S1", "E1", "D", text="first")
        await engine.dk_trade("S1", "E1", "C", text="")
        row = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert (row["dk_reason"], row["dk_text"]) == ("QuantityExceedsOrder", "")

    @pytest.mark.asyncio
    async def test_their_correction_clears_our_dk(self, stack):
        db, writer, engine = stack
        stub = await self._fill(engine)
        await engine.dk_trade("S1", "E1", "E", text="bad px")
        correct = ("8=FIX.4.2|35=8|11=C1|37=O1|17=E2|19=E1|20=2|150=2|39=2|55=AAPL|54=1|"
                   "38=100|32=100|31=149|14=100|6=149|151=0")
        await engine.on_app_message(stub, "8", parse_fix(correct))
        row = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert (row["exec_id"], row["last_price"]) == ("E2", 149.0)
        assert (row["dk_reason"], row["dk_text"]) == ("", ""), \
            "the DK answered an ExecID the row no longer carries"

    @pytest.mark.asyncio
    async def test_renotified_fill_is_a_new_trade(self, stack):
        # A fresh ExecID and no ExecRefID name no trade of ours, and nothing
        # is inferred: the disputed ExecID stays disputed beside the new report.
        db, writer, engine = stack
        stub = await self._fill(engine)
        await engine.dk_trade("S1", "E1", "E")
        await engine.on_app_message(stub, "8", parse_fix(CLIENT_FILL_RX.replace("17=E1", "17=E3")))
        rows = await _fetch_all(db, "SELECT * FROM fix_executions ORDER BY id")
        assert [(r["exec_id"], r["dk_reason"]) for r in rows] == [("E1", "PriceExceedsLimit"), ("E3", "")]
        assert rows[0]["trade_id"] != rows[1]["trade_id"]

    @pytest.mark.asyncio
    async def test_failed_dk_leaves_no_mark(self, stack):
        db, writer, engine = stack
        stub = await self._fill(engine)

        async def refuse(msg):
            raise ConnectionError("socket closed")
        stub.send_message = refuse
        with pytest.raises(ConnectionError):
            await engine.dk_trade("S1", "E1", "D", text="never sent")
        row = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert (row["dk_reason"], row["dk_text"]) == ("", "")

    @pytest.mark.asyncio
    async def test_mark_is_written_before_the_send(self, stack):
        # Their correction can arrive inside send_message; written after it,
        # our mark would land on the corrected row the DK never answered.
        db, writer, engine = stack
        stub = await self._fill(engine)
        seen = []
        plain_send = stub.send_message

        async def spy(msg):
            seen.append((await _fetch_all(db, "SELECT dk_reason FROM fix_executions"))[0]["dk_reason"])
            return await plain_send(msg)
        stub.send_message = spy
        await engine.dk_trade("S1", "E1", "D")
        assert seen == ["NoMatchingOrder"]

    @pytest.mark.asyncio
    async def test_dk_of_a_sent_trade_never_arms_renotify(self, stack):
        # On a sent trade dk_reason is the counterparty's DK and gates Re-notify.
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        await engine.accept_order("S1", "C100")
        exec_id = await engine.fill_order("S1", "C100", 100, 150.25)
        await engine.dk_trade("S1", exec_id, "D")
        assert stub.sent[-1]["35"] == "Q"
        row = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert row["direction"] == "TX" and row["dk_reason"] == ""

    @pytest.mark.asyncio
    async def test_dk_order_qty_survives_a_renamed_chain(self, stack):
        """The fill-time ClOrdID no longer names an order row after an
        accepted replace; the ER's own CumQty + LeavesQty is the OrderQty."""
        db, writer, engine = stack
        stub = await self._fill(engine)
        replaced = ("8=FIX.4.2|35=8|11=C2|41=C1|37=O1|17=E2|20=0|150=5|39=5|55=AAPL|54=1|"
                    "38=250|44=151|14=100|6=150|151=150")
        await engine.on_app_message(stub, "8", parse_fix(replaced))
        await engine.dk_trade("S1", "E1", "C")
        assert stub.sent[-1]["38"] == "100"

    @pytest.mark.asyncio
    async def test_dk_requires_reason_known_execution_and_active_session(self, stack):
        db, writer, engine = stack
        stub = await self._fill(engine)
        with pytest.raises(ValueError, match="reason"):
            await engine.dk_trade("S1", "E1", "")
        with pytest.raises(ValueError, match="Unknown execution"):
            await engine.dk_trade("S1", "NOPE", "D")
        stub.is_active = False
        with pytest.raises(ValueError, match="not active"):
            await engine.dk_trade("S1", "E1", "D")
        assert [m["35"] for m in stub.sent] == []


class TestInboundDk:
    """A counterparty's DontKnowTrade (35=Q) marks the sent trade whose
    ExecID(17) it names: DKReason(127) and Text(58) land on the row as a new
    version, the trade's own terms untouched."""

    async def _fill(self, engine):
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        await engine.accept_order("S1", "C100")
        exec_id = await engine.fill_order("S1", "C100", qty=40, price=150.0)
        return stub, exec_id

    async def _dk(self, engine, stub, exec_id, reason="B", text="wrong side"):
        raw = f"8=FIX.4.2|35=Q|37=OR1|17={exec_id}|127={reason}|55=AAPL|54=1|38=100|32=40|31=150"
        if text:
            raw += f"|58={text}"
        await engine.on_app_message(stub, "Q", parse_fix(raw))

    @pytest.mark.asyncio
    async def test_dk_marks_the_sent_trade(self, stack):
        db, writer, engine = stack
        stub, exec_id = await self._fill(engine)
        before = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        await self._dk(engine, stub, exec_id)
        rows = await _fetch_all(db, "SELECT * FROM fix_executions")
        assert len(rows) == 1
        row = rows[0]
        assert row["dk_reason"] == "WrongSide"
        assert row["dk_text"] == "wrong side"
        untouched = {k: v for k, v in row.items()
                     if k not in ("dk_reason", "dk_text", "_mkio_version", "_mkio_ref")}
        assert untouched == {k: v for k, v in before.items() if k in untouched}
        assert row["exec_type"] == "PartialFill"
        chain = await _versions(db, "fix_executions")
        assert [v["dk_reason"] for v in chain] == ["", "WrongSide"]
        assert len(stub.sent) == 2, "a DK draws no answer"

    @pytest.mark.asyncio
    async def test_dk_reason_falls_back_to_the_code_and_text_is_optional(self, stack):
        db, writer, engine = stack
        stub, exec_id = await self._fill(engine)
        await self._dk(engine, stub, exec_id, reason="9", text="")
        row = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert row["dk_reason"] == "9"
        assert row["dk_text"] == ""

    @pytest.mark.asyncio
    async def test_dk_of_the_fill_after_a_correction_lands_on_the_corrected_trade(self, stack):
        db, writer, engine = stack
        stub, fill_id = await self._fill(engine)
        correct_id = await engine.correct_trade("S1", fill_id, qty=50, price=151.0)
        await self._dk(engine, stub, fill_id, reason="D")
        rows = await _fetch_all(db, "SELECT * FROM fix_executions")
        assert len(rows) == 1
        assert rows[0]["exec_id"] == correct_id
        assert rows[0]["dk_reason"] == "NoMatchingOrder"

    @pytest.mark.asyncio
    async def test_a_correction_or_bust_clears_the_dk(self, stack):
        """The DK answered an ExecID the row no longer carries; the trade's
        new report stands on its own, the DK'd version kept in history."""
        db, writer, engine = stack
        stub, fill_id = await self._fill(engine)
        await self._dk(engine, stub, fill_id)
        correct_id = await engine.correct_trade("S1", fill_id, qty=50, price=151.0)
        row = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert (row["exec_id"], row["dk_reason"], row["dk_text"]) == (correct_id, "", "")
        await self._dk(engine, stub, correct_id, reason="A")
        await engine.bust_trade("S1", correct_id)
        row = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert (row["exec_type"], row["dk_reason"]) == ("Cancel", "")
        chain = await _versions(db, "fix_executions")
        assert [v["dk_reason"] for v in chain] == ["", "WrongSide", "", "UnknownSymbol", ""]

    @pytest.mark.asyncio
    async def test_dk_still_allows_a_correction_and_a_bust(self, stack):
        db, writer, engine = stack
        stub, fill_id = await self._fill(engine)
        await self._dk(engine, stub, fill_id)
        correct_id = await engine.correct_trade("S1", fill_id, qty=50, price=151.0)
        await engine.bust_trade("S1", correct_id)
        assert stub.sent[-1]["19"] == correct_id

    @pytest.mark.asyncio
    async def test_dk_naming_nothing_or_a_received_trade_writes_nothing(self, stack):
        db, writer, engine = stack
        stub, exec_id = await self._fill(engine)
        await engine.on_app_message(stub, "8", parse_fix(CLIENT_FILL_RX))
        before = await _fetch_all(db, "SELECT * FROM fix_executions ORDER BY id")
        assert [r["direction"] for r in before] == ["TX", "RX"]
        await self._dk(engine, stub, "NOPE")
        await self._dk(engine, stub, "E1")
        assert await _fetch_all(db, "SELECT * FROM fix_executions ORDER BY id") == before


class TestRenotify:
    """Re-notify answers a DontKnowTrade: the trade's current report — fill,
    correction or bust — goes out again under a fresh ExecID, the trade's
    terms as the row holds them and the order's state as it stands now. The
    row becomes a new version under the new ExecID with the DK cleared."""

    _fill = TestInboundDk._fill
    _dk = TestInboundDk._dk

    @pytest.mark.asyncio
    async def test_a_dked_fill_goes_out_again_under_a_new_exec_id(self, stack):
        db, writer, engine = stack
        stub, fill_id = await self._fill(engine)
        await self._dk(engine, stub, fill_id)
        before = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        order_before = await _fetch_all(db, "SELECT * FROM fix_orders")
        new_id = await engine.renotify_trade("S1", fill_id)
        assert new_id not in ("", fill_id)

        fill, sent = stub.sent[-2], stub.sent[-1]
        assert sent["17"] == new_id
        assert "19" not in sent.fields and "97" not in sent.fields
        for tag in ("35", "37", "11", "20", "150", "39", "55", "54", "38", "32", "31", "14", "6", "151"):
            assert sent[tag] == fill[tag], tag

        rows = await _fetch_all(db, "SELECT * FROM fix_executions")
        assert len(rows) == 1
        row = rows[0]
        assert (row["id"], row["trade_id"]) == (before["id"], before["trade_id"])
        assert (row["exec_id"], row["exec_ref_id"], row["exec_type"]) == (new_id, "", "PartialFill")
        assert (row["last_qty"], row["last_price"], row["cum_qty"]) == (40, 150.0, 40)
        assert (row["dk_reason"], row["dk_text"]) == ("", "")
        chain = await _versions(db, "fix_executions")
        assert [(v["exec_id"], v["dk_reason"]) for v in chain] == [
            (fill_id, ""), (fill_id, "WrongSide"), (new_id, "")]
        assert await _fetch_all(db, "SELECT * FROM fix_orders") == order_before, \
            "the DK never moved our book, so neither does the re-notification"

    @pytest.mark.asyncio
    async def test_the_order_is_reported_as_it_stands_now(self, stack):
        """A fill that landed after the DK'd one must not be reported backwards."""
        db, writer, engine = stack
        stub, fill_id = await self._fill(engine)
        await self._dk(engine, stub, fill_id)
        await engine.fill_order("S1", "C100", qty=60, price=151.0)
        await engine.renotify_trade("S1", fill_id)
        sent = stub.sent[-1]
        assert (sent["32"], sent["31"]) == ("40", "150.0")
        assert (sent["14"], sent["151"], sent["39"], sent["150"]) == ("100", "0", "2", "2")
        assert float(sent["6"]) == pytest.approx(150.6)
        rows = await _fetch_all(db, "SELECT * FROM fix_executions ORDER BY id")
        assert [(r["last_qty"], r["cum_qty"], r["exec_type"]) for r in rows] == [
            (40, 100, "Fill"), (60, 100, "Fill")]

    @pytest.mark.asyncio
    async def test_a_dked_correction_is_restated_with_its_original_reference(self, stack):
        db, writer, engine = stack
        stub, fill_id = await self._fill(engine)
        correct_id = await engine.correct_trade("S1", fill_id, qty=50, price=151.0)
        await self._dk(engine, stub, correct_id, reason="C")
        new_id = await engine.renotify_trade("S1", correct_id)
        sent = stub.sent[-1]
        assert (sent["17"], sent["19"], sent["20"]) == (new_id, fill_id, "2")
        assert (sent["32"], sent["31"], sent["14"]) == ("50", "151.0", "50")
        row = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert (row["exec_id"], row["exec_ref_id"], row["exec_type"], row["dk_reason"]) == (
            new_id, fill_id, "Correct", "")

    @pytest.mark.asyncio
    async def test_a_dked_bust_is_restated_and_the_trade_stays_busted(self, stack):
        db, writer, engine = stack
        stub, fill_id = await self._fill(engine)
        bust_id = await engine.bust_trade("S1", fill_id)
        await self._dk(engine, stub, bust_id, reason="Z")
        new_id = await engine.renotify_trade("S1", bust_id)
        sent = stub.sent[-1]
        assert (sent["17"], sent["19"], sent["20"]) == (new_id, fill_id, "1")
        assert (sent["32"], sent["14"], sent["151"], sent["39"]) == ("40", "0", "100", "0")
        row = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert (row["exec_id"], row["exec_type"], row["dk_reason"]) == (new_id, "Cancel", "")
        with pytest.raises(ValueError, match="busted"):
            await engine.correct_trade("S1", new_id, qty=10, price=150.0)

    @pytest.mark.asyncio
    async def test_fix44_restates_by_exec_type(self, stack):
        db, writer, engine = stack
        stub = _stub44()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX.replace("FIX.4.2", "FIX.4.4")))
        await engine.accept_order("S1", "C100")
        fill_id = await engine.fill_order("S1", "C100", qty=40, price=150.0)
        await self._dk(engine, stub, fill_id)
        refill_id = await engine.renotify_trade("S1", fill_id)
        assert "20" not in stub.sent[-1].fields
        assert (stub.sent[-1]["150"], stub.sent[-1]["39"]) == ("F", "1")
        bust_id = await engine.bust_trade("S1", refill_id)
        await self._dk(engine, stub, bust_id)
        await engine.renotify_trade("S1", bust_id)
        assert (stub.sent[-1]["150"], stub.sent[-1]["19"]) == ("H", refill_id)
        row = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert row["exec_type"] == "TradeCancel"

    @pytest.mark.asyncio
    async def test_a_second_dk_rearms_and_an_old_exec_id_still_finds_the_trade(self, stack):
        db, writer, engine = stack
        stub, fill_id = await self._fill(engine)
        await self._dk(engine, stub, fill_id)
        second = await engine.renotify_trade("S1", fill_id)
        with pytest.raises(ValueError, match="has not been DK'ed"):
            await engine.renotify_trade("S1", second)
        await self._dk(engine, stub, second, reason="D")
        third = await engine.renotify_trade("S1", fill_id)
        assert len({fill_id, second, third}) == 3
        await engine.bust_trade("S1", fill_id)
        rows = await _fetch_all(db, "SELECT * FROM fix_executions")
        assert len(rows) == 1
        assert (rows[0]["exec_type"], rows[0]["exec_ref_id"]) == ("Cancel", fill_id)

    @pytest.mark.asyncio
    async def test_refused_without_a_dk_an_execution_or_an_active_session(self, stack):
        db, writer, engine = stack
        stub, fill_id = await self._fill(engine)
        with pytest.raises(ValueError, match="has not been DK'ed"):
            await engine.renotify_trade("S1", fill_id)
        with pytest.raises(ValueError, match="Unknown execution"):
            await engine.renotify_trade("S1", "NOPE")
        await self._dk(engine, stub, fill_id)
        sent = len(stub.sent)
        stub.is_active = False
        with pytest.raises(ValueError):
            await engine.renotify_trade("S1", fill_id)
        assert len(stub.sent) == sent
        row = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert (row["exec_id"], row["dk_reason"]) == (fill_id, "WrongSide")

    @pytest.mark.asyncio
    async def test_after_a_replace_it_goes_out_under_the_current_cl_ord_id(self, stack):
        db, writer, engine = stack
        stub, fill_id = await self._fill(engine)
        await self._dk(engine, stub, fill_id)
        replace_req = "8=FIX.4.2|35=G|11=C200|41=C100|55=AAPL|54=1|38=200|40=2|44=151.0"
        await engine.on_app_message(stub, "G", parse_fix(replace_req))
        await engine.accept_replace("S1", "C100")
        await engine.renotify_trade("S1", fill_id)
        sent = stub.sent[-1]
        assert (sent["11"], sent["38"], sent["151"]) == ("C200", "200", "160")

    @pytest.mark.asyncio
    async def test_extras_can_recast_a_dked_correction_as_a_fill(self, stack):
        """20=0|19= restates the corrected terms as a plain fill, for a
        counterparty that never knew the fill the correction referenced; the
        row records the kind and reference as sent."""
        db, writer, engine = stack
        stub = RecordingStub(engine)
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        await engine.accept_order("S1", "C100")
        fill_id = await engine.fill_order("S1", "C100", qty=40, price=150.0)
        correct_id = await engine.correct_trade("S1", fill_id, qty=50, price=151.0)
        await self._dk(engine, stub, fill_id, reason="D")
        new_id = await engine.renotify_trade("S1", correct_id, extra_tags="20=0|19=")
        wire = parse_fix(stub.sent[-1].to_wire_string())
        assert (wire["17"], wire["20"], wire["32"]) == (new_id, "0", "50")
        assert "19" not in wire.fields
        row = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert (row["exec_id"], row["exec_ref_id"], row["exec_type"]) == (new_id, "", "PartialFill")


class RecordingStub(StubSession):
    """A StubSession that sends the way FixSession does — sendprep (so
    extras apply and the wire pairs exist) and record the message — and
    records what it receives, so fix_messages fills as on a live session."""

    def __init__(self, engine, session_id="S1"):
        super().__init__(session_id)
        self.engine = engine
        self._seq = 1

    async def send_message(self, msg):
        msg.sendprep(self.dictionary, self.factory.sender, self.factory.target, self._seq)
        self._seq += 1
        self.sent.append(msg)
        await self.engine.record_message(self.session_id, "TX", msg)
        return msg

    async def receive(self, raw):
        msg = parse_fix(raw)
        await self.engine.record_message(self.session_id, "RX", msg)
        await self.engine.on_app_message(self, msg["35"], msg)


class TestClientColumn:
    """Orders, trades and messages carry the client they name, read and
    written through the owning session's client tags (fix_sessions
    .client_tags; blank = the default chain). A sent order stamps the
    dialog's client on the session's tag, an extra tag naming that tag
    wins, and a trade inherits its order's client when its ExecutionReport
    names none."""

    async def _stub(self, engine, version="FIX.4.2", client_tags=""):
        stub = RecordingStub(engine)
        stub.dictionary = FixDictionary(version)
        stub.factory = FixMessageFactory(stub.dictionary, "MKT", "CLIENT")
        stub.config = {"client_tags": client_tags}
        engine.sessions["S1"] = stub
        return stub

    async def _rows(self, db, table, where=""):
        return await _fetch_all(db, f"SELECT * FROM {table} {where} ORDER BY id")

    @pytest.mark.asyncio
    async def test_sent_order_stamps_the_client_and_records_it(self, stack):
        db, writer, engine = stack
        stub = await self._stub(engine)
        cl_ord_id = await engine.send_new_order("S1", "AAPL", "1", 100, client="ACME")
        msg = stub.sent[-1]
        assert msg["109"] == "ACME", "the default chain's first 4.2 tag"
        assert "448" not in msg.fields
        order = (await self._rows(db, "fix_orders"))[0]
        assert order["client"] == "ACME"
        recorded = (await self._rows(db, "fix_messages"))[0]
        assert recorded["client"] == "ACME"

        await engine.send_cancel_replace("S1", cl_ord_id, "AAPL", "1", 120, client="ACME2")
        assert stub.sent[-1]["109"] == "ACME2"
        assert (await self._rows(db, "fix_orders"))[0]["client"] == "ACME", "not until it is accepted"
        await _accept_replace(engine, stub, cl_ord_id)
        assert (await self._rows(db, "fix_orders"))[0]["client"] == "ACME2"
        await engine.send_cancel("S1", cl_ord_id, "AAPL", "1", 120, client="ACME2")
        assert stub.sent[-1]["109"] == "ACME2"
        assert [r["client"] for r in await self._rows(db, "fix_messages")] == ["ACME", "ACME2", "ACME2"]

    @pytest.mark.asyncio
    async def test_extra_tag_overrides_the_dialog_client(self, stack):
        db, writer, engine = stack
        stub = await self._stub(engine)
        await engine.send_new_order("S1", "AAPL", "1", 100, client="ACME", extra_tags="109=OTHER")
        assert stub.sent[-1]["109"] == "OTHER"
        assert (await self._rows(db, "fix_orders"))[0]["client"] == "OTHER"
        assert (await self._rows(db, "fix_messages"))[0]["client"] == "OTHER"

    @pytest.mark.asyncio
    async def test_session_client_tags_pick_the_tag(self, stack):
        db, writer, engine = stack
        stub = await self._stub(engine, client_tags="115")
        await engine.send_new_order("S1", "AAPL", "1", 100, client="HUB")
        msg = stub.sent[-1]
        assert msg["115"] == "HUB" and "109" not in msg.fields
        assert (await self._rows(db, "fix_orders"))[0]["client"] == "HUB"
        # a received order naming the client elsewhere is not recognized here
        await stub.receive(NEW_ORDER_RX + "|109=ACME")
        assert (await self._rows(db, "fix_orders", "WHERE direction = 'RX'"))[0]["client"] == ""

    @pytest.mark.asyncio
    async def test_custom_and_malformed_client_tags(self, stack):
        db, writer, engine = stack
        stub = await self._stub(engine, client_tags="5001")
        await engine.send_new_order("S1", "AAPL", "1", 100, client="ACME")
        assert stub.sent[-1]["5001"] == "ACME"
        stub.config["client_tags"] = "109;115"
        await engine.send_new_order("S1", "AAPL", "1", 100, client="ACME")
        assert stub.sent[-1]["109"] == "ACME", "a malformed setting falls back to the default chain"

    @pytest.mark.asyncio
    async def test_fix44_stamps_a_parties_group(self, stack):
        db, writer, engine = stack
        stub = await self._stub(engine, version="FIX.4.4")
        await engine.send_new_order("S1", "AAPL", "1", 100, client="ACME")
        pairs = [(t, v) for t, v in stub.sent[-1]._pairs if t in ("453", "448", "447", "452")]
        assert pairs == [("453", "1"), ("448", "ACME"), ("447", "D"), ("452", "3")]
        assert (await self._rows(db, "fix_orders"))[0]["client"] == "ACME"
        received = ("8=FIX.4.4|35=D|11=C7|55=AAPL|54=1|38=100|40=2|44=150|59=0|"
                    "453=2|448=BROKER|447=D|452=1|448=CUST|447=D|452=3")
        await stub.receive(received)
        rx = (await self._rows(db, "fix_orders", "WHERE direction = 'RX'"))[0]
        assert rx["client"] == "CUST"
        await engine.accept_order("S1", "C7")
        pairs = [(t, v) for t, v in stub.sent[-1]._pairs if t in ("448", "452")]
        assert pairs == [("448", "CUST"), ("452", "3")], "the ER answers with the order's client"
        # the Accept dialog prefills Extra Tags with the request's tags, the
        # Parties group among them: echoed as given, not stamped a second time
        await engine.fill_order("S1", "C7", qty=100, price=150.0, extra_tags=rx["pending_extra_tags"])
        er = stub.sent[-1]
        assert [v for t, v in er._pairs if t == "453"] == ["2"]
        assert [v for t, v in er._pairs if t == "448"] == ["BROKER", "CUST"]
        assert (await self._rows(db, "fix_messages"))[-1]["client"] == "CUST"
        assert (await self._rows(db, "fix_executions"))[0]["client"] == "CUST"

    @pytest.mark.asyncio
    async def test_received_order_client_flows_to_its_answers_and_trades(self, stack):
        db, writer, engine = stack
        stub = await self._stub(engine)
        await stub.receive(NEW_ORDER_RX + "|109=ACME")
        order = (await self._rows(db, "fix_orders"))[0]
        assert order["client"] == "ACME"
        assert (await self._rows(db, "fix_messages"))[0]["client"] == "ACME"
        await engine.accept_order("S1", "C100")
        assert stub.sent[-1]["109"] == "ACME"
        exec_id = await engine.fill_order("S1", "C100", qty=40, price=150.0)
        assert stub.sent[-1]["109"] == "ACME"
        trade = (await self._rows(db, "fix_executions"))[0]
        assert (trade["exec_id"], trade["client"]) == (exec_id, "ACME")
        await engine.correct_trade("S1", exec_id, qty=50, price=151.0)
        assert stub.sent[-1]["109"] == "ACME"
        assert (await self._rows(db, "fix_executions"))[0]["client"] == "ACME"
        assert all(m["client"] == "ACME" for m in await self._rows(db, "fix_messages"))

    @pytest.mark.asyncio
    async def test_received_trade_inherits_its_order_client(self, stack):
        db, writer, engine = stack
        stub = await self._stub(engine)
        await engine.send_new_order("S1", "AAPL", "1", 100, client="ACME")
        cl_ord_id = (await self._rows(db, "fix_orders"))[0]["cl_ord_id"]
        silent = (f"8=FIX.4.2|35=8|11={cl_ord_id}|37=O1|17=E1|20=0|150=2|39=2|55=AAPL|54=1|"
                  "38=100|32=100|31=150|14=100|6=150|151=0")
        await stub.receive(silent)
        trade = (await self._rows(db, "fix_executions"))[0]
        assert trade["client"] == "ACME", "an ER naming no client takes the order's"
        assert (await self._rows(db, "fix_messages"))[-1]["client"] == ""
        assert (await self._rows(db, "fix_orders"))[0]["client"] == "ACME"

        named = (f"8=FIX.4.2|35=8|11={cl_ord_id}|37=O1|17=E2|20=0|150=1|39=1|55=AAPL|54=1|"
                 "38=100|32=10|31=150|14=110|6=150|151=0|109=THEIRS")
        await stub.receive(named)
        trades = await self._rows(db, "fix_executions")
        assert [t["client"] for t in trades] == ["ACME", "THEIRS"], "an ER's own client wins"
        assert (await self._rows(db, "fix_orders"))[0]["client"] == "ACME", \
            "the order keeps the client it was sent with"

    @pytest.mark.asyncio
    async def test_startup_backfill_seeds_older_rows_once(self, stack):
        """Rows recorded before the column: messages from their wire bytes,
        orders from their NewOrderSingle, trades from their order (through
        history after a rename) or their ExecutionReport."""
        db, writer, engine = stack
        stub = await self._stub(engine)
        await engine.send_new_order("S1", "AAPL", "1", 100, client="ACME")
        sent = (await self._rows(db, "fix_orders"))[0]
        accepted = (f"8=FIX.4.2|35=8|11=RT2|41={sent['cl_ord_id']}|37=O1|17=E1|20=0|150=5|39=0|"
                    "55=AAPL|54=1|38=100|14=0|6=0|151=100")
        await stub.receive(accepted)
        fill = ("8=FIX.4.2|35=8|11=RT2|37=O1|17=E2|20=0|150=2|39=2|55=AAPL|54=1|"
                "38=100|32=100|31=150|14=100|6=150|151=0")
        await stub.receive(fill)
        orphan = ("8=FIX.4.2|35=8|11=NOPE|37=O9|17=E9|20=0|150=2|39=2|55=AAPL|54=1|"
                  "38=10|32=10|31=150|14=10|6=150|151=0|109=ORPHAN")
        await stub.receive(orphan)
        conn = db.write_conn
        await (await conn.execute("UPDATE fix_messages SET client = ''")).close()
        await (await conn.execute("UPDATE fix_orders SET client = ''")).close()
        await (await conn.execute("UPDATE fix_executions SET client = ''")).close()
        await (await conn.execute(
            "INSERT INTO fix_sessions (session_id, sender_comp_id, target_comp_id, client_tags) "
            "VALUES ('S1', 'MKT', 'CLIENT', '')")).close()
        await conn.commit()

        await engine._backfill_client()
        assert [m["client"] for m in await self._rows(db, "fix_messages")] == \
            ["ACME", "", "", "ORPHAN"]
        orders = await self._rows(db, "fix_orders")
        assert [(o["cl_ord_id"], o["client"]) for o in orders] == [("RT2", "ACME"), ("NOPE", "ORPHAN")]
        trades = await self._rows(db, "fix_executions")
        assert [(t["exec_id"], t["client"]) for t in trades] == [("E2", "ACME"), ("E9", "ORPHAN")]

        await (await conn.execute("UPDATE fix_messages SET client = ''")).close()
        await conn.commit()
        await engine._backfill_client()
        assert all(m["client"] == "" for m in await self._rows(db, "fix_messages")), \
            "a second startup does not rescan"
        flag = await _fetch_all(db, "SELECT value FROM fix_settings WHERE key = 'client_backfill'")
        assert flag == [{"value": "1"}]


class TestClientExecutionReports:
    @pytest.mark.asyncio
    async def test_fill_records_tx_order_and_rx_execution(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        await engine.on_app_message(stub, "8", parse_fix(CLIENT_FILL_RX))
        orders = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert orders[0]["direction"] == "TX"
        assert orders[0]["status"] == "Filled"
        execs = await _fetch_all(db, "SELECT * FROM fix_executions")
        assert execs[0]["direction"] == "RX"
        assert execs[0]["exec_type"] == "Fill"

    @pytest.mark.asyncio
    async def test_bust_recorded_via_exec_trans_type(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        await engine.on_app_message(stub, "8", parse_fix(CLIENT_FILL_RX))
        await engine.on_app_message(stub, "8", parse_fix(CLIENT_BUST_RX))
        orders = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert orders[0]["cum_qty"] == 0.0
        execs = await _fetch_all(db, "SELECT * FROM fix_executions ORDER BY id")
        assert [(e["exec_type"], e["exec_type_code"], e["exec_id"], e["exec_ref_id"])
                for e in execs] == [("Cancel", "1", "E2", "E1")], \
            "a bust referencing the fill (tag 19) is a new version of its trade"
        assert execs[0]["trade_id"].startswith("TR")
        chain = await _versions(db, "fix_executions")
        assert [(v["_mkio_version"], v["exec_type"]) for v in chain] == [(1, "Fill"), (2, "Cancel")]
        assert {v["trade_id"] for v in chain} == {execs[0]["trade_id"]}

    @pytest.mark.asyncio
    async def test_unknown_exec_ref_id_starts_a_new_trade(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        await engine.on_app_message(stub, "8", parse_fix(CLIENT_FILL_RX))
        await engine.on_app_message(
            stub, "8", parse_fix(CLIENT_BUST_RX.replace("19=E1", "19=NOPE")))
        execs = await _fetch_all(db, "SELECT * FROM fix_executions ORDER BY id")
        assert [e["exec_type"] for e in execs] == ["Fill", "Cancel"]
        assert execs[0]["trade_id"] != execs[1]["trade_id"]
        assert execs[1]["exec_ref_id"] == "NOPE"

    @pytest.mark.asyncio
    async def test_inbound_reference_to_earlier_exec_id_resolves_through_history(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        await engine.on_app_message(stub, "8", parse_fix(CLIENT_FILL_RX))
        correct = ("8=FIX.4.2|35=8|11=C1|37=O1|17=E2|19=E1|20=2|150=2|39=2|55=AAPL|54=1|"
                   "38=100|32=100|31=149|14=100|6=149|151=0")
        await engine.on_app_message(stub, "8", parse_fix(correct))
        await engine.on_app_message(stub, "8", parse_fix(CLIENT_BUST_RX.replace("17=E2", "17=E3")))
        execs = await _fetch_all(db, "SELECT * FROM fix_executions")
        assert [(e["exec_id"], e["exec_type"]) for e in execs] == [("E3", "Cancel")]
        chain = await _versions(db, "fix_executions")
        assert [v["exec_id"] for v in chain] == ["E1", "E2", "E3"]
        assert len({v["trade_id"] for v in chain}) == 1

    @pytest.mark.asyncio
    async def test_cancel_er_updates_order_but_keeps_price(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        new_er = ("8=FIX.4.2|35=8|11=C1|37=O1|17=E1|20=0|150=0|39=0|55=AAPL|54=1|"
                  "38=100|44=150.25|14=0|6=0|151=100")
        canceled_er = ("8=FIX.4.2|35=8|11=C2|41=C1|37=O1|17=E2|20=0|150=4|39=4|55=AAPL|54=1|"
                       "38=100|14=0|6=0|151=0")
        await engine.on_app_message(stub, "8", parse_fix(new_er))
        await engine.on_app_message(stub, "8", parse_fix(canceled_er))
        orders = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert len(orders) == 1, "cancel ER folds into the original order row"
        assert orders[0]["cl_ord_id"] == "C2", "the chain takes the cancel request's ClOrdID"
        assert orders[0]["orig_cl_ord_id"] == "C1"
        assert orders[0]["status"] == "Canceled"
        assert orders[0]["leaves_qty"] == 0.0
        assert orders[0]["price"] == 150.25, "ER without tag 44 must not zero the price"

    @pytest.mark.asyncio
    async def test_replace_er_updates_qty_and_price(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        new_er = ("8=FIX.4.2|35=8|11=C1|37=O1|17=E1|20=0|150=0|39=0|55=AAPL|54=1|"
                  "38=100|44=150.25|14=0|6=0|151=100")
        replaced_er = ("8=FIX.4.2|35=8|11=C2|41=C1|37=O1|17=E2|20=0|150=5|39=5|55=AAPL|54=1|"
                       "38=200|44=151.5|14=0|6=0|151=200")
        await engine.on_app_message(stub, "8", parse_fix(new_er))
        await engine.on_app_message(stub, "8", parse_fix(replaced_er))
        orders = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert len(orders) == 1
        assert orders[0]["cl_ord_id"] == "C2", "the chain takes the replace request's ClOrdID"
        assert orders[0]["orig_cl_ord_id"] == "C1"
        assert orders[0]["status"] == "Replaced"
        assert orders[0]["order_qty"] == 200.0
        assert orders[0]["price"] == 151.5
        assert orders[0]["leaves_qty"] == 200.0

    @pytest.mark.asyncio
    async def test_rename_skipped_when_target_row_exists(self, stack):
        """If a row already exists under the new ClOrdID, the rename is skipped
        instead of violating the (cl_ord_id, session_id) uniqueness."""
        db, writer, engine = stack
        stub = StubSession()
        new_er_c1 = ("8=FIX.4.2|35=8|11=C1|37=O1|17=E1|20=0|150=0|39=0|55=AAPL|54=1|"
                     "38=100|44=150.25|14=0|6=0|151=100")
        new_er_c2 = ("8=FIX.4.2|35=8|11=C2|37=O2|17=E2|20=0|150=0|39=0|55=MSFT|54=1|"
                     "38=50|44=300|14=0|6=0|151=50")
        canceled_er = ("8=FIX.4.2|35=8|11=C2|41=C1|37=O2|17=E3|20=0|150=4|39=4|55=MSFT|54=1|"
                       "38=50|14=0|6=0|151=0")
        await engine.on_app_message(stub, "8", parse_fix(new_er_c1))
        await engine.on_app_message(stub, "8", parse_fix(new_er_c2))
        await engine.on_app_message(stub, "8", parse_fix(canceled_er))
        orders = await _fetch_all(db, "SELECT * FROM fix_orders ORDER BY cl_ord_id")
        assert len(orders) == 2, "both rows survive; the colliding rename is a no-op"
        assert orders[0]["cl_ord_id"] == "C1"
        assert orders[1]["cl_ord_id"] == "C2"
        assert orders[1]["status"] == "Canceled"


class TestClientSideIds:
    @pytest.mark.asyncio
    async def test_send_new_order_assigns_rt_and_or_ids(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        cl_ord_id = await engine.send_new_order(
            "S1", symbol="AAPL", side="1", qty=100, price=150.0,
        )
        assert cl_ord_id.startswith("RT")
        assert stub.sent[-1]["11"] == cl_ord_id
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert rows[0]["order_id"].startswith("OR")

    @pytest.mark.asyncio
    async def test_market_order_id_does_not_overwrite_ours(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        cl_ord_id = await engine.send_new_order(
            "S1", symbol="AAPL", side="1", qty=100, price=150.0,
        )
        our_order_id = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]["order_id"]
        er = (f"8=FIX.4.2|35=8|11={cl_ord_id}|37=CLMKT100|17=E1|20=0|150=0|39=0|"
              "55=AAPL|54=1|38=100|14=0|6=0|151=100")
        await engine.on_app_message(stub, "8", parse_fix(er))
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert len(rows) == 1
        assert rows[0]["status"] == "New"
        assert rows[0]["order_id"] == our_order_id, \
            "the counterparty's OrderID(37) must not replace our immutable Order ID"

    @pytest.mark.asyncio
    async def test_cancel_and_replace_mint_fresh_rt_ids(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        first = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
        second = await engine.send_cancel_replace(
            "S1", orig_cl_ord_id=first, symbol="AAPL", side="1", qty=200, price=151.0,
        )
        third = await engine.send_cancel(
            "S1", orig_cl_ord_id=second, symbol="AAPL", side="1", qty=200,
        )
        assert len({first, second, third}) == 3
        assert all(i.startswith("RT") for i in (first, second, third))


class EagerSession(StubSession):
    """A counterparty that answers the moment a message hits the wire — the
    inbound ExecutionReport is processed inside send_message, before the
    sending call has written its own row."""

    def __init__(self, engine, session_id="S1"):
        super().__init__(session_id)
        self.engine = engine
        self.answer_to = ""

    async def send_message(self, msg):
        await super().send_message(msg)
        if msg.get("35") != self.answer_to:
            return msg
        if self.answer_to == "D":
            er = (f"8=FIX.4.2|35=8|11={msg.get('11')}|37=ORMKT99|17=E1|20=0|150=8|39=8|"
                  f"55={msg.get('55')}|54={msg.get('54')}|38={msg.get('38')}|14=0|6=0|151=0|58=No")
        else:
            er = (f"8=FIX.4.2|35=8|11={msg.get('11')}|41={msg.get('41')}|37=ORMKT99|17=E2|20=0|"
                  f"150=5|39=5|55={msg.get('55')}|54={msg.get('54')}|38={msg.get('38')}|"
                  f"44={msg.get('44')}|14=0|6=0|151={msg.get('38')}")
        await self.engine.on_app_message(self, "8", parse_fix(er))
        return msg


class InterruptingSession(StubSession):
    """A counterparty whose cancel request lands while the market side's own
    ExecutionReport is still being sent — the inbound 35=F is processed inside
    send_message, before the sending call has written its own row."""

    def __init__(self, engine, session_id="S1"):
        super().__init__(session_id)
        self.engine = engine
        self.interrupt = ""  # ClOrdID the request supersedes

    async def send_message(self, msg):
        await super().send_message(msg)
        if not self.interrupt:
            return msg
        orig, self.interrupt = self.interrupt, ""
        req = (f"8=FIX.4.2|35=F|11=C200|41={orig}|55=AAPL|54=1|38=100|9001=late")
        await self.engine.on_app_message(self, "F", parse_fix(req))
        return msg


class TestRequestDuringMarketSend:
    """A market-side action writes its own row before its ExecutionReport goes
    out: the send awaits, so an inbound cancel request can be parked in between
    and must not be clobbered by the pre-send snapshot."""

    async def _received_order(self, engine, stub):
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        return "C100"

    @pytest.mark.asyncio
    async def test_accept_order_keeps_request_that_arrived_mid_send(self, stack):
        db, writer, engine = stack
        stub = InterruptingSession(engine)
        cl_ord_id = await self._received_order(engine, stub)
        stub.interrupt = cl_ord_id
        await engine.accept_order("S1", cl_ord_id)
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert rows[0]["status"] == "New"
        assert rows[0]["pending_action"] == "Cancel", \
            "the cancel request that arrived during the send must stay parked"
        assert rows[0]["pending_cl_ord_id"] == "C200"
        assert rows[0]["pending_extra_tags"] == "9001=late"

    @pytest.mark.asyncio
    async def test_fill_keeps_request_that_arrived_mid_send(self, stack):
        db, writer, engine = stack
        stub = InterruptingSession(engine)
        cl_ord_id = await self._received_order(engine, stub)
        await engine.accept_order("S1", cl_ord_id)
        stub.interrupt = cl_ord_id
        await engine.fill_order("S1", cl_ord_id, qty=40, price=150.25)
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert rows[0]["cum_qty"] == 40.0, "the fill must still be recorded"
        assert rows[0]["status"] == "PartiallyFilled"
        assert rows[0]["pending_action"] == "Cancel"
        execs = await _fetch_all(db, "SELECT * FROM fix_executions")
        assert len(execs) == 1 and execs[0]["last_qty"] == 40.0

    @pytest.mark.asyncio
    async def test_reject_order_keeps_request_that_arrived_mid_send(self, stack):
        db, writer, engine = stack
        stub = InterruptingSession(engine)
        cl_ord_id = await self._received_order(engine, stub)
        stub.interrupt = cl_ord_id
        await engine.reject_order("S1", cl_ord_id, text="no")
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert rows[0]["status"] == "Rejected"
        assert rows[0]["pending_action"] == "Cancel"

    @pytest.mark.asyncio
    async def test_every_market_action_writes_before_it_sends(self, stack):
        """The invariant behind the three tests above, checked on every
        market-side action: all of an action's writes are submitted before its
        message reaches the wire, so nothing it derived from a pre-send snapshot
        can land on top of what arrived during the send."""
        db, writer, engine = stack
        trace: list[str] = []

        class TracingSession(StubSession):
            async def send_message(self, msg):
                trace.append("sent")
                return await super().send_message(msg)

        stub = TracingSession()
        engine.sessions["S1"] = stub
        real_submit = writer.submit

        async def spy(ops, params_list, data, *a, **kw):
            trace.append("wrote")
            return await real_submit(ops, params_list, data, *a, **kw)

        async def traced(label, coro):
            trace.clear()
            writer.submit = spy
            try:
                result = await coro
            finally:
                writer.submit = real_submit
            assert trace.count("sent") == 1, f"{label}: {trace}"
            assert trace[-1] == "sent", f"{label} sent before writing: {trace}"
            assert "wrote" in trace, f"{label} wrote nothing: {trace}"
            return result

        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        await traced("accept_order", engine.accept_order("S1", "C100"))
        filled = await traced("fill_order",
                              engine.fill_order("S1", "C100", qty=40, price=150.25))
        corrected = await traced("correct_trade",
                                 engine.correct_trade("S1", filled, qty=50, price=150.5))
        busted = await traced("bust_trade", engine.bust_trade("S1", corrected))
        dk = f"8=FIX.4.2|35=Q|37=OR1|17={busted}|127=Z|55=AAPL|54=1|38=100|32=50|31=150.5"
        await engine.on_app_message(stub, "Q", parse_fix(dk))
        await traced("renotify_trade", engine.renotify_trade("S1", busted))

        replace_req = "8=FIX.4.2|35=G|11=C200|41=C100|55=AAPL|54=1|38=200|40=2|44=151.0"
        await engine.on_app_message(stub, "G", parse_fix(replace_req))
        await traced("accept_replace", engine.accept_replace("S1", "C100"))

        cancel_req = "8=FIX.4.2|35=F|11=C300|41=C200|55=AAPL|54=1|38=200"
        await engine.on_app_message(stub, "F", parse_fix(cancel_req))
        await traced("reject_cancel", engine.reject_cancel("S1", "C200", text="no"))

        cancel_again = "8=FIX.4.2|35=F|11=C400|41=C200|55=AAPL|54=1|38=200"
        await engine.on_app_message(stub, "F", parse_fix(cancel_again))
        await traced("accept_cancel", engine.accept_cancel("S1", "C200"))

        second = "8=FIX.4.2|35=D|11=C500|55=AAPL|54=1|38=100|40=2|44=150.25|59=0"
        await engine.on_app_message(stub, "D", parse_fix(second))
        await traced("reject_order", engine.reject_order("S1", "C500", text="no"))

        third = "8=FIX.4.2|35=D|11=C600|55=AAPL|54=1|38=100|40=2|44=150.25|59=0"
        await engine.on_app_message(stub, "D", parse_fix(third))
        await traced("unsolicited_cancel", engine.unsolicited_cancel("S1", "C600"))

        fourth = "8=FIX.4.2|35=D|11=C700|55=AAPL|54=1|38=100|40=2|44=150.25|59=0"
        await engine.on_app_message(stub, "D", parse_fix(fourth))
        await traced("restate_order",
                     engine.restate_order("S1", "C700", qty=80, price=149.5, reason="3"))


class TestAnswerBeforeOwnWrite:
    """A sent order's own row must be written before the message goes out: the
    send awaits, so a fast counterparty's answer can be processed first."""

    @pytest.mark.asyncio
    async def test_reject_answered_during_send_survives(self, stack):
        db, writer, engine = stack
        stub = EagerSession(engine)
        stub.answer_to = "D"
        engine.sessions["S1"] = stub
        await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert len(rows) == 1
        assert rows[0]["status"] == "Rejected", \
            "the reject must not be overwritten by the order's own PendingNew write"
        assert rows[0]["order_id"].startswith("OR"), rows[0]["order_id"]
        assert rows[0]["order_id"] != "ORMKT99", \
            "the counterparty's OrderID(37) must not become our Order ID"

    @pytest.mark.asyncio
    async def test_replace_answered_during_send_keeps_entered_terms(self, stack):
        db, writer, engine = stack
        stub = EagerSession(engine)
        engine.sessions["S1"] = stub
        first = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
        stub.answer_to = "G"
        await engine.send_cancel_replace(
            "S1", orig_cl_ord_id=first, symbol="AAPL", side="1", qty=250, price=151.5,
        )
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert len(rows) == 1
        assert rows[0]["status"] == "Replaced"
        assert rows[0]["entered_qty"] == 250.0, \
            "the Replace dialog's terms must be recorded before the chain is renamed"
        assert rows[0]["entered_price"] == 151.5

    @pytest.mark.asyncio
    async def test_every_sent_side_action_writes_before_it_sends(self, stack):
        """The market side's invariant, checked on the sending side too: all of
        an action's writes are submitted before its message reaches the wire."""
        db, writer, engine = stack
        trace: list[str] = []

        class TracingSession(StubSession):
            async def send_message(self, msg):
                trace.append("sent")
                return await super().send_message(msg)

        stub = TracingSession()
        engine.sessions["S1"] = stub
        real_submit = writer.submit

        async def spy(ops, params_list, data, *a, **kw):
            trace.append("wrote")
            return await real_submit(ops, params_list, data, *a, **kw)

        async def traced(label, coro):
            trace.clear()
            writer.submit = spy
            try:
                result = await coro
            finally:
                writer.submit = real_submit
            assert trace.count("sent") == 1, f"{label}: {trace}"
            assert trace[-1] == "sent", f"{label} sent before writing: {trace}"
            return result

        first = await traced("send_new_order", engine.send_new_order(
            "S1", symbol="AAPL", side="1", qty=100, price=150.0))
        second = await traced("send_cancel_replace", engine.send_cancel_replace(
            "S1", orig_cl_ord_id=first, symbol="AAPL", side="1", qty=200, price=151.0))
        await traced("send_cancel", engine.send_cancel(
            "S1", orig_cl_ord_id=second, symbol="AAPL", side="1", qty=200))
        fill = (f"8=FIX.4.2|35=8|11={second}|37=O1|17=E9|20=0|150=2|39=2|55=AAPL|54=1|"
                "38=200|32=200|31=150|14=200|6=150|151=0")
        await engine.on_app_message(stub, "8", parse_fix(fill))
        await traced("dk_trade", engine.dk_trade("S1", "E9", "D"))

    @pytest.mark.asyncio
    async def test_failed_send_leaves_no_live_order(self, stack):
        db, writer, engine = stack

        class DeadSession(StubSession):
            async def send_message(self, msg):
                raise ConnectionError("socket gone")

        engine.sessions["S1"] = DeadSession()
        with pytest.raises(ConnectionError):
            await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert rows[0]["status"] == "Rejected"
        assert "Send failed" in rows[0]["text"]


class TestExtraTags:
    @pytest.mark.asyncio
    async def test_send_new_order_attaches_extra_pairs(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.send_new_order(
            "S1", symbol="AAPL", side="1", qty=100, price=150.0,
            extra_tags="5001=X|382=2|375=A|375=B",
        )
        assert stub.sent[-1].extra == [
            ("5001", "X"), ("382", "2"), ("375", "A"), ("375", "B"),
        ]

    @pytest.mark.asyncio
    async def test_accept_request_routes_extra_tags(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        await engine.accept_request("S1", "C100", extra_tags="58=custom ack")
        msg = stub.sent[-1]
        assert msg["35"] == "8"
        assert msg.extra == [("58", "custom ack")]

    @pytest.mark.asyncio
    async def test_invalid_extra_tags_rejected_before_send(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        with pytest.raises(ValueError):
            await engine.send_new_order(
                "S1", symbol="AAPL", side="1", qty=100, price=150.0,
                extra_tags="not-a-tag",
            )
        assert stub.sent == []


class TestEnteredTerms:
    """The Replace dialog prefills from the as-submitted terms: what the New
    dialog carried, or the latest Replace dialog — never the counterparty's
    ExecutionReport."""

    @pytest.mark.asyncio
    async def test_send_new_order_records_entered_terms(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.send_new_order(
            "S1", symbol="AAPL", side="1", qty=100, ord_type="2", price=150.0,
            tif="1", extra_tags="5001=X",
        )
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["tif_code"] == "1"
        assert row["time_in_force"] == "GoodTillCancel"
        assert row["extra_tags"] == "5001=X"
        assert row["entered_qty"] == 100.0
        assert row["entered_price"] == 150.0

    @pytest.mark.asyncio
    async def test_market_order_enters_null_price(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, ord_type="1")
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["entered_price"] is None, \
            "a market order's Replace dialog must open with an empty price, not 0"
        assert "44" not in stub.sent[-1].fields

    @pytest.mark.asyncio
    async def test_cancel_replace_sends_tif_only_when_given(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        first = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
        await engine.send_cancel_replace(
            "S1", orig_cl_ord_id=first, symbol="AAPL", side="1", qty=200, price=151.0,
        )
        assert "59" not in stub.sent[-1].fields
        await engine.send_cancel_replace(
            "S1", orig_cl_ord_id=first, symbol="AAPL", side="1", qty=200, price=151.0, tif="3",
        )
        assert stub.sent[-1]["35"] == "G"
        assert stub.sent[-1]["59"] == "3"

    @pytest.mark.asyncio
    async def test_cancel_replace_records_submitted_terms_on_row(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        first = await engine.send_new_order(
            "S1", symbol="AAPL", side="1", qty=100, ord_type="2", price=150.0,
            tif="0", extra_tags="5001=X",
        )
        await engine.send_cancel_replace(
            "S1", orig_cl_ord_id=first, symbol="MSFT", side="2", qty=200, ord_type="4",
            price=151.5, tif="1", extra_tags="5002=Y",
        )
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["cl_ord_id"] == first, "the row keeps its ClOrdID until the replace is accepted"
        assert (row["symbol"], row["entered_qty"], row["extra_tags"]) == ("AAPL", 100.0, "5001=X"), \
            "and its entered terms: a refused replace must not be what the next Replace opens on"
        await _accept_replace(engine, stub, first, echo=False)
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["symbol"] == "MSFT"
        assert row["side_code"] == "2" and row["side"] == "Sell"
        assert row["ord_type_code"] == "4" and row["ord_type"] == "StopLimit"
        assert row["tif_code"] == "1" and row["time_in_force"] == "GoodTillCancel"
        assert row["extra_tags"] == "5002=Y"
        assert row["entered_qty"] == 200.0
        assert row["entered_price"] == 151.5
        assert row["order_qty"] == 100.0 and row["price"] == 150.0, \
            "working terms change only when the counterparty accepts the replace"

    @pytest.mark.asyncio
    async def test_cancel_replace_without_tif_keeps_entered_tif(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        first = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0, tif="4")
        await engine.send_cancel_replace(
            "S1", orig_cl_ord_id=first, symbol="AAPL", side="1", qty=200, price=151.0,
        )
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["tif_code"] == "4" and row["time_in_force"] == "FillOrKill"
        assert row["extra_tags"] == "", "a submitted empty extra_tags is what the next dialog shows"

    @pytest.mark.asyncio
    async def test_cancel_replace_for_unknown_order_still_sends(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        cl_ord_id = await engine.send_cancel_replace(
            "S1", orig_cl_ord_id="NOPE", symbol="AAPL", side="1", qty=200, price=151.0,
        )
        assert stub.sent[-1]["11"] == cl_ord_id
        assert stub.sent[-1]["41"] == "NOPE"
        assert await _fetch_all(db, "SELECT * FROM fix_orders") == []

    @pytest.mark.asyncio
    async def test_execution_reports_never_rewrite_entered_terms(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        first = await engine.send_new_order(
            "S1", symbol="AAPL", side="1", qty=100, price=150.0, tif="1", extra_tags="5001=X",
        )
        second = await engine.send_cancel_replace(
            "S1", orig_cl_ord_id=first, symbol="AAPL", side="1", qty=200, price=151.0, tif="1",
            extra_tags="5001=X",
        )
        replaced_er = (f"8=FIX.4.2|35=8|11={second}|41={first}|37=O1|17=E2|20=0|150=5|39=5|"
                       "55=AAPL|54=1|38=150|44=152|59=3|14=0|6=0|151=150")
        await engine.on_app_message(stub, "8", parse_fix(replaced_er))
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["cl_ord_id"] == second
        assert row["order_qty"] == 150.0 and row["price"] == 152.0, \
            "working terms follow the accepting ER"
        assert row["entered_qty"] == 200.0 and row["entered_price"] == 151.0, \
            "entered terms stay what was submitted"
        assert row["tif_code"] == "1" and row["extra_tags"] == "5001=X"

    @pytest.mark.asyncio
    async def test_expiry_rides_new_order_and_replace(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        first = await engine.send_new_order(
            "S1", symbol="AAPL", side="1", qty=100, price=150.0, tif="6",
            expire_time="20260912-21:00:00", expire_date="20260912",
        )
        assert stub.sent[-1]["126"] == "20260912-21:00:00"
        assert stub.sent[-1]["432"] == "20260912"
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["expire_time"] == "20260912-21:00:00"
        assert row["expire_date"] == "20260912"
        await engine.send_cancel_replace(
            "S1", orig_cl_ord_id=first, symbol="AAPL", side="1", qty=200, price=151.0,
            expire_time="20260913-21:00:00",
        )
        assert stub.sent[-1]["35"] == "G"
        assert stub.sent[-1]["126"] == "20260913-21:00:00"
        assert "432" not in stub.sent[-1].fields, "an empty expiry field sends no tag"
        await _accept_replace(engine, stub, first)
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["expire_time"] == "20260913-21:00:00" and row["expire_date"] == "", \
            "the Replace dialog prefills from what the last Replace submitted"

    def test_dialog_instant_becomes_fix_stamp_at_precision(self):
        from mkfix.fix.message import normalize_expire_time
        iso = "2026-09-12T21:00:00Z"
        assert normalize_expire_time(iso, "second") == "20260912-21:00:00"
        assert normalize_expire_time(iso, "millisecond") == "20260912-21:00:00.000"
        assert normalize_expire_time(iso, "microsecond") == "20260912-21:00:00.000000"
        assert normalize_expire_time(iso, "nanosecond") == "20260912-21:00:00.000000000"
        assert normalize_expire_time(iso, "picosecond") == "20260912-21:00:00.000000000000"
        assert normalize_expire_time("2026-09-12T21:00:00.123Z", "microsecond") == "20260912-21:00:00.123000"
        assert normalize_expire_time("2026-09-12T21:00:00.123456789Z", "millisecond") == "20260912-21:00:00.123"
        assert normalize_expire_time("2026-09-12T17:00:00-04:00", "second") == "20260912-21:00:00"
        assert normalize_expire_time("2026-09-13T02:30+05:30", "second") == "20260912-21:00:00"
        assert normalize_expire_time("2026-09-12 21:00", "second") == "20260912-21:00:00", "naive is UTC"

    def test_fix_stamp_passes_unless_precision_asked(self):
        from mkfix.fix.message import normalize_expire_date, normalize_expire_time
        stamp = "20260912-21:00:00.123456"
        assert normalize_expire_time(stamp, "millisecond", explicit=False) == stamp
        assert normalize_expire_time(stamp, "millisecond") == "20260912-21:00:00.123"
        assert normalize_expire_time(stamp, "second") == "20260912-21:00:00"
        assert normalize_expire_time("whatever", "second") == "whatever"
        assert normalize_expire_time("  ", "second") == ""
        assert normalize_expire_date("2026-09-12") == "20260912"
        assert normalize_expire_date("20260912") == "20260912"
        assert normalize_expire_date("") == ""

    def test_factory_expire_precision_defaults_to_session(self):
        iso = "2026-09-12T21:00:00Z"
        d42 = FixDictionary("FIX.4.2")
        assert FixMessageFactory(d42, "A", "B").expire_time_stamp(iso) == "20260912-21:00:00.000"
        assert FixMessageFactory(FixDictionary("FIX.4.0"), "A", "B").expire_time_stamp(iso) == "20260912-21:00:00"
        micro = FixMessageFactory(d42, "A", "B", timestamp_precision="microsecond")
        assert micro.expire_time_stamp(iso) == "20260912-21:00:00.000000"
        assert micro.expire_time_stamp(iso, "picosecond") == "20260912-21:00:00.000000000000"
        msg = micro.new_order_single("C1", "AAPL", "1", 100, price=1.0, tif="6",
                                     expire_time=iso, expire_date="2026-09-12",
                                     expire_precision="nanosecond")
        assert msg["126"] == "20260912-21:00:00.000000000" and msg["432"] == "20260912"

    def test_bare_date_in_expire_time_is_an_expire_date(self):
        factory = FixMessageFactory(FixDictionary("FIX.4.2"), "A", "B")
        assert factory.expiry("2026-09-12", "", "") == ("", "20260912")
        assert factory.expiry("20260912", "", "") == ("", "20260912")
        assert factory.expiry("2026-09-12", "20260930", "") == ("", "20260930"), \
            "an explicit ExpireDate wins over the dialog's bare date"
        assert factory.expiry("2026-09-12T21:00:00Z", "", "second") == ("20260912-21:00:00", "")
        assert factory.expiry("", "", "") == ("", "")
        msg = factory.new_order_single("C1", "AAPL", "1", 100, price=1.0, tif="6", expire_time="2026-09-12")
        assert "126" not in msg.fields and msg["432"] == "20260912"

    @pytest.mark.asyncio
    async def test_date_only_expiry_sends_and_records_expire_date(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        first = await engine.send_new_order(
            "S1", symbol="AAPL", side="1", qty=100, price=150.0, tif="6",
            expire_time="2026-09-12",
        )
        assert "126" not in stub.sent[-1].fields and stub.sent[-1]["432"] == "20260912"
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert (row["expire_time"], row["expire_date"]) == ("", "20260912")
        await engine.send_cancel_replace(
            "S1", orig_cl_ord_id=first, symbol="AAPL", side="1", qty=200, price=151.0,
            expire_time="2026-09-13T21:00:00Z",
        )
        assert stub.sent[-1]["126"] == "20260913-21:00:00.000" and "432" not in stub.sent[-1].fields
        await _accept_replace(engine, stub, first)
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert (row["expire_time"], row["expire_date"]) == ("20260913-21:00:00.000", ""), \
            "switching from a date to an instant clears the date the Replace prefill would show"

    @pytest.mark.asyncio
    async def test_dialog_expiry_is_recorded_as_sent(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.send_new_order(
            "S1", symbol="AAPL", side="1", qty=100, price=150.0, tif="6",
            expire_time="2026-09-12T21:00:00Z", expire_date="2026-09-12",
            expire_precision="microsecond",
        )
        assert stub.sent[-1]["126"] == "20260912-21:00:00.000000"
        assert stub.sent[-1]["432"] == "20260912"
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["expire_time"] == "20260912-21:00:00.000000", "the column holds the stamp as sent"
        assert row["expire_date"] == "20260912"

    @pytest.mark.asyncio
    async def test_expiry_omitted_when_blank(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
        assert "126" not in stub.sent[-1].fields and "432" not in stub.sent[-1].fields

    def test_expire_date_withheld_before_fix42(self):
        for version, defined in (("FIX.4.0", False), ("FIX.4.1", False), ("FIX.4.2", True)):
            factory = FixMessageFactory(FixDictionary(version), "A", "B")
            msg = factory.new_order_single(
                "C1", "AAPL", "1", 100, price=1.0, tif="6",
                expire_time="20260912-21:00:00", expire_date="20260912",
            )
            assert msg["126"] == "20260912-21:00:00", version
            assert ("432" in msg.fields) is defined, version

    @pytest.mark.asyncio
    async def test_received_order_records_expiry(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        raw = NEW_ORDER_RX + "|126=20260912-21:00:00|432=20260912"
        await engine.on_app_message(stub, "D", parse_fix(raw))
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["expire_time"] == "20260912-21:00:00"
        assert row["expire_date"] == "20260912"

    @pytest.mark.asyncio
    async def test_received_order_records_tif_code(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["tif_code"] == "0"
        assert row["entered_qty"] == 100.0
        assert row["entered_price"] == 150.25

    @pytest.mark.asyncio
    async def test_startup_backfills_legacy_rows_from_working_terms(self, stack):
        db, writer, engine = stack
        legacy = _order_params(cl_ord_id="OLD", order_qty=300.0, price=99.5,
                               entered_qty=0.0, entered_price=None)
        market = _order_params(cl_ord_id="OLDMKT", order_qty=50.0, price=0.0,
                               entered_qty=0.0, entered_price=None)
        fresh = _order_params(cl_ord_id="NEW", order_qty=100.0, price=150.25,
                              entered_qty=200.0, entered_price=151.0)
        for params in (legacy, market, fresh):
            await writer.submit(engine._compiled_ops["upsert_order"], (params,), {})
        await engine._backfill_entered_terms()
        rows = {r["cl_ord_id"]: r for r in await _fetch_all(db, "SELECT * FROM fix_orders")}
        assert rows["OLD"]["entered_qty"] == 300.0 and rows["OLD"]["entered_price"] == 99.5
        assert rows["OLDMKT"]["entered_qty"] == 50.0 and rows["OLDMKT"]["entered_price"] is None
        assert rows["NEW"]["entered_qty"] == 200.0 and rows["NEW"]["entered_price"] == 151.0, \
            "rows that already carry entered terms are left alone"


NEW_ORDER_RX_EXTRAS = ("8=FIX.4.2|35=D|11=C200|55=AAPL|54=1|38=100|40=2|44=150.25|59=0|"
                       "1=ACCT|100=XNAS|382=2|375=A|375=B")


class TestExtraTagEcho:
    """Inbound custom tags land on the order row so the Accept/Reject/Fill
    dialogs can prefill their Extra Tags field and echo them back."""

    @pytest.mark.asyncio
    async def test_received_order_captures_custom_tags_in_order(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX_EXTRAS))
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["extra_tags"] == "1=ACCT|100=XNAS|382=2|375=A|375=B", \
            "duplicates (repeating groups) and order preserved; consumed tags excluded"
        assert row["pending_extra_tags"] == row["extra_tags"], \
            "the pending New echoes the order's own tags"

    @pytest.mark.asyncio
    async def test_received_order_without_custom_tags_captures_none(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["extra_tags"] == ""
        assert row["pending_extra_tags"] == ""

    @pytest.mark.asyncio
    async def test_accept_clears_pending_but_keeps_order_tags(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX_EXTRAS))
        await engine.accept_request("S1", "C200")
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["pending_extra_tags"] == ""
        assert row["extra_tags"] == "1=ACCT|100=XNAS|382=2|375=A|375=B", \
            "the Fill dialog still echoes the order's tags"

    @pytest.mark.asyncio
    async def test_cancel_request_parks_its_own_tags(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX_EXTRAS))
        await engine.accept_request("S1", "C200")
        cancel = "8=FIX.4.2|35=F|11=C201|41=C200|55=AAPL|54=1|38=100|5002=Y"
        await engine.on_app_message(stub, "F", parse_fix(cancel))
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["pending_action"] == "Cancel"
        assert row["pending_extra_tags"] == "5002=Y", "the request's tags, not the order's"
        assert row["extra_tags"] == "1=ACCT|100=XNAS|382=2|375=A|375=B"

    @pytest.mark.asyncio
    async def test_accepted_replace_promotes_request_tags_to_order(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX_EXTRAS))
        await engine.accept_request("S1", "C200")
        replace = ("8=FIX.4.2|35=G|11=C201|41=C200|55=AAPL|54=1|38=200|40=2|44=151|"
                   "5002=Y|375=C")
        await engine.on_app_message(stub, "G", parse_fix(replace))
        await engine.accept_request("S1", "C200")
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["cl_ord_id"] == "C201"
        assert row["status"] == "Replaced"
        assert row["pending_extra_tags"] == ""
        assert row["extra_tags"] == "5002=Y|375=C", \
            "the accepted replace's tags are now the order's — Fill echoes them"

    @pytest.mark.asyncio
    async def test_rejected_request_drops_its_tags_and_keeps_orders(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX_EXTRAS))
        await engine.accept_request("S1", "C200")
        cancel = "8=FIX.4.2|35=F|11=C201|41=C200|55=AAPL|54=1|38=100|5002=Y"
        await engine.on_app_message(stub, "F", parse_fix(cancel))
        await engine.reject_request("S1", "C200", text="no")
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["pending_extra_tags"] == ""
        assert row["extra_tags"] == "1=ACCT|100=XNAS|382=2|375=A|375=B"

    @pytest.mark.asyncio
    async def test_fill_consuming_pending_new_clears_pending_tags(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX_EXTRAS))
        await engine.fill_order("S1", "C200", qty=100, price=150.25)
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["pending_action"] == "" and row["pending_extra_tags"] == ""
        assert row["extra_tags"] == "1=ACCT|100=XNAS|382=2|375=A|375=B"

    @pytest.mark.asyncio
    async def test_dialog_roundtrip_echoes_tags_on_the_wire(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX_EXTRAS))
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        await engine.accept_request("S1", "C200", extra_tags=row["pending_extra_tags"])
        er = stub.sent[-1]
        assert er["35"] == "8"
        assert er.extra == [("1", "ACCT"), ("100", "XNAS"), ("382", "2"),
                            ("375", "A"), ("375", "B")]


class TestRxExtraTagBackfill:
    @staticmethod
    async def _record_raw(engine, writer, session_id, msg_type, cl_ord_id, raw):
        params = (session_id, "20260823-00:00:00.000", "RX", 1, msg_type, "", "APP",
                  raw, "CLIENT", "MKT", cl_ord_id, "", "", "AAPL", "1", len(raw), "000", "", None)
        await writer.submit(engine._compiled_ops["insert_message"], (params,), {})

    @pytest.mark.asyncio
    async def test_backfills_from_recorded_new_order(self, stack):
        db, writer, engine = stack
        legacy = _order_params(cl_ord_id="OLD1", direction="RX", extra_tags="",
                               pending_action="New", pending_extra_tags="")
        await writer.submit(engine._compiled_ops["upsert_order"], (legacy,), {})
        await self._record_raw(
            engine, writer, "S1", "D", "OLD1",
            "8=FIX.4.2|9=1|35=D|49=CLIENT|56=MKT|34=2|52=20260823-00:00:00|"
            "11=OLD1|55=AAPL|54=1|38=100|40=2|44=150|1=ACCT|375=A|375=B|10=000")
        await engine._backfill_rx_extra_tags()
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["extra_tags"] == "1=ACCT|375=A|375=B"
        assert row["pending_extra_tags"] == "1=ACCT|375=A|375=B", \
            "a pending New echoes the order's own tags"

    @pytest.mark.asyncio
    async def test_backfills_pending_request_tags_via_orig_clordid(self, stack):
        db, writer, engine = stack
        legacy = _order_params(cl_ord_id="C2", orig_cl_ord_id="C1", direction="RX",
                               extra_tags="", pending_action="Replace",
                               pending_cl_ord_id="C3", pending_extra_tags="")
        await writer.submit(engine._compiled_ops["upsert_order"], (legacy,), {})
        await self._record_raw(
            engine, writer, "S1", "D", "C1",
            "8=FIX.4.2|9=1|35=D|11=C1|55=AAPL|54=1|38=100|40=2|5001=X|10=000")
        await self._record_raw(
            engine, writer, "S1", "G", "C3",
            "8=FIX.4.2|9=1|35=G|11=C3|41=C2|55=AAPL|54=1|38=200|40=2|5002=Y|10=000")
        await engine._backfill_rx_extra_tags()
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert row["extra_tags"] == "5001=X", "found through the chain's original ClOrdID"
        assert row["pending_extra_tags"] == "5002=Y", "the parked request's own tags"

    @pytest.mark.asyncio
    async def test_leaves_rows_without_recorded_message_or_tags_alone(self, stack):
        db, writer, engine = stack
        plain = _order_params(cl_ord_id="P1", direction="RX", extra_tags="")
        tx = _order_params(cl_ord_id="T1", direction="TX", extra_tags="")
        await writer.submit(engine._compiled_ops["upsert_order"], (plain,), {})
        await writer.submit(engine._compiled_ops["upsert_order"], (tx,), {})
        await self._record_raw(
            engine, writer, "S1", "D", "P1",
            "8=FIX.4.2|9=1|35=D|11=P1|55=AAPL|54=1|38=100|40=2|10=000")
        await engine._backfill_rx_extra_tags()
        rows = {r["cl_ord_id"]: r for r in await _fetch_all(db, "SELECT * FROM fix_orders")}
        assert rows["P1"]["extra_tags"] == "", "no custom tags on the D — nothing to seed"
        assert rows["T1"]["extra_tags"] == "", "sent orders are not touched"


NEW_ORDER_44_RX = "8=FIX.4.4|35=D|11=C100|55=AAPL|54=1|38=100|40=2|44=150.25|59=0"

CLIENT_FILL_44_RX = (
    "8=FIX.4.4|35=8|11=C1|37=O1|17=E1|150=F|39=2|55=AAPL|54=1|"
    "38=100|32=100|31=150|14=100|6=150|151=0"
)

CLIENT_BUST_44_RX = (
    "8=FIX.4.4|35=8|11=C1|37=O1|17=E2|19=E1|150=H|39=0|55=AAPL|54=1|"
    "38=100|32=100|31=150|14=0|6=0|151=100"
)

CLIENT_CORRECT_44_RX = (
    "8=FIX.4.4|35=8|11=C1|37=O1|17=E3|19=E1|150=G|39=2|55=AAPL|54=1|"
    "38=100|32=100|31=149|14=100|6=149|151=0"
)


def _stub44(session_id="S1"):
    stub = StubSession(session_id)
    stub.dictionary = FixDictionary("FIX.4.4")
    stub.factory = FixMessageFactory(stub.dictionary, "MKT", "CLIENT")
    return stub


class TestFix44Executions:
    @pytest.mark.asyncio
    async def test_fill_sends_trade_without_tag_20_and_records_it(self, stack):
        db, writer, engine = stack
        stub = _stub44()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_44_RX))
        exec_id = await engine.fill_order("S1", "C100", qty=100, price=150.0)
        er = stub.sent[-1]
        assert er["150"] == "F"
        assert er["20"] is None
        rows = await _fetch_all(db, "SELECT * FROM fix_executions")
        row = next(e for e in rows if e["exec_id"] == exec_id)
        assert row["exec_type"] == "Trade"
        assert row["exec_type_code"] == "F"

    @pytest.mark.asyncio
    async def test_bust_sends_trade_cancel(self, stack):
        db, writer, engine = stack
        stub = _stub44()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_44_RX))
        fill_id = await engine.fill_order("S1", "C100", qty=100, price=150.0)
        bust_id = await engine.bust_trade("S1", fill_id)
        er = stub.sent[-1]
        assert er["150"] == "H"
        assert er["20"] is None
        assert er["19"] == fill_id
        rows = await _fetch_all(db, "SELECT * FROM fix_executions ORDER BY id")
        assert [(r["exec_type"], r["exec_type_code"]) for r in rows] == [("TradeCancel", "H")]
        chain = await _versions(db, "fix_executions")
        assert [v["exec_type"] for v in chain] == ["Trade", "TradeCancel"]
        assert chain[0]["trade_id"] == chain[1]["trade_id"]

    @pytest.mark.asyncio
    async def test_correct_sends_trade_correct(self, stack):
        db, writer, engine = stack
        stub = _stub44()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_44_RX))
        fill_id = await engine.fill_order("S1", "C100", qty=100, price=150.0)
        await engine.correct_trade("S1", fill_id, qty=80, price=149.0)
        er = stub.sent[-1]
        assert er["150"] == "G"
        rows = await _fetch_all(db, "SELECT * FROM fix_executions ORDER BY id")
        assert rows[-1]["exec_type"] == "TradeCorrect"

    @pytest.mark.asyncio
    async def test_inbound_trade_and_trade_cancel_recorded(self, stack):
        db, writer, engine = stack
        stub = _stub44()
        await engine.on_app_message(stub, "8", parse_fix(CLIENT_FILL_44_RX))
        await engine.on_app_message(stub, "8", parse_fix(CLIENT_BUST_44_RX))
        rows = await _fetch_all(db, "SELECT * FROM fix_executions ORDER BY id")
        assert [r["exec_type"] for r in rows] == ["TradeCancel"]
        chain = await _versions(db, "fix_executions")
        assert [v["exec_type"] for v in chain] == ["Trade", "TradeCancel"]
        assert chain[0]["trade_id"] == chain[1]["trade_id"]
        orders = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert orders[0]["cum_qty"] == 0.0

    @pytest.mark.asyncio
    async def test_inbound_trade_correct_recorded(self, stack):
        db, writer, engine = stack
        stub = _stub44()
        await engine.on_app_message(stub, "8", parse_fix(CLIENT_FILL_44_RX))
        await engine.on_app_message(stub, "8", parse_fix(CLIENT_CORRECT_44_RX))
        rows = await _fetch_all(db, "SELECT * FROM fix_executions ORDER BY id")
        assert [r["exec_type"] for r in rows] == ["TradeCorrect"]
        chain = await _versions(db, "fix_executions")
        assert [v["exec_type"] for v in chain] == ["Trade", "TradeCorrect"]
        assert chain[0]["trade_id"] == chain[1]["trade_id"]


class TestNonStandardOrderCodes:
    """The order dialogs offer OrdType/TimeInForce values annotated with the
    versions that define them, but the engine never narrows by the session's
    dictionary: sending a code the dictionary lacks is a test macro, so
    the picked code goes out unchanged on any version."""

    @pytest.mark.asyncio
    async def test_market_on_close_goes_out_on_fix44(self, stack):
        db, writer, engine = stack
        stub = _stub44()
        engine.sessions["S1"] = stub
        assert not stub.dictionary.has_enum("40", "5")
        await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, ord_type="5", tif="7")
        d = stub.sent[-1]
        assert d["40"] == "5"
        assert d["59"] == "7"
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert rows[0]["ord_type_code"] == "5"
        assert rows[0]["tif_code"] == "7"

    @pytest.mark.asyncio
    async def test_at_the_close_goes_out_on_fix40(self, stack):
        db, writer, engine = stack
        stub = StubSession("S1")
        stub.dictionary = FixDictionary("FIX.4.0")
        stub.factory = FixMessageFactory(stub.dictionary, "MKT", "CLIENT")
        engine.sessions["S1"] = stub
        assert not stub.dictionary.has_enum("59", "7")
        assert not stub.dictionary.has_enum("40", "I")
        await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, ord_type="I", price=150.0, tif="7")
        d = stub.sent[-1]
        assert d["40"] == "I"
        assert d["59"] == "7"
        await engine.send_cancel_replace("S1", orig_cl_ord_id=d["11"], symbol="AAPL", side="1", qty=200, ord_type="B", price=151.0, tif="2")
        g = stub.sent[-1]
        assert g["40"] == "B"
        assert g["59"] == "2"


REPLACE_REQ_44_RX = "8=FIX.4.4|35=G|11=C102|41=C100|55=AAPL|54=1|38=200|40=2|44=151.5"


class TestFix44ReplaceStatus:
    @pytest.mark.asyncio
    async def test_accept_replace_on_44_reports_working_status(self, stack):
        db, writer, engine = stack
        stub = _stub44()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_44_RX))
        await engine.on_app_message(stub, "G", parse_fix(REPLACE_REQ_44_RX))
        await engine.accept_replace("S1", "C100")
        er = stub.sent[-1]
        assert er["150"] == "5"       # ExecType Replaced marks the event
        assert er["39"] == "0"        # 39=5 does not exist in FIX 4.4
        orders = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert orders[0]["cl_ord_id"] == "C102"
        assert orders[0]["status"] == "New"

    @pytest.mark.asyncio
    async def test_accept_replace_on_44_partial_fill_keeps_fill_status(self, stack):
        db, writer, engine = stack
        stub = _stub44()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_44_RX))
        await engine.fill_order("S1", "C100", qty=40, price=150.0)
        await engine.on_app_message(stub, "G", parse_fix(REPLACE_REQ_44_RX))
        await engine.accept_replace("S1", "C100")
        er = stub.sent[-1]
        assert er["150"] == "5"
        assert er["39"] == "1"        # partially filled, per 4.4 semantics
        orders = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert orders[0]["status"] == "PartiallyFilled"

    @pytest.mark.asyncio
    async def test_accept_replace_on_42_still_reports_replaced(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        await engine.on_app_message(stub, "G", parse_fix(REPLACE_REQ_RX))
        await engine.accept_replace("S1", "C100")
        er = stub.sent[-1]
        assert er["39"] == "5"
        assert er["150"] == "5"
        orders = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert orders[0]["status"] == "Replaced"


class TestEngineStop:
    @pytest.mark.asyncio
    async def test_sessions_log_out_concurrently(self, stack):
        """Each stop may wait up to its logout timeout for the peer's
        confirming Logout; shutdown must pay that once, not per session."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        _, _, engine = stack

        async def slow_stop():
            await asyncio.sleep(0.1)

        for sid in ("A", "B", "C"):
            session = MagicMock()
            session.stop = AsyncMock(side_effect=slow_stop)
            engine.sessions[sid] = session

        started = asyncio.get_event_loop().time()
        await engine.stop()
        elapsed = asyncio.get_event_loop().time() - started

        assert elapsed < 0.25, f"stops ran one after another: {elapsed:.2f}s"
        assert engine.sessions == {}


INSERT_SESSION = (CompiledOp(
    table="fix_sessions",
    op_type="insert",
    sql=(
        "INSERT INTO fix_sessions (session_id, sender_comp_id, target_comp_id, host, port, _mkio_ref) "
        "VALUES (?, ?, ?, ?, ?, ?) RETURNING *"
    ),
    param_names=("session_id", "sender_comp_id", "target_comp_id", "host", "port", "_mkio_ref"),
),)


async def _add_session(writer, session_id="S1", host="", port=9876):
    """Insert a session row the way session_mgmt.add does: through the writer,
    so the row gets its version 1 recorded."""
    await writer.submit(
        INSERT_SESSION, ((session_id, "A", "B", host, port, None),),
        {"session_id": session_id},
    )


async def _versions(db, table, where=""):
    return await _fetch_all(
        db, f"SELECT * FROM {table}__history {where} ORDER BY _mkio_version"
    )


class TestVersioning:
    """fix_sessions, fix_orders and fix_executions are versioned; the writer
    records a version per change and steps the row's counter itself, so the
    engine's hand-written ops need only their RETURNING *. A session's live
    state is its own unversioned table, so a heartbeat or a session
    transition records nothing on any versioned row."""

    @pytest.mark.asyncio
    async def test_tables_are_versioned(self):
        assert set(versioned_tables(CONFIG)) == {
            "fix_sessions", "fix_orders", "fix_executions", "fix_macros"}

    @pytest.mark.asyncio
    async def test_order_lifecycle_is_one_chain(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        await engine.accept_order("S1", "C100")
        await engine.fill_order("S1", "C100", qty=40, price=150.0)

        live = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        chain = await _versions(db, "fix_orders")
        assert live["_mkio_version"] == 3
        assert [(v["_mkio_version"], v["_mkio_op"], v["status"]) for v in chain] == [
            (1, "insert", "PendingNew"), (2, "update", "New"), (3, "update", "PartiallyFilled")]
        assert "session_status" not in chain[0], "the joined column is not a column"
        # The live row sits on its newest version.
        for col in ("cl_ord_id", "status", "cum_qty", "pending_action"):
            assert live[col] == chain[-1][col]

    @pytest.mark.asyncio
    async def test_rename_versions_the_row_and_keeps_its_identity(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        await engine.accept_order("S1", "C100")
        await engine.on_app_message(stub, "F", parse_fix(CANCEL_REQ_RX))
        await engine.accept_cancel("S1", "C100")

        chain = await _versions(db, "fix_orders")
        assert [v["cl_ord_id"] for v in chain][-1] == "C101"
        assert len({v["id"] for v in chain}) == 1, "one chain, keyed by the immutable id"

    @pytest.mark.asyncio
    async def test_fill_rows_have_one_version(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        await engine.fill_order("S1", "C100", qty=40, price=150.0)
        await engine.update_session_state("S1", {"status": "DOWN"})
        await engine.update_session_state("S1", {"status": "ACTIVE"})

        chain = await _versions(db, "fix_executions")
        assert [(v["_mkio_version"], v["_mkio_op"]) for v in chain] == [(1, "insert")]
        live = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert live["_mkio_version"] == 1

    @pytest.mark.asyncio
    async def test_trade_chain_is_its_corrections_and_bust(self, stack):
        """One chain per trade, keyed by the row id: the fill, each correction
        and the bust are its versions, all under the one Trade ID."""
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        fill_id = await engine.fill_order("S1", "C100", qty=40, price=150.0)
        corr_id = await engine.correct_trade("S1", fill_id, qty=30, price=149.0)
        bust_id = await engine.bust_trade("S1", corr_id)

        chain = await _versions(db, "fix_executions")
        assert [(v["_mkio_version"], v["_mkio_op"], v["exec_id"], v["exec_type"], v["last_qty"])
                for v in chain] == [
            (1, "insert", fill_id, "PartialFill", 40.0),
            (2, "update", corr_id, "Correct", 30.0),
            (3, "update", bust_id, "Cancel", 30.0),
        ]
        assert len({v["id"] for v in chain}) == 1
        assert len({v["trade_id"] for v in chain}) == 1
        live = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert (live["_mkio_version"], live["exec_id"]) == (3, bust_id)

    @pytest.mark.asyncio
    async def test_session_transition_records_no_order_version(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        before = await _versions(db, "fix_orders")
        await engine.update_session_state("S1", {"status": "DOWN"})
        await engine.update_session_state("S1", {"status": "ACTIVE"})
        after = await _versions(db, "fix_orders")
        assert after == before
        live = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert live["_mkio_version"] == 1

    @pytest.mark.asyncio
    async def test_state_writes_record_no_session_version(self, stack):
        db, writer, engine = stack
        await _add_session(writer)
        for n in range(1, 4):
            await engine.update_session_state(
                "S1", {"status": "ACTIVE", "tx_seq_num": n, "rx_seq_num": n})
        await engine.update_session_state("S1", {"status": "DOWN"})

        live = (await _fetch_all(db, "SELECT * FROM fix_sessions"))[0]
        assert live["_mkio_version"] == 1
        state = (await _fetch_all(db, "SELECT * FROM fix_session_state"))[0]
        assert (state["status"], state["tx_seq_num"]) == ("DOWN", 3)
        chain = await _versions(db, "fix_sessions")
        assert [(v["_mkio_version"], v["_mkio_op"]) for v in chain] == [(1, "insert")]
        assert not {"status", "tx_seq_num", "rx_seq_num"} & set(chain[0])

    @pytest.mark.asyncio
    async def test_identical_rewrite_of_an_order_records_nothing(self, stack):
        """The writer compares against the pre-image, so re-upserting the
        row as it stands (same updated_at included) is not an edit."""
        from mkfix.fix.engine import _order_params
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        row = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        ops = engine._compiled_ops["upsert_order"]
        await writer.submit(ops, (_order_params(row),), {"cl_ord_id": row["cl_ord_id"]})
        live = (await _fetch_all(db, "SELECT * FROM fix_orders"))[0]
        assert live["_mkio_version"] == 1
        assert len(await _versions(db, "fix_orders")) == 1


def _move(table, op, new, old, cause):
    """A ChangeEvent shaped like the one mkio's on_undo_redo hook delivers."""
    return ChangeBus.make_event(table, op, new if new is not None else old, "r1",
                                cause=cause, old=old)


class TestUndoRedoHook:
    """FixEngine.handle_undo_redo follows a cursor move on fix_sessions: the
    engine's session map and the (unversioned) fix_session_state row."""

    @pytest.mark.asyncio
    async def test_ignores_other_tables(self, stack):
        db, writer, engine = stack
        await engine.handle_undo_redo(_move("fix_orders", "update", {"id": 1}, {"id": 1}, "undo"))
        assert engine.sessions == {}

    @pytest.mark.asyncio
    async def test_undone_edit_rebuilds_a_stopped_session(self, stack):
        db, writer, engine = stack
        await _add_session(writer, host="old.example")
        row = (await _fetch_all(db, "SELECT * FROM fix_sessions"))[0]
        await engine.handle_undo_redo(_move(
            "fix_sessions", "update", row, {**row, "host": "new.example"}, "undo"))
        assert engine.sessions["S1"].config["host"] == "old.example"

    @pytest.mark.asyncio
    async def test_undone_edit_swaps_config_on_a_running_session(self, stack):
        db, writer, engine = stack
        await _add_session(writer, host="old.example")
        row = (await _fetch_all(db, "SELECT * FROM fix_sessions"))[0]
        running = StubSession()
        running._transport = object()
        running._socket = None
        running.config = {**row, "host": "new.example"}
        engine.sessions["S1"] = running
        await engine.handle_undo_redo(_move(
            "fix_sessions", "update", row, running.config, "undo"))
        assert engine.sessions["S1"] is running, "a live transport is kept"
        assert running.config["host"] == "old.example"

    @pytest.mark.asyncio
    async def test_undone_add_drops_the_session_and_its_state(self, stack):
        db, writer, engine = stack
        await _add_session(writer)
        await engine.update_session_state("S1", {"status": "DOWN"})
        stub = StubSession()
        stopped = []

        async def stop():
            stopped.append(True)
        stub.stop = stop
        engine.sessions["S1"] = stub
        row = (await _fetch_all(db, "SELECT * FROM fix_sessions"))[0]

        await engine.handle_undo_redo(_move("fix_sessions", "delete", None, row, "undo"))
        assert "S1" not in engine.sessions
        assert stopped == [True]
        assert await _fetch_all(db, "SELECT * FROM fix_session_state") == []

    @pytest.mark.asyncio
    async def test_redone_add_rebuilds_the_session_and_its_state(self, stack):
        db, writer, engine = stack
        await _add_session(writer)
        row = (await _fetch_all(db, "SELECT * FROM fix_sessions"))[0]
        await engine.handle_undo_redo(_move("fix_sessions", "insert", row, None, "redo"))
        assert engine.sessions["S1"].config["session_id"] == "S1"
        state = await _fetch_all(db, "SELECT * FROM fix_session_state")
        assert [(s["session_id"], s["status"]) for s in state] == [("S1", "DOWN")]


class TestTemplates:
    """A dialog's Save-as keeps its terms under a scope and name; the name is
    unique within its scope (an engine index), so saving it again replaces
    the terms in place rather than adding a second row."""

    @pytest.mark.asyncio
    async def test_save_creates_then_replaces_by_scope_and_name(self, stack):
        db, writer, engine = stack
        await engine.save_template("fill", "half", qty="50", price="", extra_tags="5001=X")
        await engine.save_template("order", "half", symbol="AAPL", side="1", qty=100, price=None)
        rows = await _fetch_all(db, "SELECT * FROM fix_templates ORDER BY scope")
        assert [(r["scope"], r["name"]) for r in rows] == [("fill", "half"), ("order", "half")]
        fill, order = rows
        assert (fill["qty"], fill["price"], fill["extra_tags"]) == ("50", "", "5001=X")
        assert (order["qty"], order["price"], order["symbol"]) == ("100", "", "AAPL"), \
            "terms are kept as text, None as blank"
        assert fill["_mkio_ref"]
        await engine.save_template("fill", "half", qty="", price="99.5", extra_tags="")
        rows = await _fetch_all(db, "SELECT * FROM fix_templates WHERE scope = 'fill'")
        assert len(rows) == 1 and rows[0]["id"] == fill["id"]
        assert (rows[0]["qty"], rows[0]["price"], rows[0]["extra_tags"]) == ("", "99.5", "")

    @pytest.mark.asyncio
    async def test_templates_list_serves_one_scope_with_every_term(self, stack):
        """The dropdown's reqrep, run as SQL against the table the engine
        writes: one scope's rows by name, every term along for the fill."""
        db, writer, engine = stack
        await engine.save_template("fill", "b-half", qty="50", price="", extra_tags="")
        await engine.save_template("fill", "a-all", qty="", price="", extra_tags="5001=X")
        await engine.save_template("order", "b-half", symbol="AAPL", side="1", qty="100", tif="0")
        sql = MKFIX_TOML["services"]["templates_list"]["sql"]
        rows = await _fetch_all(db, sql.replace(":scope", "'fill'"))
        assert [r["name"] for r in rows] == ["a-all", "b-half"], "one scope, by name"
        from mkfix.fix.engine import TEMPLATE_TERM_COLS
        assert set(rows[0]) >= {"id", "name", "scope", *TEMPLATE_TERM_COLS}
        assert (rows[1]["qty"], rows[1]["price"], rows[0]["extra_tags"]) == ("50", "", "5001=X")

    @pytest.mark.asyncio
    async def test_save_refuses_bad_scope_name_or_term(self, stack):
        db, writer, engine = stack
        with pytest.raises(ValueError, match="scope"):
            await engine.save_template("orders", "x", symbol="AAPL")
        with pytest.raises(ValueError, match="name"):
            await engine.save_template("order", "  ", symbol="AAPL")
        with pytest.raises(ValueError, match="cl_ord_id"):
            await engine.save_template("order", "x", cl_ord_id="C1")
        assert await _fetch_all(db, "SELECT * FROM fix_templates") == []

    @pytest.mark.asyncio
    async def test_index_keeps_the_newest_of_hand_made_duplicates(self, stack):
        db, writer, engine = stack
        conn = db.write_conn
        await (await conn.execute("DROP INDEX idx_fix_templates_scope_name")).close()
        for qty in ("1", "2"):
            await (await conn.execute(
                "INSERT INTO fix_templates (scope, name, qty) VALUES ('fill', 'dup', ?)", (qty,))).close()
        await conn.commit()
        await engine._ensure_indexes()
        rows = await _fetch_all(db, "SELECT * FROM fix_templates")
        assert [r["qty"] for r in rows] == ["2"]



NEW_ORDER_RX_HANDLING = ("8=FIX.4.2|35=D|11=C300|55=AAPL|54=1|38=100|40=2|44=150.25|59=0|"
                         "21=3|58=work it|5001=X")


async def _order(db, where="1=1"):
    return (await _fetch_all(db, f"SELECT * FROM fix_orders WHERE {where}"))[0]


class TestSentOrderHandling:
    """HandlInst(21) and Text(58) on the client side: typed in the New,
    Replace and Cancel dialogs, recorded as sent — sent_text is ours alone,
    the counterparty's 58 lands in `text`."""

    @pytest.mark.asyncio
    async def test_new_order_sends_and_records_21_and_58(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0,
                                    handl_inst="3", text="work it")
        msg = stub.sent[-1]
        assert msg["21"] == "3" and msg["58"] == "work it"
        row = await _order(db)
        assert (row["handl_inst"], row["handl_inst_code"]) == ("Manual", "3")
        assert row["sent_text"] == "work it" and row["text"] == ""

    @pytest.mark.asyncio
    async def test_handl_inst_defaults_to_1(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
        assert stub.sent[-1]["21"] == "1" and "58" not in stub.sent[-1].fields
        assert (await _order(db))["handl_inst_code"] == "1"

    @pytest.mark.asyncio
    async def test_execution_report_never_touches_sent_text(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        cl_ord_id = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100,
                                                price=150.0, handl_inst="2", text="mine")
        er = (f"8=FIX.4.2|35=8|11={cl_ord_id}|37=O1|17=E1|20=0|150=0|39=0|55=AAPL|54=1|"
              "38=100|14=0|6=0|151=100|58=theirs")
        await engine.on_app_message(stub, "8", parse_fix(er))
        row = await _order(db)
        assert row["status"] == "New"
        assert row["sent_text"] == "mine" and row["text"] == "theirs"
        assert row["handl_inst_code"] == "2"

    @pytest.mark.asyncio
    async def test_replace_then_cancel_rewrite_sent_text(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        first = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100,
                                            price=150.0, text="new")
        await engine.send_cancel_replace("S1", orig_cl_ord_id=first, symbol="AAPL", side="1",
                                         qty=200, price=151.0, handl_inst="3", text="more")
        assert stub.sent[-1]["21"] == "3" and stub.sent[-1]["58"] == "more"
        row = await _order(db)
        assert row["sent_text"] == "more" and row["handl_inst_code"] == "1", \
            "the text went out whatever the answer; the terms wait for the accept"
        first = await _accept_replace(engine, stub, first)
        assert (await _order(db))["handl_inst"] == "Manual"

        await engine.send_cancel("S1", orig_cl_ord_id=first, symbol="AAPL", side="1",
                                 qty=200, text="pull it")
        assert stub.sent[-1]["35"] == "F" and stub.sent[-1]["58"] == "pull it"
        assert "21" not in stub.sent[-1].fields, "OrderCancelRequest carries no HandlInst"
        row = await _order(db)
        assert row["sent_text"] == "pull it"
        assert row["handl_inst_code"] == "3" and row["entered_qty"] == 200, \
            "a cancel rewrites the text alone"

        await engine.send_cancel("S1", orig_cl_ord_id=first, symbol="AAPL", side="1", qty=200)
        assert (await _order(db))["sent_text"] == "", "the last message sent carried no text"

    @pytest.mark.asyncio
    async def test_cancel_of_unknown_order_still_goes_out(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.send_cancel("S1", orig_cl_ord_id="NOPE", symbol="AAPL", side="1", text="x")
        assert stub.sent[-1]["41"] == "NOPE"
        assert await _fetch_all(db, "SELECT * FROM fix_orders") == []

    @pytest.mark.asyncio
    async def test_columns_record_what_extras_actually_sent(self, stack):
        """A 58 or 21 among the extra tags overrides the dialog's field on
        the wire (sendprep), so the row records that value."""
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0,
                                    text="typed", extra_tags="58=from extras|21=2")
        row = await _order(db)
        assert row["sent_text"] == "from extras" and row["handl_inst_code"] == "2"
        await engine.send_new_order("S1", symbol="MSFT", side="1", qty=100, price=150.0,
                                    text="typed", extra_tags="58=|21=")
        row = await _order(db, "symbol = 'MSFT'")
        assert row["sent_text"] == "" and row["handl_inst"] == "" and row["handl_inst_code"] == ""

    @pytest.mark.asyncio
    async def test_dictionary_without_21_withholds_it(self, stack):
        from mkfix.fix.dictionary import register_custom, unregister_custom
        db, writer, engine = stack
        register_custom("NO21", "FIX.4.2", {"fields": {"21": None}})
        try:
            stub = StubSession()
            stub.dictionary = FixDictionary("NO21")
            stub.factory = FixMessageFactory(stub.dictionary, "MKT", "CLIENT")
            engine.sessions["S1"] = stub
            first = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
            assert "21" not in stub.sent[-1].fields
            await engine.send_cancel_replace("S1", orig_cl_ord_id=first, symbol="AAPL",
                                             side="1", qty=200, price=151.0)
            assert "21" not in stub.sent[-1].fields
            assert (await _order(db))["handl_inst_code"] == ""
        finally:
            unregister_custom("NO21")


class TestReceivedOrderHandling:
    """The market side: `text` is what the counterparty last sent (the New,
    then each request), sent_text what we last answered with."""

    @pytest.mark.asyncio
    async def test_inbound_order_records_21_and_58(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX_HANDLING))
        row = await _order(db)
        assert (row["handl_inst"], row["handl_inst_code"]) == ("Manual", "3")
        assert row["text"] == "work it" and row["sent_text"] == ""
        assert row["extra_tags"] == "5001=X", "21 and 58 are columns, not extras"

    @pytest.mark.asyncio
    async def test_accept_and_fill_text_land_in_sent_text(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX_HANDLING))
        await engine.accept_request("S1", "C300", text="got it")
        assert stub.sent[-1]["58"] == "got it"
        row = await _order(db)
        assert row["sent_text"] == "got it" and row["text"] == "work it"

        await engine.fill_order("S1", "C300", qty=40, price=150.0, text="first clip",
                                extra_tags="5001=X|9001=Y")
        assert stub.sent[-1]["58"] == "first clip"
        assert (await _order(db))["sent_text"] == "first clip"
        trade = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert trade["text"] == "first clip" and trade["extra_tags"] == "5001=X|9001=Y"

        await engine.fill_order("S1", "C300", qty=10, price=150.0)
        assert (await _order(db))["sent_text"] == ""

    @pytest.mark.asyncio
    async def test_reject_reason_is_sent_text(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX_HANDLING))
        await engine.reject_request("S1", "C300", text="no thanks")
        row = await _order(db)
        assert row["status"] == "Rejected"
        assert row["sent_text"] == "no thanks" and row["text"] == "work it"

    @pytest.mark.asyncio
    async def test_request_text_shows_until_answered_and_after(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX_HANDLING))
        await engine.accept_request("S1", "C300")
        await engine.on_app_message(stub, "F", parse_fix(
            "8=FIX.4.2|35=F|11=C301|41=C300|55=AAPL|54=1|38=100|58=changed my mind"))
        row = await _order(db)
        assert row["pending_action"] == "Cancel" and row["text"] == "changed my mind"
        await engine.reject_request("S1", "C300", text="too late")
        assert stub.sent[-1]["35"] == "9" and stub.sent[-1]["58"] == "too late"
        row = await _order(db)
        assert row["sent_text"] == "too late" and row["text"] == "changed my mind"

        await engine.on_app_message(stub, "G", parse_fix(
            "8=FIX.4.2|35=G|11=C302|41=C300|55=AAPL|54=1|38=200|40=2|44=151.5|7001=R"))
        assert (await _order(db))["text"] == "", "the latest request carried none"

    @pytest.mark.asyncio
    async def test_accepted_replace_keeps_its_answer_text(self, stack):
        """accept_replace promotes the request's tags through record_entered,
        whose snapshot must not put sent_text back."""
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX_HANDLING))
        await engine.accept_request("S1", "C300", text="ack")
        await engine.on_app_message(stub, "G", parse_fix(
            "8=FIX.4.2|35=G|11=C302|41=C300|55=AAPL|54=1|38=200|40=2|44=151.5|7001=R"))
        await engine.accept_request("S1", "C300", text="replaced")
        row = await _order(db)
        assert row["cl_ord_id"] == "C302" and row["extra_tags"] == "7001=R"
        assert row["sent_text"] == "replaced"
        await engine.on_app_message(stub, "F", parse_fix(
            "8=FIX.4.2|35=F|11=C303|41=C302|55=AAPL|54=1|38=200"))
        await engine.accept_request("S1", "C302", text="out")
        row = await _order(db)
        assert row["status"] == "Canceled" and row["sent_text"] == "out"


class TestTradeTextAndTags:
    async def _filled(self, engine, stub, **fill):
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        return await engine.fill_order("S1", "C100", qty=40, price=150.0, **fill)

    @pytest.mark.asyncio
    async def test_correction_and_bust_are_versions_with_their_own_text_and_tags(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        fill_id = await self._filled(engine, stub, text="clip", extra_tags="5001=X")
        corrected = await engine.correct_trade("S1", fill_id, qty=30, price=149.0,
                                               text="fat finger", extra_tags="5001=Y")
        assert stub.sent[-1]["58"] == "fat finger"
        live = await _fetch_all(db, "SELECT * FROM fix_executions")
        assert len(live) == 1
        assert live[0]["text"] == "fat finger" and live[0]["extra_tags"] == "5001=Y"
        history = await _fetch_all(
            db, "SELECT text, extra_tags FROM fix_executions__history ORDER BY _mkio_version")
        assert (history[0]["text"], history[0]["extra_tags"]) == ("clip", "5001=X")

        await engine.bust_trade("S1", corrected, text="void")
        live = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert live["text"] == "void" and live["extra_tags"] == ""

    @pytest.mark.asyncio
    async def test_renotify_carries_text_and_tags(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        fill_id = await self._filled(engine, stub, extra_tags="5001=X")
        await engine.on_app_message(stub, "Q", parse_fix(
            f"8=FIX.4.2|35=Q|37=O|17={fill_id}|127=D|55=AAPL|54=1|38=100"))
        await engine.renotify_trade("S1", fill_id, text="again", extra_tags="5001=X|58=really")
        live = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert live["text"] == "really", "extras override the field, and the row says so"
        assert live["extra_tags"] == "5001=X|58=really" and live["dk_reason"] == ""

    @pytest.mark.asyncio
    async def test_received_trade_keeps_the_reports_custom_tags(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        cl_ord_id = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
        fill = (f"8=FIX.4.2|35=8|11={cl_ord_id}|37=O1|17=E9|20=0|150=2|39=2|55=AAPL|54=1|"
                "38=100|32=100|31=150|14=100|6=150|151=0|58=done|30=XNYS|9001=A|9001=B")
        await engine.on_app_message(stub, "8", parse_fix(fill))
        trade = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert trade["text"] == "done" and trade["extra_tags"] == "30=XNYS|9001=A|9001=B"


class TestHandlingBackfill:
    @staticmethod
    async def _record_raw(engine, writer, direction, msg_type, cl_ord_id, exec_id, raw):
        params = ("S1", "20260823-00:00:00.000", direction, 1, msg_type, "", "APP",
                  raw, "CLIENT", "MKT", cl_ord_id, "", exec_id, "AAPL", "1", len(raw), "000", "", None)
        await writer.submit(engine._compiled_ops["insert_message"], (params,), {})

    @pytest.mark.asyncio
    async def test_seeds_handl_inst_and_trade_tags_once(self, stack):
        db, writer, engine = stack
        await _add_session(writer)
        await writer.submit(engine._compiled_ops["upsert_order"],
                            (_order_params(cl_ord_id="C2", orig_cl_ord_id="C1", direction="RX"),), {})
        await self._record_raw(
            engine, writer, "RX", "D", "C1", "",
            "8=FIX.4.2|9=1|35=D|49=CLIENT|56=MKT|34=2|52=20260823-00:00:00|"
            "11=C1|21=2|55=AAPL|54=1|38=100|40=2|44=150|10=000")
        order = {**(await _order(db)), "order_id": "OR1"}
        await engine._write_sent_execution(order, "EX1", "TR1", "Fill", "2",
                                           40.0, 150.0, 40.0, 150.0, 60.0)
        await self._record_raw(
            engine, writer, "TX", "8", "C2", "EX1",
            "8=FIX.4.2|9=1|35=8|49=MKT|56=CLIENT|34=3|52=20260823-00:00:01|37=OR1|11=C2|17=EX1|"
            "20=0|150=1|39=1|55=AAPL|54=1|38=100|32=40|31=150|14=40|6=150|151=60|"
            "60=20260823-00:00:01|5001=X|375=A|10=000")

        await engine._backfill_handling_and_trade_tags()
        row = await _order(db)
        assert (row["handl_inst"], row["handl_inst_code"]) == ("AutoExecPublic", "2")
        trade = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert trade["extra_tags"] == "5001=X|375=A"

        await db.write_conn.execute("UPDATE fix_executions SET extra_tags = ''")
        await db.write_conn.commit()
        await engine._backfill_handling_and_trade_tags()
        trade = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert trade["extra_tags"] == "", "the backfill runs once per database"


class TestHandlingEdges:
    @pytest.mark.asyncio
    async def test_order_created_by_an_execution_report_has_blank_sent_text(self, stack):
        """An ER for an order this engine never recorded takes the upsert's
        INSERT branch: the columns are blank strings, never NULL, so the
        blotter's Sent Text cell and the Replace prefill stay well-defined."""
        db, writer, engine = stack
        stub = StubSession()
        er = ("8=FIX.4.2|35=8|11=CX|37=O1|17=E1|20=0|150=0|39=0|55=AAPL|54=1|"
              "38=100|14=0|6=0|151=100|58=hello")
        await engine.on_app_message(stub, "8", parse_fix(er))
        row = await _order(db)
        assert (row["sent_text"], row["handl_inst"], row["handl_inst_code"]) == ("", "", "")
        assert row["text"] == "hello"

    @pytest.mark.asyncio
    async def test_cancel_text_survives_the_answer_arriving_mid_send(self, stack):
        """send_cancel records its text before the send: the accepting ER
        renames the chain, after which a lookup by the superseded ClOrdID
        would miss and the text be lost."""
        db, writer, engine = stack

        class Canceling(StubSession):
            async def send_message(inner, msg):
                await StubSession.send_message(inner, msg)
                if msg.get("35") == "F":
                    er = (f"8=FIX.4.2|35=8|11={msg.get('11')}|41={msg.get('41')}|37=O1|17=E2|"
                          "20=0|150=4|39=4|55=AAPL|54=1|38=100|14=0|6=0|151=0|58=done")
                    await engine.on_app_message(inner, "8", parse_fix(er))
                return msg

        stub = Canceling()
        engine.sessions["S1"] = stub
        first = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
        second = await engine.send_cancel("S1", orig_cl_ord_id=first, symbol="AAPL",
                                          side="1", qty=100, text="pull it")
        row = await _order(db)
        assert row["cl_ord_id"] == second and row["status"] == "Canceled"
        assert row["sent_text"] == "pull it" and row["text"] == "done"

    @pytest.mark.asyncio
    async def test_history_keeps_each_text_the_order_carried(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        first = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100,
                                            price=150.0, text="one")
        await engine.send_cancel_replace("S1", orig_cl_ord_id=first, symbol="AAPL", side="1",
                                         qty=200, price=151.0, text="two")
        await engine.send_cancel("S1", orig_cl_ord_id=first, symbol="AAPL", side="1", text="three")
        texts = [r["sent_text"] for r in await _fetch_all(
            db, "SELECT sent_text FROM fix_orders__history ORDER BY _mkio_version")]
        assert texts == ["one", "two", "three"]

    @pytest.mark.asyncio
    async def test_unchanged_text_records_no_version(self, stack):
        """A cancel repeating the standing text changes nothing versioned
        but updated_at — and the row must still be one chain."""
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        first = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100,
                                            price=150.0, text="same")
        await engine.send_cancel("S1", orig_cl_ord_id=first, symbol="AAPL", side="1", text="same")
        rows = await _fetch_all(db, "SELECT DISTINCT id FROM fix_orders__history")
        assert len(rows) == 1
        assert (await _order(db))["sent_text"] == "same"

    @pytest.mark.asyncio
    async def test_fix44_handling_names_and_trade_text(self, stack):
        db, writer, engine = stack
        stub = _stub44()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(
            "8=FIX.4.4|35=D|11=C400|55=AAPL|54=1|38=100|40=2|44=150|59=0|21=1|58=hi"))
        row = await _order(db)
        assert row["handl_inst"] == FixDictionary("FIX.4.4").enum_name("21", "1")
        assert row["handl_inst"] != "1" and row["text"] == "hi"
        fill_id = await engine.fill_order("S1", "C400", qty=40, price=150.0, text="clip")
        await engine.correct_trade("S1", fill_id, qty=30, price=150.0, text="fix",
                                   extra_tags="9001=Z")
        msg = stub.sent[-1]
        assert msg["150"] == "G" and msg["58"] == "fix" and "20" not in msg.fields
        trade = (await _fetch_all(db, "SELECT * FROM fix_executions"))[0]
        assert (trade["exec_type"], trade["text"], trade["extra_tags"]) == ("TradeCorrect", "fix", "9001=Z")

    @pytest.mark.asyncio
    async def test_received_trade_correction_replaces_its_tags(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        cl = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
        base = f"8=FIX.4.2|35=8|11={cl}|37=O1|55=AAPL|54=1|38=100|14=100|6=150|151=0|"
        await engine.on_app_message(stub, "8", parse_fix(
            base + "17=E1|20=0|150=2|39=2|32=100|31=150|30=XNYS"))
        await engine.on_app_message(stub, "8", parse_fix(
            base + "17=E2|19=E1|20=2|150=2|39=2|32=100|31=149|58=px|30=XNAS"))
        trades = await _fetch_all(db, "SELECT * FROM fix_executions")
        assert len(trades) == 1
        assert (trades[0]["text"], trades[0]["extra_tags"]) == ("px", "30=XNAS")

    @pytest.mark.asyncio
    async def test_templates_keep_handl_inst_and_text(self, stack):
        db, writer, engine = stack
        await engine.save_template("order", "manual", symbol="AAPL", handl_inst="3", text="work it")
        await engine.save_template("bust", "void", text="void", extra_tags="")
        rows = {r["scope"]: r for r in await _fetch_all(db, "SELECT * FROM fix_templates")}
        assert (rows["order"]["handl_inst"], rows["order"]["text"]) == ("3", "work it")
        assert (rows["bust"]["handl_inst"], rows["bust"]["text"]) == ("", "void")
        listed = await _fetch_all(
            db, MKFIX_TOML["services"]["templates_list"]["sql"].replace(":scope", "'order'"))
        assert listed[0]["handl_inst"] == "3", "the dropdown's fill needs the column"


class TestHandlingBackfillEdges:
    @pytest.mark.asyncio
    async def test_rows_without_a_recorded_message_or_session_are_left_blank(self, stack):
        db, writer, engine = stack
        await writer.submit(engine._compiled_ops["upsert_order"],
                            (_order_params(cl_ord_id="LONE", session_id="GONE"),), {})
        order = {**(await _order(db)), "order_id": "OR9"}
        await engine._write_sent_execution(order, "EX9", "TR9", "Fill", "2",
                                           1.0, 1.0, 1.0, 1.0, 0.0)
        await engine._backfill_handling_and_trade_tags()
        assert (await _order(db))["handl_inst_code"] == ""
        assert (await _fetch_all(db, "SELECT extra_tags FROM fix_executions"))[0]["extra_tags"] == ""

    @pytest.mark.asyncio
    async def test_sent_order_reads_its_own_new_order_not_the_counterpartys(self, stack):
        """ClOrdIDs are per direction: a received D sharing the ClOrdID must
        not seed a sent order."""
        db, writer, engine = stack
        await writer.submit(engine._compiled_ops["upsert_order"],
                            (_order_params(cl_ord_id="C1", direction="TX"),), {})
        for direction, code in (("RX", "3"), ("TX", "2")):
            raw = f"8=FIX.4.2|9=1|35=D|11=C1|21={code}|55=AAPL|54=1|38=100|40=2|10=000"
            await TestHandlingBackfill._record_raw(engine, writer, direction, "D", "C1", "", raw)
        await engine._backfill_handling_and_trade_tags()
        assert (await _order(db))["handl_inst_code"] == "2"

    @pytest.mark.asyncio
    async def test_engine_start_runs_it(self, stack):
        db, writer, _ = stack
        engine = FixEngine(db=db, writer=writer)
        await engine.start()
        rows = await _fetch_all(db, "SELECT value FROM fix_settings WHERE key = 'handling_backfill'")
        assert rows == [{"value": "1"}]
        await engine.stop()


class FailingSession(StubSession):
    """A session whose cancel/replace requests never reach the wire."""

    async def send_message(self, msg):
        if msg.get("35") in ("F", "G"):
            raise ConnectionError("socket closed")
        return await super().send_message(msg)


def _er(cl_ord_id, exec_type, status, orig="", qty=100, cum=0, extra=""):
    orig_tag = f"41={orig}|" if orig else ""
    return parse_fix(
        f"8=FIX.4.2|35=8|11={cl_ord_id}|{orig_tag}37=MKT1|17=E{exec_type}{status}{cum}|20=0|"
        f"150={exec_type}|39={status}|55=AAPL|54=1|38={qty}|14={cum}|6=0|151={qty - cum}{extra}")


async def _accept_replace(engine, stub, orig, echo=True):
    """The counterparty's Replaced ER for the last 35=G sent on `stub`;
    returns the chain's new ClOrdID. `echo` False leaves 38/44 off the ER."""
    request = next(m for m in reversed(stub.sent) if m.get("35") == "G")
    terms = f"38={request.get('38')}|44={request.get('44')}|" if echo else ""
    await engine.on_app_message(stub, "8", parse_fix(
        f"8=FIX.4.2|35=8|11={request.get('11')}|41={orig}|37=MKT1|17=EACC{request.get('11')}|20=0|"
        f"150=5|39=5|55={request.get('55')}|54={request.get('54')}|{terms}14=0|6=0|151=0"))
    return request.get("11")


def _cancel_reject(request_id, orig, status="0", response_to="2", reason="0", text="too late"):
    tags = [f"8=FIX.4.2|35=9|11={request_id}|41={orig}|37=MKT1"]
    tags += [f"39={status}"] if status else []
    tags += [f"434={response_to}"] if response_to else []
    tags += [f"102={reason}"] if reason else []
    tags += [f"58={text}"] if text else []
    return parse_fix("|".join(tags))


def _slot(row):
    return (row["pending_action"], row["pending_cl_ord_id"], row["pending_qty"], row["pending_price"])


class TestSentRequests:
    """The client side of cancel/replace: a sent order's request slot holds
    the request outstanding until the ExecutionReport or OrderCancelReject
    answering its ClOrdID, and only an accept moves the chain."""

    async def _working_order(self, engine, stub):
        engine.sessions["S1"] = stub
        cl_ord_id = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
        await engine.on_app_message(stub, "8", _er(cl_ord_id, "0", "0"))
        return cl_ord_id

    async def _replace(self, engine, orig, qty=200, price=151.0):
        return await engine.send_cancel_replace("S1", orig, symbol="AAPL", side="1", qty=qty, price=price)

    async def _cancel(self, engine, orig):
        return await engine.send_cancel("S1", orig, symbol="AAPL", side="1", qty=100)

    @pytest.mark.asyncio
    async def test_requests_fill_the_slot(self, stack):
        db, writer, engine = stack
        c1 = await self._working_order(engine, StubSession())
        r1 = await self._replace(engine, c1)
        assert _slot(await _order(db)) == ("Replace", r1, 200.0, 151.0)
        x1 = await self._cancel(engine, c1)
        assert _slot(await _order(db)) == ("Cancel", x1, 0.0, 0.0), "the slot holds the latest request"

    @pytest.mark.asyncio
    async def test_request_is_one_row_version(self, stack):
        db, writer, engine = stack
        c1 = await self._working_order(engine, StubSession())
        before = len(await _versions(db, "fix_orders"))
        await self._replace(engine, c1)
        assert len(await _versions(db, "fix_orders")) == before + 1

    @pytest.mark.asyncio
    async def test_accept_renames_and_clears(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        r1 = await self._replace(engine, c1)
        accept = _er(r1, "5", "5", orig=c1, qty=200, extra="|44=151")
        await engine.on_app_message(stub, "8", accept)
        row = await _order(db)
        assert row["cl_ord_id"] == r1 and row["status"] == "Replaced"
        assert _slot(row) == ("", "", 0.0, 0.0)
        await engine.on_app_message(stub, "8", accept)
        assert len(await _fetch_all(db, "SELECT id FROM fix_orders")) == 1, "a duplicate accept changes nothing"

    @pytest.mark.asyncio
    async def test_fill_while_pending_keeps_the_slot(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        r1 = await self._replace(engine, c1)
        await engine.on_app_message(stub, "8", _er(c1, "1", "1", cum=40, extra="|32=40|31=150"))
        row = await _order(db)
        assert row["status"] == "PartiallyFilled" and row["cum_qty"] == 40.0
        assert _slot(row) == ("Replace", r1, 200.0, 151.0)

    @pytest.mark.asyncio
    async def test_pending_report_does_not_move_the_chain(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        r1 = await self._replace(engine, c1)
        await engine.on_app_message(stub, "8", _er(r1, "E", "E", orig=c1))
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert len(rows) == 1, "a PendingReplace ER lands on the order, not a second row"
        assert rows[0]["cl_ord_id"] == c1 and rows[0]["status"] == "PendingReplace"
        assert _slot(rows[0])[:2] == ("Replace", r1)

        await engine.on_app_message(stub, "9", _cancel_reject(r1, c1, status="0"))
        row = await _order(db)
        assert row["cl_ord_id"] == c1, "a refused request never advances the chain"
        assert row["status"] == "New", "OrdStatus(39) on the reject undoes PendingReplace"
        assert _slot(row) == ("", "", 0.0, 0.0)
        assert row["cxl_rej_reason"] == f"Replace {r1}: TooLateToCancel"
        assert row["text"] == "too late"

    @pytest.mark.asyncio
    async def test_pending_report_without_a_slot_resolves_by_41(self, stack):
        # A request mkfix did not send through send_cancel_replace (a replayed log).
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        await engine.on_app_message(stub, "8", _er("EXT1", "6", "6", orig=c1))
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert [(r["cl_ord_id"], r["status"]) for r in rows] == [(c1, "PendingCancel")]

    @pytest.mark.asyncio
    async def test_accept_without_41_lands_through_the_slot(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        x1 = await self._cancel(engine, c1)
        await engine.on_app_message(stub, "8", _er(x1, "4", "4"))
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert [(r["cl_ord_id"], r["status"], r["pending_action"]) for r in rows] == [(x1, "Canceled", "")]

    @pytest.mark.asyncio
    async def test_status_only_accept_on_fix41(self, stack):
        # FIX 4.0/4.1 has no ExecType(150): OrdStatus alone says Replaced.
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        r1 = await self._replace(engine, c1)
        er = parse_fix(f"8=FIX.4.1|35=8|11={r1}|41={c1}|37=MKT1|17=E9|20=0|39=5|55=AAPL|54=1|"
                       "38=200|14=0|6=0")
        await engine.on_app_message(stub, "8", er)
        row = await _order(db)
        assert row["cl_ord_id"] == r1 and row["pending_action"] == ""

    @pytest.mark.asyncio
    async def test_reject_of_an_earlier_request_leaves_the_later_one(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        r1 = await self._replace(engine, c1)
        x1 = await self._cancel(engine, c1)
        await engine.on_app_message(stub, "9", _cancel_reject(r1, c1))
        row = await _order(db)
        assert _slot(row)[:2] == ("Cancel", x1)
        assert row["cxl_rej_reason"] == f"Replace {r1}: TooLateToCancel"

    @pytest.mark.asyncio
    async def test_reject_after_a_rename_resolves_through_history(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        x1 = await self._cancel(engine, c1)
        r1 = await self._replace(engine, c1)
        await engine.on_app_message(stub, "8", _er(r1, "5", "5", orig=c1, qty=200))
        await engine.on_app_message(
            stub, "9", _cancel_reject(x1, c1, status="5", response_to="1", reason="3", text=""))
        row = await _order(db)
        assert row["cl_ord_id"] == r1
        assert row["cxl_rej_reason"] == f"Cancel {x1}: AlreadyPendingCancel"

    @pytest.mark.asyncio
    async def test_kind_without_434_comes_from_the_recorded_request(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        x1 = await self._cancel(engine, c1)
        r1 = await self._replace(engine, c1)  # overwrites the slot, so it can't name x1's kind
        await engine.record_message("S1", "TX", next(m for m in stub.sent if m.get("11") == x1))
        await engine.on_app_message(stub, "9", _cancel_reject(x1, c1, response_to="", reason=""))
        row = await _order(db)
        assert row["cxl_rej_reason"] == f"Cancel {x1}"
        assert _slot(row)[:2] == ("Replace", r1)

    @pytest.mark.asyncio
    async def test_unknown_order_reject_keeps_our_status(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        x1 = await self._cancel(engine, c1)
        await engine.on_app_message(stub, "9", _cancel_reject(x1, c1, status="8", response_to="1", reason="1"))
        row = await _order(db)
        assert row["status"] == "New", "under UnknownOrder their OrdStatus describes no order of ours"
        assert row["cxl_rej_reason"] == f"Cancel {x1}: UnknownOrder" and row["pending_action"] == ""

    @pytest.mark.asyncio
    async def test_reject_naming_nothing_is_only_a_message(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        await self._working_order(engine, stub)
        before = await _order(db)
        await engine.on_app_message(stub, "9", _cancel_reject("NOPE", "NOPE2"))
        assert await _order(db) == before

    @pytest.mark.asyncio
    async def test_reject_never_touches_a_received_order(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        before = await _order(db)
        await engine.on_app_message(stub, "9", _cancel_reject("X9", "C100"))
        assert await _order(db) == before

    @pytest.mark.asyncio
    async def test_request_on_a_received_order_leaves_its_slot(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        await engine.send_cancel("S1", "C100", symbol="AAPL", side="1", qty=100)
        assert (await _order(db))["pending_action"] == "New", "that slot is the counterparty's request"

    @pytest.mark.asyncio
    async def test_new_request_retires_the_reject_note(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        r1 = await self._replace(engine, c1)
        await engine.on_app_message(stub, "9", _cancel_reject(r1, c1))
        await self._cancel(engine, c1)
        assert (await _order(db))["cxl_rej_reason"] == ""

    @pytest.mark.asyncio
    async def test_failed_send_clears_the_slot(self, stack):
        db, writer, engine = stack
        stub = FailingSession()
        c1 = await self._working_order(engine, stub)
        with pytest.raises(ConnectionError):
            await self._replace(engine, c1)
        assert _slot(await _order(db)) == ("", "", 0.0, 0.0)

    @pytest.mark.asyncio
    async def test_slot_is_written_before_the_send(self, stack):
        # EagerSession accepts inside send_message: the rename must find the
        # slot already written, and clear it.
        db, writer, engine = stack
        stub = EagerSession(engine)
        c1 = await self._working_order(engine, stub)
        stub.answer_to = "G"
        r1 = await self._replace(engine, c1)
        row = await _order(db)
        assert row["cl_ord_id"] == r1 and row["pending_action"] == ""

    @pytest.mark.asyncio
    async def test_market_side_flow_is_unchanged(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        await engine.accept_order("S1", "C100")
        await engine.on_app_message(
            stub, "G", parse_fix("8=FIX.4.2|35=G|11=C101|41=C100|55=AAPL|54=1|38=300|40=2|44=151"))
        assert _slot(await _order(db)) == ("Replace", "C101", 300.0, 151.0)
        await engine.accept_replace("S1", "C100")
        row = await _order(db)
        assert row["cl_ord_id"] == "C101" and _slot(row) == ("", "", 0.0, 0.0)

    @pytest.mark.asyncio
    async def test_fix44_accept_is_known_by_exec_type(self, stack):
        # FIX 4.4 has no OrdStatus Replaced: 150=5 rides with the working 39.
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        r1 = await self._replace(engine, c1)
        await engine.on_app_message(stub, "8", _er(r1, "5", "0", orig=c1, qty=200))
        row = await _order(db)
        assert (row["cl_ord_id"], row["status"], row["pending_action"]) == (r1, "New", "")

    @pytest.mark.asyncio
    async def test_later_reports_echoing_41_stay_on_the_row(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        r1 = await self._replace(engine, c1)
        await engine.on_app_message(stub, "8", _er(r1, "5", "5", orig=c1, qty=200))
        await engine.on_app_message(stub, "8", _er(r1, "1", "1", orig=c1, qty=200, cum=50,
                                                   extra="|32=50|31=151"))
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert [(r["cl_ord_id"], r["cum_qty"]) for r in rows] == [(r1, 50.0)]
        execs = await _fetch_all(db, "SELECT cl_ord_id FROM fix_executions")
        assert [e["cl_ord_id"] for e in execs] == [r1]

    @pytest.mark.asyncio
    async def test_fill_under_the_request_id_before_the_accept_lands_on_the_order(self, stack):
        # A non-accepting ER naming the request's ClOrdID belongs to the chain's current row.
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        r1 = await self._replace(engine, c1)
        await engine.on_app_message(stub, "8", _er(r1, "1", "1", orig=c1, cum=10, extra="|32=10|31=150"))
        rows = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert [(r["cl_ord_id"], r["cum_qty"], r["pending_cl_ord_id"]) for r in rows] == [(c1, 10.0, r1)]
        execs = await _fetch_all(db, "SELECT cl_ord_id FROM fix_executions")
        assert [e["cl_ord_id"] for e in execs] == [c1], "the trade names the row it filled"

    @pytest.mark.asyncio
    async def test_request_and_refusal_are_the_orders_history(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        r1 = await self._replace(engine, c1)
        await engine.on_app_message(stub, "9", _cancel_reject(r1, c1))
        chain = await _versions(db, "fix_orders")
        assert [(v["pending_action"], v["cxl_rej_reason"]) for v in chain][-2:] == [
            ("Replace", ""), ("", f"Replace {r1}: TooLateToCancel")]
        assert {v["cl_ord_id"] for v in chain} == {c1}

    @pytest.mark.asyncio
    async def test_reject_without_status_keeps_ours(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        x1 = await self._cancel(engine, c1)
        await engine.on_app_message(stub, "9", _cancel_reject(x1, c1, status="", response_to="1"))
        assert (await _order(db))["status"] == "New"

    @pytest.mark.asyncio
    async def test_duplicate_reject_is_harmless(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        r1 = await self._replace(engine, c1)
        for _ in range(2):
            await engine.on_app_message(stub, "9", _cancel_reject(r1, c1))
        row = await _order(db)
        assert row["pending_action"] == "" and row["cxl_rej_reason"] == f"Replace {r1}: TooLateToCancel"

    @pytest.mark.asyncio
    async def test_slot_lookup_is_per_session(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1 = await self._working_order(engine, stub)
        r1 = await self._replace(engine, c1)
        other = StubSession("S2")
        await engine.on_app_message(other, "9", _cancel_reject(r1, c1))
        assert (await _order(db))["pending_action"] == "Replace", "another session's reject is not ours"

    @pytest.mark.asyncio
    async def test_request_for_an_unknown_order_still_goes_out(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.send_cancel("S1", "GHOST", symbol="AAPL", side="1", qty=100)
        assert stub.sent[-1]["35"] == "F" and stub.sent[-1]["41"] == "GHOST"
        assert await _fetch_all(db, "SELECT * FROM fix_orders") == []

    def test_cxl_rej_reason_is_named_wherever_it_is_defined(self):
        from mkfix.fix.dictionary import STANDARD_VERSIONS
        for version in STANDARD_VERSIONS:
            d = FixDictionary(version)
            if d.defines("102"):
                assert d.enum_name("102", "0") == "TooLateToCancel", version
                assert d.enum_name("102", "1") == "UnknownOrder", version


class TestReplaceTermsWaitForAccept:
    """The Replace dialog opens on the last *accepted* terms: a replace's
    entered terms ride in the request slot and reach ENTERED_COLS only with
    the ExecutionReport accepting it."""

    async def _order_and_replace(self, engine, stub):
        engine.sessions["S1"] = stub
        c1 = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
        await engine.on_app_message(stub, "8", _er(c1, "0", "0"))
        r1 = await engine.send_cancel_replace("S1", c1, symbol="AAPL", side="1", qty=500, price=155.0)
        return c1, r1

    @pytest.mark.asyncio
    async def test_rejected_replace_leaves_the_entered_terms(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1, r1 = await self._order_and_replace(engine, stub)
        await engine.on_app_message(stub, "9", _cancel_reject(r1, c1))
        row = await _order(db)
        assert (row["entered_qty"], row["entered_price"]) == (100.0, 150.0)
        assert row["pending_entered"] == ""

    @pytest.mark.asyncio
    async def test_accepted_replace_promotes_them(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1, r1 = await self._order_and_replace(engine, stub)
        await _accept_replace(engine, stub, c1)
        row = await _order(db)
        assert (row["entered_qty"], row["entered_price"]) == (500.0, 155.0)
        assert row["pending_entered"] == "" and row["cl_ord_id"] == r1

    @pytest.mark.asyncio
    async def test_reject_then_accept_prefills_the_accepted(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1, r1 = await self._order_and_replace(engine, stub)
        await engine.on_app_message(stub, "9", _cancel_reject(r1, c1))
        await engine.send_cancel_replace("S1", c1, symbol="AAPL", side="1", qty=300, price=152.0)
        await _accept_replace(engine, stub, c1)
        await engine.send_cancel_replace("S1", (await _order(db))["cl_ord_id"],
                                         symbol="AAPL", side="1", qty=900, price=160.0)
        row = await _order(db)
        assert (row["entered_qty"], row["entered_price"]) == (300.0, 152.0)

    @pytest.mark.asyncio
    async def test_accept_whose_terms_left_the_slot_takes_the_echoed_38_44(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        c1, r1 = await self._order_and_replace(engine, stub)
        await engine.send_cancel("S1", c1, symbol="AAPL", side="1", qty=100)
        await _accept_replace(engine, stub, c1)
        row = await _order(db)
        assert (row["entered_qty"], row["entered_price"]) == (500.0, 155.0)
        assert row["pending_action"] == "Cancel", "the cancel is still outstanding"

    @pytest.mark.asyncio
    async def test_failed_send_drops_the_terms(self, stack):
        db, writer, engine = stack
        stub = FailingSession()
        engine.sessions["S1"] = stub
        c1 = await engine.send_new_order("S1", symbol="AAPL", side="1", qty=100, price=150.0)
        with pytest.raises(ConnectionError):
            await engine.send_cancel_replace("S1", c1, symbol="AAPL", side="1", qty=500, price=155.0)
        row = await _order(db)
        assert row["pending_entered"] == "" and row["entered_qty"] == 100.0

