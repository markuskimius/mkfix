"""FIX engine: manages sessions, bridges FIX messages to mkio database."""

from __future__ import annotations

import asyncio
import functools
import json
import uuid
from typing import Any, TYPE_CHECKING

from mkfix.fix.dictionary import (FixDictionary, STANDARD_VERSIONS,
                                  custom_meta, custom_names, register_custom,
                                  unregister_custom)
from mkfix.fix.actions import ACTIONS, ORDER_KEY, TRADE_KEY
from mkfix.fix.events import EngineEvent, EventBus, report_kinds
from mkfix.fix.idgen import IdGenerator
from mkfix.fix.message import (FixMessage, _fix_timestamp, parse_extra_tags,
                               extra_pairs_of, format_extra_tags, parse_fix,
                               CONSUMED_EXEC_TAGS,
                               ClientTag, client_of, parse_client_tags)
from mkfix.fix import replay
from mkfix.fix.replay import ReplayTask
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
    "pending_extra_tags",
    "tif_code", "extra_tags", "entered_qty", "entered_price",
    "expire_time", "expire_date", "client",
    "handl_inst", "handl_inst_code", "sent_text",
]

# The as-submitted terms of a sent order — what the New dialog or the latest
# Replace dialog carried. Kept outside the ER upsert so the counterparty's
# ExecutionReports never rewrite them; the Replace dialog prefills from them.
# sent_text rides along — the Text(58) of the last message this engine sent on
# the order, which a Cancel rewrites too and no dialog prefills from.
ENTERED_COLS = [
    "symbol", "side", "side_code", "ord_type", "ord_type_code",
    "time_in_force", "tif_code", "extra_tags", "entered_qty", "entered_price",
    "expire_time", "expire_date", "client",
    "handl_inst", "handl_inst_code", "sent_text",
]

ORDER_UPDATE_COLS = [
    "status", "cum_qty", "avg_price", "leaves_qty",
    "last_qty", "last_price", "text", "transact_time", "updated_at",
    "pending_action", "pending_cl_ord_id", "pending_qty", "pending_price",
    "pending_extra_tags",
]

# The request slot. On a received order it parks the counterparty's
# cancel/replace (or the New) until Accept/Reject; on a sent order it holds
# the latest request this engine sent, until the ER or OrderCancelReject
# answering its ClOrdID. One slot: a later request overwrites an earlier one,
# whose answer then matches nothing and leaves the slot alone.
PENDING_COLS = [
    "pending_action", "pending_cl_ord_id", "pending_qty", "pending_price",
    "pending_extra_tags",
]
# pending_entered rides in a sent order's slot only: a replace request's
# ENTERED_COLS as JSON, which become the row's entered terms when the request
# is accepted — a refused replace must not be what the next Replace dialog
# opens on.
_PENDING_BLANKS = {"pending_action": "''", "pending_cl_ord_id": "''", "pending_qty": "0",
                   "pending_price": "0", "pending_extra_tags": "''", "pending_entered": "''"}

EXEC_COLS = [
    "session_id", "exec_id", "exec_ref_id", "trade_id", "order_id", "cl_ord_id",
    "symbol", "side", "side_code", "last_qty", "last_price", "cum_qty",
    "avg_price", "exec_type", "exec_type_code", "leaves_qty",
    "transact_time", "text", "timestamp", "direction", "client", "extra_tags",
]

# exec_type display names a bust records (ExecTransType Cancel through FIX
# 4.2, ExecType TradeCancel from 4.3); the trade blotters' gates test the same.
BUSTED_EXEC_TYPES = ("Cancel", "TradeCancel")
CORRECTED_EXEC_TYPES = ("Correct", "TradeCorrect")

# What a correction or bust rewrites on its trade's row: everything but the
# identity (session, trade_id, direction). The DK columns go back to blank:
# a DontKnowTrade answered the ExecID the row no longer carries.
EXEC_UPDATE_COLS = [
    "exec_id", "exec_ref_id", "order_id", "cl_ord_id", "symbol", "side",
    "side_code", "last_qty", "last_price", "cum_qty", "avg_price",
    "exec_type", "exec_type_code", "leaves_qty", "transact_time", "text",
    "timestamp", "dk_reason", "dk_text", "extra_tags",
]

# Blotter action templates (fix_templates): the scopes a dialog can load and
# save one under, and the term columns kept as typed (text; blank means
# "ask the row" when loaded). A name is unique within its scope, so a
# dialog's Save-as overwrites the template it names.
TEMPLATE_SCOPES = ("order", "cancel", "accept", "reject", "fill", "unsolicited", "restate", "dk", "correct",
                   "bust", "renotify")
TEMPLATE_TERM_COLS = [
    "session_id", "symbol", "side", "ord_type", "qty", "price", "tif",
    "dk_reason", "restate_reason", "text", "extra_tags", "client", "handl_inst",
]

# A replay job: what Load found in the file (summary and its readable
# columns), what Configure chose, and the run's own progress. The direction
# is asked at every Start, so the job keeps only the one it played last.
REPLAY_CONFIG_COLS = ["target_session", "speed", "msg_filter", "time_from", "time_to", "max_gap",
                      "default_direction"]
REPLAY_JOB_COLS = ["name", "file_path", "status", "total_messages", "sent_messages", "selected_messages",
                   "error_text", "created_at", "summary", "pairs", "first_time", "last_time", "direction",
                   ] + REPLAY_CONFIG_COLS


def _order_params(row: dict[str, Any], keep_sent_text: bool = False,
                  keep_pending: bool = False) -> tuple[Any, ...]:
    """Upsert parameters. sent_text is ours alone: an inbound ExecutionReport
    passes `keep_sent_text` so the update leaves the column as it stands, and
    `keep_pending` likewise — on a sent order pending_* is the request still
    outstanding, which only its answer clears (rename_order, resolve_request)."""
    insert = tuple(row[c] for c in ORDER_COLS)
    update = tuple(None if keep_pending and c in PENDING_COLS else row[c]
                   for c in ORDER_UPDATE_COLS)
    return insert + (None,) + update + (None if keep_sent_text else row["sent_text"],)


def _exec_params(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row[c] for c in EXEC_COLS) + (None,)


def _exec_update_params(row: dict[str, Any], row_id: int) -> tuple[Any, ...]:
    return tuple(row[c] for c in EXEC_UPDATE_COLS) + (None, row_id)


def _sent_exec_kind(dictionary: FixDictionary, msg: FixMessage,
                    fallback_code: str) -> tuple[str, str]:
    """Display name and code for a sent execution row, from what actually went
    on the wire: corrects/busts ride ExecTransType(20) through FIX 4.2 and
    ExecType(150) G/H from 4.3 on; fills ride 150 where it exists (F from
    4.3), with the 4.2-style code as the FIX 4.0 fallback."""
    trans_type = msg.get("20", "")
    if trans_type in ("1", "2"):
        return dictionary.enum_name("20", trans_type), trans_type
    code = msg.get("150") or fallback_code
    return dictionary.enum_name("150", code), code


def _client_specs_of(spec: str | None) -> list[ClientTag]:
    """A session's client_tags, the default chain when blank or malformed —
    the column is written by a plain mkio transaction, so a bad value must
    not stop messages being recorded."""
    try:
        return parse_client_tags(spec or "")
    except ValueError:
        return parse_client_tags("")


def _market_action(fn):
    """A market-side action: ``fn`` loads its order, builds the answer and
    submits its writes, returning ``(message, result)``; this holds the
    session's order lock across all of that, then sends the message with the
    lock released and returns the result.

    The lock is what a script acting at wire speed needs and a hand on a
    button rarely did: these actions write the whole of ORDER_UPDATE_COLS
    back from the snapshot they loaded, so a cancel/replace request the read
    loop parks between the load and the write lost its pending_* columns.
    The send stays outside it — it awaits, the counterparty may answer inside
    it, and that answer's handler takes the same lock."""
    @functools.wraps(fn)
    async def action(self: "FixEngine", session_id: str, *args: Any, **kwargs: Any) -> Any:
        session = self._active_session(session_id)
        async with self._order_lock(session_id):
            msg, result = await fn(self, session_id, *args, **kwargs)
        await session.send_message(msg)
        return result
    return action


