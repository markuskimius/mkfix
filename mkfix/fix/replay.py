"""FIX log file parser and replay engine."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Awaitable, NamedTuple, TYPE_CHECKING

from mkfix.fix.message import parse_fix, FixMessage, SOH

if TYPE_CHECKING:
    from mkfix.fix.session import FixSession


class ReplayMessage(NamedTuple):
    timestamp: datetime | None
    raw: str
    msg_type: str


_QF_TS = re.compile(r"^(\d{8}-\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*:\s*(.+)$")
_ISO_TS = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*[|:]\s*(.+)$"
)


def parse_log_file(path: str | Path) -> list[ReplayMessage]:
    """Parse a FIX log file, auto-detecting format.

    Supports: raw SOH-delimited, pipe-delimited, QuickFIX log, ISO-timestamped.
    """
    text = Path(path).read_text(encoding="latin-1")
    messages: list[ReplayMessage] = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        ts, raw = _extract_timestamp_and_raw(line)
        if not raw or ("8=FIX" not in raw and "35=" not in raw):
            continue

        normalized = raw.replace(SOH, "|")
        msg = parse_fix(normalized)
        msg_type = msg.get("35", "")

        if ts is None:
            sending_time = msg.get("52", "")
            if sending_time:
                ts = _parse_fix_timestamp(sending_time)

        messages.append(ReplayMessage(timestamp=ts, raw=normalized, msg_type=msg_type))

    return messages


def _extract_timestamp_and_raw(line: str) -> tuple[datetime | None, str]:
    m = _QF_TS.match(line)
    if m:
        return _parse_fix_timestamp(m.group(1)), m.group(2).strip()

    m = _ISO_TS.match(line)
    if m:
        ts_str = m.group(1).replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(ts_str, fmt), m.group(2).strip()
            except ValueError:
                continue

    return None, line


def _parse_fix_timestamp(ts: str) -> datetime | None:
    for fmt in ("%Y%m%d-%H:%M:%S.%f", "%Y%m%d-%H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


ProgressCallback = Callable[[int, int, str, str], Awaitable[None]]


class ReplayTask:
    """Runs a FIX log replay as an asyncio task."""

    def __init__(
        self,
        job_id: int,
        session: FixSession,
        messages: list[ReplayMessage],
        speed: float = 1.0,
        msg_filter: set[str] | None = None,
        on_progress: ProgressCallback | None = None,
    ):
        self.job_id = job_id
        self.session = session
        self.messages = messages
        self.speed = speed
        self.msg_filter = msg_filter
        self.on_progress = on_progress
        self._task: asyncio.Task | None = None
        self._paused = asyncio.Event()
        self._paused.set()
        self._stopped = False
        self.sent = 0
        self.total = len(messages)

    async def start(self) -> None:
        self._stopped = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        self._paused.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def pause(self) -> None:
        self._paused.clear()

    def resume(self) -> None:
        self._paused.set()

    async def _run(self) -> None:
        prev_ts: datetime | None = None

        try:
            await self._report("running", "")

            for rm in self.messages:
                if self._stopped:
                    break

                await self._paused.wait()
                if self._stopped:
                    break

                if self.msg_filter and rm.msg_type not in self.msg_filter:
                    continue

                if self.speed > 0 and prev_ts and rm.timestamp and prev_ts < rm.timestamp:
                    delay = (rm.timestamp - prev_ts).total_seconds() / self.speed
                    delay = min(delay, 30.0)
                    await asyncio.sleep(delay)

                if self._stopped:
                    break

                if not self.session.is_active:
                    await self._report("error", "Session disconnected during replay")
                    return

                msg = parse_fix(rm.raw)
                await self.session.send_message(msg)
                self.sent += 1
                prev_ts = rm.timestamp

                if self.sent % 100 == 0 or self.sent == self.total:
                    await self._report("running", "")

            if not self._stopped:
                await self._report("completed", "")

        except asyncio.CancelledError:
            await self._report("stopped", "Cancelled")
        except Exception as e:
            await self._report("error", str(e))

    async def _report(self, status: str, error: str) -> None:
        if self.on_progress:
            await self.on_progress(self.job_id, self.sent, status, error)
