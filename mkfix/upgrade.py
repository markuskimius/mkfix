"""What an upgraded mkfix does about the data an older one left behind.

Through 0.33 the engine mirrored a session's live status and sequence numbers
onto `fix_sessions`, and the owning session's status onto `fix_orders` and
`fix_executions`, so the blotters could live-update off a single table. The
queries join `fix_session_state` now (mkio 0.8), and the columns are gone
from the schema. Two places still meet them: a database file the old
server wrote, and an archive it took.

0.51 renamed scenarios to macros, tables and all, and starts them afresh:
what 0.48-0.50 kept under the old names — the scripts, their runs, and the
`scenario` column naming the script that took an order — is dropped from a
database (`retire_scenarios`) and left out of an archive on restore
(`strip_retired`). Orders, trades and messages are untouched.

Through 0.54 every macro opened with `macro NAME`, a line the parser now
refuses: a macro is named where it is saved. `retire_macro_lines` deletes
the line from the saved rows so they still check; an old archive needs no
help, since the strip runs at every start, after a restore too.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

MIRROR_COLUMNS: dict[str, tuple[str, ...]] = {
    "fix_sessions": ("status", "tx_seq_num", "rx_seq_num"),
    "fix_orders": ("session_status",),
    "fix_executions": ("session_status",),
}


# What 0.48-0.50 called scenarios. `fix_orders` is versioned, so its history
# table carries the column too.
SCENARIO_TABLES = ("fix_scenarios", "fix_scenarios__history", "fix_scenario_runs", "fix_scenario_instances",
                   "fix_scenario_log")
SCENARIO_COLUMNS: dict[str, tuple[str, ...]] = {"fix_orders": ("scenario",), "fix_orders__history": ("scenario",)}


def retire_scenarios(db_path: str) -> dict[str, list[str]]:
    """Drop the pre-0.51 scenario tables and the `scenario` column from an
    existing database file, once: `{"tables": [...], "columns": [...]}` of
    what went, empty lists when there was nothing. mkio's `auto_migrate`
    refuses a column the schema no longer has, so this runs before it."""
    gone: dict[str, list[str]] = {"tables": [], "columns": []}
    if not db_path or db_path == ":memory:" or not Path(db_path).is_file():
        return gone
    conn = sqlite3.connect(db_path)
    try:
        present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in SCENARIO_TABLES:
            if table in present:
                conn.execute(f"DROP TABLE {table}")
                gone["tables"].append(table)
        for table, columns in SCENARIO_COLUMNS.items():
            if table not in present:
                continue
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for column in columns:
                if column in have:
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
                    gone["columns"].append(f"{table}.{column}")
        conn.commit()
    finally:
        conn.close()
    if gone["tables"] or gone["columns"]:
        print("  Scenarios are macros now, and start afresh: dropped "
              + ", ".join([*gone["tables"], *gone["columns"]]))
    return gone


MACRO_LINE = re.compile(r"^[ \t]*macro[ \t]+[^\n]*\n?(?:[ \t]*\n)?", re.M)


def retire_macro_lines(db_path: str) -> list[str]:
    """Delete the `macro NAME` line (and the blank after it) from every saved
    macro in an existing database file: the names of the rows changed, none
    when there was nothing to do. A plain UPDATE, outside mkio's writer, so
    the row's version and history stay as they were."""
    if not db_path or db_path == ":memory:" or not Path(db_path).is_file():
        return []
    changed: list[str] = []
    conn = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "fix_macros" not in tables:
            return []
        for name, source in conn.execute("SELECT name, source FROM fix_macros").fetchall():
            stripped = MACRO_LINE.sub("", source or "", count=1)
            if stripped != source:
                conn.execute("UPDATE fix_macros SET source = ? WHERE name = ?", (stripped, name))
                changed.append(name)
        conn.commit()
    finally:
        conn.close()
    if changed:
        print("  A macro no longer names itself: dropped the `macro NAME` line from " + ", ".join(changed))
    return changed


