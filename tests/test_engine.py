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
from mkfix.fix.message import FixMessageFactory, parse_fix

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


def _state_params(session_id, status):
    vals = (session_id, status, 1, 1, "", "", "", "")
    return vals + (None,) + vals[1:]


ORDER_COLS = [
    "cl_ord_id", "session_id", "order_id", "orig_cl_ord_id", "symbol",
    "side", "side_code", "ord_type", "ord_type_code", "price", "stop_price",
    "order_qty", "time_in_force", "status", "cum_qty", "avg_price",
    "leaves_qty", "last_qty", "last_price", "text", "transact_time",
    "created_at", "updated_at", "direction",
]

ORDER_UPDATE_COLS = [
    "order_id", "status", "cum_qty", "avg_price", "leaves_qty",
    "last_qty", "last_price", "text", "transact_time", "updated_at",
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
    }
    base.update(overrides)
    insert = tuple(base[c] for c in ORDER_COLS)
    update = tuple(base[c] for c in ORDER_UPDATE_COLS)
    return insert + (None,) + update


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
        ops = engine._compiled_ops["upsert_state"]
        await writer.submit(ops, (_state_params("S1", "DOWN"),), {})
        await writer.submit(ops, (_state_params("S1", "ACTIVE"),), {})
        rows = await _fetch_all(db, "SELECT * FROM fix_session_state")
        assert len(rows) == 1
        assert rows[0]["status"] == "ACTIVE"
        assert rows[0]["_mkio_ref"], "update path must not null out _mkio_ref"

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
        assert rows[0]["_mkio_ref"], "update path must not null out _mkio_ref"

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
        self.sent = []

    async def send_message(self, msg):
        self.sent.append(msg)
        return msg


NEW_ORDER_RX = "8=FIX.4.2|35=D|11=C100|55=AAPL|54=1|38=100|40=2|44=150.25|59=0"


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
        assert row["direction"] == "RX"

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

    @pytest.mark.asyncio
    async def test_fill_rejects_nonpositive_qty(self, stack):
        db, writer, engine = stack
        await self._seed(engine)
        with pytest.raises(ValueError):
            await engine.fill_order("S1", "C100", qty=0, price=150.0)

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
