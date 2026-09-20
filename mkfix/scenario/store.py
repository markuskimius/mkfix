"""Scenarios, their runs and their scripts' progress, kept in the database.

`ScenarioManager` is what `fix_cmd` talks to: it saves scripts, arms and
stops them through a `ScenarioRunner`, and mirrors what the runner does into
four tables the panes watch. The runner stays the authority on what is
running; the rows are its shadow, written in order by one worker so a
script's progress never lands out of sequence.

The rows survive the process, the scripts do not: at startup whatever the
last process left live is marked `interrupted`, and every scenario that was
armed is armed again as a fresh run.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mkfix.fix.message import _fix_timestamp

from . import vocab
from .check import check, errors
from .instance import LIVE, Instance
from .nodes import Diagnostic
from .runner import Run, ScenarioError, ScenarioRunner

if TYPE_CHECKING:
    from mkfix.fix.engine import FixEngine

log = logging.getLogger(__name__)

EXAMPLES = Path(__file__).parent / "examples"

# The pair of sessions the sending examples run over: this engine talking to
# itself, so both sides of an order are on the screen.
LOOPBACK = {"market": "LOOP-MKT", "client": "LOOP-CLI"}
LOOPBACK_PORT = 9880
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


class ScenarioManager:
    def __init__(self, engine: FixEngine, runner: ScenarioRunner | None = None) -> None:
        self.engine = engine
        self.runner = runner or ScenarioRunner(engine)
        self.runner.on_change = self._instance_changed
        self.runner.on_log = self._logged
        self.runner.on_run_finished = self._run_finished
        self._ops: dict[str, Any] = {}
        self._run_rows: dict[Run, int] = {}
        self._runs_by_row: dict[int, Run] = {}
        self._tagged: set[int] = set()
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._closing = False

    # -- life ----------------------------------------------------------------------------

    async def start(self) -> None:
        self._compile_ops()
        conn = self.engine.db.write_conn
        for sql in (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_fix_scenarios_name ON fix_scenarios(name)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_fix_scenario_instances_order "
            "ON fix_scenario_instances(run_id, order_row)",
            "CREATE INDEX IF NOT EXISTS idx_fix_scenario_log_run ON fix_scenario_log(run_id)",
        ):
            await (await conn.execute(sql)).close()
        await conn.commit()
        self._queue = asyncio.Queue()
        self._worker = asyncio.get_running_loop().create_task(self._drain())
        await self._recover()

    async def stop(self) -> None:
        """Shut down without a word in the tables: the runs stay `armed`, so
        the next start re-arms them and marks what was live `interrupted`."""
        self._closing = True
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
        rows = await self._fetch(f"SELECT * FROM fix_scenario_runs WHERE status IN {_LIVE_RUNS!r} ORDER BY id")
        now = _fix_timestamp()
        for row in rows:
            await self._write("update_run", ("interrupted", row["verdict"], row["orders"], 0, row["passed"],
                                             row["failed"], now, None, row["id"]))
        stale = await self._fetch(f"SELECT * FROM fix_scenario_instances WHERE status IN {LIVE!r}")
        for inst in stale:
            await self._write("finish_instance", ("interrupted", "the server stopped", now, None, inst["id"]))
        for row in rows:
            try:
                saved = await self.load(row["scenario"])
                scenario, _ = check(saved["source"])
                if any(b.kind == vocab.CLIENT for b in scenario.blocks):
                    # Waiting for orders again is harmless; sending orders
                    # again because the server restarted is not.
                    log.warning("scenario %s sends orders: not run again after the restart", row["scenario"])
                    continue
                await self.arm(row["scenario"], session=row["session"], seed=row["seed"], speed=row["speed"])
            except (ScenarioError, ValueError) as e:
                log.warning("scenario %s was armed but cannot be re-armed: %s", row["scenario"], e)

    # -- scripts ---------------------------------------------------------------------------

    async def known(self) -> dict[str, Any]:
        templates: dict[str, set[str]] = {}
        for row in await self._fetch("SELECT scope, name FROM fix_templates"):
            templates.setdefault(row["scope"], set()).add(row["name"])
        sessions = {r["session_id"] for r in await self._fetch("SELECT session_id FROM fix_sessions")}
        sessions |= set(self.engine.sessions)
        return {"templates": templates, "sessions": sessions}

    async def check(self, source: str) -> dict[str, Any]:
        scenario, diagnostics = check(source, **await self.known())
        return {"name": scenario.name, "diagnostics": [_diagnostic(d) for d in diagnostics],
                "errors": len(errors(diagnostics)),
                "blocks": [{"kind": b.kind, "line": b.line, "session": b.session or ""} for b in scenario.blocks]}

    async def save(self, name: str, source: str) -> dict[str, Any]:
        """Keep ``source`` under ``name`` — with its problems, if it has any:
        a draft is worth keeping, and only arming needs a clean script."""
        name = " ".join(str(name).split())
        if not name:
            raise ValueError("A scenario needs a name")
        result = await self.check(source)
        if result["name"] and result["name"] != name:
            raise ValueError(f"The script calls itself {result['name']!r}; it is being saved as {name!r}. "
                             "Make the two agree")
        now = _fix_timestamp()
        await self._write("upsert_scenario", (name, source, result["errors"], now, now, None))
        return result

    async def delete(self, name: str) -> None:
        if any(run.scenario.name == name and run.status == "armed" for run in self.runner.runs):
            raise ValueError(f"{name!r} is armed: stop its run first")
        await self._write("delete_scenario", (name,))

    async def load(self, name: str) -> dict[str, Any]:
        rows = await self._fetch("SELECT * FROM fix_scenarios WHERE name = ?", (name,))
        if not rows:
            raise ValueError(f"No scenario named {name!r}")
        return rows[0]

    def examples(self) -> list[dict[str, str]]:
        return [{"name": p.stem, **example_header(p.read_text(encoding="utf-8"))}
                for p in sorted(EXAMPLES.glob("*.scenario"))]

    def example(self, name: str) -> dict[str, str]:
        path = EXAMPLES / f"{Path(name).name}.scenario"
        if not path.is_file():
            raise ValueError(f"No example named {name!r}")
        text = path.read_text(encoding="utf-8")
        return {"name": path.stem, "source": text, **example_header(text)}

    # -- runs --------------------------------------------------------------------------------

    async def arm(self, name: str, *, session: str = "", seed: int | None = None, speed: float = 1.0) -> dict[str, Any]:
        row = await self.load(name)
        scenario, diagnostics = check(row["source"], **await self.known())
        bad = errors(diagnostics)
        if bad:
            raise ValueError(f"{name!r} has {len(bad)} problem(s); the first: {bad[0]}")
        if any(r.scenario.name == name and r.status == "armed" for r in self.runner.runs):
            raise ValueError(f"{name!r} is already armed")
        if session and session not in self.engine.sessions:
            raise ValueError(f"No session named {session!r}")
        # Armed but not started: the run's row is written and known to the
        # hooks before any script can act. A script may fail in its first
        # instant — `new` on a session that has just dropped — ending a run
        # that only sends; started first, all of that went unrecorded and
        # left a row saying `armed` that Stop could not stop.
        run = self.runner.arm(scenario, session=session or None, seed=seed or None, speed=float(speed or 1.0),
                              start=False)
        try:
            await self._write("insert_run", (name, row.get("_mkio_version") or 0, session or "", run.seed,
                                             run.speed, "armed", _fix_timestamp(), None))
            run_id = (await self._fetch("SELECT MAX(id) AS id FROM fix_scenario_runs"))[0]["id"]
        except BaseException:
            self.runner.stop(run)
            raise
        self._run_rows[run] = run_id
        self._runs_by_row[run_id] = run
        self.runner.start(run)
        return {"run_id": run_id, "seed": run.seed}

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
            rows = await self._fetch("SELECT * FROM fix_scenario_runs WHERE id = ?", (int(run_id),))
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

    async def pause_run(self, run_id: Any) -> None:
        run = self._run(run_id)
        self.runner.pause(run)
        await self._write_run(run, "paused")

    async def resume_run(self, run_id: Any) -> None:
        run = self._run(run_id)
        self.runner.resume(run)
        await self._write_run(run, "armed")

    async def detach(self, order_row: Any) -> None:
        if not self.runner.detach(int(order_row)):
            raise ValueError("No live script owns that order")
        await self.flush()

    async def setup_loopback(self, port: Any = None, start: bool = True) -> dict[str, Any]:
        """Create the two loopback sessions if they are not there — an acceptor
        and an initiator facing it on localhost — and start them."""
        port = int(port or LOOPBACK_PORT)
        existing = {r["session_id"]: r for r in await self._fetch(
            "SELECT * FROM fix_sessions WHERE session_id IN (?, ?)", tuple(LOOPBACK.values()))}
        created = []
        for role, session_id in LOOPBACK.items():
            if session_id in existing:
                continue
            sender, target = ("LOOPMKT", "LOOPCLI") if role == "market" else ("LOOPCLI", "LOOPMKT")
            host = "" if role == "market" else "127.0.0.1"
            await self.engine.writer.submit(
                (*self._ops["insert_session"], *self._ops["insert_session_state"]),
                ((session_id, sender, target, host, port, "Loopback for scenario examples", None), (session_id, None)),
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
        return {"sessions": LOOPBACK, "port": port, "created": created, "started": started}

    def live_runs(self) -> list[Run]:
        return [r for r in self.runner.runs if r.status == "armed"]

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
                    run_id, -1 - instance.index, instance.run.scenario.name, instance.block.session or "", "", "",
                    "", instance.block.line, instance.status, instance.line, "", instance.message,
                    instance.actions, now, now, None)))
                self._queue.put_nowait(("run", instance.run))
            return
        order, now = instance.order, _fix_timestamp()
        if instance.key not in self._tagged:
            self._tagged.add(instance.key)
            label = f"{instance.run.scenario.name} #{run_id}"
            self._queue.put_nowait(("tag_order", (label, None, instance.key)))
        self._queue.put_nowait(("upsert_instance", (
            run_id, instance.key, instance.run.scenario.name, order["session_id"], order["cl_ord_id"],
            order["order_id"], order["symbol"], instance.block.line, instance.status, instance.line,
            instance.waiting_for, instance.message, instance.actions, now, now, None)))
        if not instance.live:
            self._tagged.discard(instance.key)
        self._queue.put_nowait(("run", instance.run))

    def _run_finished(self, run: Run) -> None:
        if not self._closing and self._queue is not None and run in self._run_rows:
            self._queue.put_nowait(("run_ended", run))

    def _logged(self, run: Run, instance: Instance | None, line: int, text: str) -> None:
        if self._closing or self._queue is None or run not in self._run_rows:
            return
        self._queue.put_nowait(("insert_log", (
            self._run_rows[run], instance.key if instance else None, run.scenario.name,
            instance.order["cl_ord_id"] if instance and instance.order else "", _fix_timestamp(), line, text, None)))

    async def _drain(self) -> None:
        while True:
            op, params = await self._queue.get()
            try:
                if op == "run":
                    await self._write_run(params, "paused" if params.paused and params.status == "armed" else params.status,
                                          ended=params.status != "armed")
                elif op == "run_ended":
                    await self._write_run(params, params.status, ended=True)
                else:
                    await self._write(op, params)
            except Exception:
                log.exception("scenario row write failed: %s", op)
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

        op("upsert_scenario", "fix_scenarios", "upsert",
           "INSERT INTO fix_scenarios (name, source, problems, created_at, updated_at, _mkio_ref) "
           "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET source = excluded.source, "
           "problems = excluded.problems, updated_at = excluded.updated_at, _mkio_ref = excluded._mkio_ref "
           "RETURNING *",
           ("name", "source", "problems", "created_at", "updated_at", "_mkio_ref"))
        op("delete_scenario", "fix_scenarios", "delete",
           "DELETE FROM fix_scenarios WHERE name = ? RETURNING *", ("name",))
        op("insert_run", "fix_scenario_runs", "insert",
           "INSERT INTO fix_scenario_runs (scenario, version, session, seed, speed, status, started_at, _mkio_ref) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
           ("scenario", "version", "session", "seed", "speed", "status", "started_at", "_mkio_ref"))
        op("update_run", "fix_scenario_runs", "update",
           "UPDATE fix_scenario_runs SET status = ?, verdict = ?, orders = ?, live = ?, passed = ?, failed = ?, "
           "ended_at = ?, _mkio_ref = ? WHERE id = ? RETURNING *",
           ("status", "verdict", "orders", "live", "passed", "failed", "ended_at", "_mkio_ref", "id"))
        op("upsert_instance", "fix_scenario_instances", "upsert",
           "INSERT INTO fix_scenario_instances (run_id, order_row, scenario, session_id, cl_ord_id, order_id, "
           "symbol, block_line, status, line, waiting_for, message, actions, started_at, updated_at, _mkio_ref) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
           "ON CONFLICT(run_id, order_row) DO UPDATE SET cl_ord_id = excluded.cl_ord_id, status = excluded.status, "
           "line = excluded.line, waiting_for = excluded.waiting_for, message = excluded.message, "
           "actions = excluded.actions, updated_at = excluded.updated_at, _mkio_ref = excluded._mkio_ref "
           "RETURNING *",
           ("run_id", "order_row", "scenario", "session_id", "cl_ord_id", "order_id", "symbol", "block_line",
            "status", "line", "waiting_for", "message", "actions", "started_at", "updated_at", "_mkio_ref"))
        op("finish_instance", "fix_scenario_instances", "update",
           "UPDATE fix_scenario_instances SET status = ?, message = ?, updated_at = ?, _mkio_ref = ? "
           "WHERE id = ? RETURNING *", ("status", "message", "updated_at", "_mkio_ref", "id"))
        op("insert_log", "fix_scenario_log", "insert",
           "INSERT INTO fix_scenario_log (run_id, order_row, scenario, cl_ord_id, timestamp, line, text, _mkio_ref) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
           ("run_id", "order_row", "scenario", "cl_ord_id", "timestamp", "line", "text", "_mkio_ref"))
        op("insert_session", "fix_sessions", "insert",
           "INSERT INTO fix_sessions (session_id, sender_comp_id, target_comp_id, host, port, description, _mkio_ref) "
           "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING *",
           ("session_id", "sender_comp_id", "target_comp_id", "host", "port", "description", "_mkio_ref"))
        op("insert_session_state", "fix_session_state", "insert",
           "INSERT INTO fix_session_state (session_id, _mkio_ref) VALUES (?, ?) RETURNING *",
           ("session_id", "_mkio_ref"))
        op("tag_order", "fix_orders", "update",
           "UPDATE fix_orders SET scenario = ?, _mkio_ref = ? WHERE id = ? RETURNING *",
           ("scenario", "_mkio_ref", "id"))


def vocabulary() -> dict[str, Any]:
    return vocab.vocabulary()
