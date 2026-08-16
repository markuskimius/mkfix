"""Business-object ID generation: <type code><instance code><8-digit counter>.

Type codes identify the ID kind at a glance:
    RT  ClOrdID (routed ID) on messages mkfix sends
    OR  immutable Order ID
    EX  ExecID on ExecutionReports mkfix sends
    TR  immutable Trade ID grouping a fill with its corrections/busts

The instance code is the first two characters of the username, uppercased and
padded with trailing X's, so concurrent mkfix users facing the same
counterparty mint distinguishable IDs.
"""

from __future__ import annotations

import asyncio
import getpass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mkio.database import Database
    from mkio.writer import WriteBatcher, CompiledOp

_INSTANCE_CODE_LEN = 2


def _instance_code(username: str) -> str:
    return (username[:_INSTANCE_CODE_LEN].upper() + "X" * _INSTANCE_CODE_LEN)[:_INSTANCE_CODE_LEN]


class IdGenerator:
    """Mints prefixed IDs with per-prefix counters, persisted in fix_id_state.

    Counters start at 1 and increment forever; state is written through the
    WriteBatcher so counter updates serialize with the engine's other writes.
    """

    def __init__(self, db: Database, writer: WriteBatcher):
        self.db = db
        self.writer = writer
        self._instance_id: str | None = None
        self._counters: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._op: tuple[CompiledOp, ...] | None = None

    @property
    def instance_id(self) -> str:
        if self._instance_id is None:
            raise RuntimeError("IdGenerator not started")
        return self._instance_id

    async def start(self) -> None:
        async with self._lock:
            await self._start_locked()

    async def next_id(self, prefix: str) -> str:
        async with self._lock:
            await self._start_locked()
            counter = self._counters.get(prefix, 0) + 1
            self._counters[prefix] = counter
            await self._persist(prefix, counter)
            return f"{prefix}{self._instance_id}{counter:08d}"

    async def _start_locked(self) -> None:
        if self._instance_id is not None:
            return
        from mkio.writer import CompiledOp

        self._op = (CompiledOp(
            table="fix_id_state",
            op_type="upsert",
            sql=(
                "INSERT INTO fix_id_state (name, counter, _mkio_ref) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET counter = excluded.counter, "
                "_mkio_ref = excluded._mkio_ref "
                "RETURNING *"
            ),
            param_names=("name", "counter", "_mkio_ref"),
        ),)

        cursor = await self.db.read_conn.execute("SELECT name, counter FROM fix_id_state")
        rows = await cursor.fetchall()
        await cursor.close()
        for row in rows:
            self._counters[row["name"]] = row["counter"]

        self._instance_id = _instance_code(getpass.getuser())

    async def _persist(self, name: str, counter: int) -> None:
        assert self._op is not None
        params = (name, counter, None)
        await self.writer.submit(self._op, (params,), {"name": name})
