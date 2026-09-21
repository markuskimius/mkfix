"""``mkfix archive`` and ``mkfix restore``: mkio's row archiving with mkfix's
defaults.

The tables come from the ``archive`` keys in mkfix.toml — the running-data
tables (messages, orders, trades, IOIs, allocations, macro runs) in the ``data`` group,
archived by default, and the config/state tables in ``config``, archived
only when named. The default cutoff is the start of today in local time, so
a plain ``mkfix archive`` clears down everything from before today. Short
table names (``orders``, ``trades``, ...) map to the real ones in ``ALIASES``.

If a server answers on the configured port and serves ``fix_cmd``, the
archive runs through it (``_mkio`` archive request): the engine's guards
apply and the blotters drop the rows live. Otherwise the database file is
archived directly, which is only correct with the server stopped. ``--url``
and ``--offline`` force one or the other. Restore is offline only and
refuses to run while a server answers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mkio.archive import (
    ArchiveError,
    archive_offline,
    midnight_today,
    parse_cutoff,
    restore_offline,
)

from mkfix.upgrade import mirror_columns_in_archive, scenarios_in_archive, strip_retired

ALIASES: dict[str, str] = {
    "messages": "fix_messages",
    "orders": "fix_orders",
    "trades": "fix_executions",
    "executions": "fix_executions",
    "iois": "fix_iois",
    "allocations": "fix_allocations",
    "sessions": "fix_sessions",
    "dictionaries": "fix_dictionaries",
    "settings": "fix_settings",
    "ids": "fix_id_state",
    "replay_jobs": "fix_replay_jobs",
    "templates": "fix_templates",
    "macros": "fix_macros",
    "macro_runs": "fix_macro_runs",
    "macro_orders": "fix_macro_orders",
    "macro_log": "fix_macro_log",
    "layouts": "mkui_layouts",
}

DEFAULT_OUT = "archive"


def resolve_tables(text: str | None) -> list[str] | None:
    if not text:
        return None
    names = [t.strip() for t in text.split(",") if t.strip()]
    return [ALIASES.get(n, n) for n in names]


def default_cutoff() -> str:
    """The start of today, local time, as an ISO instant mkio's cutoff parser
    takes verbatim."""
    return midnight_today().isoformat(timespec="seconds")


_ARCHIVE_EPILOG = """\
examples:
  mkfix archive --dry-run                what would go: the data tables, from before today
  mkfix archive                          archive it (asks first; -y skips the question)
  mkfix archive --cutoff 2026-09-01      everything from before that date (local time)
  mkfix archive --cutoff 7d --tables orders,trades
  mkfix archive --all --cutoff 0m        every table, config included, from before now
"""

_RESTORE_EPILOG = """\
examples:
  mkfix restore archive/mkfix_20260912-020000
  mkfix restore --dry-run --tables orders,trades archive/mkfix_20260912-020000
  mkfix restore -d mytest myconfig.toml archive/mkfix_20260912-020000
