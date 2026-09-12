"""mkfix archive / restore: the table declarations in mkfix.toml, the CLI's
defaults, the engine's guards on an online run, and a round trip through a
real server."""

from __future__ import annotations

import asyncio
import csv
import json
import socket
import sqlite3
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from mkio.archive import ArchiveSpec, archive_specs, cutoff_value, parse_cutoff
from mkio.change_bus import ChangeBus
from mkio.config import load_config
from mkio.database import Database
from mkio.history import history_specs, versioned_tables
from mkio.writer import WriteBatcher

from mkfix.archive import ALIASES, default_cutoff, resolve_tables, server_answers
from mkfix.fix.dictionary import custom_names, register_custom
from mkfix.fix.engine import FixEngine

from tests.test_engine import INSERT_SESSION, StubSession, _add_session, _fetch_all

ROOT = Path(__file__).parent.parent
TOML = ROOT / "mkfix" / "mkfix.toml"
TABLES = tomllib.loads(TOML.read_text())["tables"]
CONFIG = load_config({"db_path": ":memory:", "tables": TABLES})
FIX_STAMP = "%Y%m%d-%H:%M:%S.000"

# Tables that travel with another (companions) rather than archive on their own.
COMPANIONS = {"fix_session_state"}


# ── Declarations ──────────────────────────────────────────────────────


class TestDeclarations:
    def test_every_table_is_archivable_or_a_companion(self):
        specs = archive_specs(CONFIG)
        travelling = {c for s in specs.values() for c in s.companions}
        assert travelling == COMPANIONS
        unclassified = set(TABLES) - set(specs) - travelling
        assert not unclassified, f"tables neither archivable nor a companion: {sorted(unclassified)}"

    def test_running_data_archives_by_fix_stamp_in_the_data_group(self):
        specs = archive_specs(CONFIG)
        data = {n: s for n, s in specs.items() if s.group == "data"}
        assert set(data) == {"fix_messages", "fix_orders", "fix_executions", "fix_iois", "fix_allocations"}
        for spec in data.values():
            assert spec.cutoff is not None and spec.format == FIX_STAMP, spec.table
        assert data["fix_orders"].cutoff == "created_at"
        assert all(data[t].cutoff == "timestamp" for t in data if t != "fix_orders")

    def test_config_tables_are_opt_in_and_paired(self):
        specs = archive_specs(CONFIG)
        config = {n: s for n, s in specs.items() if s.group == "config"}
        assert set(config) == {
            "fix_sessions", "fix_dictionaries", "fix_settings", "fix_id_state",
            "fix_replay_jobs", "mkui_layouts",
        }
        assert config["fix_sessions"].companions == ("fix_session_state",)
        # Whole tables except the layouts, whose `saved` is a real timestamp.
        assert {n for n, s in config.items() if s.cutoff} == {"mkui_layouts"}

    def test_fix_stamp_format_matches_the_engine(self):
        from mkfix.fix.message import _fix_timestamp
        rendered = datetime.now(timezone.utc).strftime(FIX_STAMP)
        assert len(rendered) == len(_fix_timestamp()) and rendered[8] == "-" and rendered[-4] == "."

    def test_aliases_name_real_tables(self):
        specs = archive_specs(CONFIG)
        for alias, table in ALIASES.items():
            assert table in specs, alias
        assert resolve_tables("orders, trades,fix_messages") == ["fix_orders", "fix_executions", "fix_messages"]
        assert resolve_tables(None) is None and resolve_tables("") is None


# ── Defaults ──────────────────────────────────────────────────────────


