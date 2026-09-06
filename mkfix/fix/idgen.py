"""Business-object ID generation: <type code><instance code><8-digit counter>.

Type codes identify the ID kind at a glance:
    RT  ClOrdID (routed ID) on messages mkfix sends
    OR  immutable Order ID
    EX  ExecID on ExecutionReports mkfix sends
    TR  immutable Trade ID grouping a fill with its corrections/busts

The instance code is the first two characters of the username, uppercased and
padded with trailing X's, so concurrent mkfix users facing the same
counterparty mint distinguishable IDs. ``mkfix -i CODE`` overrides it, for
several instances run by one user or IDs that must name a particular source;
the override is saved in fix_settings and reused by later runs on the same
database until ``-i`` is given again — ``-i ''`` clears it.
"""

from __future__ import annotations

import asyncio
import getpass
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mkio.database import Database
    from mkio.writer import WriteBatcher, CompiledOp

_INSTANCE_CODE_LEN = 2
_INSTANCE_CODE_RE = re.compile(r"[A-Za-z0-9]{%d}" % _INSTANCE_CODE_LEN)


def _instance_code(username: str) -> str:
    return (username[:_INSTANCE_CODE_LEN].upper() + "X" * _INSTANCE_CODE_LEN)[:_INSTANCE_CODE_LEN]


def validate_instance_code(code: str) -> str:
    """Check an explicit instance code: exactly two ASCII letters or digits,
    used verbatim (the derived code is uppercased, an explicit one is not)."""
    if not _INSTANCE_CODE_RE.fullmatch(code):
        raise ValueError(
            f"instance code must be exactly {_INSTANCE_CODE_LEN} ASCII letters or digits, got {code!r}"
        )
    return code


class IdGenerator:
    """Mints prefixed IDs with per-prefix counters, persisted in fix_id_state.

    Counters start at 1 and increment forever; state is written through the
    WriteBatcher so counter updates serialize with the engine's other writes.
    """

    def __init__(self, db: Database, writer: WriteBatcher, instance_code: str | None = None):
        """``instance_code`` None keeps whatever the database holds (or the
        username default); a code saves and uses it; '' clears the saved one."""
        self.db = db
        self.writer = writer
        self._explicit_code = (
            validate_instance_code(instance_code) if instance_code else instance_code
        )
        self._instance_id: str | None = None
        self._instance_source: str = ""
        self._counters: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._op: tuple[CompiledOp, ...] | None = None
        self._setting_op: tuple[CompiledOp, ...] | None = None

    @property
    def instance_id(self) -> str:
        if self._instance_id is None:
            raise RuntimeError("IdGenerator not started")
        return self._instance_id

    @property
    def instance_source(self) -> str:
        """Where the code in effect came from: 'saved' (fix_settings, whether
        written by this run's -i or an earlier one) or 'username'."""
        if self._instance_id is None:
            raise RuntimeError("IdGenerator not started")
        return self._instance_source

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
        self._setting_op = (CompiledOp(
            table="fix_settings",
            op_type="upsert",
            sql=(
                "INSERT INTO fix_settings (key, value, _mkio_ref) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "_mkio_ref = excluded._mkio_ref "
                "RETURNING *"
            ),
            param_names=("key", "value", "_mkio_ref"),
        ),)

        cursor = await self.db.read_conn.execute("SELECT name, counter FROM fix_id_state")
        rows = await cursor.fetchall()
        await cursor.close()
        for row in rows:
            self._counters[row["name"]] = row["counter"]

        saved = await self._resolve_saved_code()
        if saved:
            self._instance_id, self._instance_source = saved, "saved"
        else:
            self._instance_id, self._instance_source = _instance_code(getpass.getuser()), "username"

    async def _resolve_saved_code(self) -> str:
        """The code fix_settings holds after applying this run's -i, if any:
        a given code is saved, '' clears the saved one, None leaves it alone."""
        if self._explicit_code is not None:
            assert self._setting_op is not None
            params = ("instance_code", self._explicit_code, None)
            await self.writer.submit(self._setting_op, (params,), {"key": "instance_code"})
            return self._explicit_code
        cursor = await self.db.read_conn.execute(
            "SELECT value FROM fix_settings WHERE key = 'instance_code'"
        )
        row = await cursor.fetchone()
        await cursor.close()
        value = row["value"] if row is not None else ""
        return value if value and _INSTANCE_CODE_RE.fullmatch(value) else ""

    async def _persist(self, name: str, counter: int) -> None:
        assert self._op is not None
        params = (name, counter, None)
        await self.writer.submit(self._op, (params,), {"name": name})