def retire_mirror_columns(db_path: str) -> dict[str, list[str]]:
    """Drop the mirror columns from an existing database file, once.

    Dropping a column is a destructive migration mkio's safe `auto_migrate`
    refuses, so without this step an upgraded server would not start on an
    existing database until `mkio dbupdate --allow-destructive`; the data is
    a copy, so nothing is lost. Returns what was dropped, by table: nothing
    for a fresh or in-memory database or one already without them."""
    if not db_path or db_path == ":memory:" or not Path(db_path).is_file():
        return {}
    dropped: dict[str, list[str]] = {}
    conn = sqlite3.connect(db_path)
    try:
        for table, columns in MIRROR_COLUMNS.items():
            present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for column in columns:
                if column in present:
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
                    dropped.setdefault(table, []).append(column)
        conn.commit()
    finally:
        conn.close()
    for table, columns in dropped.items():
        print(f"  Dropped mirror column(s) {', '.join(columns)} from {table}")
    return dropped


def mirror_columns_in_archive(archive_dir: str | Path) -> dict[str, list[str]]:
    """The mirror columns an archive's live-row CSVs carry, by table."""
    manifest = json.loads((Path(archive_dir) / "manifest.json").read_text(encoding="utf-8"))
    found: dict[str, list[str]] = {}
    for table, entry in manifest.get("tables", {}).items():
        stale = [c for c in MIRROR_COLUMNS.get(table, ()) if c in entry.get("columns", {})]
        if stale:
            found[table] = stale
    return found


def strip_mirror_columns(archive_dir: str | Path) -> Path:
    """An archive taken before 0.34 carries the mirror columns on its live
    rows, and mkio's restore refuses a column the schema no longer has.
    Returns a directory to restore from: the archive itself when clean,
    otherwise a temporary copy with those columns cut from the manifest and
    the CSVs (history CSVs never had them). The archive on disk is untouched."""
    archive_dir = Path(archive_dir)
    stale = mirror_columns_in_archive(archive_dir)
    if not stale:
        return archive_dir
    copy = Path(tempfile.mkdtemp(prefix="mkfix-restore-")) / archive_dir.name
    shutil.copytree(archive_dir, copy)
    manifest_path = copy / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for table, columns in stale.items():
        entry = manifest["tables"][table]
        for column in columns:
            entry["columns"].pop(column, None)
        _drop_csv_columns(copy / entry["file"], set(columns))
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return copy


def scenarios_in_archive(archive_dir: str | Path) -> list[str]:
    """What a pre-0.51 archive holds that macros replaced: its scenario
    tables, and `fix_orders.scenario`."""
    manifest = json.loads((Path(archive_dir) / "manifest.json").read_text(encoding="utf-8"))
    tables = manifest.get("tables", {})
    found = [t for t in SCENARIO_TABLES if t in tables]
    if "scenario" in tables.get("fix_orders", {}).get("columns", {}):
        found.append("fix_orders.scenario")
    return found


def strip_retired(archive_dir: str | Path) -> Path:
    """A directory to restore from with everything this module retires
    taken out: the mirror columns, the scenario tables, and the `scenario`
    column of orders and their history. The archive itself when it has none
    of them, else a temporary copy; the archive on disk is untouched."""
    source = strip_mirror_columns(archive_dir)
    if not scenarios_in_archive(source):
        return source
    if source == Path(archive_dir):
        copy = Path(tempfile.mkdtemp(prefix="mkfix-restore-")) / Path(archive_dir).name
        shutil.copytree(archive_dir, copy)
        source = copy
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for table in SCENARIO_TABLES:
        manifest["tables"].pop(table, None)
    orders = manifest["tables"].get("fix_orders")
    for entry in (orders, (orders or {}).get("history")):
        if entry and "scenario" in entry.get("columns", {}):
            entry["columns"].pop("scenario")
            _drop_csv_columns(source / entry["file"], {"scenario"})
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return source


def _drop_csv_columns(path: Path, drop: set[str]) -> None:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        return
    keep = [i for i, name in enumerate(rows[0]) if name not in drop]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for row in rows:
            w.writerow([row[i] for i in keep if i < len(row)])