def test_default_cutoff_is_local_midnight_rendered_as_a_fix_stamp():
    instant = parse_cutoff(default_cutoff())
    local = instant.astimezone()
    assert (local.hour, local.minute, local.second) == (0, 0, 0)
    assert local.date() == datetime.now().date()
    spec = archive_specs(CONFIG)["fix_messages"]
    value = cutoff_value(spec, instant, None)
    assert value == instant.strftime(FIX_STAMP) and value.endswith(".000")
    # Sorts against engine stamps: a message from just before midnight goes,
    # one from just after stays.
    before = (instant.timestamp() - 1)
    after = (instant.timestamp() + 1)
    assert datetime.fromtimestamp(before, timezone.utc).strftime(FIX_STAMP) < value
    assert datetime.fromtimestamp(after, timezone.utc).strftime(FIX_STAMP) > value


def test_server_probe_is_false_when_nothing_listens():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert server_answers({"host": "127.0.0.1", "port": port}, timeout=0.2) is False


# ── Engine guards ─────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def stack():
    db = Database(path=":memory:", tables=CONFIG["tables"], config=CONFIG)
    await db.start()
    bus = ChangeBus()
    writer = WriteBatcher(
        db, bus, versioned=history_specs(CONFIG), versioned_configs=versioned_tables(CONFIG),
    )
    await writer.start()
    engine = FixEngine(db=db, writer=writer)
    engine._compile_ops()
    await engine._ensure_indexes()
    yield db, writer, engine
    await writer.stop()
    await db.stop()


class TestGuards:
    @pytest.mark.asyncio
    async def test_a_running_session_refuses_the_run(self, stack):
        db, writer, engine = stack
        await _add_session(writer, "S1")
        await _add_session(writer, "S2")
        engine.sessions["S1"] = StubSession("S1")  # ACTIVE
        down = StubSession("S2")
        down.status = "DOWN"
        engine.sessions["S2"] = down
        rows = await _fetch_all(db, "SELECT * FROM fix_sessions ORDER BY session_id")
        with pytest.raises(ValueError, match="session S1 is ACTIVE"):
            await engine.check_archive({"fix_sessions": rows})
        await engine.check_archive({"fix_sessions": rows[1:]})
        # Not built by the engine (disabled): archivable.
        await engine.check_archive({"fix_sessions": [{"session_id": "S9"}]})

    @pytest.mark.asyncio
    async def test_a_bound_dictionary_refuses_unless_its_session_leaves_too(self, stack):
        db, writer, engine = stack
        await writer.submit(
            INSERT_SESSION, (("S1", "A", "B", "", 9876, None),), {"session_id": "S1"})
        conn = db.write_conn
        await conn.execute("UPDATE fix_sessions SET dictionary = 'custom' WHERE session_id = 'S1'")
        await conn.commit()
        dicts = [{"name": "custom"}]
        with pytest.raises(ValueError, match="dictionary custom is bound to session S1"):
            await engine.check_archive({"fix_dictionaries": dicts})
        await engine.check_archive({"fix_dictionaries": dicts, "fix_sessions": [{"session_id": "S1"}]})

    @pytest.mark.asyncio
    async def test_id_counters_never_go_while_the_engine_runs(self, stack):
        db, writer, engine = stack
        with pytest.raises(ValueError, match="fix_id_state holds the ID counters"):
            await engine.check_archive({"fix_id_state": [{"name": "RT", "counter": 5}]})

    @pytest.mark.asyncio
    async def test_after_archive_drops_sessions_and_dictionaries(self, stack):
        db, writer, engine = stack
        stub = StubSession("S1")
        stub.status = "DOWN"
        stopped = []

        async def stop():
            stopped.append(True)
        stub.stop = stop
        engine.sessions["S1"] = stub
        register_custom("gone", "FIX.4.2", {"fields": {}})
        assert "gone" in custom_names()
        await engine.after_archive({
            "fix_sessions": [{"session_id": "S1"}, {"session_id": "S9"}],
            "fix_dictionaries": [{"name": "gone"}],
        })
        assert "S1" not in engine.sessions and stopped == [True]
        assert "gone" not in custom_names()


# ── Through a real server ─────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cli(*args, expect=0, cwd=None):
    result = subprocess.run(
        [sys.executable, "-m", "mkfix", *args],
        capture_output=True, text=True, timeout=60, cwd=cwd,
    )
    assert result.returncode == expect, result.stdout + result.stderr
    return result.stdout + result.stderr


