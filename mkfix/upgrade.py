"""What an upgraded mkfix does about the data an older one left behind.

Through 0.33 the engine mirrored a session's live status and sequence numbers
onto `fix_sessions`, and the owning session's status onto `fix_orders` and
`fix_executions`, so the blotters could live-update off a single table. The
queries join `fix_session_state` now (mkio 0.8), and the columns are gone
from the schema. Two places still meet them: a database file the old
server wrote, and an archive it took.
"""

from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

MIRROR_COLUMNS: dict[str, tuple[str, ...]] = {
    "fix_sessions": ("status", "tx_seq_num", "rx_seq_num"),
    "fix_orders": ("session_status",),
    "fix_executions": ("session_status",),
}


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
    manifest = json.loads((Path(archive_dir) / "manifest.json").read_text())
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
    manifest = json.loads(manifest_path.read_text())
    for table, columns in stale.items():
        entry = manifest["tables"][table]
        for column in columns:
            entry["columns"].pop(column, None)
        _drop_csv_columns(copy / entry["file"], set(columns))
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return copy


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
