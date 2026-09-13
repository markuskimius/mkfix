"""Upgrading from a mkfix that mirrored session state onto other tables
(through 0.33): the database file loses the mirror columns on first start,
and an archive that carries them still restores."""

import asyncio
import csv
import json
import sqlite3
import tomllib
from pathlib import Path

import pytest

from mkio.archive import ArchiveError, restore_offline
from mkio.config import load_config
from mkio.database import Database

from mkfix.upgrade import (
    MIRROR_COLUMNS, mirror_columns_in_archive, retire_mirror_columns, strip_mirror_columns,
)

ROOT = Path(__file__).resolve().parent.parent
TABLES = tomllib.loads((ROOT / "mkfix" / "mkfix.toml").read_text())["tables"]


def _current_db(path: Path) -> dict:
    """A database file with today's schema, built the way the server builds
    it (mkio's Database adds its own columns); returns the loaded config."""
    cfg = load_config({"db_path": str(path), "tables": TABLES, "auto_migrate": True})

    async def build():
        db = Database(path=str(path), tables=cfg["tables"], config=cfg)
        await db.start()
        await db.stop()
    asyncio.run(build())
    return cfg


def _legacy_db(path: Path) -> None:
    """A database as 0.33 left it: today's schema plus the mirror columns."""
    _current_db(path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE fix_sessions ADD COLUMN status TEXT DEFAULT 'DOWN'")
    conn.execute("ALTER TABLE fix_sessions ADD COLUMN tx_seq_num INTEGER DEFAULT 1")
    conn.execute("ALTER TABLE fix_sessions ADD COLUMN rx_seq_num INTEGER DEFAULT 1")
    conn.execute("ALTER TABLE fix_orders ADD COLUMN session_status TEXT DEFAULT 'DOWN'")
    conn.execute("ALTER TABLE fix_executions ADD COLUMN session_status TEXT DEFAULT 'DOWN'")
    conn.execute(
        "INSERT INTO fix_sessions (session_id, sender_comp_id, target_comp_id, status, tx_seq_num) "
        "VALUES ('S1', 'A', 'B', 'ACTIVE', 42)")
    conn.execute(
        "INSERT INTO fix_session_state (session_id, status, tx_seq_num) VALUES ('S1', 'ACTIVE', 42)")
    conn.execute(
        "INSERT INTO fix_orders (cl_ord_id, session_id, symbol, side, session_status) "
        "VALUES ('C1', 'S1', 'AAPL', 'Buy', 'ACTIVE')")
    conn.execute(
        "INSERT INTO fix_executions (session_id, exec_id, symbol, side, session_status) "
        "VALUES ('S1', 'E1', 'AAPL', 'Buy', 'ACTIVE')")
    conn.commit()
    conn.close()


def _columns(path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


class TestRetireMirrorColumns:
    def test_drops_exactly_the_mirrors_and_keeps_the_rows(self, tmp_path):
        db = tmp_path / "m.db"
        _legacy_db(db)
        before = {t: _columns(db, t) for t in MIRROR_COLUMNS}

        dropped = retire_mirror_columns(str(db))

        assert dropped == {t: list(c) for t, c in MIRROR_COLUMNS.items()}
        for table, columns in MIRROR_COLUMNS.items():
            assert _columns(db, table) == [c for c in before[table] if c not in columns]
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT session_id FROM fix_sessions").fetchall() == [("S1",)]
        assert conn.execute("SELECT cl_ord_id FROM fix_orders").fetchall() == [("C1",)]
        assert conn.execute("SELECT exec_id FROM fix_executions").fetchall() == [("E1",)]
        assert conn.execute("SELECT status, tx_seq_num FROM fix_session_state").fetchall() == \
            [("ACTIVE", 42)], "the state of record is untouched"
        assert "status" in _columns(db, "fix_orders"), "an order's own status is not a mirror"
        conn.close()

    def test_is_a_no_op_once_done_and_on_a_current_schema(self, tmp_path, capsys):
        db = tmp_path / "m.db"
        _legacy_db(db)
        retire_mirror_columns(str(db))
        capsys.readouterr()
        assert retire_mirror_columns(str(db)) == {}
        assert capsys.readouterr().out == ""

        fresh = tmp_path / "fresh.db"
        _current_db(fresh)
        assert retire_mirror_columns(str(fresh)) == {}

    def test_skips_memory_and_missing_databases(self, tmp_path):
        assert retire_mirror_columns(":memory:") == {}
        assert retire_mirror_columns("") == {}
        assert retire_mirror_columns(str(tmp_path / "absent.db")) == {}
        assert not (tmp_path / "absent.db").exists()

    def test_reports_what_it_dropped(self, tmp_path, capsys):
        db = tmp_path / "m.db"
        _legacy_db(db)
        retire_mirror_columns(str(db))
        out = capsys.readouterr().out
        assert "Dropped mirror column(s) status, tx_seq_num, rx_seq_num from fix_sessions" in out
        assert "Dropped mirror column(s) session_status from fix_orders" in out

    def test_runs_before_mkio_migration_at_startup(self):
        """mkio's safe auto_migrate refuses a column drop, so the step must
        come before create_app builds the migrating Database."""
        main = (ROOT / "mkfix" / "__main__.py").read_text()
        assert main.index("retire_mirror_columns(cfg[\"db_path\"])") < main.index("app = create_app(cfg)")


def _legacy_archive(path: Path) -> Path:
    """An archive as 0.33's `mkfix archive --all` wrote it: live rows carry
    the mirror columns, history rows never did."""
    path.mkdir()
    manifest = {
        "app": "mkfix", "mode": "offline", "cutoff": {"given": None, "utc": None},
        "tables": {
            "fix_orders": {
                "file": "fix_orders.csv", "rows": 1, "primary_key": ["id"],
                "cutoff_column": "created_at", "cutoff_value": "9", "group": "data",
                "columns": {"id": "INTEGER", "cl_ord_id": "TEXT", "session_id": "TEXT",
                            "symbol": "TEXT", "side": "TEXT", "session_status": "TEXT",
                            "_mkio_ref": "TEXT", "_mkio_version": "INTEGER"},
                "history": {
                    "table": "fix_orders__history", "file": "fix_orders__history.csv", "rows": 1,
                    "columns": {"id": "INTEGER", "cl_ord_id": "TEXT", "session_id": "TEXT",
                                "symbol": "TEXT", "side": "TEXT", "_mkio_ref": "TEXT",
                                "_mkio_version": "INTEGER", "_mkio_op": "TEXT"},
                },
            },
            "fix_sessions": {
                "file": "fix_sessions.csv", "rows": 1, "primary_key": ["session_id"],
                "cutoff_column": None, "cutoff_value": None, "group": "config",
                "columns": {"session_id": "TEXT", "sender_comp_id": "TEXT", "target_comp_id": "TEXT",
                            "status": "TEXT", "tx_seq_num": "INTEGER", "rx_seq_num": "INTEGER",
                            "_mkio_ref": "TEXT", "_mkio_version": "INTEGER"},
                "history": {
                    "table": "fix_sessions__history", "file": "fix_sessions__history.csv", "rows": 1,
                    "columns": {"session_id": "TEXT", "sender_comp_id": "TEXT", "target_comp_id": "TEXT",
                                "_mkio_ref": "TEXT", "_mkio_version": "INTEGER", "_mkio_op": "TEXT"},
                },
                "companions": {"fix_session_state": {
                    "file": "fix_session_state.csv", "rows": 1,
                    "columns": {"session_id": "TEXT", "status": "TEXT", "tx_seq_num": "INTEGER"},
                }},
            },
        },
    }
    (path / "manifest.json").write_text(json.dumps(manifest, indent=2))
    files = {
        "fix_orders.csv": [
            ["id", "cl_ord_id", "session_id", "symbol", "side", "session_status", "_mkio_ref", "_mkio_version"],
            ["1", "C1", "S1", "AAPL", "Buy", "ACTIVE", "r1", "1"]],
        "fix_orders__history.csv": [
            ["id", "cl_ord_id", "session_id", "symbol", "side", "_mkio_ref", "_mkio_version", "_mkio_op"],
            ["1", "C1", "S1", "AAPL", "Buy", "r1", "1", "insert"]],
        "fix_sessions.csv": [
            ["session_id", "sender_comp_id", "target_comp_id", "status", "tx_seq_num", "rx_seq_num",
             "_mkio_ref", "_mkio_version"],
            ["S1", "A", "B", "ACTIVE", "42", "7", "r0", "1"]],
        "fix_sessions__history.csv": [
            ["session_id", "sender_comp_id", "target_comp_id", "_mkio_ref", "_mkio_version", "_mkio_op"],
            ["S1", "A", "B", "r0", "1", "insert"]],
        "fix_session_state.csv": [
            ["session_id", "status", "tx_seq_num"], ["S1", "ACTIVE", "42"]],
    }
    for name, rows in files.items():
        with open(path / name, "w", newline="") as f:
            csv.writer(f).writerows(rows)
    return path


class TestLegacyArchives:
    def test_finds_the_mirror_columns_an_old_archive_carries(self, tmp_path):
        arc = _legacy_archive(tmp_path / "arc")
        assert mirror_columns_in_archive(arc) == {
            "fix_orders": ["session_status"],
            "fix_sessions": ["status", "tx_seq_num", "rx_seq_num"],
        }

    def test_a_current_archive_is_restored_as_it_is(self, tmp_path):
        arc = _legacy_archive(tmp_path / "arc")
        strip_mirror_columns(arc)  # a copy; the original still carries them
        clean = strip_mirror_columns(arc)
        assert mirror_columns_in_archive(clean) == {}
        assert strip_mirror_columns(clean) == clean, "nothing to strip: no copy is made"

    def test_strips_the_columns_from_a_copy(self, tmp_path):
        arc = _legacy_archive(tmp_path / "arc")
        original = {p.name: p.read_text() for p in arc.iterdir()}

        copy = strip_mirror_columns(arc)

        assert copy != arc and copy.name == arc.name
        assert {p.name: p.read_text() for p in arc.iterdir()} == original, "the archive is untouched"
        manifest = json.loads((copy / "manifest.json").read_text())
        assert "session_status" not in manifest["tables"]["fix_orders"]["columns"]
        assert not {"status", "tx_seq_num", "rx_seq_num"} & set(manifest["tables"]["fix_sessions"]["columns"])
        assert manifest["tables"]["fix_sessions"]["companions"]["fix_session_state"]["columns"] == \
            {"session_id": "TEXT", "status": "TEXT", "tx_seq_num": "INTEGER"}, \
            "the state table's own status column is not a mirror"
        with open(copy / "fix_orders.csv", newline="") as f:
            rows = list(csv.reader(f))
        assert rows == [["id", "cl_ord_id", "session_id", "symbol", "side", "_mkio_ref", "_mkio_version"],
                        ["1", "C1", "S1", "AAPL", "Buy", "r1", "1"]]
        with open(copy / "fix_sessions.csv", newline="") as f:
            rows = list(csv.reader(f))
        assert rows == [["session_id", "sender_comp_id", "target_comp_id", "_mkio_ref", "_mkio_version"],
                        ["S1", "A", "B", "r0", "1"]]
        assert (copy / "fix_sessions__history.csv").read_text() == original["fix_sessions__history.csv"]

    def test_mkio_refuses_the_old_archive_and_takes_the_stripped_copy(self, tmp_path):
        db = tmp_path / "m.db"
        cfg = _current_db(db)
        arc = _legacy_archive(tmp_path / "arc")

        with pytest.raises(ArchiveError, match="session_status no longer exist"):
            restore_offline(cfg, arc, dry_run=True)

        result = restore_offline(cfg, strip_mirror_columns(arc))
        assert result["tables"]["fix_orders"]["rows"] == 1
        assert result["tables"]["fix_sessions"]["rows"] == 1
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT cl_ord_id, symbol FROM fix_orders").fetchall() == [("C1", "AAPL")]
        assert conn.execute("SELECT session_id FROM fix_sessions").fetchall() == [("S1",)]
        assert conn.execute("SELECT status, tx_seq_num FROM fix_session_state").fetchall() == [("ACTIVE", 42)]
        assert conn.execute("SELECT _mkio_op FROM fix_orders__history").fetchall() == [("insert",)]
        conn.close()

    def test_restore_cli_goes_through_the_strip(self):
        archive = (ROOT / "mkfix" / "archive.py").read_text()
        assert "restore_offline(cfg, strip_mirror_columns(args.archive_dir)" in archive