class FixEngine:
    """Manages all FIX sessions and bridges messages to mkio's database."""

    def __init__(self, db: Database, writer: WriteBatcher, instance_code: str | None = None):
        self.db = db
        self.writer = writer
        self.ids = IdGenerator(db, writer, instance_code)
        self.sessions: dict[str, FixSession] = {}
        self._compiled_ops: dict[str, tuple[CompiledOp, ...]] = {}
        self._replay_tasks: dict[int, ReplayTask] = {}
        self.events = EventBus()
        self._order_locks: dict[str, asyncio.Lock] = {}
        from mkfix.macro.store import MacroManager    # imports this module's siblings
        self.macros = MacroManager(self)

    async def start(self) -> None:
        """Load session configs from DB and compile write operations."""
        self._compile_ops()
        await self._ensure_indexes()
        await self._backfill_entered_terms()
        await self._backfill_rx_extra_tags()
        await self._backfill_client()
        await self._backfill_handling_and_trade_tags()
        await self.ids.start()
        await self._load_custom_dictionaries()
        await self._load_sessions()
        await self.macros.start()

    async def stop(self) -> None:
        """Stop all sessions and replay tasks."""
        await self.macros.stop()
        for task in list(self._replay_tasks.values()):
            await task.stop()
        self._replay_tasks.clear()
        # Each stop may wait up to its logout_timeout for the peer's
        # confirming Logout, so the sessions log out side by side.
        await asyncio.gather(*(s.stop() for s in list(self.sessions.values())))
        self.sessions.clear()

    def _compile_ops(self) -> None:
        """Pre-compile SQL operations for writing FIX data."""
        from mkio.writer import CompiledOp

        self._compiled_ops["upsert_dictionary"] = (CompiledOp(
            table="fix_dictionaries",
            op_type="upsert",
            sql=(
                "INSERT INTO fix_dictionaries (name, base_version, doc, created_at, updated_at, _mkio_ref) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET base_version = excluded.base_version, "
                "doc = excluded.doc, updated_at = excluded.updated_at, _mkio_ref = excluded._mkio_ref "
                "RETURNING *"
            ),
            param_names=("name", "base_version", "doc", "created_at", "updated_at", "_mkio_ref"),
        ),)

        self._compiled_ops["delete_dictionary"] = (CompiledOp(
            table="fix_dictionaries",
            op_type="delete",
            sql="DELETE FROM fix_dictionaries WHERE name = ? RETURNING *",
            param_names=("name",),
        ),)

        msg_cols = [
            "session_id", "timestamp", "direction", "seq_num", "msg_type",
            "msg_type_name", "category", "raw_message", "sender_comp",
            "target_comp", "cl_ord_id", "order_id", "exec_id", "symbol",
            "side", "body_length", "checksum", "client",
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
                      "last_tx_time", "last_rx_time", "session_start", "error_text",
                      "seq_epoch"]
        set_clause = ", ".join(f"{c} = ?" for c in state_cols[1:])
        # Follows an undo that removes a session row (handle_undo_redo): the
        # state table is not versioned, so the cursor move leaves it behind.
        self._compiled_ops["delete_state"] = (CompiledOp(
            table="fix_session_state",
            op_type="delete",
            sql="DELETE FROM fix_session_state WHERE session_id = ?",
            param_names=("session_id",),
        ),)

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
        ),)

        order_set = ", ".join(
            f"{c} = coalesce(?, fix_orders.{c})" if c in PENDING_COLS else f"{c} = ?"
            for c in ORDER_UPDATE_COLS)
        # order_qty/price update from the insert values, guarded so an
        # ExecutionReport without tag 38/44 can't zero them out. order_id is
        # write-once: the immutable Order ID assigned when the order is created
        # must survive later ERs carrying the counterparty's OrderID(37).
        order_set += (
            ", order_qty = iif(excluded.order_qty = 0, fix_orders.order_qty, excluded.order_qty)"
            ", price = iif(excluded.price = 0, fix_orders.price, excluded.price)"
            ", order_id = iif(fix_orders.order_id = '', excluded.order_id, fix_orders.order_id)"
            ", sent_text = coalesce(?, fix_orders.sent_text)"
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
            param_names=tuple(ORDER_COLS + ["_mkio_ref"] + ORDER_UPDATE_COLS + ["sent_text"]),
        ),)

        # Records the terms a Replace dialog (or scripted cancel/replace)
        # submitted, so the next Replace dialog opens on them. Zero rows when
        # the referenced order is unknown — the request still went out.
        self._compiled_ops["record_entered"] = (CompiledOp(
            table="fix_orders",
            op_type="update",
            sql=(
                f"UPDATE fix_orders SET {', '.join(c + ' = ?' for c in ENTERED_COLS)}, "
                "updated_at = ?, _mkio_ref = ? WHERE session_id = ? AND cl_ord_id = ? RETURNING *"
            ),
            param_names=tuple(ENTERED_COLS + ["updated_at", "_mkio_ref", "session_id", "cl_ord_id"]),
        ),)

        # record_entered for a request this engine sends: the same terms plus
        # the request slot, in one write so the request is one row version.
        # A new request also retires the previous one's reject note.
        self._compiled_ops["record_request"] = (CompiledOp(
            table="fix_orders",
            op_type="update",
            sql=(
                f"UPDATE fix_orders SET {', '.join(c + ' = ?' for c in ENTERED_COLS)}, "
                "pending_action = ?, pending_cl_ord_id = ?, pending_qty = ?, pending_price = ?, "
                "pending_entered = ?, cxl_rej_reason = '', updated_at = ?, _mkio_ref = ? "
                "WHERE session_id = ? AND cl_ord_id = ? RETURNING *"
            ),
            param_names=tuple(ENTERED_COLS + [
                "pending_action", "pending_cl_ord_id", "pending_qty", "pending_price",
                "pending_entered", "updated_at", "_mkio_ref", "session_id", "cl_ord_id"]),
        ),)

        slot_clear = ", ".join(f"{c} = {blank}" for c, blank in _PENDING_BLANKS.items())
        # A request that never reached the wire is not outstanding.
        self._compiled_ops["clear_request"] = (CompiledOp(
            table="fix_orders",
            op_type="update",
            sql=(
                f"UPDATE fix_orders SET {slot_clear}, updated_at = ?, _mkio_ref = ? "
                "WHERE session_id = ? AND direction = 'TX' AND pending_cl_ord_id = ? "
                "AND pending_cl_ord_id != '' RETURNING *"
            ),
            param_names=("updated_at", "_mkio_ref", "session_id", "pending_cl_ord_id"),
        ),)

        # An answer clears the slot only when it answers the request the slot
        # holds; SQLite evaluates every right-hand side against the old row.
        slot_answered = ", ".join(
            f"{c} = iif(pending_cl_ord_id = ?, {blank}, {c})" for c, blank in _PENDING_BLANKS.items())
        # An inbound OrderCancelReject: the counterparty's view of the order
        # (OrdStatus, kept when the reject carries none), its Text, and which
        # request it refused.
        self._compiled_ops["resolve_request"] = (CompiledOp(
            table="fix_orders",
            op_type="update",
            sql=(
                "UPDATE fix_orders SET status = iif(? = '', status, ?), text = ?, "
                f"cxl_rej_reason = ?, {slot_answered}, updated_at = ?, _mkio_ref = ? "
                "WHERE id = ? RETURNING *"
            ),
            param_names=("status", "status", "text", "cxl_rej_reason",
                         *(["pending_cl_ord_id"] * len(_PENDING_BLANKS)),
                         "updated_at", "_mkio_ref", "id"),
        ),)

        # Moves an order chain to its next ClOrdID when a cancel/replace is
        # accepted — which answers the request of that ClOrdID, so the slot
        # holding it clears in the same write. Guarded so a duplicate ER can't
        # collide with an existing row.
        self._compiled_ops["rename_order"] = (CompiledOp(
            table="fix_orders",
            op_type="update",
            sql=(
                f"UPDATE fix_orders SET cl_ord_id = ?, orig_cl_ord_id = ?, {slot_answered}, "
                "updated_at = ?, _mkio_ref = ? "
                "WHERE session_id = ? AND cl_ord_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM fix_orders x "
                "WHERE x.session_id = fix_orders.session_id AND x.cl_ord_id = ?) "
                "RETURNING *"
            ),
            param_names=("cl_ord_id", "orig_cl_ord_id",
                         *(["pending_cl_ord_id"] * len(_PENDING_BLANKS)),
                         "updated_at", "_mkio_ref",
                         "session_id", "old_cl_ord_id", "new_cl_ord_id"),
        ),)

        exec_placeholders = ", ".join(["?"] * (len(EXEC_COLS) + 1))
        exec_col_str = ", ".join(EXEC_COLS + ["_mkio_ref"])
        self._compiled_ops["insert_execution"] = (CompiledOp(
            table="fix_executions",
            op_type="insert",
            sql=f"INSERT INTO fix_executions ({exec_col_str}) VALUES ({exec_placeholders}) RETURNING *",
            param_names=tuple(EXEC_COLS + ["_mkio_ref"]),
        ),)
        # A correction or bust is a new version of its trade's row, not a new
        # row: keyed by the row id so databases holding pre-0.27 append-only
        # chains need no unique index and keep working.
        exec_set = ", ".join(f"{c} = ?" for c in EXEC_UPDATE_COLS + ["_mkio_ref"])
        self._compiled_ops["update_execution"] = (CompiledOp(
            table="fix_executions",
            op_type="update",
            sql=f"UPDATE fix_executions SET {exec_set} WHERE id = ? RETURNING *",
            param_names=tuple(EXEC_UPDATE_COLS + ["_mkio_ref", "id"]),
        ),)
        # An inbound DontKnowTrade marks the sent trade it names; the trade's
        # own terms stay, so the DK is a new version of the row, not a report.
        self._compiled_ops["dk_execution"] = (CompiledOp(
            table="fix_executions",
            op_type="update",
            sql="UPDATE fix_executions SET dk_reason = ?, dk_text = ?, _mkio_ref = ? "
                "WHERE id = ? RETURNING *",
            param_names=("dk_reason", "dk_text", "_mkio_ref", "id"),
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

        tmpl_cols = ["scope", "name"] + TEMPLATE_TERM_COLS
        tmpl_set = ", ".join(f"{c} = excluded.{c}" for c in TEMPLATE_TERM_COLS)
        self._compiled_ops["upsert_template"] = (CompiledOp(
            table="fix_templates",
            op_type="upsert",
            sql=(
                f"INSERT INTO fix_templates ({', '.join(tmpl_cols)}, _mkio_ref) "
                f"VALUES ({', '.join(['?'] * (len(tmpl_cols) + 1))}) "
                f"ON CONFLICT(scope, name) DO UPDATE SET {tmpl_set}, _mkio_ref = excluded._mkio_ref "
                f"RETURNING *"
            ),
            param_names=tuple(tmpl_cols + ["_mkio_ref"]),
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
        job_ph = ", ".join(["?"] * (len(REPLAY_JOB_COLS) + 1))
        self._compiled_ops["insert_replay"] = (CompiledOp(
            table="fix_replay_jobs",
            op_type="insert",
            sql=(f"INSERT INTO fix_replay_jobs ({', '.join(REPLAY_JOB_COLS)}, _mkio_ref) "
                 f"VALUES ({job_ph}) RETURNING *"),
            param_names=tuple(REPLAY_JOB_COLS + ["_mkio_ref"]),
        ),)
        cfg_set = ", ".join(f"{c} = ?" for c in REPLAY_CONFIG_COLS)
        self._compiled_ops["configure_replay"] = (CompiledOp(
            table="fix_replay_jobs",
            op_type="update",
            sql=f"UPDATE fix_replay_jobs SET {cfg_set}, _mkio_ref = ? WHERE id = ? RETURNING *",
            param_names=tuple(REPLAY_CONFIG_COLS + ["_mkio_ref", "id"]),
        ),)
        self._compiled_ops["begin_replay"] = (CompiledOp(
            table="fix_replay_jobs",
            op_type="update",
            sql=("UPDATE fix_replay_jobs SET direction = ?, selected_messages = ?, sent_messages = 0, "
                 "status = 'running', error_text = '', _mkio_ref = ? WHERE id = ? RETURNING *"),
            param_names=("direction", "selected_messages", "_mkio_ref", "id"),
        ),)
        self._compiled_ops["delete_replay"] = (CompiledOp(
            table="fix_replay_jobs",
            op_type="delete",
            sql="DELETE FROM fix_replay_jobs WHERE id = ? RETURNING *",
            param_names=("id",),
        ),)

    async def _ensure_indexes(self) -> None:
        """Create application-level indexes that can't be expressed in TOML config."""
        conn = self.db.write_conn
        await (await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_fix_orders_clord_session "
            "ON fix_orders(cl_ord_id, session_id)"
        )).close()
        # _find_requested runs for every ER naming a ClOrdID no row holds.
        await (await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_fix_orders_pending ON fix_orders(session_id, pending_cl_ord_id) "
            "WHERE pending_cl_ord_id != ''"
        )).close()
        # orders_query/executions_query join fix_session_state by session_id
        # and re-run on every state write.
        for table in ("fix_orders", "fix_executions"):
            await (await conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_session ON {table}(session_id)"
            )).close()
        # A template name is unique within its scope (save_template
        # overwrites by it). Templates never shipped without the index, so a
        # duplicate can only be a hand-made row; the newest wins rather than
        # the index failing and the server with it.
        await (await conn.execute(
            "DELETE FROM fix_templates WHERE id NOT IN "
            "(SELECT MAX(id) FROM fix_templates GROUP BY scope, name)"
        )).close()
        await (await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_fix_templates_scope_name "
            "ON fix_templates(scope, name)"
        )).close()
        await conn.commit()

    async def _backfill_entered_terms(self) -> None:
        """Seed the as-submitted terms of orders recorded before those columns
        existed from their working terms, so their Replace dialog doesn't open
        on qty 0. Idempotent: every row written since carries entered_qty.

        Runs on the raw connection, after migration has baselined the rows, so
        a row it touches shows the pre-backfill values at version 1 of its
        history; the next edit records the row as it then stands."""
        conn = self.db.write_conn
        await (await conn.execute(
            "UPDATE fix_orders SET entered_qty = order_qty, entered_price = nullif(price, 0) "
            "WHERE entered_qty = 0 AND order_qty <> 0"
        )).close()
        await conn.commit()

    async def _backfill_rx_extra_tags(self) -> None:
        """Seed custom tags of orders received before extra_tags existed from
        their recorded messages, so the Accept/Reject/Fill dialogs echo them.
        Best-effort: the recorded NewOrderSingle must still match the row's
        current or original ClOrdID (an accepted replace renames the chain)."""
        conn = self.db.read_conn
        cur = await conn.execute(
            "SELECT id, session_id, cl_ord_id, orig_cl_ord_id, pending_action, "
            "pending_cl_ord_id, pending_extra_tags "
            "FROM fix_orders WHERE direction = 'RX' AND extra_tags = ''"
        )
        orders = [dict(r) for r in await cur.fetchall()]
        await cur.close()

        async def recorded_extras(session_id: str, msg_types: tuple[str, ...],
                                  cl_ord_ids: tuple[str, ...]) -> str:
            marks = ", ".join("?" * len(msg_types))
            cur = await conn.execute(
                f"SELECT raw_message FROM fix_messages WHERE session_id = ? "
                f"AND direction = 'RX' AND msg_type IN ({marks}) "
                f"AND cl_ord_id IN (?, ?) ORDER BY id LIMIT 1",
                (session_id, *msg_types, *cl_ord_ids),
            )
            row = await cur.fetchone()
            await cur.close()
            if not row:
                return ""
            msg = parse_fix(row["raw_message"])
            dictionary = FixDictionary(msg.get("8", "FIX.4.2"))
            return format_extra_tags(extra_pairs_of(msg, dictionary))

        changed = False
        for o in orders:
            extras = await recorded_extras(
                o["session_id"], ("D",), (o["cl_ord_id"], o["orig_cl_ord_id"]))
            pending = o["pending_extra_tags"]
            if not pending:
                if o["pending_action"] == "New":
                    pending = extras
                elif o["pending_action"] in ("Cancel", "Replace"):
                    pending = await recorded_extras(
                        o["session_id"], ("F", "G"),
                        (o["pending_cl_ord_id"], o["pending_cl_ord_id"]))
            if not extras and pending == o["pending_extra_tags"]:
                continue
            wconn = self.db.write_conn
            await (await wconn.execute(
                "UPDATE fix_orders SET extra_tags = ?, pending_extra_tags = ? WHERE id = ?",
                (extras, pending, o["id"]),
            )).close()
            changed = True
        if changed:
            await self.db.write_conn.commit()

    async def _backfill_client(self) -> None:
        """Seed the client column of rows recorded before it existed, once
        (fix_settings `client_backfill`): messages from their wire bytes
        through the owning session's client tags, orders from their earliest
        recorded message naming one (the NewOrderSingle, or the ER that
        created the row), trades from their order (live, or through history
        after a rename) else their ExecutionReport. Raw-connection writes,
        like _backfill_entered_terms, so the touched rows show the
        pre-backfill value at version 1."""
        conn = self.db.write_conn
        cur = await conn.execute(
            "SELECT value FROM fix_settings WHERE key = 'client_backfill'")
        done = await cur.fetchone()
        await cur.close()
        if done:
            return

        cur = await conn.execute("SELECT session_id, client_tags FROM fix_sessions")
        specs = {r["session_id"]: _client_specs_of(r["client_tags"])
                 for r in await cur.fetchall()}
        await cur.close()
        default = _client_specs_of("")

        cur = await conn.execute(
            "SELECT id, session_id, raw_message FROM fix_messages WHERE client = ''")
        messages = [(client_of(parse_fix(r["raw_message"]), specs.get(r["session_id"], default)),
                     r["id"]) for r in await cur.fetchall()]
        await cur.close()
        await (await conn.executemany(
            "UPDATE fix_messages SET client = ? WHERE id = ?",
            [m for m in messages if m[0]])).close()

        await (await conn.execute(
            "UPDATE fix_orders SET client = COALESCE((SELECT m.client FROM fix_messages m "
            "WHERE m.session_id = fix_orders.session_id "
            "AND m.cl_ord_id IN (fix_orders.cl_ord_id, fix_orders.orig_cl_ord_id) "
            "AND m.client != '' ORDER BY m.id LIMIT 1), '') WHERE client = ''")).close()

        await (await conn.execute(
            "UPDATE fix_executions SET client = COALESCE("
            "(SELECT o.client FROM fix_orders o WHERE o.session_id = fix_executions.session_id "
            "AND o.cl_ord_id = fix_executions.cl_ord_id AND o.client != ''), "
            "(SELECT o.client FROM fix_orders o JOIN fix_orders__history h ON h.id = o.id "
            "WHERE h.session_id = fix_executions.session_id "
            "AND h.cl_ord_id = fix_executions.cl_ord_id AND o.client != '' LIMIT 1), "
            "(SELECT m.client FROM fix_messages m WHERE m.session_id = fix_executions.session_id "
            "AND m.exec_id = fix_executions.exec_id AND m.client != '' ORDER BY m.id LIMIT 1), "
            "'') WHERE client = ''")).close()

        await (await conn.execute(
            "INSERT OR REPLACE INTO fix_settings (key, value) VALUES ('client_backfill', '1')"
        )).close()
        await conn.commit()

    async def _backfill_handling_and_trade_tags(self) -> None:
        """Seed, once (fix_settings `handling_backfill`), the columns 0.41
        added, from the recorded messages: an order's HandlInst(21) from its
        NewOrderSingle (matched like _backfill_rx_extra_tags, by current or
        original ClOrdID), a trade's extra_tags from the latest
        ExecutionReport carrying its ExecID. A sent trade's come out as the
        report carried them, client tag included. sent_text is not seeded.
        Raw-connection writes, like the other backfills."""
        conn = self.db.write_conn
        cur = await conn.execute(
            "SELECT value FROM fix_settings WHERE key = 'handling_backfill'")
        done = await cur.fetchone()
        await cur.close()
        if done:
            return

        cur = await conn.execute("SELECT session_id, fix_version FROM fix_sessions")
        versions = {r["session_id"]: r["fix_version"] for r in await cur.fetchall()}
        await cur.close()
        dictionaries: dict[str, FixDictionary] = {}

        def dictionary_of(session_id: str) -> FixDictionary:
            version = versions.get(session_id) or "FIX.4.2"
            if version not in dictionaries:
                try:
                    dictionaries[version] = FixDictionary(version)
                except Exception:
                    dictionaries[version] = FixDictionary("FIX.4.2")
            return dictionaries[version]

        cur = await conn.execute(
            "SELECT id, session_id, cl_ord_id, orig_cl_ord_id, direction "
            "FROM fix_orders WHERE handl_inst_code = ''")
        orders = [dict(r) for r in await cur.fetchall()]
        await cur.close()
        for o in orders:
            cur = await conn.execute(
                "SELECT raw_message FROM fix_messages WHERE session_id = ? AND direction = ? "
                "AND msg_type = 'D' AND cl_ord_id IN (?, ?) ORDER BY id LIMIT 1",
                (o["session_id"], o["direction"], o["cl_ord_id"], o["orig_cl_ord_id"]))
            row = await cur.fetchone()
            await cur.close()
            code = parse_fix(row["raw_message"]).get("21", "") if row else ""
            if code:
                name = dictionary_of(o["session_id"]).enum_name("21", code)
                await (await conn.execute(
                    "UPDATE fix_orders SET handl_inst = ?, handl_inst_code = ? WHERE id = ?",
                    (name, code, o["id"]))).close()

        cur = await conn.execute(
            "SELECT id, session_id, exec_id FROM fix_executions WHERE extra_tags = ''")
        trades = [dict(r) for r in await cur.fetchall()]
        await cur.close()
        for t in trades:
            cur = await conn.execute(
                "SELECT raw_message FROM fix_messages WHERE session_id = ? "
                "AND msg_type = '8' AND exec_id = ? ORDER BY id DESC LIMIT 1",
                (t["session_id"], t["exec_id"]))
            row = await cur.fetchone()
            await cur.close()
            if not row:
                continue
            extras = format_extra_tags(extra_pairs_of(
                parse_fix(row["raw_message"]), dictionary_of(t["session_id"]), CONSUMED_EXEC_TAGS))
            if extras:
                await (await conn.execute(
                    "UPDATE fix_executions SET extra_tags = ? WHERE id = ?",
                    (extras, t["id"]))).close()

        await (await conn.execute(
            "INSERT OR REPLACE INTO fix_settings (key, value) VALUES ('handling_backfill', '1')"
        )).close()
        await conn.commit()

    @staticmethod
    def _client_specs(session: FixSession) -> list[ClientTag]:
        """The session's client tag specs (fix_sessions.client_tags)."""
        config = getattr(session, "config", None) or {}
        return _client_specs_of(config.get("client_tags"))

    def _stamp_client(self, session: FixSession, msg: FixMessage, client: str) -> None:
        """Put `client` on an outgoing message, unless its extra tags already
        name one — extras win, and an echoed Parties group must not go out
        twice. Call after `msg.extra` is set."""
        specs = self._client_specs(session)
        if client_of(FixMessage(dict(msg.extra), pairs=msg.extra), specs):
            return
        session.factory.stamp_client(msg, client or "", specs)

    def _client_as_sent(self, session: FixSession, msg: FixMessage) -> str:
        """The client a message will carry once sent — extras applied, as
        sendprep will apply them — for the row written before the send."""
        return client_of(self._as_sent(session, msg), self._client_specs(session))

    def _text_as_sent(self, session: FixSession, msg: FixMessage) -> str:
        """Text(58) as the message will carry it: a 58 among the extra tags
        overrides (or, blank, deletes) the dialog's Text field."""
        return self._as_sent(session, msg).get("58", "")

    def _handling_as_sent(self, session: FixSession, msg: FixMessage) -> dict[str, str]:
        """The order columns recording HandlInst(21) and Text(58) as an order
        or replace request will carry them, extras applied."""
        sent = self._as_sent(session, msg)
        code = sent.get("21", "")
        return {
            "handl_inst": session.dictionary.enum_name("21", code),
            "handl_inst_code": code,
            "sent_text": sent.get("58", ""),
        }

    @staticmethod
    def _as_sent(session: FixSession, msg: FixMessage) -> FixMessage:
        """A sendprepped copy of a message about to go out, extras applied."""
        preview = FixMessage(dict(msg.fields))
        preview.extra = list(msg.extra)
        preview.sendprep(session.dictionary, session.factory.sender, session.factory.target, 0)
        return preview

    async def _client_of_order(self, session_id: str, cl_ord_id: str) -> str:
        try:
            order = await self._load_order(session_id, cl_ord_id)
        except ValueError:
            return ""
        return order.get("client") or ""

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

    async def last_message_id(self) -> int:
        """The newest fix_messages id — the boundary a session records when
        its sequence numbers reset, so a later resend looks only at the
        current sequence space. Every prior send awaited its commit, so the
        read is current."""
        conn = self.db.read_conn
        cursor = await conn.execute("SELECT COALESCE(MAX(id), 0) AS id FROM fix_messages")
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["id"]) if row else 0

    async def sent_messages(self, session_id: str, begin: int, end: int,
                            after_id: int = 0) -> list[dict[str, Any]]:
        """Recorded TX messages of a session with MsgSeqNum in [begin, end],
        newest row per sequence number (a number can recur only across a
        reset, and `after_id` normally excludes those), in sequence order."""
        conn = self.db.read_conn
        cursor = await conn.execute(
            "SELECT seq_num, msg_type, raw_message FROM fix_messages "
            "WHERE id IN (SELECT MAX(id) FROM fix_messages "
            "  WHERE session_id = ? AND direction = 'TX' AND id > ? "
            "  AND seq_num BETWEEN ? AND ? GROUP BY seq_num) "
            "ORDER BY seq_num",
            (session_id, after_id, begin, end),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        await cursor.close()
        return rows

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
        # Reload first: a stopped session rebuilds with fresh config (and
        # dictionary); a running one keeps its live transport.
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
        session = self.sessions.get(session_id)
        dictionary = session.dictionary if session else FixDictionary(msg.get("8", "FIX.4.2"))
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
            msg.to_wire_string(),
            msg.get("49", ""),
            msg.get("56", ""),
            msg.get("11", ""),
            msg.get("37", ""),
            msg.get("17", ""),
            msg.get("55", ""),
            msg.get("54", ""),
            msg.get_int("9", 0),
            msg.get("10", ""),
            client_of(msg, self._client_specs(session) if session else _client_specs_of("")),
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
            "seq_epoch": 0,
        }
        was_active = state["status"] == "ACTIVE"
        state.update(updates)

        values = (
            state["status"],
            state["tx_seq_num"],
            state["rx_seq_num"],
            state["last_tx_time"],
            state["last_rx_time"],
            state["session_start"],
            state["error_text"],
            state.get("seq_epoch") or 0,
        )
        params = (state["session_id"], *values, None, *values)
        ops = self._compiled_ops["upsert_state"]
        await self.writer.submit(ops, (params,), {"session_id": session_id})
        # A script's orders can do nothing while their session is not ACTIVE.
        is_active = state["status"] == "ACTIVE"
        if is_active != was_active and self.events.active:
            self.events.emit(EngineEvent(("session up" if is_active else "session down",), session_id,
                                         detail={"status": state["status"]}))

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
        elif msg_type == "9":
            await self._handle_cancel_reject(session, msg)
        elif msg_type == "Q":
            await self._handle_dont_know_trade(session, msg)
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

        side_code = msg.get("54", "")
        status_code = msg.get("39", "")
        ord_type_code = msg.get("40", "")
        tif_code = msg.get("59", "")
        exec_type_code = msg.get("150", "")
        trans_type_code = msg.get("20", "0")

        # An accepted cancel/replace re-identifies the chain: tag 11 carries the
        # request's ClOrdID and tag 41 the one it supersedes, so our order row
        # moves to the new ClOrdID before the upsert under it. Only an accept
        # does: a PendingCancel/PendingReplace ER names the request the same
        # way, and renaming on it would leave the row on an ID the counterparty
        # never adopted once an OrderCancelReject follows. Any other ER lands
        # on the row the chain currently lives on.
        accepted = (exec_type_code or status_code) in ("4", "5")
        order = await self._find_order_for_report(session_id, cl_ord_id, orig_cl_ord_id)
        if order and order["cl_ord_id"] != cl_ord_id:
            if accepted:
                await self._rename_order(session_id, order["cl_ord_id"], cl_ord_id)
                # The accepted replace's terms become the order's entered
                # terms. One whose terms the slot no longer holds (a later
                # request took it) still moves the quantity and price the
                # Replace dialog opens on, from the ER's own 38/44.
                if order["pending_cl_ord_id"] == cl_ord_id and order["pending_entered"]:
                    await self._promote_entered(session_id, cl_ord_id, order["pending_entered"])
                elif (exec_type_code or status_code) == "5":
                    echoed = {"entered_qty": msg.get_float("38", 0.0),
                              "entered_price": msg.get_float("44", 0.0)}
                    echoed = {c: v for c, v in echoed.items() if v}
                    if echoed:
                        await self._promote_entered(session_id, cl_ord_id, json.dumps(echoed))
            else:
                cl_ord_id = order["cl_ord_id"]

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
            "pending_extra_tags": "",
            "tif_code": tif_code,
            "extra_tags": "",
            "entered_qty": msg.get_float("38", 0.0),
            "entered_price": msg.get_float("44", 0.0) or None,
            "expire_time": msg.get("126", ""),
            "expire_date": msg.get("432", ""),
            "client": client_of(msg, self._client_specs(session)),
            "handl_inst": "",
            "handl_inst_code": "",
            "sent_text": "",
        }
        ops = self._compiled_ops["upsert_order"]
        await self.writer.submit(
            ops, (_order_params(order_row, keep_sent_text=True, keep_pending=True),),
            {"cl_ord_id": cl_ord_id})

        trade = await self._record_report_trade(session, msg, cl_ord_id, order_row["client"])

        if not self.events.active:
            return
        after = await self._find_order(session_id, cl_ord_id)
        if order is None and after is not None:
            # A row this report created: an order that went out around
            # send_new_order — a replayed log — becomes known here.
            self.events.emit(EngineEvent(("sent order",), session_id, order=after, msg=msg,
                                         request=after["cl_ord_id"]))
        if trade is not None:
            trade = await self._find_execution(session_id, trade["exec_id"])
        self.events.emit(EngineEvent(
            report_kinds(msg), session_id, msg=msg, request=msg.get("11", ""),
            order=after, prev=order, trade=trade))

    async def _record_report_trade(self, session: FixSession, msg: FixMessage, cl_ord_id: str,
                                   client: str) -> dict[str, Any] | None:
        """The trade an ExecutionReport reports — a fill, or a correction or
        bust of one — written as a row or a new version of one; None for a
        report that is about the order alone."""
        dictionary = session.dictionary
        session_id = session.session_id
        now = _fix_timestamp()
        side_code = msg.get("54", "")
        exec_type_code = msg.get("150", "")
        trans_type_code = msg.get("20", "0")

        # Record fills — and trade corrections/busts, which arrive via
        # ExecTransType(20) on FIX 4.2 and as ExecType(150) G/H from 4.3 on
        if trans_type_code in ("1", "2"):
            exec_type = dictionary.enum_name("20", trans_type_code)
            exec_code = trans_type_code
        elif exec_type_code in ("1", "2", "F", "G", "H"):
            exec_type = dictionary.enum_name("150", exec_type_code)
            exec_code = exec_type_code
        else:
            return None

        # A correction/bust is a new version of the trade whose execution it
        # references (tag 19); a new fill, or a reference we can't resolve,
        # starts a new trade.
        referenced = None
        exec_ref_id = msg.get("19", "")
        if exec_ref_id:
            referenced = await self._find_execution(session_id, exec_ref_id)
        trade_id = referenced["trade_id"] if referenced else await self.ids.next_id("TR")

        exec_row = {
            "session_id": session_id,
            "exec_id": msg.get("17", ""),
            "exec_ref_id": exec_ref_id,
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
            "dk_reason": "",
            "dk_text": "",
            "client": client or await self._client_of_order(session_id, cl_ord_id),
            "extra_tags": format_extra_tags(extra_pairs_of(msg, dictionary, CONSUMED_EXEC_TAGS)),
        }
        await self._submit_execution(exec_row, referenced["id"] if referenced else None)
        return exec_row

    async def _handle_cancel_reject(self, session: FixSession, msg: FixMessage) -> None:
        """Process an OrderCancelReject (35=9): the request of its ClOrdID(11)
        was refused, so the sent order keeps its ClOrdID, its request slot
        clears when it held that request, and the row records what was refused
        and why. A reject resolving to no sent order stays a recorded message.
        """
        session_id = session.session_id
        request_id = msg.get("11", "")
        order = (await self._find_requested(session_id, request_id)
                 or await self._find_order(session_id, msg.get("41", "")))
        if not order or order["direction"] != "TX":
            return

        dictionary = session.dictionary
        kind = {"1": "Cancel", "2": "Replace"}.get(msg.get("434", ""))
        if not kind and order["pending_cl_ord_id"] == request_id:
            kind = order["pending_action"]
        if not kind:
            kind = await self._sent_request_kind(session_id, request_id)
        reason_code = msg.get("102", "")
        note = f"{kind} {request_id}".strip()
        if reason_code:
            note += f": {dictionary.enum_name('102', reason_code)}"

        # OrdStatus(39) is the order as the counterparty holds it — what undoes
        # a PendingCancel/PendingReplace. Under UnknownOrder it describes no
        # order of theirs, so ours keeps its status.
        status_code = "" if reason_code == "1" else msg.get("39", "")
        status = dictionary.enum_name("39", status_code) if status_code else ""
        params = (status, status, msg.get("58", ""), note,
                  *([request_id] * len(_PENDING_BLANKS)),
                  _fix_timestamp(), None, order["id"])
        ops = self._compiled_ops["resolve_request"]
        await self.writer.submit(ops, (params,), {"cl_ord_id": order["cl_ord_id"]})
        if self.events.active:
            self.events.emit(EngineEvent(
                ("cancel rejected", "message"), session_id, msg=msg, request=request_id, prev=order,
                order=await self._load_order_by_id(order["id"]),
                detail={"response_to": kind.lower(),
                        "reason": dictionary.enum_name("102", reason_code) if reason_code else ""}))

    async def _handle_dont_know_trade(self, session: FixSession, msg: FixMessage) -> None:
        """Process a DontKnowTrade (35=Q): mark the sent trade whose ExecID(17)
        it names with the counterparty's DKReason(127) and Text(58).

        The ExecID resolves like a correction's ExecRefID — live row, then
        history — so a DK of the original fill after a correction lands on
        the corrected trade. Only a trade this engine sent can be DK'd; a
        reference to nothing, or to a received trade, is left as the recorded
        message.
        """
        execution = await self._find_execution(session.session_id, msg.get("17", ""))
        if not execution or execution["direction"] != "TX":
            return
        reason = session.dictionary.enum_name("127", msg.get("127", ""))
        params = (reason, msg.get("58", ""), None, execution["id"])
        ops = self._compiled_ops["dk_execution"]
        await self.writer.submit(ops, (params,), {"exec_id": execution["exec_id"]})
        if self.events.active:
            try:
                order = await self._load_order_for_execution(execution)
            except ValueError:
                order = None
            self.events.emit(EngineEvent(
                ("dk", "message"), session.session_id, msg=msg, order=order, prev=order,
                trade=await self._load_execution_by_id(execution["id"]),
                detail={"reason": reason, "text": msg.get("58", "")}))

    async def _handle_new_order(self, session: FixSession, msg: FixMessage) -> None:
        """Record an inbound NewOrderSingle (35=D) as a received order awaiting action."""
        dictionary = session.dictionary
        now = _fix_timestamp()
        qty = msg.get_float("38", 0.0)
        side_code = msg.get("54", "")
        ord_type_code = msg.get("40", "")
        tif_code = msg.get("59", "")
        handl_inst_code = msg.get("21", "")
        extras = format_extra_tags(extra_pairs_of(msg, dictionary))

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
            "pending_extra_tags": extras,
            "tif_code": tif_code,
            "extra_tags": extras,
            "entered_qty": qty,
            "entered_price": msg.get_float("44", 0.0) or None,
            "expire_time": msg.get("126", ""),
            "expire_date": msg.get("432", ""),
            "client": client_of(msg, self._client_specs(session)),
            "handl_inst": dictionary.enum_name("21", handl_inst_code),
            "handl_inst_code": handl_inst_code,
            "sent_text": "",
        }
        ops = self._compiled_ops["upsert_order"]
        await self.writer.submit(ops, (_order_params(order_row),), {"cl_ord_id": order_row["cl_ord_id"]})
        if self.events.active:
            self.events.emit(EngineEvent(
                ("order", "message"), session.session_id, msg=msg, request=order_row["cl_ord_id"],
                order=await self._find_order(session.session_id, order_row["cl_ord_id"])))

    async def _handle_cancel_request(self, session: FixSession, msg: FixMessage, action: str) -> None:
        """Park an inbound OrderCancelRequest (35=F) or OrderCancelReplaceRequest
        (35=G) on the received order so the market side can accept or reject it.
        The order's status stays live — fills remain possible while a request
        is pending, as on a real market."""
        session_id = session.session_id
        orig_cl_ord_id = msg.get("41", "")
        request_id = msg.get("11", "")
        kind = action.lower()
        async with self._order_lock(session_id):
            try:
                order = await self._load_order(session_id, orig_cl_ord_id)
            except ValueError:
                order = None
            if order is not None:
                await self._write_order(
                    order,
                    pending_action=action,
                    pending_cl_ord_id=request_id,
                    pending_qty=msg.get_float("38", 0.0),
                    pending_price=msg.get_float("44", 0.0),
                    pending_extra_tags=format_extra_tags(extra_pairs_of(msg, session.dictionary)),
                    text=msg.get("58", ""),
                )
        if order is None:
            reject = session.factory.order_cancel_reject(
                cl_ord_id=request_id,
                orig_cl_ord_id=orig_cl_ord_id,
                ord_status="8",
                response_to="1" if action == "Cancel" else "2",
                text=f"Unknown order: {orig_cl_ord_id}",
                # UnknownOrder: the 39=8 above describes no order of the
                # sender's, and a receiver that knows better keeps its status.
                reason="1",
            )
            await session.send_message(reject)
            self.events.emit(EngineEvent(("message",), session_id, msg=msg, request=request_id,
                                         detail={"unknown_order": orig_cl_ord_id, "request": kind}))
            return
        if self.events.active:
            self.events.emit(EngineEvent(
                (kind, "message"), session_id, msg=msg, request=request_id, prev=order,
                order=await self._load_order_by_id(order["id"])))

    async def send_new_order(
        self,
        session_id: str,
        symbol: str,
        side: str,
        qty: float,
        ord_type: str = "2",
        price: float | None = None,
        tif: str = "0",
        extra_tags: str = "",
        expire_time: str = "",
        expire_date: str = "",
        expire_precision: str = "",
        client: str = "",
        handl_inst: str = "1",
        text: str = "",
        source: str = "manual",
        tag: str = "",
        **extra: str,
    ) -> str:
        """Send a NewOrderSingle and return the ClOrdID. `client` goes out on
        the session's client tag; an extra tag naming that tag overrides it.

        The order is announced (`sent order`) once its row is written and
        *before* the message goes out: the counterparty may acknowledge inside
        the send, and whoever takes the order — a script that sent it (`tag`
        tells the runner which), or one waiting for orders sent by hand —
        must own it by then or it would never hear that acknowledgement."""
        session = self.sessions.get(session_id)
        if not session or not session.is_active:
            raise ValueError(f"Session {session_id} is not active")

        extra_pairs = parse_extra_tags(extra_tags)
        cl_ord_id = await self.ids.next_id("RT")
        msg = session.factory.new_order_single(
            cl_ord_id=cl_ord_id,
            symbol=symbol,
            side=side,
            qty=qty,
            ord_type=ord_type,
            price=price,
            tif=tif,
            handl_inst=handl_inst,
            expire_time=expire_time,
            expire_date=expire_date,
            expire_precision=expire_precision,
            text=text or None,
            **extra,
        )
        msg.extra = extra_pairs
        self._stamp_client(session, msg, client)
        expire_time, expire_date = session.factory.expiry(expire_time, expire_date, expire_precision)

        # Pre-populate the order row as PendingNew *before* the message goes on
        # the wire: send_message awaits, so the counterparty's answer can be read
        # and processed first, and an ExecutionReport reaching the upsert with no
        # row to update creates one itself — adopting its OrderID(37) as our
        # write-once order_id, only for this write to then land on top of it and
        # put the order back to PendingNew.
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
            "pending_extra_tags": "",
            "tif_code": tif,
            "extra_tags": extra_tags,
            "entered_qty": qty,
            "entered_price": price,
            "expire_time": expire_time,
            "expire_date": expire_date,
            "client": self._client_as_sent(session, msg),
            **self._handling_as_sent(session, msg),
        }
        ops = self._compiled_ops["upsert_order"]
        await self.writer.submit(ops, (_order_params(order_row),), {"cl_ord_id": cl_ord_id})
        if self.events.active:
            self.events.emit(EngineEvent(
                ("sent order",), session_id, source=source, request=cl_ord_id, msg=msg,
                order=await self._find_order(session_id, cl_ord_id), detail={"tag": tag}))

        try:
            await session.send_message(msg)
        except Exception as exc:
            await self._write_order(order_row, status="Rejected", leaves_qty=0.0,
                                    text=f"Send failed: {exc}")
            raise

        return cl_ord_id

    async def send_cancel(
        self,
        session_id: str,
        orig_cl_ord_id: str,
        symbol: str,
        side: str,
        qty: float = 0,
        extra_tags: str = "",
        client: str = "",
        text: str = "",
    ) -> str:
        """Send an OrderCancelRequest and return the new ClOrdID. Its Text(58)
        — blank included — becomes the order's sent_text."""
        session = self.sessions.get(session_id)
        if not session or not session.is_active:
            raise ValueError(f"Session {session_id} is not active")

        extra_pairs = parse_extra_tags(extra_tags)
        cl_ord_id = await self.ids.next_id("RT")
        msg = session.factory.cancel_request(
            cl_ord_id=cl_ord_id,
            orig_cl_ord_id=orig_cl_ord_id,
            symbol=symbol,
            side=side,
            qty=qty,
            text=text or None,
        )
        msg.extra = extra_pairs
        self._stamp_client(session, msg, client)
        await self._record_entered(session_id, orig_cl_ord_id,
                                   {"sent_text": self._text_as_sent(session, msg)},
                                   request=("Cancel", cl_ord_id, 0.0, 0.0))
        await self._send_request(session, msg, cl_ord_id)
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
        tif: str | None = None,
        extra_tags: str = "",
        expire_time: str = "",
        expire_date: str = "",
        expire_precision: str = "",
        client: str = "",
        handl_inst: str = "1",
        text: str = "",
        **extra: str,
    ) -> str:
        """Send an OrderCancelReplaceRequest and return the new ClOrdID.

        The submitted terms are recorded on the order row (ENTERED_COLS) so
        the next Replace dialog opens on them; tag 59 goes out only when
        ``tif`` is given."""
        session = self.sessions.get(session_id)
        if not session or not session.is_active:
            raise ValueError(f"Session {session_id} is not active")

        extra_pairs = parse_extra_tags(extra_tags)
        cl_ord_id = await self.ids.next_id("RT")
        msg = session.factory.cancel_replace_request(
            cl_ord_id=cl_ord_id,
            orig_cl_ord_id=orig_cl_ord_id,
            symbol=symbol,
            side=side,
            qty=qty,
            ord_type=ord_type,
            price=price,
            tif=tif,
            handl_inst=handl_inst,
            expire_time=expire_time,
            expire_date=expire_date,
            expire_precision=expire_precision,
            text=text or None,
            **extra,
        )
        msg.extra = extra_pairs
        self._stamp_client(session, msg, client)
        expire_time, expire_date = session.factory.expiry(expire_time, expire_date, expire_precision)

        dictionary = session.dictionary
        entered = {
            **self._handling_as_sent(session, msg),
            "client": self._client_as_sent(session, msg),
            "symbol": symbol,
            "side": dictionary.enum_name("54", side),
            "side_code": side,
            "ord_type": dictionary.enum_name("40", ord_type),
            "ord_type_code": ord_type,
            "extra_tags": extra_tags,
            "entered_qty": qty,
            "entered_price": price,
            "expire_time": expire_time,
            "expire_date": expire_date,
        }
        if tif is not None:
            entered["time_in_force"] = dictionary.enum_name("59", tif)
            entered["tif_code"] = tif
        # Recorded before the send for the same reason send_new_order writes
        # first: an ExecutionReport accepting the replace renames the chain to
        # the new ClOrdID, and this lookup by the superseded one would miss.
        await self._record_entered(session_id, orig_cl_ord_id, entered,
                                   request=("Replace", cl_ord_id, qty, price or 0.0))
        await self._send_request(session, msg, cl_ord_id)
        return cl_ord_id

    async def _record_entered(self, session_id: str, cl_ord_id: str, entered: dict[str, Any],
                              request: tuple[str, str, float, float] | None = None) -> None:
        """Write submitted terms onto the order row; a no-op for unknown orders.

        `request` — (action, ClOrdID, qty, price) of the cancel/replace about
        to go out — also fills the sent order's request slot, and the terms
        wait there (pending_entered) until the request is accepted: only
        sent_text, which went out whatever the answer, is written now. A
        received order is left without a slot of ours: its slot is the
        counterparty's request, so the terms are written as before.
        """
        try:
            order = await self._load_order(session_id, cl_ord_id)
        except ValueError:
            return
        if request and order["direction"] == "TX":
            deferred = {c: v for c, v in entered.items() if c != "sent_text"}
            row = {**order, "sent_text": entered.get("sent_text", order["sent_text"])}
            params = tuple(row[c] for c in ENTERED_COLS) + request + (
                json.dumps(deferred) if deferred else "",)
            ops = self._compiled_ops["record_request"]
        else:
            row = {**order, **entered}
            params = tuple(row[c] for c in ENTERED_COLS)
            ops = self._compiled_ops["record_entered"]
        params += (_fix_timestamp(), None, session_id, cl_ord_id)
        await self.writer.submit(ops, (params,), {"cl_ord_id": cl_ord_id})

    async def _promote_entered(self, session_id: str, cl_ord_id: str, pending_entered: str) -> None:
        """An accepted replace's terms become the order's entered terms. The
        row is read again so the write carries its other columns as they
        stand now, not as the ExecutionReport handler first found them."""
        try:
            terms = json.loads(pending_entered)
            order = await self._load_order(session_id, cl_ord_id)
        except ValueError:
            return
        row = {**order, **{c: v for c, v in terms.items() if c in ENTERED_COLS}}
        params = tuple(row[c] for c in ENTERED_COLS) + (_fix_timestamp(), None, session_id, cl_ord_id)
        await self.writer.submit(self._compiled_ops["record_entered"], (params,),
                                 {"cl_ord_id": cl_ord_id})

    async def _send_request(self, session: FixSession, msg: FixMessage, request_id: str) -> None:
        """Send a cancel/replace whose request slot is already written; one
        that fails to go out is not outstanding, so its slot clears."""
        try:
            await session.send_message(msg)
        except Exception:
            params = (_fix_timestamp(), None, session.session_id, request_id)
            await self.writer.submit(self._compiled_ops["clear_request"], (params,),
                                     {"cl_ord_id": request_id})
            raise

    # ── Actions by name ──────────────────────────────────────────────

    async def perform(self, op: str, data: dict[str, Any], source: str = "manual") -> dict[str, Any]:
        """Run the order or trade action ``op`` (a key of ``ACTIONS``) on a
        payload of its terms, and announce it. The one way in for the UI's
        ``fix_cmd`` and for scripted macros alike, so both meet the same
        checks; ``source`` tells listeners which of them acted.

        The subject is found before the action runs — an accepted replace
        renames the order it was named by — and re-read after it by row id.
        """
        action = ACTIONS.get(op)
        if action is None:
            raise ValueError(f"Unknown command: {op}")
        if not self.events.active:
            return await action(self, data)

        session_id = data.get("session_id", "")
        data = {**data, "_source": source}
        prev, trade = await self._action_subject(op, data)
        result = await action(self, data)
        if op == "send_new_order":      # announced as `sent order` by send_new_order itself, before its send
            order = await self._find_order(session_id, result["cl_ord_id"])
        else:
            order = await self._load_order_by_id(prev["id"] if prev else None)
        self.events.emit(EngineEvent(
            ("action",), session_id, source=source, order=order, prev=prev,
            trade=await self._load_execution_by_id(trade["id"] if trade else None),
            request=str(result.get("cl_ord_id", "")),
            detail={"op": op, "result": result, "trade_before": trade,
                    "data": {k: v for k, v in data.items() if not k.startswith("_")}}))
        return result

    async def _action_subject(self, op: str, data: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """The order and trade rows an action is about, as they stand before it."""
        session_id = data.get("session_id", "")
        if op in ORDER_KEY:
            return await self._find_order(session_id, str(data.get(ORDER_KEY[op], ""))), None
        if op in TRADE_KEY:
            trade = await self._find_execution(session_id, str(data.get(TRADE_KEY[op], "")))
            if trade is None:
                return None, None
            if trade["direction"] == "TX":
                try:
                    return await self._load_order_for_execution(trade), trade
                except ValueError:
                    return None, trade
            return await self._find_order(session_id, trade["cl_ord_id"]), trade
        return None, None

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

    async def _find_order(self, session_id: str, cl_ord_id: str) -> dict[str, Any] | None:
        """The live order row a ClOrdID belongs to, or None: the row holding
        it now, else the one that held it earlier in its chain (through the
        row's recorded versions, as `_find_execution` resolves an ExecID)."""
        if not cl_ord_id:
            return None
        conn = self.db.read_conn
        cursor = await conn.execute(
            "SELECT * FROM fix_orders WHERE session_id = ? AND cl_ord_id = ?",
            (session_id, cl_ord_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row:
            return dict(row)
        cursor = await conn.execute(
            "SELECT o.* FROM fix_orders o JOIN fix_orders__history h ON h.id = o.id "
            "WHERE h.session_id = ? AND h.cl_ord_id = ? ORDER BY o.id DESC LIMIT 1",
            (session_id, cl_ord_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    def _order_lock(self, session_id: str) -> asyncio.Lock:
        """One lock per session for every snapshot-then-write of its received
        orders (see `_market_action`); held by the inbound cancel/replace
        handler too. Never held across a send."""
        lock = self._order_locks.get(session_id)
        if lock is None:
            lock = self._order_locks[session_id] = asyncio.Lock()
        return lock

    async def _load_order_by_id(self, row_id: int | None) -> dict[str, Any] | None:
        if row_id is None:
            return None
        cursor = await self.db.read_conn.execute("SELECT * FROM fix_orders WHERE id = ?", (row_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def _load_execution_by_id(self, row_id: int | None) -> dict[str, Any] | None:
        if row_id is None:
            return None
        cursor = await self.db.read_conn.execute("SELECT * FROM fix_executions WHERE id = ?", (row_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def _find_requested(self, session_id: str, request_id: str) -> dict[str, Any] | None:
        """The sent order whose request slot holds `request_id`, or None."""
        if not request_id:
            return None
        cursor = await self.db.read_conn.execute(
            "SELECT * FROM fix_orders WHERE session_id = ? AND direction = 'TX' "
            "AND pending_cl_ord_id = ? AND pending_cl_ord_id != ''",
            (session_id, request_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def _find_order_for_report(self, session_id: str, cl_ord_id: str,
                                     orig_cl_ord_id: str) -> dict[str, Any] | None:
        """The order an ExecutionReport is about: the row under its ClOrdID(11),
        else the one that sent the request of that ID (an answer lacking tag
        41 still lands), else the chain OrigClOrdID(41) names."""
        conn = self.db.read_conn
        cursor = await conn.execute(
            "SELECT * FROM fix_orders WHERE session_id = ? AND cl_ord_id = ?",
            (session_id, cl_ord_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row:
            return dict(row)
        return (await self._find_requested(session_id, cl_ord_id)
                or await self._find_order(session_id, orig_cl_ord_id))

    async def _sent_request_kind(self, session_id: str, request_id: str) -> str:
        """Cancel or Replace, from the request as recorded on its way out: what
        an OrderCancelReject without CxlRejResponseTo(434) — FIX 4.0/4.1 —
        leaves unsaid. Blank when this engine never sent it."""
        cursor = await self.db.read_conn.execute(
            "SELECT msg_type FROM fix_messages WHERE session_id = ? AND direction = 'TX' "
            "AND cl_ord_id = ? AND msg_type IN ('F', 'G') ORDER BY id DESC LIMIT 1",
            (session_id, request_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return {"F": "Cancel", "G": "Replace"}.get(row[0], "") if row else ""

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
        row = await self._find_execution(session_id, exec_id)
        if not row:
            raise ValueError(f"Unknown execution: {exec_id} on {session_id}")
        return row

    @staticmethod
    def _require_live_trade(execution: dict[str, Any], action: str) -> None:
        """A busted trade is done: its row's exec_type is the trade's state.
        Tested by name — code 1 is ExecTransType Cancel on one dialect and
        ExecType PartialFill on the other."""
        if execution["exec_type"] in BUSTED_EXEC_TYPES:
            raise ValueError(
                f"Cannot {action} {execution['exec_id']}: trade "
                f"{execution['trade_id']} is busted")

    async def _write_order(self, order: dict[str, Any], **updates: Any) -> None:
        """Apply updates to an order row on top of the snapshot its caller loaded.

        Callers must submit this — and any rename or execution insert that goes
        with it — *before* putting their message on the wire. The send awaits, so
        an inbound message for the same order (a cancel request, say) can be
        processed in between; this write then puts the whole of ORDER_UPDATE_COLS
        back to the pre-send snapshot, losing the pending_* columns it set.
        """
        row = {**order, **updates, "updated_at": _fix_timestamp()}
        ops = self._compiled_ops["upsert_order"]
        await self.writer.submit(ops, (_order_params(row),), {"cl_ord_id": row["cl_ord_id"]})

    async def _rename_order(self, session_id: str, old_cl_ord_id: str, new_cl_ord_id: str) -> None:
        """Move an order row to its next ClOrdID after an accepted cancel/replace."""
        if not old_cl_ord_id or old_cl_ord_id == new_cl_ord_id:
            return
        params = (new_cl_ord_id, old_cl_ord_id, *([new_cl_ord_id] * len(_PENDING_BLANKS)),
                  _fix_timestamp(), None, session_id, old_cl_ord_id, new_cl_ord_id)
        ops = self._compiled_ops["rename_order"]
        await self.writer.submit(ops, (params,), {"cl_ord_id": new_cl_ord_id})

    async def _find_execution(self, session_id: str, exec_id: str) -> dict[str, Any] | None:
        """The live trade row an ExecID belongs to, or None.

        The live row carries the trade's latest ExecID; an earlier one in the
        chain — the original fill's after a correction — is found through the
        row's recorded versions, which is what makes a bust of the fill land
        on the corrected trade rather than start a new one.
        """
        conn = self.db.read_conn
        cursor = await conn.execute(
            "SELECT * FROM fix_executions WHERE session_id = ? AND exec_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (session_id, exec_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row:
            return dict(row)
        cursor = await conn.execute(
            "SELECT e.* FROM fix_executions e JOIN fix_executions__history h ON h.id = e.id "
            "WHERE h.session_id = ? AND h.exec_id = ? ORDER BY e.id DESC LIMIT 1",
            (session_id, exec_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def _submit_execution(self, exec_row: dict[str, Any], row_id: int | None) -> None:
        """Insert a new trade, or rewrite the trade row `row_id` as a new version."""
        if row_id is None:
            ops = self._compiled_ops["insert_execution"]
            params = _exec_params(exec_row)
        else:
            ops = self._compiled_ops["update_execution"]
            params = _exec_update_params(exec_row, row_id)
        await self.writer.submit(ops, (params,), {"exec_id": exec_row["exec_id"]})

    async def _write_sent_execution(
        self, order: dict[str, Any], exec_id: str, trade_id: str, exec_type: str,
        exec_type_code: str, last_qty: float, last_price: float,
        cum_qty: float, avg_price: float, leaves_qty: float,
        exec_ref_id: str = "", row_id: int | None = None,
        text: str = "", extra_tags: str = "",
    ) -> None:
        now = _fix_timestamp()
        exec_row = {
            "session_id": order["session_id"],
            "exec_id": exec_id,
            "exec_ref_id": exec_ref_id,
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
            "text": text,
            "timestamp": now,
            "direction": "TX",
            "dk_reason": "",
            "dk_text": "",
            "client": order.get("client") or "",
            "extra_tags": extra_tags,
        }
        await self._submit_execution(exec_row, row_id)

    @_market_action
    async def accept_order(self, session_id: str, cl_ord_id: str, extra_tags: str = "",
                           text: str = "") -> tuple[FixMessage, str]:
        """Accept a received order: send ExecutionReport(New), return the OrderID."""
        session = self._active_session(session_id)
        extra_pairs = parse_extra_tags(extra_tags)
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
            text=text or None,
        )
        msg.extra = extra_pairs
        self._stamp_client(session, msg, order.get("client"))

        await self._write_order(order, order_id=order_id, status="New",
                                sent_text=self._text_as_sent(session, msg),
                                pending_action="", pending_extra_tags="")
        return msg, order_id

    @_market_action
    async def reject_order(self, session_id: str, cl_ord_id: str, text: str = "",
                           extra_tags: str = "") -> tuple[FixMessage, None]:
        """Reject a received order: send ExecutionReport(Rejected)."""
        session = self._active_session(session_id)
        extra_pairs = parse_extra_tags(extra_tags)
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
        msg.extra = extra_pairs
        self._stamp_client(session, msg, order.get("client"))

        await self._write_order(order, status="Rejected", leaves_qty=0.0,
                                sent_text=self._text_as_sent(session, msg),
                                pending_action="", pending_extra_tags="")
        return msg, None

    @_market_action
    async def fill_order(
        self, session_id: str, cl_ord_id: str, qty: float, price: float,
        extra_tags: str = "", text: str = "",
    ) -> tuple[FixMessage, str]:
        """Fill a received order (partially or fully) and return the ExecID."""
        if qty <= 0:
            raise ValueError("Fill quantity must be positive")
        session = self._active_session(session_id)
        extra_pairs = parse_extra_tags(extra_tags)
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
            text=text or None,
        )
        msg.extra = extra_pairs
        self._stamp_client(session, msg, order.get("client"))
        sent_text = self._text_as_sent(session, msg)

        # A fill on a not-yet-accepted order implicitly acknowledges it, so the
        # pending New is consumed; a pending Cancel/Replace stays parked.
        consumed = order["pending_action"] == "New"
        await self._write_order(
            order, order_id=order_id, status=dictionary.enum_name("39", status_code),
            cum_qty=cum_qty, avg_price=avg_price, leaves_qty=leaves_qty,
            last_qty=qty, last_price=price, sent_text=sent_text,
            pending_action="" if consumed else order["pending_action"],
            pending_extra_tags="" if consumed else order["pending_extra_tags"],
        )
        await self._write_sent_execution(
            {**order, "order_id": order_id}, exec_id, trade_id,
            *_sent_exec_kind(dictionary, msg, status_code),
            qty, price, cum_qty, avg_price, leaves_qty,
            text=sent_text, extra_tags=extra_tags,
        )
        return msg, exec_id

    @_market_action
    async def unsolicited_cancel(self, session_id: str, cl_ord_id: str, extra_tags: str = "",
                                 text: str = "") -> tuple[FixMessage, str]:
        """Cancel a received order nobody asked to cancel: send
        ExecutionReport(Canceled) under the order's own ClOrdID, without
        OrigClOrdID(41), and return the ExecID."""
        session = self._active_session(session_id)
        extra_pairs = parse_extra_tags(extra_tags)
        order = await self._load_order(session_id, cl_ord_id)

        order_id = order["order_id"] or await self.ids.next_id("OR")
        exec_id = await self.ids.next_id("EX")
        msg = session.factory.execution_report(
            order_id=order_id,
            cl_ord_id=cl_ord_id,
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
            text=text or None,
        )
        msg.extra = extra_pairs
        self._stamp_client(session, msg, order.get("client"))

        # As with a fill, the report implicitly acknowledges a not-yet-accepted
        # order, so the pending New is consumed; a pending Cancel/Replace stays
        # parked — rejecting it afterwards is the "too late to cancel" macro.
        consumed = order["pending_action"] == "New"
        await self._write_order(
            order, order_id=order_id, status="Canceled", leaves_qty=0.0,
            sent_text=self._text_as_sent(session, msg),
            pending_action="" if consumed else order["pending_action"],
            pending_extra_tags="" if consumed else order["pending_extra_tags"],
        )
        return msg, exec_id

    @_market_action
    async def restate_order(
        self, session_id: str, cl_ord_id: str, qty: float, price: float = 0.0,
        reason: str = "", extra_tags: str = "", text: str = "",
    ) -> tuple[FixMessage, str]:
        """Restate a received order's terms unasked: send ExecutionReport
        (Restated) under the order's own ClOrdID, without OrigClOrdID(41),
        carrying the new OrderQty(38)/Price(44) and ExecRestatementReason
        (378), and return the ExecID. No price (a market order's 0) sends no
        44 and leaves the row's alone."""
        session = self._active_session(session_id)
        dictionary = session.dictionary
        if not dictionary.has_enum("150", "D"):
            raise ValueError(f"ExecType Restated (150=D) is not defined by {dictionary.version}")
        extra_pairs = parse_extra_tags(extra_tags)
        order = await self._load_order(session_id, cl_ord_id)
        if qty <= 0:
            raise ValueError("Restated quantity must be positive")
        if qty < order["cum_qty"]:
            raise ValueError("Restated quantity is below executed quantity")

        order_id = order["order_id"] or await self.ids.next_id("OR")
        exec_id = await self.ids.next_id("EX")
        leaves_qty = qty - order["cum_qty"]
        # A restatement carries the order's working status, as an accepted
        # replace does on FIX 4.4.
        status_code = "0" if order["cum_qty"] <= 0 else ("2" if leaves_qty <= 0 else "1")
        terms = {"44": str(price)} if price else {}
        if reason and dictionary.defines("378"):
            terms["378"] = reason
        msg = session.factory.execution_report(
            order_id=order_id,
            cl_ord_id=cl_ord_id,
            exec_id=exec_id,
            exec_trans_type="0",
            exec_type="D",
            ord_status=status_code,
            symbol=order["symbol"],
            side=order["side_code"],
            qty=qty,
            cum_qty=order["cum_qty"],
            avg_price=order["avg_price"],
            leaves_qty=leaves_qty,
            text=text or None,
            **terms,
        )
        msg.extra = extra_pairs
        self._stamp_client(session, msg, order.get("client"))

        # As with a fill, the report implicitly acknowledges a not-yet-accepted
        # order; a pending Cancel/Replace stays parked on the restated order.
        consumed = order["pending_action"] == "New"
        await self._write_order(
            order, order_id=order_id, status=dictionary.enum_name("39", status_code),
            order_qty=qty, price=price or order["price"], leaves_qty=leaves_qty,
            sent_text=self._text_as_sent(session, msg),
            pending_action="" if consumed else order["pending_action"],
            pending_extra_tags="" if consumed else order["pending_extra_tags"],
        )
        return msg, exec_id

    async def accept_request(self, session_id: str, cl_ord_id: str, extra_tags: str = "",
                             text: str = "") -> str:
        """Accept whatever is pending on the order — the new order itself,
        a cancel request, or a replace request."""
        order = await self._load_order(session_id, cl_ord_id)
        action = order["pending_action"]
        if action == "New":
            return await self.accept_order(session_id, cl_ord_id, extra_tags=extra_tags, text=text)
        if action == "Cancel":
            return await self.accept_cancel(session_id, cl_ord_id, extra_tags=extra_tags, text=text)
        if action == "Replace":
            return await self.accept_replace(session_id, cl_ord_id, extra_tags=extra_tags, text=text)
        raise ValueError(f"Nothing pending on {cl_ord_id}")

    async def reject_request(self, session_id: str, cl_ord_id: str, text: str = "",
                             extra_tags: str = "") -> None:
        """Reject whatever is pending on the order — ExecutionReport(Rejected)
        for a new order, OrderCancelReject for a cancel/replace request."""
        order = await self._load_order(session_id, cl_ord_id)
        action = order["pending_action"]
        if action == "New":
            await self.reject_order(session_id, cl_ord_id, text=text, extra_tags=extra_tags)
        elif action in ("Cancel", "Replace"):
            await self.reject_cancel(session_id, cl_ord_id, text=text, extra_tags=extra_tags)
        else:
            raise ValueError(f"Nothing pending on {cl_ord_id}")

    @_market_action
    async def accept_cancel(self, session_id: str, cl_ord_id: str, extra_tags: str = "",
                            text: str = "") -> tuple[FixMessage, str]:
        """Accept the pending cancel request: send ExecutionReport(Canceled)."""
        session = self._active_session(session_id)
        extra_pairs = parse_extra_tags(extra_tags)
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
            text=text or None,
            **{"41": cl_ord_id},
        )
        msg.extra = extra_pairs
        self._stamp_client(session, msg, order.get("client"))

        await self._rename_order(session_id, cl_ord_id, new_cl_ord_id)
        await self._write_order(
            {**order, "cl_ord_id": new_cl_ord_id, "orig_cl_ord_id": cl_ord_id},
            status="Canceled", leaves_qty=0.0,
            sent_text=self._text_as_sent(session, msg),
            pending_action="", pending_cl_ord_id="",
            pending_qty=0.0, pending_price=0.0, pending_extra_tags="",
        )
        return msg, exec_id

    @_market_action
    async def accept_replace(self, session_id: str, cl_ord_id: str, extra_tags: str = "",
                             text: str = "") -> tuple[FixMessage, str]:
        """Accept the pending cancel/replace request: send ExecutionReport(Replaced)
        with the requested quantity and price."""
        session = self._active_session(session_id)
        extra_pairs = parse_extra_tags(extra_tags)
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
        # OrdStatus Replaced(5) was removed in FIX 4.4 (ExecType 5 alone marks
        # the event there; 39 carries the working status) and restored in 5.0.
        dictionary = session.dictionary
        if dictionary.has_enum("39", "5"):
            status_code = "5"
        else:
            status_code = "0" if order["cum_qty"] <= 0 else ("2" if leaves_qty <= 0 else "1")
        msg = session.factory.execution_report(
            order_id=order["order_id"],
            cl_ord_id=new_cl_ord_id,
            exec_id=exec_id,
            exec_trans_type="0",
            exec_type="5",
            ord_status=status_code,
            symbol=order["symbol"],
            side=order["side_code"],
            qty=new_qty,
            cum_qty=order["cum_qty"],
            avg_price=order["avg_price"],
            leaves_qty=leaves_qty,
            text=text or None,
            **{"41": cl_ord_id, "44": str(new_price)},
        )
        msg.extra = extra_pairs
        self._stamp_client(session, msg, order.get("client"))
        sent_text = self._text_as_sent(session, msg)

        await self._rename_order(session_id, cl_ord_id, new_cl_ord_id)
        await self._write_order(
            {**order, "cl_ord_id": new_cl_ord_id, "orig_cl_ord_id": cl_ord_id},
            status=dictionary.enum_name("39", status_code),
            order_qty=new_qty, price=new_price,
            leaves_qty=leaves_qty, sent_text=sent_text,
            pending_action="", pending_cl_ord_id="",
            pending_qty=0.0, pending_price=0.0, pending_extra_tags="",
        )
        # The accepted replace's terms are now the order's — its custom tags
        # included, so the next Fill echoes them (extra_tags is insert-only in
        # the upsert; the record_entered op is the update path for it).
        entered_row = {**order, "extra_tags": order["pending_extra_tags"], "sent_text": sent_text}
        params = tuple(entered_row[c] for c in ENTERED_COLS) + (
            _fix_timestamp(), None, session_id, new_cl_ord_id)
        await self.writer.submit(self._compiled_ops["record_entered"], (params,),
                                 {"cl_ord_id": new_cl_ord_id})
        return msg, exec_id

    @_market_action
    async def reject_cancel(self, session_id: str, cl_ord_id: str, text: str = "",
                            extra_tags: str = "") -> tuple[FixMessage, None]:
        """Reject the pending cancel/replace request: send OrderCancelReject (35=9).
        The order itself is untouched — its status was never changed by the request."""
        session = self._active_session(session_id)
        extra_pairs = parse_extra_tags(extra_tags)
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
        msg.extra = extra_pairs
        self._stamp_client(session, msg, order.get("client"))

        await self._write_order(
            order, sent_text=self._text_as_sent(session, msg),
            pending_action="", pending_cl_ord_id="",
            pending_qty=0.0, pending_price=0.0, pending_extra_tags="",
        )
        return msg, None

    @_market_action
    async def correct_trade(
        self, session_id: str, exec_id: str, qty: float, price: float,
        extra_tags: str = "", text: str = "",
    ) -> tuple[FixMessage, str]:
        """Correct a sent trade (ExecTransType=Correct) and return the new ExecID."""
        if qty <= 0:
            raise ValueError("Corrected quantity must be positive")
        session = self._active_session(session_id)
        extra_pairs = parse_extra_tags(extra_tags)
        dictionary = session.dictionary
        execution = await self._load_execution(session_id, exec_id)
        self._require_live_trade(execution, "correct")
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
            text=text or None,
        )
        msg.extra = extra_pairs
        self._stamp_client(session, msg, order.get("client"))

        await self._write_order(
            order, status=dictionary.enum_name("39", status_code),
            cum_qty=cum_qty, avg_price=avg_price, leaves_qty=leaves_qty,
            last_qty=qty, last_price=price,
        )
        await self._write_sent_execution(
            order, new_exec_id, trade_id, *_sent_exec_kind(dictionary, msg, "2"),
            qty, price, cum_qty, avg_price, leaves_qty,
            exec_ref_id=exec_id, row_id=execution["id"],
            text=self._text_as_sent(session, msg), extra_tags=extra_tags,
        )
        return msg, new_exec_id

    @_market_action
    async def bust_trade(self, session_id: str, exec_id: str, extra_tags: str = "",
                         text: str = "") -> tuple[FixMessage, str]:
        """Bust a sent trade (ExecTransType=Cancel) and return the new ExecID."""
        session = self._active_session(session_id)
        extra_pairs = parse_extra_tags(extra_tags)
        dictionary = session.dictionary
        execution = await self._load_execution(session_id, exec_id)
        self._require_live_trade(execution, "bust")
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
            text=text or None,
        )
        msg.extra = extra_pairs
        self._stamp_client(session, msg, order.get("client"))

        await self._write_order(
            order, status=dictionary.enum_name("39", status_code),
            cum_qty=cum_qty, avg_price=avg_price, leaves_qty=leaves_qty,
            last_qty=execution["last_qty"], last_price=execution["last_price"],
        )
        await self._write_sent_execution(
            order, new_exec_id, trade_id, *_sent_exec_kind(dictionary, msg, "1"),
            execution["last_qty"], execution["last_price"],
            cum_qty, avg_price, leaves_qty,
            exec_ref_id=exec_id, row_id=execution["id"],
            text=self._text_as_sent(session, msg), extra_tags=extra_tags,
        )
        return msg, new_exec_id

    @_market_action
    async def renotify_trade(self, session_id: str, exec_id: str, extra_tags: str = "",
                             text: str = "") -> tuple[FixMessage, str]:
        """Re-notify a sent trade the counterparty DK'd and return the new ExecID.

        The trade's current report — its fill, correction or bust, by the
        row's exec_type — goes out again under a fresh ExecID: the trade's own
        terms and ExecRefID(19) as the row holds them, the order's state as it
        stands now (the DK never moved our book, and fills since then must not
        be reported backwards). The row becomes a new version under the new
        ExecID with the DK cleared, so a DK of the re-notification shows; the
        order row is not written. Extras can recast the report (20=0|19= sends
        a DK'd correction as a plain fill), so the row records the kind and
        reference as sent.
        """
        session = self._active_session(session_id)
        extra_pairs = parse_extra_tags(extra_tags)
        dictionary = session.dictionary
        execution = await self._load_execution(session_id, exec_id)
        if not execution["dk_reason"]:
            raise ValueError(
                f"Cannot re-notify {execution['exec_id']}: trade "
                f"{execution['trade_id']} has not been DK'ed")
        order = await self._load_order_for_execution(execution)

        if execution["exec_type"] in BUSTED_EXEC_TYPES:
            trans_type = "1"
        elif execution["exec_type"] in CORRECTED_EXEC_TYPES:
            trans_type = "2"
        else:
            trans_type = "0"
        cum_qty, leaves_qty = order["cum_qty"], order["leaves_qty"]
        status_code = "0" if cum_qty == 0 else ("2" if leaves_qty == 0 else "1")
        new_exec_id = await self.ids.next_id("EX")

        msg = session.factory.execution_report(
            order_id=order["order_id"],
            cl_ord_id=order["cl_ord_id"],
            exec_id=new_exec_id,
            exec_trans_type=trans_type,
            exec_type=status_code,
            ord_status=status_code,
            symbol=execution["symbol"],
            side=execution["side_code"],
            qty=order["order_qty"],
            last_qty=execution["last_qty"],
            last_price=execution["last_price"],
            cum_qty=cum_qty,
            avg_price=order["avg_price"],
            leaves_qty=leaves_qty,
            exec_ref_id=execution["exec_ref_id"] or None,
            text=text or None,
        )
        msg.extra = extra_pairs
        self._stamp_client(session, msg, order.get("client"))

        sent = self._as_sent(session, msg)
        await self._write_sent_execution(
            order, new_exec_id, execution["trade_id"],
            *_sent_exec_kind(dictionary, sent, trans_type if trans_type != "0" else status_code),
            execution["last_qty"], execution["last_price"],
            cum_qty, order["avg_price"], leaves_qty,
            exec_ref_id=sent.get("19", ""), row_id=execution["id"],
            text=sent.get("58", ""), extra_tags=extra_tags,
        )
        return msg, new_exec_id

    async def dk_trade(
        self, session_id: str, exec_id: str, reason: str, text: str = "",
        extra_tags: str = "",
    ) -> None:
        """Answer a received trade with DontKnowTrade (35=Q).

        The trade's terms are the counterparty's report and stay as received;
        the row only records that we disputed it — DKReason(127) and Text(58)
        as sent, extras applied — as a new version, which their correction or
        bust blanks again (EXEC_UPDATE_COLS). On a sent trade those columns
        are the counterparty's DK and arm Re-notify, so nothing is written there.
        """
        if not reason:
            raise ValueError("DK reason is required")
        session = self._active_session(session_id)
        extra_pairs = parse_extra_tags(extra_tags)
        execution = await self._load_execution(session_id, exec_id)

        msg = session.factory.dont_know_trade(
            order_id=execution["order_id"],
            exec_id=exec_id,
            dk_reason=reason,
            symbol=execution["symbol"],
            side=execution["side_code"],
            qty=await self._order_qty_of(execution),
            last_qty=execution["last_qty"],
            last_price=execution["last_price"],
            text=text or None,
        )
        msg.extra = extra_pairs
        self._stamp_client(session, msg, execution.get("client"))
        if execution["direction"] != "RX":
            await session.send_message(msg)
            return

        sent = self._as_sent(session, msg)
        ops = self._compiled_ops["dk_execution"]
        mark = (session.dictionary.enum_name("127", sent.get("127", "")), sent.get("58", ""))
        await self.writer.submit(ops, ((*mark, None, execution["id"]),), {"exec_id": exec_id})
        try:
            await session.send_message(msg)
        except Exception:
            unmark = (execution["dk_reason"], execution["dk_text"], None, execution["id"])
            await self.writer.submit(ops, (unmark,), {"exec_id": exec_id})
            raise

    async def _order_qty_of(self, execution: dict[str, Any]) -> float:
        """OrderQty for a received execution. Its order_id is the counterparty's
        OrderID(37), never ours, so the order is found by the fill-time ClOrdID;
        an accepted replace since then renamed the chain, in which case the
        execution's own CumQty + LeavesQty is the quantity the ER reported."""
        try:
            order = await self._load_order(execution["session_id"], execution["cl_ord_id"])
        except ValueError:
            return execution["cum_qty"] + execution["leaves_qty"]
        return order["order_qty"]

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
            msg.to_wire_string(),
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
            msg.to_wire_string(),
            None,
        )
        ops = self._compiled_ops["insert_allocation"]
        await self.writer.submit(ops, (params,), {"alloc_id": msg.get("70", "")})

    # ── Replay ───────────────────────────────────────────────────────

    async def load_replay(self, name: str = "", file_path: str = "", example: str = "") -> dict[str, Any]:
        """Create a replay job: parse the file once (a day's log, in a
        thread) and keep what it holds on the row — CompID pairs, message
        types with counts, time span — for Configure and Start to offer.
        Every type starts ticked; the direction waits for Start."""
        spec = f"{replay.EXAMPLE_PREFIX}{example.strip()}" if example.strip() else file_path
        path = replay.resolve_path(spec)
        messages = await asyncio.to_thread(replay.parse_log_file, path)
        if not messages:
            raise ValueError(f"No FIX application messages found in {path}")
        summary = replay.summarize(messages)
        if not name.strip():
            name = next((e["title"] for e in replay.examples() if e["name"] == path.stem), path.stem) \
                if spec.startswith(replay.EXAMPLE_PREFIX) else path.name
        job = {
            "name": name.strip(), "file_path": spec.strip(), "status": "loaded",
            "total_messages": summary["count"], "sent_messages": 0, "selected_messages": 0,
            "error_text": "", "created_at": _fix_timestamp(), "summary": json.dumps(summary),
            "pairs": replay.describe_pairs(summary), "first_time": summary["first"],
            "last_time": summary["last"], "direction": "",
            "target_session": "", "speed": 1.0, "msg_filter": ",".join(summary["types"]),
            "time_from": "", "time_to": "", "max_gap": 30.0,
            "default_direction": await self._default_direction(summary, ""),
        }
        params = tuple(job[c] for c in REPLAY_JOB_COLS) + (None,)
        await self.writer.submit(self._compiled_ops["insert_replay"], (params,), {})
        row = await self._fetch_one("SELECT MAX(id) AS id FROM fix_replay_jobs")
        return {"job_id": row["id"], "count": summary["count"], "name": job["name"]}

    async def configure_replay(self, job_id: int, target_session: str = "", speed: Any = 1.0,
                               msg_filter: str = "", time_from: str = "", time_to: str = "",
                               max_gap: Any = 30.0) -> None:
        job = await self._replay_job(job_id)
        if job["id"] in self._replay_tasks and not self._replay_tasks[job["id"]].done:
            raise ValueError(f"Replay job {job_id} is {job['status']} — stop it before changing it")
        if target_session and target_session not in self.sessions:
            raise ValueError(f"Unknown session: {target_session}")
        try:
            speed, max_gap = float(speed or 0), float(max_gap or 0)
        except (TypeError, ValueError):
            raise ValueError("Speed and max gap are numbers") from None
        if speed < 0 or max_gap < 0:
            raise ValueError("Speed and max gap cannot be negative")
        replay.select_messages([], time_from=time_from, time_to=time_to)   # validates the window
        summary = json.loads(job["summary"] or "{}")
        known = set(summary.get("types", {}))
        types = [t.strip() for t in str(msg_filter).split(",") if t.strip()]
        unknown = [t for t in types if t not in known]
        if unknown:
            raise ValueError(f"The file has no {', '.join(unknown)} messages")
        config = {
            "target_session": target_session, "speed": speed, "msg_filter": ",".join(types),
            "time_from": time_from.strip(), "time_to": time_to.strip(), "max_gap": max_gap,
            "default_direction": await self._default_direction(summary, target_session),
        }
        params = tuple(config[c] for c in REPLAY_CONFIG_COLS) + (None, job["id"])
        await self.writer.submit(self._compiled_ops["configure_replay"], (params,), {"id": job["id"]})

    async def _default_direction(self, summary: dict[str, Any], session_id: str) -> str:
        """The direction Start preselects: the file's only pair, else the one
        pair whose sender is the session's own SenderCompID."""
        pairs = summary.get("pairs", [])
        if len(pairs) == 1:
            return replay.direction_key(pairs[0]["sender"], pairs[0]["target"])
        if not session_id:
            return ""
        row = await self._fetch_one("SELECT sender_comp_id FROM fix_sessions WHERE session_id = ?", (session_id,))
        if not row:
            return ""
        ours = [p for p in pairs if p["sender"] == row["sender_comp_id"]]
        return replay.direction_key(ours[0]["sender"], ours[0]["target"]) if len(ours) == 1 else ""

    async def start_replay(self, job_id: int, direction: str = "") -> dict[str, Any]:
        """Start a replay job in one direction: the messages the file holds
        from that sender to that target, of the configured types, inside
        the configured window, sent as the target session."""
        job = await self._replay_job(job_id)
        live = self._replay_tasks.get(job["id"])
        if live and not live.done:
            raise ValueError(f"Replay job {job_id} is already {job['status']}")

        session_id = job.get("target_session", "")
        session = self.sessions.get(session_id)
        if not session_id:
            raise ValueError("Configure the job with a session to replay into")
        if not session or not session.is_active:
            raise ValueError(f"Target session {session_id} is not active")

        summary = json.loads(job["summary"] or "{}")
        keys = [replay.direction_key(p["sender"], p["target"]) for p in summary.get("pairs", [])]
        direction = direction.strip() or job.get("default_direction", "")
        if len(keys) > 1 and not direction:
            raise ValueError("Choose a direction: the file holds messages both ways")
        if direction and keys and direction not in keys:
            raise ValueError(f"The file holds no {direction} messages")

        messages = await asyncio.to_thread(replay.parse_log_file, replay.resolve_path(job["file_path"]))
        chosen = replay.select_messages(messages, direction, job.get("msg_filter", ""),
                                        job.get("time_from", ""), job.get("time_to", ""))
        if not chosen:
            raise ValueError("Nothing to replay: no message matches the direction, types and window")

        params = (direction, len(chosen), None, job["id"])
        await self.writer.submit(self._compiled_ops["begin_replay"], (params,), {"id": job["id"]})

        task = ReplayTask(
            job_id=job["id"],
            session=session,
            messages=chosen,
            speed=float(job.get("speed") or 0),
            max_gap=float(job.get("max_gap") or 0),
            on_progress=self._on_replay_progress,
        )
        self._replay_tasks[job["id"]] = task
        await task.start()
        return {"selected": len(chosen), "direction": direction}

    async def pause_replay(self, job_id: int) -> None:
        task = self._live_replay(job_id)
        task.pause()
        await self._on_replay_progress(job_id, task.sent, "paused", "")

    async def resume_replay(self, job_id: int) -> None:
        task = self._live_replay(job_id)
        task.resume()
        await self._on_replay_progress(job_id, task.sent, "running", "")

    async def stop_replay(self, job_id: int) -> None:
        task = self._live_replay(job_id)
        self._replay_tasks.pop(job_id, None)
        await task.stop()
        await self._on_replay_progress(job_id, task.sent, "stopped", "")

    async def delete_replay(self, job_id: int) -> None:
        """Delete a job, stopping it first if it is playing."""
        job = await self._replay_job(job_id)
        task = self._replay_tasks.pop(job["id"], None)
        if task:
            await task.stop()
        await self.writer.submit(self._compiled_ops["delete_replay"], ((job["id"],),), {"id": job["id"]})

    def _live_replay(self, job_id: int) -> ReplayTask:
        task = self._replay_tasks.get(job_id)
        if not task or task.done:
            raise ValueError(f"Replay job {job_id} is not playing")
        return task

    async def _replay_job(self, job_id: int) -> dict[str, Any]:
        row = await self._fetch_one("SELECT * FROM fix_replay_jobs WHERE id = ?", (int(job_id),))
        if not row:
            raise ValueError(f"Unknown replay job: {job_id}")
        return row

    async def _fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        cursor = await self.db.read_conn.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def _on_replay_progress(self, job_id: int, sent: int, status: str, error: str) -> None:
        params = (status, sent, error, None, job_id)
        ops = self._compiled_ops["update_replay"]
        await self.writer.submit(ops, (params,), {"id": job_id})

    async def _load_custom_dictionaries(self) -> None:
        """Register user-defined dictionaries before sessions bind them."""
        conn = self.db.read_conn
        cursor = await conn.execute("SELECT * FROM fix_dictionaries")
        rows = await cursor.fetchall()
        await cursor.close()
        for row in rows:
            try:
                doc = json.loads(row["doc"] or "{}")
            except json.JSONDecodeError:
                continue
            register_custom(row["name"], row["base_version"] or None, doc)

    async def save_dictionary(self, name: str, base_version: str = "",
                              doc: Any = None) -> str:
        """Create or update a custom dictionary and register it immediately.

        With base_version the doc is a delta over that standard; without it
        the doc stands alone. Running sessions keep their built dictionary
        until restarted; the UI resolves the new content right away.
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("Dictionary name is required")
        if name in STANDARD_VERSIONS:
            raise ValueError(f"{name} is a standard dictionary and cannot be modified")
        if base_version and base_version not in STANDARD_VERSIONS:
            raise ValueError(f"Unknown base version: {base_version}")
        if isinstance(doc, str):
            doc = json.loads(doc or "{}")
        doc = doc or {}
        register_custom(name, base_version or None, doc)
        now = _fix_timestamp()
        params = (name, base_version, json.dumps(doc), now, now, None)
        await self.writer.submit(
            self._compiled_ops["upsert_dictionary"], (params,), {"name": name})
        return name

    async def save_template(self, scope: str, name: str, **terms: Any) -> str:
        """Keep a dialog's terms as a template of `scope`, replacing the one
        of the same name. Terms are stored as typed (text, None as blank): a
        blank one means "ask the row" when the template is loaded. A write
        like any other, so a dialog's Save-as lands before its send."""
        if scope not in TEMPLATE_SCOPES:
            raise ValueError(f"Unknown template scope: {scope}")
        name = (name or "").strip()
        if not name:
            raise ValueError("A template needs a name")
        unknown = set(terms) - set(TEMPLATE_TERM_COLS)
        if unknown:
            raise ValueError(f"Not template terms: {', '.join(sorted(unknown))}")
        values = tuple("" if terms.get(c) is None else str(terms[c]) for c in TEMPLATE_TERM_COLS)
        await self.writer.submit(
            self._compiled_ops["upsert_template"], ((scope, name) + values + (None,),),
            {"scope": scope, "name": name})
        return name

    async def delete_dictionary(self, name: str) -> None:
        conn = self.db.read_conn
        cursor = await conn.execute(
            "SELECT session_id FROM fix_sessions WHERE dictionary = ?", (name,))
        rows = await cursor.fetchall()
        await cursor.close()
        if rows:
            used = ", ".join(sorted(r["session_id"] for r in rows))
            raise ValueError(f"Dictionary {name!r} is bound to session(s): {used}")
        unregister_custom(name)
        await self.writer.submit(
            self._compiled_ops["delete_dictionary"], ((name,),), {"name": name})

    def get_dictionary(self, name: str) -> dict[str, Any]:
        """Resolved dictionary document plus its metadata, for the UI."""
        meta = custom_meta(name)
        if meta is None and name not in STANDARD_VERSIONS:
            raise ValueError(f"Unknown dictionary: {name}")
        d = FixDictionary(name)
        return {
            "name": name,
            "kind": "standard" if meta is None else "custom",
            "base_version": (meta or {}).get("base_version", ""),
            "doc": (meta or {}).get("doc"),
            "dictionary": {
                "version": name,
                "begin_string": d.begin_string(),
                "header": d.header_tags,
                "trailer": d.trailer_tags,
                "fields": d.fields,
                "enums": d.enums,
                "messages": d.messages,
                "groups": d.groups,
            },
        }

    def list_dictionaries(self) -> list[dict[str, str]]:
        out = [{"name": v, "kind": "standard", "base_version": ""}
               for v in STANDARD_VERSIONS]
        for name in custom_names():
            meta = custom_meta(name) or {}
            out.append({"name": name, "kind": "custom",
                        "base_version": meta.get("base_version", "")})
        return out

    async def handle_undo_redo(self, event: Any) -> None:
        """Follow a version cursor move on fix_sessions (mkio's on_undo_redo).

        mkio puts the config row right; this puts the engine right: the
        session object and its state row need following. An undone Add (the row is gone) stops and drops the
        session and deletes its state row, which the cursor move left behind
        because fix_session_state is not versioned; a redone Add rebuilds
        both; an undone or redone edit reloads, which rebuilds a stopped
        session and swaps config on a running one, exactly as Edit does."""
        if event.table != "fix_sessions":
            return
        if event.new is None:
            session_id = event.old["session_id"]
            session = self.sessions.pop(session_id, None)
            if session is not None:
                await session.stop()
            await self.writer.submit(
                self._compiled_ops["delete_state"], ((session_id,),),
                {"session_id": session_id},
            )
            return
        session_id = event.new["session_id"]
        if event.old is None:
            await self.update_session_state(session_id, {"status": "DOWN"})
        await self.reload_session(session_id)

    async def check_archive(self, selection: dict[str, list[dict[str, Any]]]) -> None:
        """mkio's ``on_archive`` "before" stage: refuse an online archive that
        would pull rows out from under the engine.

        A session goes only while stopped (``DOWN``/``ERROR`` — the same gate
        as Delete); a dictionary only while no surviving session names it
        (the ``delete_dictionary`` rule, minus the sessions leaving in the
        same run); the ID counters never while the engine holds them in
        memory, since emptying the table would restart every prefix at 1
        on the next run and reissue IDs. Offline runs (server stopped) have
        no engine to ask and are the user's responsibility."""
        problems: list[str] = []
        leaving = {r["session_id"] for r in selection.get("fix_sessions", [])}
        for session_id in sorted(leaving):
            session = self.sessions.get(session_id)
            status = session.status if session else "DOWN"
            if status not in ("DOWN", "ERROR"):
                problems.append(f"session {session_id} is {status} — stop it before archiving it")
        names = {r["name"] for r in selection.get("fix_dictionaries", [])}
        if names:
            conn = self.db.read_conn
            cursor = await conn.execute(
                "SELECT session_id, dictionary FROM fix_sessions WHERE dictionary != ''")
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                if row["dictionary"] in names and row["session_id"] not in leaving:
                    problems.append(
                        f"dictionary {row['dictionary']} is bound to session {row['session_id']}")
        live = [run.macro.name for run in self.macros.live_runs()]
        scripted = ("fix_orders", "fix_executions", "fix_macros", "fix_macro_runs",
                    "fix_macro_orders", "fix_macro_log")
        if live and any(selection.get(t) for t in scripted):
            problems.append(f"macro {', '.join(sorted(live))} is armed: its scripts read the orders, trades "
                            "and run rows this archive would take — stop the run first")
        playing = [r["id"] for r in selection.get("fix_replay_jobs", [])
                   if r["id"] in self._replay_tasks and not self._replay_tasks[r["id"]].done]
        if playing:
            problems.append(f"replay job {', '.join(str(i) for i in playing)} is playing — stop it first")
        if selection.get("fix_id_state"):
            problems.append(
                "fix_id_state holds the ID counters the running engine is using — "
                "archive it offline, with the server stopped")
        if problems:
            raise ValueError("; ".join(problems))

    async def after_archive(self, selection: dict[str, list[dict[str, Any]]]) -> None:
        """mkio's ``on_archive`` "after" stage: drop what the archived config
        rows had built — the session objects (already stopped, per
        ``check_archive``) and the custom dictionary registrations."""
        for row in selection.get("fix_sessions", []):
            session = self.sessions.pop(row["session_id"], None)
            if session is not None:
                await session.stop()
        for row in selection.get("fix_dictionaries", []):
            unregister_custom(row["name"])

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
            session = self.sessions.get(session_id)
            if session and (session._transport or session._socket):
                # Running: swap config only; the live transport reads it
                # where needed.
                session.config = config
            else:
                # Stopped or new: rebuild so __init__-time wiring (dictionary,
                # message factory) picks up the current config.
                self.sessions[session_id] = FixSession(self, config)
        elif session_id in self.sessions:
            await self.sessions[session_id].stop()
            del self.sessions[session_id]
