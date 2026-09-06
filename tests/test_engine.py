"""Tests for FixEngine's compiled write operations against a real mkio stack."""

import tomllib
from pathlib import Path

import pytest
import pytest_asyncio

from mkio.change_bus import ChangeBus
from mkio.database import Database
from mkio.writer import WriteBatcher

from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.engine import FixEngine
from mkfix.fix.message import FixMessage, FixMessageFactory, parse_fix, SOH

TABLES = tomllib.loads(
    (Path(__file__).parent.parent / "mkfix" / "mkfix.toml").read_text()
)["tables"]


@pytest_asyncio.fixture
async def stack():
    db = Database(path=":memory:", tables=TABLES, config={})
    await db.start()
    bus = ChangeBus()
    writer = WriteBatcher(db, bus)
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


ORDER_COLS = [
    "cl_ord_id", "session_id", "order_id", "orig_cl_ord_id", "symbol",
    "side", "side_code", "ord_type", "ord_type_code", "price", "stop_price",
    "order_qty", "time_in_force", "status", "cum_qty", "avg_price",
    "leaves_qty", "last_qty", "last_price", "text", "transact_time",
    "created_at", "updated_at", "direction",
    "pending_action", "pending_cl_ord_id", "pending_qty", "pending_price",
    "pending_extra_tags", "session_status",
    "tif_code", "extra_tags", "entered_qty", "entered_price",
]