def _rows(db: Path, sql: str) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


class _Server:
    def __init__(self, tmp_path: Path):
        self.port = _free_port()
        self.db = tmp_path / "t.db"
        self.proc: subprocess.Popen | None = None
        self.url = f"http://127.0.0.1:{self.port}"

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mkfix", "-d", str(self.db), "-p", str(self.port), "--host", "127.0.0.1"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"server exited early:\n{self.proc.stdout.read().decode()}")
                time.sleep(0.1)
        self.stop()
        raise RuntimeError("server did not start within 10s")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        self.proc = None


def _snapshot(db: Path) -> dict[str, list[dict]]:
    tables = ("fix_orders", "fix_orders__history", "fix_executions", "fix_executions__history",
              "fix_messages", "fix_sessions", "fix_sessions__history", "fix_session_state")
    return {
        t: sorted(_rows(db, f"SELECT * FROM {t}"), key=lambda r: json.dumps(r, sort_keys=True, default=str))
        for t in tables
    }


async def _trade_a_little(url: str, db: Path) -> None:
    """Two sessions logging on to each other, one order sent and filled."""
    from mkio.client import MkioClient

    fix_port = _free_port()
    async with MkioClient(url.replace("http://", "ws://") + "/ws", reconnect=False) as c:
        for sid, sender, target, host in (("ACC", "MKT", "CLIENT", ""), ("INI", "CLIENT", "MKT", "127.0.0.1")):
            r = await c.send("session_mgmt", {
                "session_id": sid, "sender_comp_id": sender, "target_comp_id": target,
                "host": host, "port": fix_port,
            }, op="add")
            assert r.get("type") == "result", r
        for sid in ("ACC", "INI"):
            r = await c.send("fix_cmd", {"session_id": sid}, op="start_session")
            assert r.get("type") == "result", r
        for _ in range(100):
            await asyncio.sleep(0.1)
            statuses = {r["session_id"]: r["status"] for r in _rows(db, "SELECT session_id, status FROM fix_sessions")}
            if statuses == {"ACC": "ACTIVE", "INI": "ACTIVE"}:
                break
        else:
            raise AssertionError(f"sessions never logged on: {statuses}")
        r = await c.send("fix_cmd", {
            "session_id": "INI", "symbol": "IBM", "side": "1", "qty": 100,
            "ord_type": "2", "price": 10.5, "tif": "0",
        }, op="send_new_order")
        assert r.get("type") == "result", r
        for _ in range(50):
            await asyncio.sleep(0.1)
            if _rows(db, "SELECT 1 FROM fix_orders WHERE direction = 'RX' AND session_id = 'ACC'"):
                break
        r = await c.send("fix_cmd", {
            "session_id": "ACC", "cl_ord_id": r["cl_ord_id"], "qty": 100, "price": 10.5,
        }, op="fill_order")
        assert r.get("type") == "result", r
        for _ in range(50):
            await asyncio.sleep(0.1)
            if len(_rows(db, "SELECT 1 FROM fix_executions")) == 2:
                break


async def _collect_deletes(url: str, service: str, seconds: float) -> list[dict]:
    """Rows a query subscriber is told to drop within the window."""
    from mkio.client import MkioClient

    deletes: list[dict] = []

    async def run():
        async with MkioClient(url.replace("http://", "ws://") + "/ws", reconnect=False) as c:
            async for item in c.subscribe(service, "query"):
                if item.get("type") == "update" and item.get("op") == "delete":
                    deletes.append(item["row"])

    task = asyncio.create_task(run())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    return deletes


