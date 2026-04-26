"""FIX engine: manages sessions, bridges FIX messages to mkio database."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, TYPE_CHECKING

from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.message import FixMessage, _fix_timestamp
from mkfix.fix.replay import ReplayTask, parse_log_file
from mkfix.fix.session import FixSession

if TYPE_CHECKING:
    from mkio.change_bus import ChangeBus
    from mkio.database import Database
    from mkio.writer import WriteBatcher, CompiledOp


class FixEngine:
    """Manages all FIX sessions and bridges messages to mkio's database."""

    def __init__(self, db: Database, writer: WriteBatcher, bus: ChangeBus):
        self.db = db
        self.writer = writer
        self.bus = bus
        self.sessions: dict[str, FixSession] = {}
        self._compiled_ops: dict[str, tuple[CompiledOp, ...]] = {}
        self._ord_id_counter = 0
        self._replay_tasks: dict[int, ReplayTask] = {}

    async def start(self) -> None:
        """Load session configs from DB and compile write operations."""
        self._compile_ops()
        await self._ensure_indexes()
        await self._load_sessions()

    async def stop(self) -> None:
        """Stop all sessions and replay tasks."""
        for task in list(self._replay_tasks.values()):
            await task.stop()
        self._replay_tasks.clear()
        for session in list(self.sessions.values()):
            await session.stop()
        self.sessions.clear()

    def _compile_ops(self) -> None:
        """Pre-compile SQL operations for writing FIX data."""
        from mkio.writer import CompiledOp

        msg_cols = [
            "session_id", "timestamp", "direction", "seq_num", "msg_type",
            "msg_type_name", "category", "raw_message", "sender_comp",
            "target_comp", "cl_ord_id", "order_id", "exec_id", "symbol",
            "side", "body_length", "checksum",
        ]
        msg_placeholders = ", ".join(["?"] * (len(msg_cols) + 1))
        msg_col_str = ", ".join(msg_cols + ["_mkio_ref"])
        self._compiled_ops["insert_message"] = (CompiledOp(
            table="fix_messages",
            op_type="insert",
            sql=f"INSERT INTO fix_messages ({msg_col_str}) VALUES ({msg_placeholders}) RETURNING *",
            param_names=tuple(msg_cols + ["_mkio_ref"]),
        ),)

        state_cols = ["session_id", "status", "tx_seq_num", "rx_seq_num",
                      "last_tx_time", "last_rx_time", "session_start", "error_text"]
        set_clause = ", ".join(f"{c} = ?" for c in state_cols[1:])
        self._compiled_ops["upsert_state"] = (CompiledOp(
            table="fix_session_state",
            op_type="upsert",
            sql=(
                f"INSERT INTO fix_session_state ({', '.join(state_cols)}, _mkio_ref) "
                f"VALUES ({', '.join(['?'] * (len(state_cols) + 1))}) "
                f"ON CONFLICT(session_id) DO UPDATE SET {set_clause}, _mkio_ref = ? "
                f"RETURNING *"
            ),
            param_names=tuple(state_cols + ["_mkio_ref"] + state_cols[1:] + ["_mkio_ref2"]),
        ),)

        order_cols = [
            "cl_ord_id", "session_id", "order_id", "orig_cl_ord_id", "symbol",
            "side", "ord_type", "price", "stop_price", "order_qty",
            "time_in_force", "status", "cum_qty", "avg_price", "leaves_qty",
            "last_qty", "last_price", "text", "transact_time", "created_at", "updated_at",
        ]
        order_update_cols = [
            "order_id", "status", "cum_qty", "avg_price", "leaves_qty",
            "last_qty", "last_price", "text", "transact_time", "updated_at",
        ]
        order_set = ", ".join(f"{c} = ?" for c in order_update_cols)
        self._compiled_ops["upsert_order"] = (CompiledOp(
            table="fix_orders",
            op_type="upsert",
            sql=(
                f"INSERT INTO fix_orders ({', '.join(order_cols)}, _mkio_ref) "
                f"VALUES ({', '.join(['?'] * (len(order_cols) + 1))}) "
                f"ON CONFLICT(cl_ord_id, session_id) DO UPDATE SET {order_set}, _mkio_ref = ? "
                f"RETURNING *"
            ),
            param_names=tuple(order_cols + ["_mkio_ref"] + order_update_cols + ["_mkio_ref2"]),
        ),)

        exec_cols = [
            "session_id", "exec_id", "order_id", "cl_ord_id", "symbol",
            "side", "side_code", "last_qty", "last_price", "cum_qty",
            "avg_price", "exec_type", "exec_type_code", "leaves_qty",
            "transact_time", "text", "timestamp",
        ]
        exec_placeholders = ", ".join(["?"] * (len(exec_cols) + 1))
        exec_col_str = ", ".join(exec_cols + ["_mkio_ref"])
        self._compiled_ops["insert_execution"] = (CompiledOp(
            table="fix_executions",
            op_type="insert",
            sql=f"INSERT INTO fix_executions ({exec_col_str}) VALUES ({exec_placeholders}) RETURNING *",
            param_names=tuple(exec_cols + ["_mkio_ref"]),
        ),)

        ioi_cols = [
            "session_id", "ioi_id", "ioi_trans_type", "symbol", "side",
            "ioi_qty", "price", "valid_until", "timestamp", "direction", "raw_message",
        ]
        ioi_ph = ", ".join(["?"] * (len(ioi_cols) + 1))
        ioi_col_str = ", ".join(ioi_cols + ["_mkio_ref"])
        self._compiled_ops["insert_ioi"] = (CompiledOp(
            table="fix_iois",
            op_type="insert",
            sql=f"INSERT INTO fix_iois ({ioi_col_str}) VALUES ({ioi_ph}) RETURNING *",
            param_names=tuple(ioi_cols + ["_mkio_ref"]),
        ),)

        alloc_cols = [
            "session_id", "alloc_id", "alloc_trans_type", "alloc_type", "symbol",
            "side", "quantity", "avg_price", "trade_date", "alloc_status",
            "num_allocs", "timestamp", "direction", "raw_message",
        ]
        alloc_ph = ", ".join(["?"] * (len(alloc_cols) + 1))
        alloc_col_str = ", ".join(alloc_cols + ["_mkio_ref"])
        self._compiled_ops["insert_allocation"] = (CompiledOp(
            table="fix_allocations",
            op_type="insert",
            sql=f"INSERT INTO fix_allocations ({alloc_col_str}) VALUES ({alloc_ph}) RETURNING *",
            param_names=tuple(alloc_cols + ["_mkio_ref"]),
        ),)

        replay_cols = ["id", "status", "sent_messages", "error_text"]
        replay_set = ", ".join(f"{c} = ?" for c in replay_cols[1:])
        self._compiled_ops["update_replay"] = (CompiledOp(
            table="fix_replay_jobs",
            op_type="update",
            sql=(
                f"UPDATE fix_replay_jobs SET {replay_set}, _mkio_ref = ? "
                f"WHERE id = ? RETURNING *"
            ),
            param_names=tuple(replay_cols[1:] + ["_mkio_ref", "id"]),
        ),)

    async def _ensure_indexes(self) -> None:
        """Create application-level indexes that can't be expressed in TOML config."""
        conn = self.db.write_conn
        await (await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_fix_orders_clord_session "
            "ON fix_orders(cl_ord_id, session_id)"
        )).close()
        await conn.commit()

    async def _load_sessions(self) -> None:
        """Load session configurations from the database."""
        conn = self.db.read_conn
        cursor = await conn.execute("SELECT * FROM fix_sessions WHERE enabled = 1")
        rows = await cursor.fetchall()
        await cursor.close()

        for row in rows:
            config = dict(row)
            session = FixSession(self, config)
            self.sessions[config["session_id"]] = session

    async def load_session_state(self, session_id: str) -> dict[str, Any] | None:
        """Load persisted session state."""
        conn = self.db.read_conn
        cursor = await conn.execute(
            "SELECT * FROM fix_session_state WHERE session_id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def start_session(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if not session:
            await self.reload_session(session_id)
            session = self.sessions.get(session_id)
        if not session:
            raise ValueError(f"Unknown session: {session_id}")
        await session.start()

    async def stop_session(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError(f"Unknown session: {session_id}")
        await session.stop()

    async def record_message(self, session_id: str, direction: str, msg: FixMessage) -> None:
        """Record a FIX message to the fix_messages table."""
        dictionary = FixDictionary(msg.get("8", "FIX.4.2"))
        msg_type = msg.get("35", "")
        now = _fix_timestamp()

        params = (
            session_id,
            now,
            direction,
            msg.get_int("34", 0),
            msg_type,
            dictionary.msg_type_name(msg_type),
            dictionary.msg_category(msg_type),
            msg.to_pipe_string(),
            msg.get("49", ""),
            msg.get("56", ""),
            msg.get("11", ""),
            msg.get("37", ""),
            msg.get("17", ""),
            msg.get("55", ""),
            msg.get("54", ""),
            msg.get_int("9", 0),
            msg.get("10", ""),
            None,  # _mkio_ref placeholder
        )

        ops = self._compiled_ops["insert_message"]
        await self.writer.submit(ops, (params,), {"session_id": session_id})

    async def update_session_state(self, session_id: str, updates: dict[str, Any]) -> None:
        """Update session state in the database."""
        state = await self.load_session_state(session_id) or {
            "session_id": session_id,
            "status": "DOWN",
            "tx_seq_num": 1,
            "rx_seq_num": 1,
            "last_tx_time": "",
            "last_rx_time": "",
            "session_start": "",
            "error_text": "",
        }
        state.update(updates)

        params = (
            state["session_id"],
            state["status"],
            state["tx_seq_num"],
            state["rx_seq_num"],
            state["last_tx_time"],
            state["last_rx_time"],
            state["session_start"],
            state["error_text"],
            None,  # _mkio_ref
            # ON CONFLICT SET values:
            state["status"],
            state["tx_seq_num"],
            state["rx_seq_num"],
            state["last_tx_time"],
            state["last_rx_time"],
            state["session_start"],
            state["error_text"],
            None,  # _mkio_ref2
        )

        ops = self._compiled_ops["upsert_state"]
        await self.writer.submit(ops, (params,), {"session_id": session_id})

    async def on_app_message(self, session: FixSession, msg_type: str, msg: FixMessage) -> None:
        """Handle an inbound application-level FIX message."""
        if msg_type == "8":
            await self._handle_execution_report(session, msg)
        elif msg_type == "6":
            await self._handle_ioi(session, msg, "RX")
        elif msg_type == "J":
            await self._handle_allocation(session, msg, "RX")

    async def _handle_execution_report(self, session: FixSession, msg: FixMessage) -> None:
        """Process an ExecutionReport (35=8): update order state and record fills."""
        dictionary = session.dictionary
        now = _fix_timestamp()
        cl_ord_id = msg.get("11", "")
        session_id = session.session_id

        side_code = msg.get("54", "")
        status_code = msg.get("39", "")
        ord_type_code = msg.get("40", "")
        tif_code = msg.get("59", "")
        exec_type_code = msg.get("150", "")

        order_params = (
            cl_ord_id,
            session_id,
            msg.get("37", ""),
            msg.get("41", ""),
            msg.get("55", ""),
            dictionary.enum_name("54", side_code),
            dictionary.enum_name("40", ord_type_code),
            msg.get_float("44", 0.0),
            msg.get_float("99", 0.0),
            msg.get_float("38", 0.0),
            dictionary.enum_name("59", tif_code),
            dictionary.enum_name("39", status_code),
            msg.get_float("14", 0.0),
            msg.get_float("6", 0.0),
            msg.get_float("151", 0.0),
            msg.get_float("32", 0.0),
            msg.get_float("31", 0.0),
            msg.get("58", ""),
            msg.get("60", ""),
            now,
            now,
            None,  # _mkio_ref
            # ON CONFLICT SET values:
            msg.get("37", ""),
            dictionary.enum_name("39", status_code),
            msg.get_float("14", 0.0),
            msg.get_float("6", 0.0),
            msg.get_float("151", 0.0),
            msg.get_float("32", 0.0),
            msg.get_float("31", 0.0),
            msg.get("58", ""),
            msg.get("60", ""),
            now,
            None,  # _mkio_ref2
        )

        ops = self._compiled_ops["upsert_order"]
        await self.writer.submit(ops, (order_params,), {"cl_ord_id": cl_ord_id})

        # Record fill/partial fill as execution
        if exec_type_code in ("1", "2", "F"):
            exec_params = (
                session_id,
                msg.get("17", ""),
                msg.get("37", ""),
                cl_ord_id,
                msg.get("55", ""),
                dictionary.enum_name("54", side_code),
                side_code,
                msg.get_float("32", 0.0),
                msg.get_float("31", 0.0),
                msg.get_float("14", 0.0),
                msg.get_float("6", 0.0),
                dictionary.enum_name("150", exec_type_code),
                exec_type_code,
                msg.get_float("151", 0.0),
                msg.get("60", ""),
                msg.get("58", ""),
                now,
                None,  # _mkio_ref
            )

            ops = self._compiled_ops["insert_execution"]
            await self.writer.submit(ops, (exec_params,), {"exec_id": msg.get("17", "")})

    async def send_new_order(
        self,
        session_id: str,
        symbol: str,
        side: str,
        qty: float,
        ord_type: str = "2",
        price: float | None = None,
        tif: str = "0",
        account: str | None = None,
        **extra: str,
    ) -> str:
        """Send a NewOrderSingle and return the ClOrdID."""
        session = self.sessions.get(session_id)
        if not session or not session.is_active:
            raise ValueError(f"Session {session_id} is not active")

        cl_ord_id = self._next_cl_ord_id()
        msg = session.factory.new_order_single(
            cl_ord_id=cl_ord_id,
            symbol=symbol,
            side=side,
            qty=qty,
            ord_type=ord_type,
            price=price,
            tif=tif,
            account=account,
            **extra,
        )
        await session.send_message(msg)

        # Pre-populate order row as PendingNew
        dictionary = session.dictionary
        now = _fix_timestamp()
        order_params = (
            cl_ord_id,
            session_id,
            "",  # order_id
            "",  # orig_cl_ord_id
            symbol,
            dictionary.enum_name("54", side),
            dictionary.enum_name("40", ord_type),
            price or 0.0,
            0.0,  # stop_price
            qty,
            dictionary.enum_name("59", tif),
            "PendingNew",
            0.0,  # cum_qty
            0.0,  # avg_price
            qty,  # leaves_qty
            0.0,  # last_qty
            0.0,  # last_price
            "",   # text
            now,  # transact_time
            now,  # created_at
            now,  # updated_at
            None,  # _mkio_ref
            # ON CONFLICT SET values (won't conflict for new order):
            "",  # order_id
            "PendingNew",
            0.0, 0.0, qty, 0.0, 0.0, "", now, now,
            None,  # _mkio_ref2
        )
        ops = self._compiled_ops["upsert_order"]
        await self.writer.submit(ops, (order_params,), {"cl_ord_id": cl_ord_id})

        return cl_ord_id

    async def send_cancel(
        self,
        session_id: str,
        orig_cl_ord_id: str,
        symbol: str,
        side: str,
        qty: float = 0,
    ) -> str:
        """Send an OrderCancelRequest and return the new ClOrdID."""
        session = self.sessions.get(session_id)
        if not session or not session.is_active:
            raise ValueError(f"Session {session_id} is not active")

        cl_ord_id = self._next_cl_ord_id()
        msg = session.factory.cancel_request(
            cl_ord_id=cl_ord_id,
            orig_cl_ord_id=orig_cl_ord_id,
            symbol=symbol,
            side=side,
            qty=qty,
        )
        await session.send_message(msg)
        return cl_ord_id

    async def send_cancel_replace(
        self,
        session_id: str,
        orig_cl_ord_id: str,
        symbol: str,
        side: str,
        qty: float,
        ord_type: str = "2",
        price: float | None = None,
        **extra: str,
    ) -> str:
        """Send an OrderCancelReplaceRequest and return the new ClOrdID."""
        session = self.sessions.get(session_id)
        if not session or not session.is_active:
            raise ValueError(f"Session {session_id} is not active")

        cl_ord_id = self._next_cl_ord_id()
        msg = session.factory.cancel_replace_request(
            cl_ord_id=cl_ord_id,
            orig_cl_ord_id=orig_cl_ord_id,
            symbol=symbol,
            side=side,
            qty=qty,
            ord_type=ord_type,
            price=price,
            **extra,
        )
        await session.send_message(msg)
        return cl_ord_id

    async def reset_sequence(self, session_id: str, tx: int = 1, rx: int = 1) -> None:
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError(f"Unknown session: {session_id}")
        await session.reset_sequence_numbers(tx, rx)

    def _next_cl_ord_id(self) -> str:
        self._ord_id_counter += 1
        return f"MKFIX-{self._ord_id_counter:08d}"

    async def _handle_ioi(self, session: FixSession, msg: FixMessage, direction: str) -> None:
        """Process an IOI (35=6)."""
        dictionary = session.dictionary
        now = _fix_timestamp()
        params = (
            session.session_id,
            msg.get("23", ""),
            dictionary.enum_name("28", msg.get("28", "")),
            msg.get("55", ""),
            dictionary.enum_name("54", msg.get("54", "")),
            msg.get("27", ""),
            msg.get_float("44", 0.0),
            msg.get("62", ""),
            now,
            direction,
            msg.to_pipe_string(),
            None,
        )
        ops = self._compiled_ops["insert_ioi"]
        await self.writer.submit(ops, (params,), {"ioi_id": msg.get("23", "")})

    async def _handle_allocation(self, session: FixSession, msg: FixMessage, direction: str) -> None:
        """Process an Allocation (35=J)."""
        dictionary = session.dictionary
        now = _fix_timestamp()
        params = (
            session.session_id,
            msg.get("70", ""),
            dictionary.enum_name("71", msg.get("71", "")),
            dictionary.enum_name("626", msg.get("626", "")),
            msg.get("55", ""),
            dictionary.enum_name("54", msg.get("54", "")),
            msg.get_float("53", 0.0),
            msg.get_float("6", 0.0),
            msg.get("75", ""),
            dictionary.enum_name("87", msg.get("87", "")),
            msg.get_int("78", 0),
            now,
            direction,
            msg.to_pipe_string(),
            None,
        )
        ops = self._compiled_ops["insert_allocation"]
        await self.writer.submit(ops, (params,), {"alloc_id": msg.get("70", "")})

    # ── Replay ───────────────────────────────────────────────────────

    async def start_replay(self, job_id: int) -> None:
        """Start a replay job."""
        conn = self.db.read_conn
        cursor = await conn.execute("SELECT * FROM fix_replay_jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            raise ValueError(f"Unknown replay job: {job_id}")
        job = dict(row)

        session_id = job.get("target_session", "")
        session = self.sessions.get(session_id)
        if not session or not session.is_active:
            raise ValueError(f"Target session {session_id} is not active")

        messages = parse_log_file(job["file_path"])
        if not messages:
            raise ValueError(f"No FIX messages found in {job['file_path']}")

        msg_filter: set[str] | None = None
        filt = job.get("msg_filter", "")
        if filt:
            msg_filter = {t.strip() for t in filt.split(",") if t.strip()}

        await self._update_replay_job(job_id, 0, len(messages))

        task = ReplayTask(
            job_id=job_id,
            session=session,
            messages=messages,
            speed=job.get("speed", 1.0),
            msg_filter=msg_filter,
            on_progress=self._on_replay_progress,
        )
        self._replay_tasks[job_id] = task
        await task.start()

    async def pause_replay(self, job_id: int) -> None:
        task = self._replay_tasks.get(job_id)
        if not task:
            raise ValueError(f"No running replay job: {job_id}")
        task.pause()
        await self._on_replay_progress(job_id, task.sent, "paused", "")

    async def resume_replay(self, job_id: int) -> None:
        task = self._replay_tasks.get(job_id)
        if not task:
            raise ValueError(f"No running replay job: {job_id}")
        task.resume()
        await self._on_replay_progress(job_id, task.sent, "running", "")

    async def stop_replay(self, job_id: int) -> None:
        task = self._replay_tasks.pop(job_id, None)
        if not task:
            raise ValueError(f"No running replay job: {job_id}")
        await task.stop()
        await self._on_replay_progress(job_id, task.sent, "stopped", "")

    async def _on_replay_progress(self, job_id: int, sent: int, status: str, error: str) -> None:
        params = (status, sent, error, None, job_id)
        ops = self._compiled_ops["update_replay"]
        await self.writer.submit(ops, (params,), {"id": job_id})

    async def _update_replay_job(self, job_id: int, sent: int, total: int) -> None:
        conn = self.db.write_conn
        await (await conn.execute(
            "UPDATE fix_replay_jobs SET total_messages = ?, sent_messages = ? WHERE id = ?",
            (total, sent, job_id),
        )).close()
        await conn.commit()

    async def reload_session(self, session_id: str) -> None:
        """Reload a session's config from the database."""
        conn = self.db.read_conn
        cursor = await conn.execute(
            "SELECT * FROM fix_sessions WHERE session_id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()

        if row:
            config = dict(row)
            if session_id in self.sessions:
                self.sessions[session_id].config = config
            else:
                self.sessions[session_id] = FixSession(self, config)
        elif session_id in self.sessions:
            await self.sessions[session_id].stop()
            del self.sessions[session_id]
