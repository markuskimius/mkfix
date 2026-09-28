"""Macros, their runs and their macros' progress, kept in the database.

`MacroManager` is what `fix_cmd` talks to: it saves macros, arms and
stops them through a `MacroRunner`, and mirrors what the runner does into
four tables the panes watch. The runner stays the authority on what is
running; the rows are its shadow, written in order by one worker so a
macro's progress never lands out of sequence.

The rows survive the process, the macros do not: at startup whatever the
last process left live is marked `interrupted`, and nothing is played again:
a restart stops every macro, the ones that wait for orders as much as the
ones that send them.

Every row carries its macro's `side` (client | market): the panes come in
pairs, one per side, and each filters on it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mkfix.fix.message import _fix_timestamp

from . import vocab
from .check import check, errors
from .instance import LIVE, Instance
from .nodes import Diagnostic
from .recorder import EndToEndRecorder, Recorder
from .runner import Run, MacroError, MacroRunner

if TYPE_CHECKING:
    from mkfix.fix.engine import FixEngine

log = logging.getLogger(__name__)

# What may name a macro. Runs, the log and an order's Macro column name it by
# name alone, and Play… joins the names it was given with commas, so a comma
# is out; a colon (the recorder's `Market 2026-09-22 14:30:15`) is in, and
# Export writes it as a dot in the file name.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:-]*")

EXAMPLES = Path(__file__).parent / "examples"

# The pair of sessions the sending examples run over: this engine talking to
# itself, so both sides of an order are on the screen.
LOOPBACK = {"market": "LOOP-MKT", "client": "LOOP-CLI"}
LOOPBACK_PORT = 9880
# FIX 4.4, the first version with every message the examples send — the
# QuoteResponse of an RFQ's Hit, Counter and Pass. Through 0.73 the pair
# was made at the column's default, FIX 4.2; `setup_loopback` moves such a
# pair on when it finds it stopped.
LOOPBACK_VERSION = "FIX.4.4"
_LOOPBACK_DESCRIPTION = "Loopback for macro examples"
# The two examples that are one demonstration, and the order to start them in.
# The loopback tour: what waits is armed, what sends is run. The
# derivatives desk, the venue, the RFQ desk and the client's checkers wait;
# loopback-client, the RFQ taker and the futures roll, and the market's
# desks, quote stream and RFQ subscriber, send (the desks keep waiting for
# hand-sent IOIs and allocations after). The derivatives desk is offered
# orders before the venue, which takes every order on its session.
TOUR = {"market": "loopback-venue", "client": "loopback-client"}
TOUR_ARMED = {"market": ("derivatives-desk", "loopback-venue", "rfq-desk"),
              "client": ("ioi-taker", "allocation-check", "quote-taker", "rfq-responder")}
TOUR_RUN = {"client": ("loopback-client", "rfq-taker", "futures-roll"),
            "market": ("ioi-desk", "allocation-desk", "quote-stream", "rfq-subscriber")}
# (ahead, behind): a run the tour keeps offered orders before another.
TOUR_AHEAD = ("derivatives-desk", "loopback-venue")
_LIVE_RUNS = ("armed", "paused")


def _diagnostic(d: Diagnostic) -> dict[str, Any]:
    return {"line": d.line, "col": d.col, "end": d.end, "message": d.message, "severity": d.severity}


def example_header(text: str) -> dict[str, str]:
    """The `# Title` / `# Shows:` … lines an example opens with."""
    out: dict[str, str] = {}
    key = ""
    for n, line in enumerate(text.splitlines()):
        if not line.startswith("#"):
            break
        body = line[1:].strip()
        if n == 0:
            out["title"] = body
        elif ":" in body and body.split(":", 1)[0] in ("Shows", "Needs", "Watch", "Outcome"):
            key, value = body.split(":", 1)
            key = key.lower()
            out[key] = value.strip()
        elif key and body:
            out[key] += " " + body
    return out


def _a(side: str) -> str:
    """`a client`, `a market`, `an end-to-end`."""
    return ("an " if side[:1] in "aeiou" else "a ") + side


class MacroManager:
    def __init__(self, engine: FixEngine, runner: MacroRunner | None = None) -> None:
        self.engine = engine
        self.runner = runner or MacroRunner(engine)
        self.runner.on_change = self._instance_changed
        self.runner.on_log = self._logged
        self.runner.on_run_finished = self._run_finished
        self._ops: dict[str, Any] = {}
        self._run_rows: dict[Run, int] = {}
        self._priorities: dict[int, int] = {}
        self.recorders: dict[str, Recorder] = {}       # side -> the recording under way
        self._runs_by_row: dict[int, Run] = {}
        self._tagged: set[tuple[str, int]] = set()
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._closing = False

    # -- life ----------------------------------------------------------------------------

    async def start(self) -> None:
        self._compile_ops()
        conn = self.engine.db.write_conn
        for sql in (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_fix_macros_name ON fix_macros(name)",
            # 0.64: a run may own an order and an IOI of the same row id, so
            # the subject joins the key; the old index goes.
            "DROP INDEX IF EXISTS idx_fix_macro_orders_order",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_fix_macro_orders_subject "
            "ON fix_macro_orders(run_id, subject, order_row)",
            "CREATE INDEX IF NOT EXISTS idx_fix_macro_log_run ON fix_macro_log(run_id)",
        ):
            await (await conn.execute(sql)).close()
        await conn.commit()
        await self._backfill_sides()
        self._queue = asyncio.Queue()
        self._worker = asyncio.get_running_loop().create_task(self._drain())
        await self._recover()

    async def _backfill_sides(self) -> None:
        """Rows from before 0.49 have no side: a macro's is what its blocks
        say, and a run, macro row or log line takes its macro's."""
        for row in await self._fetch("SELECT name, source FROM fix_macros WHERE side = ''"):
            macro, _ = check(row["source"])
            await self._write("set_macro_side", (macro.side or "market", None, row["name"]))
        conn = self.engine.db.write_conn
        for table in ("fix_macro_runs", "fix_macro_orders", "fix_macro_log"):
            await (await conn.execute(
                f"UPDATE {table} SET side = coalesce((SELECT s.side FROM fix_macros s "
                f"WHERE s.name = {table}.macro), 'market') WHERE side = ''")).close()
        await conn.commit()

    async def stop(self) -> None:
        """Shut down without a word in the tables: the runs stay `armed`, so
        the next start marks what was live `interrupted`."""
        self._closing = True
        for recorder in self.recorders.values():
            await recorder.stop()
        self.recorders.clear()
        self.runner.stop_all()
        await self.runner.settle()
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    async def flush(self) -> None:
        """Every write the runner has asked for is in the database."""
        await self.runner.settle()
        if self._queue is not None:
            await self._queue.join()

    async def _recover(self) -> None:
        """A restart stops every macro. What the last process left live is
        marked `interrupted` — the runs, and each order's macro — and nothing
        is played again: a run is something a person started, and the server
        coming back is not that person. (Through 0.50 a macro that only
        waited for orders was armed again here. With Play and Stop on the
        blotters that read as a run nobody had asked for: ▶ lit after a
        restart that, to everyone watching, had plainly stopped it.)"""
        rows = await self._fetch(f"SELECT * FROM fix_macro_runs WHERE status IN {_LIVE_RUNS!r} ORDER BY id")
        now = _fix_timestamp()
        for row in rows:
            await self._write("update_run", ("interrupted", row["verdict"], row["orders"], 0, row["passed"],
                                             row["failed"], now, None, row["id"]))
            await self._write("set_priority", (0, None, row["id"]))
        stale = await self._fetch(f"SELECT * FROM fix_macro_orders WHERE status IN {LIVE!r}")
        for inst in stale:
            await self._write("finish_instance", ("interrupted", "the server stopped", now, None, inst["id"]))
        if rows:
            log.warning("%d macro run(s) were live when the server last stopped: marked interrupted, not played again",
                        len(rows))

    # -- macros ---------------------------------------------------------------------------

    async def known(self) -> dict[str, Any]:
        templates: dict[str, set[str]] = {}
        for row in await self._fetch("SELECT scope, name FROM fix_templates"):
            templates.setdefault(row["scope"], set()).add(row["name"])
        sessions = {r["session_id"] for r in await self._fetch("SELECT session_id FROM fix_sessions")}
        sessions |= set(self.engine.sessions)
        from .check import instrument_payload
        instruments = {r["name"]: instrument_payload(r) for r in await self._fetch("SELECT * FROM fix_instruments")}
        return {"templates": templates, "sessions": sessions, "instruments": instruments}

    async def check(self, source: str, side: str = "") -> dict[str, Any]:
        """``side`` is the pane the macro is edited in: a block of the other
        side is a problem. Without it the macro's first block decides."""
        side = self._side(side)
        macro, diagnostics = check(source, side=side or None, **await self.known())
        named = {b.session for b in macro.blocks if b.kind == vocab.CLIENT and b.signal is None}
        return {"side": side or macro.side,
                "diagnostics": [_diagnostic(d) for d in diagnostics], "errors": len(errors(diagnostics)),
                "needs_session": macro.needs_session,
                # An end-to-end macro has a session a side: the second is asked for the same way.
                "needs_market_session": macro.needs_market_session,
                # Whether ▶ is Run… (it has a `run` block: it sends, on either
                # side) or Arm… (it only waits for what arrives).
                "sends": macro.sends,
                # What Run… opens on: the one session every `run` block names.
                "session": next(iter(named)) if len(named) == 1 and None not in named else "",
                "blocks": [{"kind": b.kind, "line": b.line, "session": b.session or ""} for b in macro.blocks]}

    @staticmethod
    def _side(side: Any) -> str:
        side = str(side or "").strip().lower()
        if side and side not in vocab.MACRO_KINDS:
            raise ValueError(f"A macro is a client, a market or an end-to-end macro, not {side!r}")
        return side

    async def save(self, name: str, source: str, side: str = "") -> dict[str, Any]:
        """Keep ``source`` under ``name`` — with its problems, if it has any:
        a draft is worth keeping, and only arming needs a clean macro."""
        name = " ".join(str(name).split())
        if not name:
            raise ValueError("A macro needs a name")
        if not NAME.fullmatch(name):
            raise ValueError(f"{name!r} cannot name a macro: letters, digits, spaces, `.`, `_`, `-` and `:` only, "
                             "starting with a letter or digit")
        result = await self.check(source, side)
        side = result["side"] = result["side"] or "market"
        # One namespace for both sides: a run, an order's Macro column and
        # the log name a macro by name alone.
        held = await self._fetch("SELECT side FROM fix_macros WHERE name = ?", (name,))
        if held and held[0]["side"] not in ("", side):
            raise ValueError(f"{_a(held[0]['side']).capitalize()} macro is already called {name!r}: choose another name")
        now = _fix_timestamp()
        await self._write("upsert_macro", (name, side, int(result["needs_session"]), source, result["errors"], now, now, None))
        return result

    async def delete(self, name: str) -> None:
        await self.delete_many([name])

    async def delete_many(self, names: list[str]) -> dict[str, Any]:
        """Delete macros, whole or not at all: a name that is not a macro, or
        one with a live run, refuses the list before anything is written —
        what the editor's Delete of a selection expects, like Pause… and
        Stop… over their lists of runs."""
        wanted = [n for n in dict.fromkeys(" ".join(str(n).split()) for n in names) if n]
        if not wanted:
            raise ValueError("Name a macro to delete")
        rows = await self._fetch(f"SELECT name FROM fix_macros WHERE name IN ({','.join('?' * len(wanted))})", tuple(wanted))
        have = {r["name"] for r in rows}
        for name in wanted:
            if name not in have:
                raise ValueError(f"No macro named {name!r}")
        busy = [(name, len(self.live_runs(name))) for name in wanted]
        busy = [(name, live) for name, live in busy if live]
        if len(busy) == 1:
            name, live = busy[0]
            raise ValueError(f"{name!r} has {live} live run{'s' if live > 1 else ''}: stop {'them' if live > 1 else 'it'} first")
        if busy:
            raise ValueError(f"{', '.join(repr(name) for name, _ in busy)} have live runs: stop them first")
        for name in wanted:
            await self._write("delete_macro", (name,))
        return {"deleted": len(wanted)}

    async def load(self, name: str) -> dict[str, Any]:
        rows = await self._fetch("SELECT * FROM fix_macros WHERE name = ?", (name,))
        if not rows:
            raise ValueError(f"No macro named {name!r}")
        return rows[0]

    def examples(self, side: str = "") -> list[dict[str, str]]:
        side = self._side(side)
        found = [self.example(p.stem) for p in sorted(EXAMPLES.glob("*.macro"))]
        return [{k: v for k, v in e.items() if k != "source"} for e in found if side in ("", e["side"])]

    def example(self, name: str) -> dict[str, str]:
        path = EXAMPLES / f"{Path(name).name}.macro"
        if not path.is_file():
            raise ValueError(f"No example named {name!r}")
        text = path.read_text(encoding="utf-8")
        return {"name": path.stem, "side": check(text)[0].side, "source": text, **example_header(text)}

    # -- runs --------------------------------------------------------------------------------

    async def arm(self, name: str, *, side: str = "", session: str = "", seed: int | None = None,
                  speed: float = 1.0, market_session: str = "") -> dict[str, Any]:
        """Start a run of the saved macro. ``side`` is what the caller takes
        it for — `arm_macro` arms market macros, `run_macro` runs
        client ones — and the macro must be that. An end-to-end macro has
        two sessions: ``session`` for its client blocks, ``market_session``
        for its market ones.

        Any number of runs may be live at once, of one macro as of many.
        The one thing refused is a second run that could never be given an
        order: the same macro, waiting only, on the same sessions — the
        first would take every order it matched."""
        row, macro = await self._armable(name, side, session, market_session)
        # Armed but not started: the run's row is written and known to the
        # hooks before any macro can act. A macro may fail in its first
        # instant — `new` on a session that has just dropped — ending a run
        # that only sends; started first, all of that went unrecorded and
        # left a row saying `armed` that Stop could not stop.
        run = self.runner.arm(macro, session=session or None, seed=seed or None, speed=float(speed or 1.0),
                              start=False, market_session=market_session or None)
        try:
            await self._write("insert_run", (name, macro.side, row.get("_mkio_version") or 0, session or "",
                                             market_session or "", run.seed, run.speed, "armed", _fix_timestamp(),
                                             None))
            run_id = (await self._fetch("SELECT MAX(id) AS id FROM fix_macro_runs"))[0]["id"]
        except BaseException:
            self.runner.stop(run)
            raise
        self._run_rows[run] = run_id
        self._runs_by_row[run_id] = run
        await self._renumber()
        self.runner.start(run)
        return {"run_id": run_id, "seed": run.seed, "side": macro.side}

    async def _armable(self, name: str, side: str = "", session: str = "",
                       market_session: str = "") -> tuple[dict[str, Any], Any]:
        """The saved macro and its parsed form, or why it cannot be started."""
        row = await self.load(name)
        macro, diagnostics = check(row["source"], side=row.get("side") or None, **await self.known())
        macro.name = name
        bad = errors(diagnostics)
        if bad:
            raise ValueError(f"{name!r} has {len(bad)} problem(s); the first: {bad[0]}")
        side = self._side(side)
        if side and macro.side != side:
            raise ValueError(f"{name!r} is {_a(macro.side)} macro: start it from "
                             f"{vocab.KIND_NAMES[macro.side]} Macros")
        for chosen in (session, market_session):
            if chosen and chosen not in self.engine.sessions:
                raise ValueError(f"No session named {chosen!r}")
        if market_session and macro.side != vocab.E2E:
            raise ValueError(f"{name!r} is {_a(macro.side)} macro: it has one session")
        if not macro.sends and any(
                (r.session, r.market_session) == (session or None, market_session or None)
                for r in self.live_runs(name)):
            where = " and ".join(s for s in (session, market_session) if s) or "every session"
            raise ValueError(f"{name!r} is already armed on {where}: a second run there "
                             "would never be given an order")
        return row, macro

    async def _renumber(self) -> None:
        """`priority` is a waiting run's place in line on its side, 1 first;
        0 for a run that only sends, and for one that is over."""
        places: dict[int, int] = {}
        for side in vocab.MACRO_KINDS:
            for n, run in enumerate((r for r in self.runner.offered(side) if r in self._run_rows), 1):
                places[self._run_rows[run]] = n
        for run, row in self._run_rows.items():
            place = places.get(row, 0)
            if self._priorities.get(row, 0) != place:
                self._priorities[row] = place
                await self._write("set_priority", (place, None, row))

    async def move_run(self, run_id: Any, direction: str) -> None:
        """Offer orders to this run one place earlier (`up`) or later (`down`)."""
        if direction not in ("up", "down"):
            raise ValueError("direction is up or down")
        run = self._run(run_id)
        if not run.waits:
            raise ValueError(f"Run {run_id} only sends orders: it has no place in line")
        if not self.runner.move(run, direction == "up"):
            raise ValueError(f"Run {run_id} is already {'first' if direction == 'up' else 'last'}")
        await self._renumber()

    async def stop_macro(self, name: str) -> dict[str, Any]:
        """Stop every live run of one macro."""
        runs = self.live_runs(name)
        for run in runs:
            await self.stop_run(self._run_rows[run])
        return {"stopped": len(runs)}

    async def pause_macro(self, name: str) -> dict[str, Any]:
        """Pause every playing run of one macro (the editor's ⏸)."""
        runs = [r for r in self.live_runs(name) if not r.paused]
        for run in runs:
            await self.pause_run(self._run_rows[run])
        return {"paused": len(runs)}

    async def resume_macro(self, name: str) -> dict[str, Any]:
        """Resume every paused run of one macro (⏸ again, once all are paused)."""
        runs = [r for r in self.live_runs(name) if r.paused]
        for run in runs:
            await self.resume_run(self._run_rows[run])
        return {"resumed": len(runs)}

    def _run(self, run_id: Any) -> Run:
        run = self._runs_by_row.get(int(run_id))
        if run is None or run.status != "armed":
            raise ValueError(f"Run {run_id} is not live")
        return run

    async def stop_run(self, run_id: Any) -> None:
        """Stop a run — and, whatever the runner thinks of it, leave its row
        saying something true: Stop is what a person reaches for when a row
        looks stuck, so it must never answer "not live" and change nothing."""
        run = self._runs_by_row.get(int(run_id))
        if run is None:
            # A row this process never armed (left by an earlier one).
            rows = await self._fetch("SELECT * FROM fix_macro_runs WHERE id = ?", (int(run_id),))
            if not rows:
                raise ValueError(f"No run {run_id}")
            if rows[0]["status"] in _LIVE_RUNS:
                row = rows[0]
                await self._write("update_run", ("interrupted", row["verdict"], row["orders"], 0, row["passed"],
                                                 row["failed"], _fix_timestamp(), None, row["id"]))
            return
        if run.status == "armed":
            self.runner.stop(run)
        await self.flush()
        await self._write_run(run, run.status, ended=True)
        await self._renumber()

    async def pause_run(self, run_id: Any) -> None:
        run = self._run(run_id)
        self.runner.pause(run)
        await self._write_run(run, "paused")

    async def resume_run(self, run_id: Any) -> None:
        run = self._run(run_id)
        self.runner.resume(run)
        await self._write_run(run, "armed")

    async def detach(self, order_row: Any, subject: str = vocab.ORDER) -> None:
        if not self.runner.detach(int(order_row), subject or vocab.ORDER):
            raise ValueError(f"No live macro owns that {subject or 'order'}")
        await self.flush()

    async def setup_loopback(self, port: Any = None, start: bool = True) -> dict[str, Any]:
        """Create the two loopback sessions if they are not there — an acceptor
        and an initiator facing it on localhost, on FIX 4.4 — and start them.
        A pair made before 0.74, on FIX 4.2, is moved to 4.4 while it is
        stopped (`upgraded`); one running is left as it is."""
        port = int(port or LOOPBACK_PORT)
        existing = {r["session_id"]: r for r in await self._fetch(
            "SELECT * FROM fix_sessions WHERE session_id IN (?, ?)", tuple(LOOPBACK.values()))}
        upgraded = []
        for session_id, row in existing.items():
            session = self.engine.sessions.get(session_id)
            stopped = session is None or session.status in ("DOWN", "ERROR")
            if row["fix_version"] == "FIX.4.2" and row["description"] == _LOOPBACK_DESCRIPTION and stopped:
                await self.engine.writer.submit(self._ops["loopback_version"], ((LOOPBACK_VERSION, None, session_id),),
                                                {"session_id": session_id})
                upgraded.append(session_id)
        created = []
        for role, session_id in LOOPBACK.items():
            if session_id in existing:
                continue
            sender, target = ("LOOPMKT", "LOOPCLI") if role == "market" else ("LOOPCLI", "LOOPMKT")
            host = "" if role == "market" else "127.0.0.1"
            await self.engine.writer.submit(
                (*self._ops["insert_session"], *self._ops["insert_session_state"]),
                ((session_id, LOOPBACK_VERSION, sender, target, host, port, _LOOPBACK_DESCRIPTION, None),
                 (session_id, None)),
                {"session_id": session_id})
            created.append(session_id)
        if existing and not created:
            port = next(iter(existing.values()))["port"]
        started = []
        if start:
            for session_id in LOOPBACK.values():           # the acceptor first
                await self.engine.reload_session(session_id)
                session = self.engine.sessions[session_id]
                if session.status in ("DOWN", "ERROR"):
                    await self.engine.start_session(session_id)
                    started.append(session_id)
        return {"sessions": LOOPBACK, "port": port, "created": created, "started": started, "upgraded": upgraded}

    async def run_tour(self, port: Any = None, timeout: float = 10.0) -> dict[str, Any]:
        """The loopback tour in one step: the two sessions; the venue armed on
        one and the client run on the other; since 0.65 the families too —
        ioi-taker and allocation-check armed on the client side, ioi-desk and
        allocation-desk run on the venue; since 0.75.1 futures-roll run
        against derivatives-desk, which is offered orders before the venue. The examples are saved under their
        own names unless macros of those names are there already — yours are
        left as they are — and whatever is already armed is left armed."""
        sessions = await self.setup_loopback(port)
        client = self.engine.sessions[LOOPBACK["client"]]
        deadline = asyncio.get_running_loop().time() + timeout
        while not client.is_active:
            if asyncio.get_running_loop().time() > deadline:
                raise ValueError(f"{LOOPBACK['client']} did not log on within {timeout:g} s: is port "
                                 f"{sessions['port']} free?")
            await asyncio.sleep(0.05)
        for side in ("market", "client"):
            for name in (*TOUR_ARMED[side], *TOUR_RUN.get(side, ())):
                if not await self._fetch("SELECT 1 FROM fix_macros WHERE name = ?", (name,)):
                    await self.save(name, self.example(name)["source"], side)
        runs: dict[str, int] = {}
        for side, names in TOUR_ARMED.items():
            session = LOOPBACK[side] if side == "market" else ""
            for name in names:
                armed = [r for r in self.live_runs(name) if r.session == (session or None)]
                runs[name] = self._run_rows[armed[0]] if armed \
                    else (await self.arm(name, side=side, session=session))["run_id"]
        ahead, behind = (self._runs_by_row.get(int(runs[name])) for name in TOUR_AHEAD)
        if ahead is not None and behind is not None:
            await self._put_ahead(ahead, behind)
        for side, names in TOUR_RUN.items():
            for name in names:
                runs[name] = (await self.arm(name, side=side, session=LOOPBACK[side]))["run_id"]
        return {"sessions": sessions["sessions"], "venue_run": runs[TOUR["market"]],
                "client_run": runs[TOUR["client"]], "runs": runs}

    async def _put_ahead(self, ahead: Run, behind: Run) -> None:
        """Offer orders to ``ahead`` before ``behind``: a venue armed by an
        earlier tour would otherwise take what the desk is for."""
        line = self.runner.offered(ahead.side)
        moved = False
        while ahead in line and behind in line and line.index(ahead) > line.index(behind):
            self.runner.move(ahead, True)
            line, moved = self.runner.offered(ahead.side), True
        if moved:
            await self._renumber()

    # -- a side's runs together: the order blotters' Play, Pause and Stop ----------------------

    def _side_runs(self, side: str, run: Any = "", paused: bool | None = None) -> list[Run]:
        """The live runs of ``side`` — all of them, or the ones numbered in
        ``run`` (one id, or several joined by commas: what the dialogs'
        checklist submits) — that are paused, playing (``paused=False``) or
        either. Every number asked for must be such a run: a list is acted
        on whole or not at all."""
        side = self._side(side)
        if not side:
            raise ValueError("Say which side: client or market")
        runs = [r for r in self.live_runs() if r.side == side and paused in (None, r.paused)]
        wanted = [part.strip() for part in str(run or "").split(",") if part.strip()]
        if not wanted:
            return runs
        by_id = {self._run_rows[r]: r for r in runs}
        state = "" if paused is None else " that is " + ("paused" if paused else "playing")
        chosen = []
        for part in dict.fromkeys(wanted):
            if not part.isdigit() or int(part) not in by_id:
                raise ValueError(f"Run {part} is not a live {side} run{state}")
            chosen.append(by_id[int(part)])
        return chosen

    async def play(self, side: str, what: str, *, session: str = "", seed: int | None = None,
                   speed: float = 1.0) -> dict[str, Any]:
        """Play…'s answer, one choice or several joined by commas (what the
        dialog's checklist submits): `macro:NAME` — `needs:NAME` for one the
        list marked as needing a session — starts a run of it, `resume:ID`
        or `resume:all` carries on what was paused. Everything asked for is
        checked before anything is started: a list is played whole or not
        at all. The session, speed and seed are the same for every macro
        started; a seed left blank is each macro's own, or random."""
        side = self._side(side)
        if not side:
            raise ValueError("Say which side: client or market")
        names: list[str] = []
        resume: list[Run] = []
        for choice in dict.fromkeys(part.strip() for part in str(what or "").split(",") if part.strip()):
            kind, _, value = choice.partition(":")
            if kind == "resume" and value:
                resume += [r for r in self._side_runs(side, "" if value == "all" else value, paused=True) if r not in resume]
            elif kind in ("macro", "needs") and value:
                names.append(value)
            else:
                raise ValueError("Choose a macro to play, or a paused run to resume")
        if not names and not resume and "resume:all" not in str(what or ""):
            raise ValueError("Choose a macro to play, or a paused run to resume")
        for name in names:
            _, macro = await self._armable(name, side, session)
            try:
                self.runner.validate(macro, session=session or None, speed=float(speed or 1.0))
            except MacroError as e:
                raise ValueError(f"{name}: {e}") from None
        for run in resume:
            await self.resume_run(self._run_rows[run])
        started = [await self.arm(name, side=side, session=session, seed=seed, speed=speed) for name in names]
        result: dict[str, Any] = {"resumed": len(resume), "started": len(started), "run_ids": [s["run_id"] for s in started]}
        if len(started) == 1:
            result.update(started[0])           # one macro: what Arm… and Run… answer
        return result

    async def pause_runs(self, side: str, run: Any = "") -> dict[str, Any]:
        runs = self._side_runs(side, run, paused=False)
        for r in runs:
            await self.pause_run(self._run_rows[r])
        return {"paused": len(runs)}

    async def stop_runs(self, side: str, run: Any = "") -> dict[str, Any]:
        runs = self._side_runs(side, run)
        for r in runs:
            await self.stop_run(self._run_rows[r])
        return {"stopped": len(runs)}

    # -- recording ---------------------------------------------------------------------------

    def record_start(self, side: str, session: str = "", market_session: str = "") -> dict[str, Any]:
        """Begin recording what is done by hand on one side's orders — every
        session's, or one's. One recording a side at a time; an end-to-end
        recording is of both sides at once, and may go on beside one of
        either."""
        side = self._side(side)
        if not side:
            raise ValueError("Record the client side, the market side, or both end to end")
        if side in self.recorders:
            raise ValueError(f"{_a(side).capitalize()} recording is already under way: stop it first")
        for chosen in (session, market_session):
            if chosen and chosen not in self.engine.sessions:
                raise ValueError(f"No session named {chosen!r}")
        if market_session and side != vocab.E2E:
            raise ValueError(f"{_a(side).capitalize()} recording is of one side: it has one session")
        self.recorders[side] = EndToEndRecorder(self.engine, session, market_session) if side == vocab.E2E \
            else Recorder(self.engine, side, session)
        return self.recorders[side].status()

    async def record_stop(self, side: str, name: str = "recorded", save: bool = False,
                          delays: bool = False) -> dict[str, Any]:
        """Stop recording and write the macro. With ``save`` it is also kept
        under ``name`` — refused, the recording still under way, if the name
        is taken: what was recorded must not be lost to a clash."""
        side = self._side(side)
        if side not in self.recorders:
            raise ValueError(f"No {side} recording is under way")
        name = " ".join(str(name).split()) or "recorded"
        if save and await self._fetch("SELECT 1 FROM fix_macros WHERE name = ?", (name,)):
            raise ValueError(f"A macro is already called {name!r}: choose another name. Still recording")
        result = await self.recorders.pop(side).stop(name, delays=delays)
        result.update(name=name, side=side, saved=False)
        if save and result["orders"]:
            await self.save(name, result["source"], side)
            result["saved"] = True
        return result

    async def from_history(self, side: str, subject: str, rows: Any, name: str = "", save: bool = False,
                           delays: bool = False) -> dict[str, Any]:
        """The macro that would have played this side's part in what these
        rows have been through (`history.FromHistory`), from the messages
        kept. With ``save`` it is kept under ``name``: refused before
        anything is read if the name is taken, and if there is nothing to
        write — a macro with no block is not worth a name."""
        from .history import FromHistory
        side = self._side(side)
        name = " ".join(str(name).split())
        ids = [int(r) for r in (rows if isinstance(rows, (list, tuple)) else str(rows or "").split(",")) if str(r).strip()]
        if not side and ids and subject in vocab.SUBJECT_TABLES:
            # The side that sent or received the first of them: the client
            # side sends orders and RFQs and receives the rest, the market side the other way round.
            held = await self._fetch(f"SELECT direction FROM {vocab.SUBJECT_TABLES[subject]} WHERE id = ?", (ids[0],))
            sent = bool(held) and held[0]["direction"] == "TX"
            side = "client" if (subject in vocab.CLIENT_SENDS) == sent else "market"
        if save:
            if not NAME.fullmatch(name):
                raise ValueError(f"{name!r} cannot name a macro: letters, digits, spaces, `.`, `_`, `-` and `:` only, "
                                 "starting with a letter or digit")
            if await self._fetch("SELECT 1 FROM fix_macros WHERE name = ?", (name,)):
                raise ValueError(f"A macro is already called {name!r}: choose another name")
        await self.flush()
        result = await FromHistory(self.engine, side, delays=delays).write(subject, ids)
        result.update(name=name, side=side, saved=False)
        if save:
            if not result["orders"]:
                raise ValueError("Nothing to write a macro from: " + ("; ".join(result["left_out"]) or "no messages"))
            await self.save(name, result["source"], side)
            result["saved"] = True
        return result

    def status(self) -> dict[str, Any]:
        """Both sides' recordings in one answer, for the status bar's poll."""
        return {side: self.record_status(side) for side in vocab.MACRO_KINDS}

    def record_status(self, side: str) -> dict[str, Any]:
        recorder = self.recorders.get(self._side(side))
        return recorder.status() if recorder else {"recording": False, "side": side, "session": "", "orders": 0,
                                                   "actions": 0, "since": ""}

    async def report(self, run_id: Any, after: Any = 0) -> dict[str, Any]:
        """A run as the tables have it — its row, its orders' (IOIs',
        allocations') rows, and its log lines after row id ``after`` — for
        `mkfix run --wait`, which polls it."""
        run_id, after = int(run_id), int(after or 0)
        await self.flush()
        rows = await self._fetch("SELECT * FROM fix_macro_runs WHERE id = ?", (run_id,))
        if not rows:
            raise ValueError(f"No run {run_id}")
        # Whether its `run` blocks are still at it: a pass still to come, or
        # a macro that sent its own subject still live. A run that also
        # waits (`on` blocks) stays armed after that, with nothing sending.
        run = self._runs_by_row.get(run_id)
        sending = bool(run) and run.status == "armed" and (
            any(i.live for i in run.generators)
            or any(i.live for i in run.instances if i.block.kind == vocab.CLIENT))
        return {
            "run": rows[0],
            "sends": bool(run) and run.macro.sends,
            "sending": sending,
            "orders": await self._fetch(
                "SELECT subject, order_row, cl_ord_id, symbol, status, line, waiting_for, message, actions "
                "FROM fix_macro_orders WHERE run_id = ? ORDER BY id", (run_id,)),
            "log": await self._fetch(
                "SELECT id, order_row, cl_ord_id, timestamp, line, text FROM fix_macro_log "
                "WHERE run_id = ? AND id > ? ORDER BY id", (run_id, after)),
        }

    def live_runs(self, name: str | None = None) -> list[Run]:
        return [r for r in self.runner.runs
                if r.status == "armed" and r in self._run_rows and name in (None, r.macro.name)]

    # -- the runner's shadow -----------------------------------------------------------------

    def _instance_changed(self, instance: Instance) -> None:
        if self._closing or self._queue is None:
            return
        run_id = self._run_rows.get(instance.run)
        if run_id is None or instance.generator:
            return
        if instance.key is None:
            # No order yet. One that ended this way — its `new` refused —
            # must still be seen: a row under a key no order can have.
            if not instance.live:
                now = _fix_timestamp()
                self._queue.put_nowait(("upsert_instance", (
                    run_id, instance.kind, -1 - instance.index, instance.run.macro.name, instance.run.side,
                    instance.run.session_of(instance.block) or "", "", "",
                    "", instance.block.line, instance.status, instance.line, "", instance.message,
                    instance.actions, now, now, None)))
                self._queue.put_nowait(("run", instance.run))
            return
        row, now = instance.row, _fix_timestamp()
        if instance.owner_key not in self._tagged:
            self._tagged.add(instance.owner_key)
            label = f"{instance.run.macro.name} #{run_id}"
            self._queue.put_nowait((f"tag_{vocab.SUBJECT_TABLES[instance.kind]}", (label, None, instance.key)))
        self._queue.put_nowait(("upsert_instance", (
            run_id, instance.kind, instance.key, instance.run.macro.name, instance.run.side, row["session_id"],
            instance.subject_id, row.get("order_id", "") if instance.kind == vocab.ORDER else "",
            row.get("symbol", row.get("symbols", "")), instance.block.line, instance.status, instance.line,
            instance.waiting_for, instance.message, instance.actions, now, now, None)))
        if not instance.live:
            self._tagged.discard(instance.owner_key)
        self._queue.put_nowait(("run", instance.run))

    def _run_finished(self, run: Run) -> None:
        if not self._closing and self._queue is not None and run in self._run_rows:
            self._queue.put_nowait(("run_ended", run))

    def _logged(self, run: Run, instance: Instance | None, line: int, text: str) -> None:
        if self._closing or self._queue is None or run not in self._run_rows:
            return
        self._queue.put_nowait(("insert_log", (
            self._run_rows[run], instance.key if instance else None, run.macro.name, run.side,
            instance.subject_id if instance else "", _fix_timestamp(), line, text, None)))

    async def _drain(self) -> None:
        while True:
            op, params = await self._queue.get()
            try:
                if op == "run":
                    await self._write_run(params, "paused" if params.paused and params.status == "armed" else params.status,
                                          ended=params.status != "armed")
                elif op == "run_ended":
                    await self._write_run(params, params.status, ended=True)
                    await self._renumber()      # an end-to-end run that waited had a place in line
                else:
                    await self._write(op, params)
            except Exception:
                log.exception("macro row write failed: %s", op)
            finally:
                self._queue.task_done()

    async def _write_run(self, run: Run, status: str, ended: bool = False) -> None:
        counts = run.counts()
        live = sum(counts.get(s, 0) for s in LIVE)
        await self._write("update_run", (status, run.verdict, len(run.instances), live, counts.get("passed", 0),
                                         counts.get("failed", 0), _fix_timestamp() if ended else "", None,
                                         self._run_rows[run]))

    # -- database ----------------------------------------------------------------------------

    async def _fetch(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cursor = await self.engine.db.read_conn.execute(sql, params)
        rows = [dict(r) for r in await cursor.fetchall()]
        await cursor.close()
        return rows

    async def _write(self, op: str, params: tuple[Any, ...]) -> None:
        await self.engine.writer.submit(self._ops[op], (params,), {})

    def _compile_ops(self) -> None:
        from mkio.writer import CompiledOp

        def op(name: str, table: str, kind: str, sql: str, names: tuple[str, ...]) -> None:
            self._ops[name] = (CompiledOp(table=table, op_type=kind, sql=sql, param_names=names),)

        op("upsert_macro", "fix_macros", "upsert",
           "INSERT INTO fix_macros (name, side, needs_session, source, problems, created_at, updated_at, _mkio_ref) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET side = excluded.side, "
           "needs_session = excluded.needs_session, source = excluded.source, problems = excluded.problems, updated_at = excluded.updated_at, "
           "_mkio_ref = excluded._mkio_ref RETURNING *",
           ("name", "side", "needs_session", "source", "problems", "created_at", "updated_at", "_mkio_ref"))
        op("set_macro_side", "fix_macros", "update",
           "UPDATE fix_macros SET side = ?, _mkio_ref = ? WHERE name = ? RETURNING *",
           ("side", "_mkio_ref", "name"))
        op("set_priority", "fix_macro_runs", "update",
           "UPDATE fix_macro_runs SET priority = ?, _mkio_ref = ? WHERE id = ? RETURNING *",
           ("priority", "_mkio_ref", "id"))
        op("delete_macro", "fix_macros", "delete",
           "DELETE FROM fix_macros WHERE name = ? RETURNING *", ("name",))
        op("insert_run", "fix_macro_runs", "insert",
           "INSERT INTO fix_macro_runs (macro, side, version, session, market_session, seed, speed, status, "
           "started_at, _mkio_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
           ("macro", "side", "version", "session", "market_session", "seed", "speed", "status", "started_at",
            "_mkio_ref"))
        op("update_run", "fix_macro_runs", "update",
           "UPDATE fix_macro_runs SET status = ?, verdict = ?, orders = ?, live = ?, passed = ?, failed = ?, "
           "ended_at = ?, _mkio_ref = ? WHERE id = ? RETURNING *",
           ("status", "verdict", "orders", "live", "passed", "failed", "ended_at", "_mkio_ref", "id"))
        op("upsert_instance", "fix_macro_orders", "upsert",
           "INSERT INTO fix_macro_orders (run_id, subject, order_row, macro, side, session_id, cl_ord_id, order_id, "
           "symbol, block_line, status, line, waiting_for, message, actions, started_at, updated_at, _mkio_ref) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
           "ON CONFLICT(run_id, subject, order_row) DO UPDATE SET cl_ord_id = excluded.cl_ord_id, "
           "status = excluded.status, line = excluded.line, waiting_for = excluded.waiting_for, "
           "message = excluded.message, actions = excluded.actions, updated_at = excluded.updated_at, "
           "_mkio_ref = excluded._mkio_ref RETURNING *",
           ("run_id", "subject", "order_row", "macro", "side", "session_id", "cl_ord_id", "order_id", "symbol",
            "block_line", "status", "line", "waiting_for", "message", "actions", "started_at", "updated_at",
            "_mkio_ref"))
        op("finish_instance", "fix_macro_orders", "update",
           "UPDATE fix_macro_orders SET status = ?, message = ?, updated_at = ?, _mkio_ref = ? "
           "WHERE id = ? RETURNING *", ("status", "message", "updated_at", "_mkio_ref", "id"))
        op("insert_log", "fix_macro_log", "insert",
           "INSERT INTO fix_macro_log (run_id, order_row, macro, side, cl_ord_id, timestamp, line, text, "
           "_mkio_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
           ("run_id", "order_row", "macro", "side", "cl_ord_id", "timestamp", "line", "text", "_mkio_ref"))
        op("insert_session", "fix_sessions", "insert",
           "INSERT INTO fix_sessions (session_id, fix_version, sender_comp_id, target_comp_id, host, port, description, "
           "_mkio_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
           ("session_id", "fix_version", "sender_comp_id", "target_comp_id", "host", "port", "description", "_mkio_ref"))
        op("loopback_version", "fix_sessions", "update",
           "UPDATE fix_sessions SET fix_version = ?, _mkio_ref = ? WHERE session_id = ? RETURNING *",
           ("fix_version", "_mkio_ref", "session_id"))
        op("insert_session_state", "fix_session_state", "insert",
           "INSERT INTO fix_session_state (session_id, _mkio_ref) VALUES (?, ?) RETURNING *",
           ("session_id", "_mkio_ref"))
        # The row a macro took carries its name: every subject's table has a `macro` column.
        for table in vocab.SUBJECT_TABLES.values():
            op(f"tag_{table}", table, "update",
               f"UPDATE {table} SET macro = ?, _mkio_ref = ? WHERE id = ? RETURNING *",
               ("macro", "_mkio_ref", "id"))


def vocabulary() -> dict[str, Any]:
    return vocab.vocabulary()