class TestThroughTheServer:
    @pytest.mark.asyncio
    async def test_online_archive_then_offline_restore(self, tmp_path):
        server = _Server(tmp_path)
        server.start()
        try:
            await _trade_a_little(server.url, server.db)
            before = _snapshot(server.db)
            assert len(before["fix_orders"]) == 2 and len(before["fix_executions"]) == 2
            assert len(before["fix_messages"]) >= 6
            flags = ("-p", str(server.port), "--host", "127.0.0.1", "-d", str(server.db))
            out_dir = tmp_path / "arc"

            # Sessions are running: the engine refuses, through the server.
            out = _cli("archive", *flags, "--tables", "sessions", "--out", str(out_dir), "-y", expect=1)
            assert "Archiving through the server" in out and "session ACC is ACTIVE" in out
            assert not out_dir.exists()

            # The default cutoff (midnight today) leaves today's rows alone.
            out = _cli("archive", *flags, "--out", str(out_dir), "--dry-run")
            assert "start of today" in out and "fix_orders: 0 rows" in out

            # A literal cutoff every stamp sorts under: the blotters' query
            # subscribers are told to drop each row as it goes.
            watch = asyncio.create_task(_collect_deletes(server.url, "orders_query", 4.0))
            await asyncio.sleep(0.3)
            out = await asyncio.get_running_loop().run_in_executor(
                None, lambda: _cli("archive", *flags, "--cutoff-literal", "9", "--out", str(out_dir), "-y"))
            assert "fix_orders: 2 rows, " in out and "to archive (created_at < 9)" in out
            assert "Archived" in out
            dropped = await watch
            assert sorted(r["cl_ord_id"] for r in dropped) == sorted(r["cl_ord_id"] for r in before["fix_orders"])

            run_dir = next(out_dir.iterdir())
            manifest = json.loads((run_dir / "manifest.json").read_text())
            assert manifest["mode"] == "online" and manifest["app"] == "mkfix"
            assert set(manifest["tables"]) == {
                "fix_messages", "fix_orders", "fix_executions", "fix_iois", "fix_allocations"}
            with open(run_dir / "fix_messages.csv", newline="") as f:
                msgs = list(csv.DictReader(f))
            assert len(msgs) == len(before["fix_messages"])
            assert all("\x01" in m["raw_message"] for m in msgs), "wire bytes kept SOH"
            assert _rows(server.db, "SELECT * FROM fix_orders") == []
            assert _rows(server.db, "SELECT * FROM fix_orders__history") == []
            assert _rows(server.db, "SELECT * FROM fix_messages") == []
            # Sessions stayed up and untouched.
            assert {r["status"] for r in _rows(server.db, "SELECT status FROM fix_sessions")} == {"ACTIVE"}

            # Restore needs the server stopped.
            out = _cli("restore", *flags, str(run_dir), expect=1)
            assert "server is answering" in out
        finally:
            server.stop()

        out = _cli("restore", *flags, str(run_dir))
        assert "fix_orders: 2 rows" in out and "Done." in out
        after = _snapshot(server.db)
        for t in ("fix_orders", "fix_orders__history", "fix_executions", "fix_executions__history"):
            assert after[t] == before[t], t
        # The server kept heartbeating and logged out after the snapshot, so
        # the archived messages are a subset of what the file holds now.
        assert all(m in after["fix_messages"] for m in before["fix_messages"])

        # Offline, with the server down, config tables go too — sessions with
        # their state — and come back.
        out = _cli("archive", *flags, "--all", "--cutoff-literal", "9", "--out", str(out_dir), "-y")
        assert "no server answering" in out
        assert "fix_sessions: 2 rows, 2 history rows, 2 fix_session_state rows to archive (whole table, cutoff ignored)" in out
        assert _rows(server.db, "SELECT * FROM fix_sessions") == []
        assert _rows(server.db, "SELECT * FROM fix_session_state") == []
        run2 = sorted(out_dir.iterdir())[-1]
        out = _cli("restore", *flags, str(run2))
        assert "fix_sessions: 2 rows, 2 history rows, 2 fix_session_state rows restored" in out
        assert _snapshot(server.db) == after

    def test_in_memory_database_is_refused(self):
        out = _cli("archive", "-d", ":memory:", expect=2)
        assert "in-memory" in out