"""


def _parser(cmd: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"mkfix {cmd}",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("config", nargs="?", default=None,
                   help="path to a mkfix.toml config file (default: ./mkfix.toml if "
                        "present, else the built-in config)")
    p.add_argument("-d", "--db", default=None, metavar="PATH",
                   help="database filename, as given to the server (.db added if no "
                        "extension; default: the config's, mkfix.db built in)")
    if cmd == "archive":
        p.add_argument("-p", "--port", type=int, default=None,
                       help="the server's web port, as given to the server (default: the "
                            "config's, 8080 built in); where a running server is looked for")
        p.add_argument("--host", default=None,
                       help="the server's web address, as given to the server")
        p.description = (
            "Archive rows to CSV and delete them from the database: one directory per\n"
            "run, holding a manifest and a CSV per table. Shows what would go, then\n"
            "asks before deleting.\n\n"
            "Give the same config, -d, -p and --host as the server. When a server\n"
            "answers on that port the archive runs through it: the blotters drop the\n"
            "rows live, and the engine refuses a running session, a dictionary a\n"
            "remaining session uses, and the ID counters. Otherwise the database file\n"
            "is archived directly, which is only safe with the server stopped."
        )
        p.epilog = _ARCHIVE_EPILOG
        p.add_argument("--tables", default=None, metavar="A,B",
                       help="archive just these tables, short names allowed: "
                            + ", ".join(ALIASES) + " (default: the data group; "
                            "overrides --group)")
        p.add_argument("--group", default=None, metavar="{data,config}",
                       help="archive one group: data (messages, orders, trades, iois, "
                            "allocations, macro_runs, macro_orders, macro_log; the default) or config (the rest). Most config "
                            "tables are archived whole, whatever the cutoff")
        p.add_argument("--all", action="store_true",
                       help="archive both groups; not with --tables or --group")
        p.add_argument("--cutoff", default=None, metavar="WHEN",
                       help="archive rows from before WHEN: Nd/Nh/Nm back from now, a date, "
                            "or a date-time (local unless it carries a zone); default: "
                            "midnight at the start of today")
        p.add_argument("--cutoff-literal", default=None, metavar="TEXT",
                       help="instead of --cutoff: archive rows whose time column sorts "
                            "before TEXT, compared as text exactly as given, e.g. the FIX "
                            "stamp 20260901-00:00:00.000 (UTC)")
        p.add_argument("--out", default=DEFAULT_OUT, metavar="DIR",
                       help=f"directory the run's folder is created in (default: ./{DEFAULT_OUT})")
        p.add_argument("--url", default=None, metavar="URL",
                       help="archive through the server at this URL, e.g. "
                            "http://localhost:8080, without looking for one; not with --offline")
        p.add_argument("--offline", action="store_true",
                       help="archive the database file directly without looking for a "
                            "server (it must be stopped)")
        p.add_argument("--dry-run", action="store_true", help="report what would go, change nothing")
        p.add_argument("-y", "--yes", action="store_true",
                       help="do not ask for confirmation (required when stdin is not a terminal)")
    else:
        p.add_argument("-p", "--port", type=int, default=None,
                       help="the server's web port, as given to the server; used only to "
                            "check that no server is running")
        p.add_argument("--host", default=None,
                       help="the server's web address, as given to the server; same use")
        p.description = (
            "Put an archive's rows back into the database, version history included.\n"
            "The server must be stopped: restore works on the database file and\n"
            "refuses while a server answers on the configured port. A row that already\n"
            "exists blocks the restore of a data table; a config table's row is\n"
            "replaced."
        )
        p.epilog = _RESTORE_EPILOG
        p.add_argument("archive_dir",
                       help="the run directory mkfix archive wrote, e.g. "
                            "archive/mkfix_20260912-020000; a lone argument is taken as this")
        p.add_argument("--tables", default=None, metavar="A,B",
                       help="restore only these tables (short names as for mkfix archive; "
                            "default: every table in the archive)")
        p.add_argument("--dry-run", action="store_true", help="report what would be restored, change nothing")
    return p


def main(cmd: str, argv: list[str]) -> None:
    from mkfix.__main__ import _find_config, _load_config, resolve_db_arg

    parser = _parser(cmd)
    args = parser.parse_args(argv)
    config_path = args.config or _find_config()
    cfg = _load_config(config_path)
    db_path = resolve_db_arg(args.db)
    if db_path is not None:
        cfg["db_path"] = db_path
    if args.port is not None:
        cfg["port"] = args.port
    if args.host is not None:
        cfg["host"] = args.host
    if cfg.get("db_path") == ":memory:":
        parser.error("an in-memory database has nothing to archive or restore into")
    tables = resolve_tables(args.tables)
    try:
        if cmd == "archive":
            _archive(cfg, args, tables)
        else:
            _restore(cfg, args, tables)
    except ArchiveError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _archive(cfg: dict[str, Any], args: argparse.Namespace, tables: list[str] | None) -> None:
    if args.all and (tables or args.group):
        raise ArchiveError("--all cannot be combined with --tables or --group")
    if args.cutoff and args.cutoff_literal:
        raise ArchiveError("give either --cutoff or --cutoff-literal, not both")
    if args.url and args.offline:
        raise ArchiveError("give either --url or --offline, not both")
    group = "all" if args.all else args.group
    cutoff = None if args.cutoff_literal else (args.cutoff or default_cutoff())
    cutoff_desc = args.cutoff_literal or args.cutoff or f"{cutoff} (start of today)"
    out_dir = Path(args.out).resolve()

    url = args.url
    if url is None and not args.offline:
        url = server_url(cfg) if server_answers(cfg) else None
    if url is not None:
        print(f"Archiving through the server at {url}")
        asyncio.run(_archive_online(url, tables, group, cutoff, args.cutoff_literal,
                                    out_dir, args.dry_run, args.yes, cutoff_desc))
        return

    print(f"Archiving the database file {Path(cfg['db_path']).resolve()} (no server answering)")
    instant = parse_cutoff(cutoff) if cutoff else None
    kwargs: dict[str, Any] = dict(
        tables=tables, group=group, cutoff=instant, cutoff_literal=args.cutoff_literal,
        cutoff_given=args.cutoff_literal or cutoff, out_dir=out_dir,
        mkio_version=_mkio_version(),
    )
    preview = archive_offline(cfg, dry_run=True, **kwargs)
    print_summary(preview, cutoff_desc=cutoff_desc, dry_run=args.dry_run)
    if args.dry_run:
        return
    if not _total(preview):
        print("Nothing to archive.")
        return
    if not args.yes and not confirm("Archive and delete these rows?"):
        print("Aborted.")
        sys.exit(1)
    result = archive_offline(cfg, dry_run=False, **kwargs)
    print(f"  Archived {_total(result):,} rows to {result['dir']}")


async def _archive_online(
    url: str, tables: list[str] | None, group: str | None, cutoff: str | None,
    cutoff_literal: str | None, out_dir: Path, dry_run: bool, assume_yes: bool,
    cutoff_desc: str,
) -> None:
    from mkio.client import MkioClient

    spec: dict[str, Any] = {
        "tables": tables, "group": group, "cutoff": cutoff,
        "cutoff_literal": cutoff_literal, "out": str(out_dir),
    }
    ws_url = url.replace("http://", "ws://", 1).replace("https://", "wss://", 1).rstrip("/") + "/ws"
    async with MkioClient(ws_url, reconnect=False) as client:
        preview = await client.request("_mkio", {"archive": {**spec, "dry_run": True}})
        if preview.get("type") == "error":
            raise ArchiveError(preview.get("message", "unknown error"))
        print_summary(preview["row"], cutoff_desc=cutoff_desc, dry_run=dry_run)
        if dry_run:
            return
        if not _total(preview["row"]):
            print("Nothing to archive.")
            return
        if not assume_yes and not confirm("Archive and delete these rows?"):
            print("Aborted.")
            sys.exit(1)
        result = await client.request("_mkio", {"archive": {**spec, "dry_run": False}})
        if result.get("type") == "error":
            raise ArchiveError(result.get("message", "unknown error"))
        print(f"  Archived {_total(result['row']):,} rows to {result['row']['dir']}")


def _restore(cfg: dict[str, Any], args: argparse.Namespace, tables: list[str] | None) -> None:
    if server_answers(cfg):
        raise ArchiveError(
            f"a server is answering at {server_url(cfg)} — restore needs it stopped"
        )
    stale = mirror_columns_in_archive(args.archive_dir)
    retired = scenarios_in_archive(args.archive_dir)
    result = restore_offline(cfg, strip_retired(args.archive_dir),
                             tables=tables, dry_run=args.dry_run)
    verb = "would be restored" if args.dry_run else "restored"
    print(f"Restoring from {args.archive_dir}" + (" (dry run)" if args.dry_run else ""))
    for table, columns in stale.items():
        print(f"  {table}: pre-0.34 mirror column(s) {', '.join(columns)} left behind")
    if retired:
        print(f"  pre-0.51 scenarios left behind (macros start afresh): {', '.join(retired)}")
    for name, t in result["tables"].items():
        parts = [f"{t['rows']:,} rows"]
        if t.get("history"):
            parts.append(f"{t['history']:,} history rows")
        for comp, n in (t.get("companions") or {}).items():
            parts.append(f"{n:,} {comp} rows")
        note = f", {t['replaced']:,} existing replaced" if t.get("replaced") else ""
        print(f"  {name}: {', '.join(parts)} {verb}{note}")
        if t.get("history_skipped"):
            print(f"    note: {t['history_skipped']}")
    if not args.dry_run:
        print("  Done.")


# ── Helpers ───────────────────────────────────────────────────────────


def server_url(cfg: dict[str, Any]) -> str:
    host = cfg.get("host", "0.0.0.0")
    if host in ("", "0.0.0.0", "::"):
        host = "localhost"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{cfg.get('port', 8080)}"


def server_answers(cfg: dict[str, Any], *, timeout: float = 1.0) -> bool:
    """True when something at the configured address serves mkfix's
    ``fix_cmd`` — the mkfix server for this config, not just any listener."""
    try:
        with urllib.request.urlopen(f"{server_url(cfg)}/api/services", timeout=timeout) as resp:
            services = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return any(isinstance(s, dict) and s.get("name") == "fix_cmd" for s in services)


def confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        print("Error: pass --yes to confirm (stdin is not a terminal)", file=sys.stderr)
        sys.exit(1)
    try:
        answer = input(f"{prompt} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _total(summary: dict[str, Any]) -> int:
    return sum(t["rows"] for t in summary.get("tables", {}).values())


def print_summary(summary: dict[str, Any], *, cutoff_desc: str, dry_run: bool) -> None:
    verb = "would be archived" if dry_run else "to archive"
    print(f"  Cutoff: {cutoff_desc}")
    for name, t in summary.get("tables", {}).items():
        parts = [f"{t['rows']:,} rows"]
        if t.get("history") is not None:
            parts.append(f"{t['history']:,} history rows")
        for comp, n in (t.get("companions") or {}).items():
            parts.append(f"{n:,} {comp} rows")
        if t.get("cutoff_column"):
            scope = f"{t['cutoff_column']} < {t['cutoff_value']}"
        else:
            scope = "whole table, cutoff ignored"
        print(f"  {name}: {', '.join(parts)} {verb} ({scope})")


def _mkio_version() -> str:
    import importlib.metadata
    try:
        return importlib.metadata.version("mkio")
    except importlib.metadata.PackageNotFoundError:
        return "dev"
