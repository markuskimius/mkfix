"""FIX log file parser and replay engine.

A log is replayed *as the session*: the tags the session owns — BeginString,
BodyLength, CheckSum, MsgSeqNum, the CompIDs and SendingTime — are the
session's, TransactTime is the send time, and the tags that described the
log's own sequence space (PossDup, OrigSendingTime, LastMsgSeqNumProcessed)
are dropped. Every other tag goes out as logged, routing tags (50, 57, 115,
116, 128, 129, 142–145) and repeating groups included. Admin messages are
never replayed; the log's timestamps serve only to pace the replay.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Callable, Awaitable, NamedTuple, TYPE_CHECKING

from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.message import FixMessage, _fix_timestamp, parse_fix
from mkfix.fix.session import ADMIN_MSG_TYPES

if TYPE_CHECKING:
    from mkfix.fix.session import FixSession

# The session's own: sendprep stamps them, so a logged value must not win.
SESSION_TAGS = frozenset({"8", "9", "10", "34", "49", "56", "52"})
# The log's sequence space, meaningless on the replaying session.
DROPPED_TAGS = frozenset({"43", "122", "369"})
RESTAMPED_BODY_TAGS = frozenset({"60"})

ARROW = "→"
EXAMPLE_PREFIX = "example:"
EXAMPLES = Path(__file__).parent / "replay_examples"


class ReplayMessage(NamedTuple):
    timestamp: datetime | None
    raw: str
    msg_type: str
    sender: str
    target: str


_QF_TS = re.compile(r"^(\d{8}-\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*:\s*(.+)$")
_ISO_TS = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*[|:]\s*(.+)$"
)


def parse_log_file(path: str | Path) -> list[ReplayMessage]:
    """Parse a FIX log file, auto-detecting format.

    Supports: raw SOH-delimited, pipe-delimited, QuickFIX log, ISO-timestamped.
    Lines starting with ``#`` are comments; admin messages are left out.
    """
    text = Path(path).read_text(encoding="latin-1")
    messages: list[ReplayMessage] = []

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        ts, raw = _extract_timestamp_and_raw(line)
        if not raw or ("8=FIX" not in raw and "35=" not in raw):
            continue

        msg = parse_fix(raw)
        msg_type = msg.get("35", "")
        if msg_type in ADMIN_MSG_TYPES:
            continue

        if ts is None:
            ts = _parse_fix_timestamp(msg.get("52", "")) or _parse_fix_timestamp(msg.get("60", ""))

        messages.append(ReplayMessage(
            timestamp=ts, raw=raw, msg_type=msg_type,
            sender=msg.get("49", ""), target=msg.get("56", ""),
        ))

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
    if not ts:
        return None
    for fmt in ("%Y%m%d-%H:%M:%S.%f", "%Y%m%d-%H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def _stamp(ts: datetime | None) -> str:
    return ts.strftime("%Y%m%d-%H:%M:%S.") + f"{ts.microsecond // 1000:03d}" if ts else ""


# ── What is in a file ─────────────────────────────────────────────────

def direction_key(sender: str, target: str) -> str:
    """The value a direction goes by: ``SENDER→TARGET``, blank for messages
    carrying no CompIDs."""
    return f"{sender}{ARROW}{target}" if sender or target else ""


def summarize(messages: list[ReplayMessage]) -> dict[str, Any]:
    """What a log holds: its CompID pairs in order of first appearance, its
    message types with counts and names, its time span."""
    pairs: dict[tuple[str, str], int] = {}
    types: dict[str, int] = {}
    stamped = [m.timestamp for m in messages if m.timestamp]
    for m in messages:
        pairs[(m.sender, m.target)] = pairs.get((m.sender, m.target), 0) + 1
        types[m.msg_type] = types.get(m.msg_type, 0) + 1
    dictionary = _dictionary_for(messages)
    return {
        "pairs": [{"sender": s, "target": t, "count": n} for (s, t), n in pairs.items()],
        "types": dict(sorted(types.items())),
        "names": {code: dictionary.msg_type_name(code) for code in types},
        "first": _stamp(min(stamped)) if stamped else "",
        "last": _stamp(max(stamped)) if stamped else "",
        "count": len(messages),
    }


def _dictionary_for(messages: list[ReplayMessage]) -> FixDictionary:
    for m in messages:
        begin = m.raw.split("|", 1)[0].split("\x01", 1)[0]
        if begin.startswith("8=") and not begin.startswith("8=FIXT"):
            try:
                return FixDictionary(begin[2:])
            except Exception:  # noqa: BLE001 — an unknown version names nothing
                break
    return FixDictionary("FIX.4.4")


def describe_pairs(summary: dict[str, Any]) -> str:
    return ", ".join(
        f"{direction_key(p['sender'], p['target']) or '(no CompIDs)'} {p['count']}"
        for p in summary.get("pairs", []))


def _time_of_day(value: str) -> dtime | None:
    value = value.strip()
    if not value:
        return None
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Bad time of day {value!r}: use HH:MM or HH:MM:SS")


def select_messages(messages: list[ReplayMessage], direction: str = "", msg_filter: str = "",
                    time_from: str = "", time_to: str = "") -> list[ReplayMessage]:
    """The messages one run sends: those of a direction (blank = every one),
    of the listed types (blank = every type), stamped inside the window —
    an unstamped message is inside every window."""
    types = {t.strip() for t in msg_filter.split(",") if t.strip()}
    start, end = _time_of_day(time_from), _time_of_day(time_to)
    chosen = []
    for m in messages:
        if direction and direction_key(m.sender, m.target) != direction:
            continue
        if types and m.msg_type not in types:
            continue
        if m.timestamp and start and m.timestamp.time() < start:
            continue
        if m.timestamp and end and m.timestamp.time() > end:
            continue
        chosen.append(m)
    return chosen


# ── Bundled examples ──────────────────────────────────────────────────

def resolve_path(spec: str) -> Path:
    """A job's file: a path on this machine, or ``example:<name>`` naming a
    bundled log."""
    spec = spec.strip()
    if spec.startswith(EXAMPLE_PREFIX):
        name = Path(spec[len(EXAMPLE_PREFIX):].strip()).name
        path = EXAMPLES / f"{name}.log"
        if not path.is_file():
            raise ValueError(f"No bundled example named {name!r}")
        return path
    if not spec:
        raise ValueError("Give a file path or pick an example")
    path = Path(spec).expanduser()
    if not path.is_file():
        raise ValueError(f"No such file: {spec}")
    return path


def examples() -> list[dict[str, str]]:
    """The bundled logs: name, title (the first comment line) and the rest
    of the comment header."""
    found = []
    for path in sorted(EXAMPLES.glob("*.log")):
        header = []
        for line in path.read_text(encoding="latin-1").splitlines():
            if not line.startswith("#"):
                break
            header.append(line.lstrip("#").strip())
        found.append({
            "name": path.stem,
            "title": header[0] if header else path.stem,
            "description": "\n".join(header[1:]).strip(),
        })
    return found


# ── Sending ───────────────────────────────────────────────────────────

def prepare_for_send(msg: FixMessage, dictionary: FixDictionary, now: str) -> FixMessage:
    """A logged message as the session will send it: the session's own tags
    left for sendprep to stamp, the sequence-space tags gone, TransactTime
    restamped, and the body carried as ordered pairs on ``extra`` so a
    repeating group survives the trip (``fields`` is a dict)."""
    pairs = msg._pairs if msg._pairs is not None else list(msg.fields.items())
    fields: dict[str, str] = {}
    body: list[tuple[str, str]] = []
    for tag, value in pairs:
        if tag in SESSION_TAGS or tag in DROPPED_TAGS:
            continue
        if dictionary.is_header(tag) or dictionary.is_trailer(tag):
            fields[tag] = value
        else:
            body.append((tag, now if tag in RESTAMPED_BODY_TAGS else value))
    out = FixMessage(fields)
    out.extra = body
    return out


ProgressCallback = Callable[[int, int, str, str], Awaitable[None]]


class ReplayTask:
    """Runs a FIX log replay as an asyncio task."""

    def __init__(
        self,
        job_id: int,
        session: FixSession,
        messages: list[ReplayMessage],
        speed: float = 1.0,
        max_gap: float = 30.0,
        on_progress: ProgressCallback | None = None,
    ):
        self.job_id = job_id
        self.session = session
        self.messages = messages
        self.speed = speed
        self.max_gap = max_gap
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

    @property
    def done(self) -> bool:
        return self._task is None or self._task.done()

    def delay_before(self, rm: ReplayMessage, prev_ts: datetime | None) -> float:
        if self.speed <= 0 or not prev_ts or not rm.timestamp or rm.timestamp <= prev_ts:
            return 0.0
        delay = (rm.timestamp - prev_ts).total_seconds() / self.speed
        return min(delay, self.max_gap) if self.max_gap > 0 else delay

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

                delay = self.delay_before(rm, prev_ts)
                if delay:
                    await asyncio.sleep(delay)
                    await self._paused.wait()      # a pause that landed during the sleep

                if self._stopped:
                    break

                if not self.session.is_active:
                    await self._report("error", "Session disconnected during replay")
                    return

                now = _fix_timestamp(self.session.factory.timestamp_precision)
                msg = prepare_for_send(parse_fix(rm.raw), self.session.dictionary, now)
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
