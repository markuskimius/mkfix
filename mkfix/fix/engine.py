"""FIX engine: manages sessions, bridges FIX messages to mkio database."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, TYPE_CHECKING

from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.idgen import IdGenerator
from mkfix.fix.message import FixMessage, _fix_timestamp
from mkfix.fix.replay import ReplayTask, parse_log_file
from mkfix.fix.session import FixSession

if TYPE_CHECKING:
    from mkio.database import Database
    from mkio.writer import WriteBatcher, CompiledOp

ORDER_COLS = [
    "cl_ord_id", "session_id", "order_id", "orig_cl_ord_id", "symbol",
    "side", "side_code", "ord_type", "ord_type_code", "price", "stop_price",
    "order_qty", "time_in_force", "status", "cum_qty", "avg_price",
    "leaves_qty", "last_qty", "last_price", "text", "transact_time",
    "created_at", "updated_at", "direction",
    "pending_action", "pending_cl_ord_id", "pending_qty", "pending_price",
    "session_status",
]

ORDER_UPDATE_COLS = [
    "status", "cum_qty", "avg_price", "leaves_qty",
    "last_qty", "last_price", "text", "transact_time", "updated_at",
    "pending_action", "pending_cl_ord_id", "pending_qty", "pending_price",
    "session_status",
]

EXEC_COLS = [
    "session_id", "exec_id", "trade_id", "order_id", "cl_ord_id", "symbol",
    "side", "side_code", "last_qty", "last_price", "cum_qty",
    "avg_price", "exec_type", "exec_type_code", "leaves_qty",
    "transact_time", "text", "timestamp", "direction", "session_status",
]


def _order_params(row: dict[str, Any]) -> tuple[Any, ...]:
    insert = tuple(row[c] for c in ORDER_COLS)
    update = tuple(row[c] for c in ORDER_UPDATE_COLS)
    return insert + (None,) + update


def _exec_params(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row[c] for c in EXEC_COLS) + (None,)


class FixEngine:
    """Manages all FIX sessions and bridges messages to mkio's database."""

    def __init__(self, db: Database, writer: WriteBatcher):
        self.db = db
        self.writer = writer
        self.ids = IdGenerator(db, writer)
        self.sessions: dict[str, FixSession] = {}
        self._compiled_ops: dict[str, tuple[CompiledOp, ...]] = {}
        self._replay_tasks: dict[int, ReplayTask] = {}

    async def start(self) -> None:
        """Load session configs from DB and compile write operations."""
        self._compile_ops()
        await self._ensure_indexes()
        await self.ids.start()
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
        # The second op mirrors the live columns onto fix_sessions so the
        # sessions blotter can be a plain single-table query pane — a query
        # service with JOIN sql drops change events from non-primary tables.
        self._compiled_ops["upsert_state"] = (CompiledOp(
            table="fix_session_state",
            op_type="upsert",
            sql=(
                f"INSERT INTO fix_session_state ({', '.join(state_cols)}, _mkio_ref) "
                f"VALUES ({', '.join(['?'] * (len(state_cols) + 1))}) "
                f"ON CONFLICT(session_id) DO UPDATE SET {set_clause}, _mkio_ref = excluded._mkio_ref "
                f"RETURNING *"
            ),
            param_names=tuple(state_cols + ["_mkio_ref"] + state_cols[1:]),
        ), CompiledOp(
            table="fix_sessions",
            op_type="update",
            sql=(
                "UPDATE fix_sessions SET status = ?, tx_seq_num = ?, rx_seq_num = ?, _mkio_ref = ? "
                "WHERE session_id = ? RETURNING *"
            ),
            param_names=("status", "tx_seq_num", "rx_seq_num", "_mkio_ref", "session_id"),
        ))

        order_set = ", ".join(f"{c} = ?" for c in ORDER_UPDATE_COLS)
        # order_qty/price update from the insert values, guarded so an
        # ExecutionReport without tag 38/44 can't zero them out. order_id is
        # write-once: the immutable Order ID assigned when the order is created
        # must survive later ERs carrying the counterparty's OrderID(37).
        order_set += (
            ", order_qty = iif(excluded.order_qty = 0, fix_orders.order_qty, excluded.order_qty)"
            ", price = iif(excluded.price = 0, fix_orders.price, excluded.price)"
            ", order_id = iif(fix_orders.order_id = '', excluded.order_id, fix_orders.order_id)"
        )
        self._compiled_ops["upsert_order"] = (CompiledOp(
            table="fix_orders",
            op_type="upsert",
            sql=(
                f"INSERT INTO fix_orders ({', '.join(ORDER_COLS)}, _mkio_ref) "
                f"VALUES ({', '.join(['?'] * (len(ORDER_COLS) + 1))}) "
                f"ON CONFLICT(cl_ord_id, session_id) DO UPDATE SET {order_set}, _mkio_ref = excluded._mkio_ref "
                f"RETURNING *"
            ),
            param_names=tuple(ORDER_COLS + ["_mkio_ref"] + ORDER_UPDATE_COLS),
        ),)

        # Moves an order chain to its next ClOrdID when a cancel/replace is
        # accepted. Guarded so a duplicate ER can't collide with an existing row.
        self._compiled_ops["rename_order"] = (CompiledOp(
            table="fix_orders",
            op_type="update",
            sql=(
                "UPDATE fix_orders SET cl_ord_id = ?, orig_cl_ord_id = ?, updated_at = ?, _mkio_ref = ? "
                "WHERE session_id = ? AND cl_ord_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM fix_orders x "
                "WHERE x.session_id = fix_orders.session_id AND x.cl_ord_id = ?) "
                "RETURNING *"
            ),
            param_names=("cl_ord_id", "orig_cl_ord_id", "updated_at", "_mkio_ref",
                         "session_id", "old_cl_ord_id", "new_cl_ord_id"),
        ),)

        # Mirrors a session status change onto its order/execution rows so the
        # blotters can gate action buttons on session_status. Keyed by rowid:
        # the writer returns (and publishes) one row per op, so a whole-session
        # UPDATE would live-update only one blotter row.
        self._compiled_ops["mirror_session_status"] = (CompiledOp(
            table="fix_orders",
            op_type="update",
            sql="UPDATE fix_orders SET session_status = ?, _mkio_ref = ? WHERE id = ? RETURNING *",
            param_names=("session_status", "_mkio_ref", "id"),
        ), CompiledOp(
            table="fix_executions",
            op_type="update",
            sql="UPDATE fix_executions SET session_status = ?, _mkio_ref = ? WHERE id = ? RETURNING *",
            param_names=("session_status", "_mkio_ref", "id"),
        ))

        exec_placeholders = ", ".join(["?"] * (len(EXEC_COLS) + 1))
        exec_col_str = ", ".join(EXEC_COLS + ["_mkio_ref"])
        self._compiled_ops["insert_execution"] = (CompiledOp(
            table="fix_executions",
            op_type="insert",
            sql=f"INSERT INTO fix_executions ({exec_col_str}) VALUES ({exec_placeholders}) RETURNING *",
            param_names=tuple(EXEC_COLS + ["_mkio_ref"]),
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
        )

        mirror = (
            state["status"],
            state["tx_seq_num"],
            state["rx_seq_num"],
            None,  # _mkio_ref
            state["session_id"],
        )
        ops = self._compiled_ops["upsert_state"]
        await self.writer.submit(ops, (params, mirror), {"session_id": session_id})
        if "status" in updates:
            await self._mirror_session_status(session_id, state["status"])

    async def _mirror_session_status(self, session_id: str, status: str) -> None:
        """Mirror a session status change onto its order/execution rows."""
        conn = self.db.read_conn
        row_ids: list[tuple[int, int]] = []  # (op index, rowid)
        for idx, table in enumerate(("fix_orders", "fix_executions")):
            cursor = await conn.execute(
                f"SELECT id FROM {table} WHERE session_id = ? AND session_status != ?",
                (session_id, status),
            )
            row_ids += [(idx, row["id"]) for row in await cursor.fetchall()]
            await cursor.close()
        if not row_ids:
            return
        mirror_ops = self._compiled_ops["mirror_session_status"]
        ops = tuple(mirror_ops[idx] for idx, _ in row_ids)
        params_list = tuple((status, None, row_id) for _, row_id in row_ids)
        await self.writer.submit(ops, params_list, {"session_id": session_id})

    async def on_app_message(self, session: FixSession, msg_type: str, msg: FixMessage) -> None:
        """Handle an inbound application-level FIX message."""
        if msg_type == "8":
            await self._handle_execution_report(session, msg)
        elif msg_type == "D":
            await self._handle_new_order(session, msg)
        elif msg_type == "F":
            await self._handle_cancel_request(session, msg, "Cancel")
        elif msg_type == "G":
            await self._handle_cancel_request(session, msg, "Replace")
        elif msg_type == "6":
            await self._handle_ioi(session, msg, "RX")
        elif msg_type == "J":
            await self._handle_allocation(session, msg, "RX")

    async def _handle_execution_report(self, session: FixSession, msg: FixMessage) -> None:
        """Process an ExecutionReport (35=8): update order state and record fills."""
        dictionary = session.dictionary
        now = _fix_timestamp()
        cl_ord_id = msg.get("11", "")
        orig_cl_ord_id = msg.get("41", "")
        session_id = session.session_id

        # An accepted cancel/replace re-identifies the chain: tag 11 carries the
        # request's ClOrdID and tag 41 the one it supersedes. Move our order row
        # to the new ClOrdID before upserting under it.
        if orig_cl_ord_id and orig_cl_ord_id != cl_ord_id:
            await self._rename_order(session_id, orig_cl_ord_id, cl_ord_id)

        side_code = msg.get("54", "")
        status_code = msg.get("39", "")
        ord_type_code = msg.get("40", "")
        tif_code = msg.get("59", "")
        exec_type_code = msg.get("150", "")
        trans_type_code = msg.get("20", "0")

        order_row = {
            "cl_ord_id": cl_ord_id,
            "session_id": session_id,
            "order_id": msg.get("37", ""),
            "orig_cl_ord_id": orig_cl_ord_id,
            "symbol": msg.get("55", ""),
            "side": dictionary.enum_name("54", side_code),
            "side_code": side_code,
            "ord_type": dictionary.enum_name("40", ord_type_code),
            "ord_type_code": ord_type_code,
            "price": msg.get_float("44", 0.0),
            "stop_price": msg.get_float("99", 0.0),
            "order_qty": msg.get_float("38", 0.0),
            "time_in_force": dictionary.enum_name("59", tif_code),
            "status": dictionary.enum_name("39", status_code),
            "cum_qty": msg.get_float("14", 0.0),
            "avg_price": msg.get_float("6", 0.0),
            "leaves_qty": msg.get_float("151", 0.0),
            "last_qty": msg.get_float("32", 0.0),
            "last_price": msg.get_float("31", 0.0),
            "text": msg.get("58", ""),
            "transact_time": msg.get("60", ""),
            "created_at": now,
            "updated_at": now,
            "direction": "TX",
            "pending_action": "",
            "pending_cl_ord_id": "",
            "pending_qty": 0.0,
            "pending_price": 0.0,
            "session_status": session.status,
        }
        ops = self._compiled_ops["upsert_order"]
        await self.writer.submit(ops, (_order_params(order_row),), {"cl_ord_id": cl_ord_id})

        # Record fills — and trade corrections/busts, which FIX 4.2 flags via
        # ExecTransType(20) rather than ExecType(150)
        if trans_type_code in ("1", "2"):
            exec_type = dictionary.enum_name("20", trans_type_code)
            exec_code = trans_type_code
        elif exec_type_code in ("1", "2", "F"):
            exec_type = dictionary.enum_name("150", exec_type_code)
            exec_code = exec_type_code
        else:
            return

        # A correction/bust belongs to the trade of the execution it references
        # (tag 19); a new fill starts a new trade.
        trade_id = ""
        exec_ref_id = msg.get("19", "")
        if exec_ref_id:
            trade_id = await self._lookup_trade_id(session_id, exec_ref_id)
        if not trade_id:
            trade_id = await self.ids.next_id("TR")

        exec_row = {
            "session_id": session_id,
            "exec_id": msg.get("17", ""),
            "trade_id": trade_id,
            "order_id": msg.get("37", ""),
            "cl_ord_id": cl_ord_id,
            "symbol": msg.get("55", ""),
            "side": dictionary.enum_name("54", side_code),
            "side_code": side_code,
            "last_qty": msg.get_float("32", 0.0),
            "last_price": msg.get_float("31", 0.0),
            "cum_qty": msg.get_float("14", 0.0),
            "avg_price": msg.get_float("6", 0.0),
            "exec_type": exec_type,
            "exec_type_code": exec_code,
            "leaves_qty": msg.get_float("151", 0.0),
            "transact_time": msg.get("60", ""),
            "text": msg.get("58", ""),
            "timestamp": now,
            "direction": "RX",
            "session_status": session.status,
        }
        ops = self._compiled_ops["insert_execution"]
        await self.writer.submit(ops, (_exec_params(exec_row),), {"exec_id": msg.get("17", "")})

    async def _handle_new_order(self, session: FixSession, msg: FixMessage) -> None:
        """Record an inbound NewOrderSingle (35=D) as a received order awaiting action."""
        dictionary = session.dictionary
        now = _fix_timestamp()
        qty = msg.get_float("38", 0.0)
        side_code = msg.get("54", "")
        ord_type_code = msg.get("40", "")
        tif_code = msg.get("59", "")

        order_row = {
            "cl_ord_id": msg.get("11", ""),
            "session_id": session.session_id,
            "order_id": await self.ids.next_id("OR"),
            "orig_cl_ord_id": "",
            "symbol": msg.get("55", ""),
            "side": dictionary.enum_name("54", side_code),
            "side_code": side_code,
            "ord_type": dictionary.enum_name("40", ord_type_code),
            "ord_type_code": ord_type_code,
            "price": msg.get_float("44", 0.0),
            "stop_price": msg.get_float("99", 0.0),
            "order_qty": qty,
            "time_in_force": dictionary.enum_name("59", tif_code),
            "status": "PendingNew",
            "cum_qty": 0.0,
            "avg_price": 0.0,
            "leaves_qty": qty,
            "last_qty": 0.0,
            "last_price": 0.0,
            "text": msg.get("58", ""),
            "transact_time": msg.get("60", ""),
            "created_at": now,
            "updated_at": now,
            "direction": "RX",
            "pending_action": "New",
            "pending_cl_ord_id": "",
            "pending_qty": 0.0,
            "pending_price": 0.0,
            "session_status": session.status,
        }
        ops = self._compiled_ops["upsert_order"]
        await self.writer.submit(ops, (_order_params(order_row),), {"cl_ord_id": order_row["cl_ord_id"]})

    async def _handle_cancel_request(self, session: FixSession, msg: FixMessage, action: str) -> None:
        """Park an inbound OrderCancelRequest (35=F) or OrderCancelReplaceRequest
        (35=G) on the received order so the market side can accept or reject it.
        The order's status stays live — fills remain possible while a request
        is pending, as on a real market."""
        session_id = session.session_id
        orig_cl_ord_id = msg.get("41", "")
        request_id = msg.get("11", "")
        try:
            order = await self._load_order(session_id, orig_cl_ord_id)
        except ValueError:
            reject = session.factory.order_cancel_reject(
                cl_ord_id=request_id,
                orig_cl_ord_id=orig_cl_ord_id,
                ord_status="8",
                response_to="1" if action == "Cancel" else "2",
                text=f"Unknown order: {orig_cl_ord_id}",
            )
            await session.send_message(reject)
            return

        await self._write_order(
            order,
            pending_action=action,
            pending_cl_ord_id=request_id,
            pending_qty=msg.get_float("38", 0.0),
            pending_price=msg.get_float("44", 0.0),
        )

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

        cl_ord_id = await self.ids.next_id("RT")
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
        order_row = {
            "cl_ord_id": cl_ord_id,
            "session_id": session_id,
            "order_id": await self.ids.next_id("OR"),
            "orig_cl_ord_id": "",
            "symbol": symbol,
            "side": dictionary.enum_name("54", side),
            "side_code": side,
            "ord_type": dictionary.enum_name("40", ord_type),
            "ord_type_code": ord_type,
            "price": price or 0.0,
            "stop_price": 0.0,
            "order_qty": qty,
            "time_in_force": dictionary.enum_name("59", tif),
            "status": "PendingNew",
            "cum_qty": 0.0,
            "avg_price": 0.0,
            "leaves_qty": qty,
            "last_qty": 0.0,
            "last_price": 0.0,
            "text": "",
            "transact_time": now,
            "created_at": now,
            "updated_at": now,
            "direction": "TX",
            "pending_action": "",
            "pending_cl_ord_id": "",
            "pending_qty": 0.0,
            "pending_price": 0.0,
            "session_status": session.status,
        }
        ops = self._compiled_ops["upsert_order"]
        await self.writer.submit(ops, (_order_params(order_row),), {"cl_ord_id": cl_ord_id})

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

        cl_ord_id = await self.ids.next_id("RT")
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

        cl_ord_id = await self.ids.next_id("RT")
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

    # ── Market-side actions (received orders, sent trades) ───────────

    def _active_session(self, session_id: str) -> FixSession:
        session = self.sessions.get(session_id)
        if not session or not session.is_active:
            raise ValueError(f"Session {session_id} is not active")
        return session

    async def _load_order(self, session_id: str, cl_ord_id: str) -> dict[str, Any]:
        conn = self.db.read_conn
        cursor = await conn.execute(
            "SELECT * FROM fix_orders WHERE session_id = ? AND cl_ord_id = ?",
            (session_id, cl_ord_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            raise ValueError(f"Unknown order: {cl_ord_id} on {session_id}")
        return dict(row)

    async def _load_order_for_execution(self, execution: dict[str, Any]) -> dict[str, Any]:
        """Resolve an execution's order by its immutable order_id: the row's
        cl_ord_id may be stale after an accepted replace renamed the chain."""
        session_id = execution["session_id"]
        if not execution["order_id"]:
            return await self._load_order(session_id, execution["cl_ord_id"])
        conn = self.db.read_conn
        cursor = await conn.execute(
            "SELECT * FROM fix_orders WHERE session_id = ? AND order_id = ?",
            (session_id, execution["order_id"]),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            raise ValueError(f"Unknown order: {execution['order_id']} on {session_id}")
        return dict(row)

    async def _load_execution(self, session_id: str, exec_id: str) -> dict[str, Any]:
        conn = self.db.read_conn
        cursor = await conn.execute(
            "SELECT * FROM fix_executions WHERE session_id = ? AND exec_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (session_id, exec_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            raise ValueError(f"Unknown execution: {exec_id} on {session_id}")
        return dict(row)

    async def _write_order(self, order: dict[str, Any], **updates: Any) -> None:
        row = {**order, **updates, "updated_at": _fix_timestamp()}
        ops = self._compiled_ops["upsert_order"]
        await self.writer.submit(ops, (_order_params(row),), {"cl_ord_id": row["cl_ord_id"]})

    async def _rename_order(self, session_id: str, old_cl_ord_id: str, new_cl_ord_id: str) -> None:
        """Move an order row to its next ClOrdID after an accepted cancel/replace."""
        if not old_cl_ord_id or old_cl_ord_id == new_cl_ord_id:
            return
        params = (new_cl_ord_id, old_cl_ord_id, _fix_timestamp(), None,
                  session_id, old_cl_ord_id, new_cl_ord_id)
        ops = self._compiled_ops["rename_order"]
        await self.writer.submit(ops, (params,), {"cl_ord_id": new_cl_ord_id})

    async def _lookup_trade_id(self, session_id: str, exec_id: str) -> str:
        cursor = await self.db.read_conn.execute(
            "SELECT trade_id FROM fix_executions WHERE session_id = ? AND exec_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (session_id, exec_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["trade_id"] if row else ""

    async def _write_sent_execution(
        self, order: dict[str, Any], exec_id: str, trade_id: str, exec_type: str,
        exec_type_code: str, last_qty: float, last_price: float,
        cum_qty: float, avg_price: float, leaves_qty: float,
    ) -> None:
        now = _fix_timestamp()
        exec_row = {
            "session_id": order["session_id"],
            "exec_id": exec_id,
            "trade_id": trade_id,
            "order_id": order["order_id"],
            "cl_ord_id": order["cl_ord_id"],
            "symbol": order["symbol"],
            "side": order["side"],
            "side_code": order["side_code"],
            "last_qty": last_qty,
            "last_price": last_price,
            "cum_qty": cum_qty,
            "avg_price": avg_price,
            "exec_type": exec_type,
            "exec_type_code": exec_type_code,
            "leaves_qty": leaves_qty,
            "transact_time": now,
            "text": "",
            "timestamp": now,
            "direction": "TX",
            "session_status": order["session_status"],
        }
        ops = self._compiled_ops["insert_execution"]
        await self.writer.submit(ops, (_exec_params(exec_row),), {"exec_id": exec_id})

    async def accept_order(self, session_id: str, cl_ord_id: str) -> str:
        """Accept a received order: send ExecutionReport(New), return the OrderID."""
        session = self._active_session(session_id)
        order = await self._load_order(session_id, cl_ord_id)

        order_id = order["order_id"] or await self.ids.next_id("OR")
        msg = session.factory.execution_report(
            order_id=order_id,
            cl_ord_id=cl_ord_id,
            exec_id=await self.ids.next_id("EX"),
            exec_trans_type="0",
            exec_type="0",
            ord_status="0",
            symbol=order["symbol"],
            side=order["side_code"],
            qty=order["order_qty"],
            leaves_qty=order["order_qty"],
        )
        await session.send_message(msg)

        await self._write_order(order, order_id=order_id, status="New", pending_action="")
        return order_id

    async def reject_order(self, session_id: str, cl_ord_id: str, text: str = "") -> None:
        """Reject a received order: send ExecutionReport(Rejected)."""
        session = self._active_session(session_id)
        order = await self._load_order(session_id, cl_ord_id)

        msg = session.factory.execution_report(
            order_id=order["order_id"],
            cl_ord_id=cl_ord_id,
            exec_id=await self.ids.next_id("EX"),
            exec_trans_type="0",
            exec_type="8",
            ord_status="8",
            symbol=order["symbol"],
            side=order["side_code"],
            qty=order["order_qty"],
            cum_qty=order["cum_qty"],
            avg_price=order["avg_price"],
            text=text or None,
        )
        await session.send_message(msg)

        await self._write_order(order, status="Rejected", leaves_qty=0.0, text=text,
                                pending_action="")

    async def fill_order(
        self, session_id: str, cl_ord_id: str, qty: float, price: float,
    ) -> str:
        """Fill a received order (partially or fully) and return the ExecID."""
        if qty <= 0:
            raise ValueError("Fill quantity must be positive")
        session = self._active_session(session_id)
        dictionary = session.dictionary
        order = await self._load_order(session_id, cl_ord_id)

        order_id = order["order_id"] or await self.ids.next_id("OR")
        exec_id = await self.ids.next_id("EX")
        trade_id = await self.ids.next_id("TR")
        cum_qty = order["cum_qty"] + qty
        leaves_qty = max(order["order_qty"] - cum_qty, 0.0)
        avg_price = (order["avg_price"] * order["cum_qty"] + qty * price) / cum_qty
        status_code = "2" if leaves_qty == 0 else "1"

        msg = session.factory.execution_report(
            order_id=order_id,
            cl_ord_id=cl_ord_id,
            exec_id=exec_id,
            exec_trans_type="0",
            exec_type=status_code,
            ord_status=status_code,
            symbol=order["symbol"],
            side=order["side_code"],
            qty=order["order_qty"],
            last_qty=qty,
            last_price=price,
            cum_qty=cum_qty,
            avg_price=avg_price,
            leaves_qty=leaves_qty,
        )
        await session.send_message(msg)

        # A fill on a not-yet-accepted order implicitly acknowledges it, so the
        # pending New is consumed; a pending Cancel/Replace stays parked.
        pending_action = "" if order["pending_action"] == "New" else order["pending_action"]
        await self._write_order(
            order, order_id=order_id, status=dictionary.enum_name("39", status_code),
            cum_qty=cum_qty, avg_price=avg_price, leaves_qty=leaves_qty,
            last_qty=qty, last_price=price, pending_action=pending_action,
        )
        await self._write_sent_execution(
            {**order, "order_id": order_id}, exec_id, trade_id,
            dictionary.enum_name("150", status_code), status_code,
            qty, price, cum_qty, avg_price, leaves_qty,
        )
        return exec_id

    async def accept_request(self, session_id: str, cl_ord_id: str) -> str:
        """Accept whatever is pending on the order — the new order itself,
        a cancel request, or a replace request."""
        order = await self._load_order(session_id, cl_ord_id)
        action = order["pending_action"]
        if action == "New":
            return await self.accept_order(session_id, cl_ord_id)
        if action == "Cancel":
            return await self.accept_cancel(session_id, cl_ord_id)
        if action == "Replace":
            return await self.accept_replace(session_id, cl_ord_id)
        raise ValueError(f"Nothing pending on {cl_ord_id}")

    async def reject_request(self, session_id: str, cl_ord_id: str, text: str = "") -> None:
        """Reject whatever is pending on the order — ExecutionReport(Rejected)
        for a new order, OrderCancelReject for a cancel/replace request."""
        order = await self._load_order(session_id, cl_ord_id)
        action = order["pending_action"]
        if action == "New":
            await self.reject_order(session_id, cl_ord_id, text=text)
        elif action in ("Cancel", "Replace"):
            await self.reject_cancel(session_id, cl_ord_id, text=text)
        else:
            raise ValueError(f"Nothing pending on {cl_ord_id}")

    async def accept_cancel(self, session_id: str, cl_ord_id: str) -> str:
        """Accept the pending cancel request: send ExecutionReport(Canceled)."""
        session = self._active_session(session_id)
        order = await self._load_order(session_id, cl_ord_id)
        if order["pending_action"] != "Cancel":
            raise ValueError(f"No pending cancel request on {cl_ord_id}")

        # The accepted cancel re-identifies the chain: the ER answers the
        # request's ClOrdID (11), referencing the one it supersedes (41).
        new_cl_ord_id = order["pending_cl_ord_id"]
        exec_id = await self.ids.next_id("EX")
        msg = session.factory.execution_report(
            order_id=order["order_id"],
            cl_ord_id=new_cl_ord_id,
            exec_id=exec_id,
            exec_trans_type="0",
            exec_type="4",
            ord_status="4",
            symbol=order["symbol"],
            side=order["side_code"],
            qty=order["order_qty"],
            cum_qty=order["cum_qty"],
            avg_price=order["avg_price"],
            leaves_qty=0.0,
            **{"41": cl_ord_id},
        )
        await session.send_message(msg)

        await self._rename_order(session_id, cl_ord_id, new_cl_ord_id)
        await self._write_order(
            {**order, "cl_ord_id": new_cl_ord_id, "orig_cl_ord_id": cl_ord_id},
            status="Canceled", leaves_qty=0.0,
            pending_action="", pending_cl_ord_id="",
            pending_qty=0.0, pending_price=0.0,
        )
        return exec_id

    async def accept_replace(self, session_id: str, cl_ord_id: str) -> str:
        """Accept the pending cancel/replace request: send ExecutionReport(Replaced)
        with the requested quantity and price."""
        session = self._active_session(session_id)
        order = await self._load_order(session_id, cl_ord_id)
        if order["pending_action"] != "Replace":
            raise ValueError(f"No pending replace request on {cl_ord_id}")

        new_qty = order["pending_qty"] or order["order_qty"]
        new_price = order["pending_price"] or order["price"]
        if new_qty < order["cum_qty"]:
            raise ValueError("Replaced quantity is below executed quantity")

        # The accepted replace re-identifies the chain: the ER answers the
        # request's ClOrdID (11), referencing the one it supersedes (41).
        new_cl_ord_id = order["pending_cl_ord_id"]
        leaves_qty = new_qty - order["cum_qty"]
        exec_id = await self.ids.next_id("EX")
        msg = session.factory.execution_report(
            order_id=order["order_id"],
            cl_ord_id=new_cl_ord_id,
            exec_id=exec_id,
            exec_trans_type="0",
            exec_type="5",
            ord_status="5",
            symbol=order["symbol"],
            side=order["side_code"],
            qty=new_qty,
            cum_qty=order["cum_qty"],
            avg_price=order["avg_price"],
            leaves_qty=leaves_qty,
            **{"41": cl_ord_id, "44": str(new_price)},
        )
        await session.send_message(msg)

        await self._rename_order(session_id, cl_ord_id, new_cl_ord_id)
        await self._write_order(
            {**order, "cl_ord_id": new_cl_ord_id, "orig_cl_ord_id": cl_ord_id},
            status="Replaced", order_qty=new_qty, price=new_price,
            leaves_qty=leaves_qty,
            pending_action="", pending_cl_ord_id="",
            pending_qty=0.0, pending_price=0.0,
        )
        return exec_id

    async def reject_cancel(self, session_id: str, cl_ord_id: str, text: str = "") -> None:
        """Reject the pending cancel/replace request: send OrderCancelReject (35=9).
        The order itself is untouched — its status was never changed by the request."""
        session = self._active_session(session_id)
        dictionary = session.dictionary
        order = await self._load_order(session_id, cl_ord_id)
        if not order["pending_action"]:
            raise ValueError(f"No pending cancel/replace request on {cl_ord_id}")

        msg = session.factory.order_cancel_reject(
            cl_ord_id=order["pending_cl_ord_id"],
            orig_cl_ord_id=cl_ord_id,
            ord_status=dictionary.enum_code("39", order["status"]),
            response_to="1" if order["pending_action"] == "Cancel" else "2",
            order_id=order["order_id"],
            text=text or None,
        )
        await session.send_message(msg)

        await self._write_order(
            order, pending_action="", pending_cl_ord_id="",
            pending_qty=0.0, pending_price=0.0,
        )

    async def correct_trade(
        self, session_id: str, exec_id: str, qty: float, price: float,
    ) -> str:
        """Correct a sent trade (ExecTransType=Correct) and return the new ExecID."""
        if qty <= 0:
            raise ValueError("Corrected quantity must be positive")
        session = self._active_session(session_id)
        dictionary = session.dictionary
        execution = await self._load_execution(session_id, exec_id)
        order = await self._load_order_for_execution(execution)

        new_exec_id = await self.ids.next_id("EX")
        trade_id = execution["trade_id"] or await self.ids.next_id("TR")
        cum_qty = max(order["cum_qty"] - execution["last_qty"] + qty, 0.0)
        leaves_qty = max(order["order_qty"] - cum_qty, 0.0)
        notional = (order["avg_price"] * order["cum_qty"]
                    - execution["last_qty"] * execution["last_price"] + qty * price)
        avg_price = notional / cum_qty if cum_qty > 0 else 0.0
        status_code = "0" if cum_qty == 0 else ("2" if leaves_qty == 0 else "1")

        msg = session.factory.execution_report(
            order_id=order["order_id"],
            cl_ord_id=order["cl_ord_id"],
            exec_id=new_exec_id,
            exec_trans_type="2",
            exec_type=status_code,
            ord_status=status_code,
            symbol=execution["symbol"],
            side=execution["side_code"],
            qty=order["order_qty"],
            last_qty=qty,
            last_price=price,
            cum_qty=cum_qty,
            avg_price=avg_price,
            leaves_qty=leaves_qty,
            exec_ref_id=exec_id,
        )
        await session.send_message(msg)

        await self._write_order(
            order, status=dictionary.enum_name("39", status_code),
            cum_qty=cum_qty, avg_price=avg_price, leaves_qty=leaves_qty,
            last_qty=qty, last_price=price,
        )
        await self._write_sent_execution(
            order, new_exec_id, trade_id, dictionary.enum_name("20", "2"), "2",
            qty, price, cum_qty, avg_price, leaves_qty,
        )
        return new_exec_id

    async def bust_trade(self, session_id: str, exec_id: str) -> str:
        """Bust a sent trade (ExecTransType=Cancel) and return the new ExecID."""
        session = self._active_session(session_id)
        dictionary = session.dictionary
        execution = await self._load_execution(session_id, exec_id)
        order = await self._load_order_for_execution(execution)

        new_exec_id = await self.ids.next_id("EX")
        trade_id = execution["trade_id"] or await self.ids.next_id("TR")
        cum_qty = max(order["cum_qty"] - execution["last_qty"], 0.0)
        leaves_qty = max(order["order_qty"] - cum_qty, 0.0)
        notional = (order["avg_price"] * order["cum_qty"]
                    - execution["last_qty"] * execution["last_price"])
        avg_price = notional / cum_qty if cum_qty > 0 else 0.0
        status_code = "0" if cum_qty == 0 else "1"

        msg = session.factory.execution_report(
            order_id=order["order_id"],
            cl_ord_id=order["cl_ord_id"],
            exec_id=new_exec_id,
            exec_trans_type="1",
            exec_type=status_code,
            ord_status=status_code,
            symbol=execution["symbol"],
            side=execution["side_code"],
            qty=order["order_qty"],
            last_qty=execution["last_qty"],
            last_price=execution["last_price"],
            cum_qty=cum_qty,
            avg_price=avg_price,
            leaves_qty=leaves_qty,
            exec_ref_id=exec_id,
        )
        await session.send_message(msg)

        await self._write_order(
            order, status=dictionary.enum_name("39", status_code),
            cum_qty=cum_qty, avg_price=avg_price, leaves_qty=leaves_qty,
            last_qty=execution["last_qty"], last_price=execution["last_price"],
        )
        await self._write_sent_execution(
            order, new_exec_id, trade_id, dictionary.enum_name("20", "1"), "1",
            execution["last_qty"], execution["last_price"],
            cum_qty, avg_price, leaves_qty,
        )
        return new_exec_id

    async def reset_sequence(self, session_id: str, tx: int = 1, rx: int = 1) -> None:
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError(f"Unknown session: {session_id}")
        await session.reset_sequence_numbers(tx, rx)

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
