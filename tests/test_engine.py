"""Tests for FixEngine's compiled write operations against a real mkio stack."""

import tomllib
from pathlib import Path

import pytest
import pytest_asyncio

from mkio.change_bus import ChangeBus
from mkio.database import Database
from mkio.writer import WriteBatcher

from mkfix.fix.engine import FixEngine

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
    "created_at", "updated_at",
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
        "transact_time": "", "created_at": "", "updated_at": "",
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