ORDER_UPDATE_COLS = [
    "status", "cum_qty", "avg_price", "leaves_qty",
    "last_qty", "last_price", "text", "transact_time", "updated_at",
    "pending_action", "pending_cl_ord_id", "pending_qty", "pending_price",
    "pending_extra_tags", "session_status",
]


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
        "session_status": "ACTIVE",
        "tif_code": "0", "extra_tags": "", "entered_qty": 100.0, "entered_price": 150.25,
    }
    base.update(overrides)
    insert = tuple(base[c] for c in ORDER_COLS)
    update = tuple(base[c] for c in ORDER_UPDATE_COLS)
    return insert + (None,) + update


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
        rows = await _fetch_all(db, "SELECT key, value FROM fix_settings")
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
                  "8=FIX.4.2|", "US", "THEM", "", "", "", "", "", 60, "123", None)
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
    async def test_state_write_mirrors_live_columns_onto_session_row(self, stack):
        """The sessions blotter reads a single table, so status and seq nums
        must be mirrored onto fix_sessions in the same write."""
        db, writer, engine = stack
        await db.write_conn.execute(
            "INSERT INTO fix_sessions (session_id, sender_comp_id, target_comp_id) "
            "VALUES ('S1', 'A', 'B')"
        )
        await db.write_conn.commit()

        await engine.update_session_state(
            "S1", {"status": "ACTIVE", "tx_seq_num": 7, "rx_seq_num": 9}
        )
        row = (await _fetch_all(db, "SELECT * FROM fix_sessions"))[0]
        assert row["status"] == "ACTIVE"
        assert row["tx_seq_num"] == 7
        assert row["rx_seq_num"] == 9
        assert row["_mkio_ref"], "the mirror update must publish a change event"

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
        """A fully filled order stays fillable — overfills are a scenario the
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
        assert execs[-1]["exec_type"] == "Correct"
        assert execs[-1]["direction"] == "TX"
        assert execs[-1]["last_qty"] == 50.0
        assert execs[-1]["trade_id"] == execs[0]["trade_id"], "the Trade ID survives a correction"

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
        assert execs[-1]["exec_type"] == "Cancel"
        assert execs[-1]["last_qty"] == 40.0
        assert execs[-1]["trade_id"] == execs[0]["trade_id"], "the Trade ID survives a bust"
        assert execs[-1]["exec_id"] != execs[0]["exec_id"], "a bust replaces the ExecID"

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


class TestSessionStatusMirror:
    """session_status on order/execution rows mirrors the owning session's
    live status so blotter buttons can gate on it (rowMatch only reads the
    row's own columns, and a JOIN query would not live-update)."""

    @pytest.mark.asyncio
    async def test_status_change_mirrors_onto_orders_and_executions(self, stack):
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        await engine.fill_order("S1", "C100", qty=40, price=150.0)

        rows = await _fetch_all(db, "SELECT session_status FROM fix_orders") \
            + await _fetch_all(db, "SELECT session_status FROM fix_executions")
        assert all(r["session_status"] == "ACTIVE" for r in rows), \
            "rows written over a live session carry ACTIVE"

        await engine.update_session_state("S1", {"status": "DOWN"})
        rows = await _fetch_all(db, "SELECT session_status FROM fix_orders") \
            + await _fetch_all(db, "SELECT session_status FROM fix_executions")
        assert all(r["session_status"] == "DOWN" for r in rows)

        await engine.update_session_state("S1", {"status": "ACTIVE"})
        rows = await _fetch_all(db, "SELECT session_status FROM fix_orders") \
            + await _fetch_all(db, "SELECT session_status FROM fix_executions")
        assert all(r["session_status"] == "ACTIVE" for r in rows)

    @pytest.mark.asyncio
    async def test_seq_num_persist_does_not_touch_rows(self, stack):
        """Only status transitions mirror — the per-message seq-num persist
        must not rewrite every order row."""
        db, writer, engine = stack
        stub = StubSession()
        engine.sessions["S1"] = stub
        await engine.on_app_message(stub, "D", parse_fix(NEW_ORDER_RX))
        await engine.update_session_state("S1", {"status": "DOWN"})
        await engine.update_session_state("S1", {"tx_seq_num": 5, "rx_seq_num": 7})
        row = (await _fetch_all(db, "SELECT session_status FROM fix_orders"))[0]
        assert row["session_status"] == "DOWN"

    @pytest.mark.asyncio
    async def test_mirror_touches_only_the_sessions_rows(self, stack):
        db, writer, engine = stack
        s1, s2 = StubSession("S1"), StubSession("S2")
        engine.sessions["S1"] = s1
        engine.sessions["S2"] = s2
        await engine.on_app_message(s1, "D", parse_fix(NEW_ORDER_RX))
        await engine.on_app_message(s2, "D", parse_fix(NEW_ORDER_RX))
        await engine.update_session_state("S1", {"status": "DOWN"})
        rows = await _fetch_all(db, "SELECT session_id, session_status FROM fix_orders")
        by = {r["session_id"]: r["session_status"] for r in rows}
        assert by == {"S1": "DOWN", "S2": "ACTIVE"}


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
        assert execs[-1]["cl_ord_id"] == "C102"
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
        assert [e["exec_type"] for e in execs] == ["Fill", "Cancel"]
        assert execs[-1]["exec_type_code"] == "1"
        assert execs[0]["trade_id"].startswith("TR")
        assert execs[-1]["trade_id"] == execs[0]["trade_id"], \
            "a bust referencing the fill (tag 19) joins its trade"

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
                  raw, "CLIENT", "MKT", cl_ord_id, "", "", "AAPL", "1", len(raw), "000", None)
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
        assert [r["exec_type"] for r in rows] == ["Trade", "TradeCancel"]
        assert rows[0]["trade_id"] == rows[1]["trade_id"]
        assert rows[-1]["exec_type_code"] == "H"

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
        assert [r["exec_type"] for r in rows] == ["Trade", "TradeCancel"]
        assert rows[0]["trade_id"] == rows[1]["trade_id"]
        orders = await _fetch_all(db, "SELECT * FROM fix_orders")
        assert orders[0]["cum_qty"] == 0.0

    @pytest.mark.asyncio
    async def test_inbound_trade_correct_recorded(self, stack):
        db, writer, engine = stack
        stub = _stub44()
        await engine.on_app_message(stub, "8", parse_fix(CLIENT_FILL_44_RX))
        await engine.on_app_message(stub, "8", parse_fix(CLIENT_CORRECT_44_RX))
        rows = await _fetch_all(db, "SELECT * FROM fix_executions ORDER BY id")
        assert [r["exec_type"] for r in rows] == ["Trade", "TradeCorrect"]
        assert rows[0]["trade_id"] == rows[1]["trade_id"]


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
