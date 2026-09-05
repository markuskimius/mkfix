"""mkfix CLI and server entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Any

from mkio import create_app
from mkio.config import load_config

from mkfix import __version__
from mkfix.fix.engine import FixEngine
from mkfix.services.fix_command import FixCommandService


def serve(
    config: str | Path | dict[str, Any] = "mkfix.toml",
    host: str | None = None,
    port: int | None = None,
    db_path: str | None = None,
) -> None:
    """Start the mkfix server. Blocks until shutdown."""
    cfg = _load_config(config)
    if host is not None:
        cfg["host"] = host
    if port is not None:
        cfg["port"] = port
    if db_path is not None:
        cfg["db_path"] = db_path

    app = create_app(cfg)
    app.add_service("fix_cmd", FixCommandService)

    engine: FixEngine | None = None

    async def start_fix_engine() -> None:
        nonlocal engine
        engine = FixEngine(db=app.db, writer=app.writer)
        app.services["fix_cmd"].set_engine(engine)
        await engine.start()

    async def stop_fix_engine() -> None:
        if engine is not None:
            await engine.stop()

    app.on_startup(start_fix_engine)
    app.on_shutdown(stop_fix_engine)

    # Mirrors MkioApp.run, but announces the server only once the port is
    # actually bound, so the URL printed is one that answers. The port is
    # probed up front because a bind failure inside app.start() happens after
    # the startup hooks have opened the database, whose aiosqlite threads then
    # keep the process alive; losing the race anyway leaves only a hard exit.
    _check_port(cfg["host"], cfg["port"])

    async def run() -> None:
        loop = asyncio.get_running_loop()
        try:
            await app.start()
        except OSError as exc:
            print(_bind_error(cfg, exc), file=sys.stderr, flush=True)
            os._exit(1)
        print(_banner(cfg, config, engine), flush=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(app.stop()))
        await app.wait()

    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError:
        pass
    asyncio.run(run())


def _bind_error(cfg: dict[str, Any], exc: OSError) -> str:
    return f"Error: cannot listen on {cfg['host']}:{cfg['port']}: {exc.strerror or exc}"


def _check_port(host: str, port: int) -> None:
    """Fail fast, before anything else starts, if the web port can't be bound."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
    except OSError as exc:
        print(_bind_error({"host": host, "port": port}, exc), file=sys.stderr)
        raise SystemExit(1) from None


def _banner(
    cfg: dict[str, Any],
    config: str | Path | dict[str, Any],
    engine: FixEngine | None,
) -> str:
    """Startup summary: where the UI is, what it's running on, and the
    sessions loaded — enabled ones only, since that is all the engine
    builds; the rest are still visible in the Sessions blotter."""
    host = cfg.get("host", "0.0.0.0")
    port = cfg.get("port", 8080)
    url_host = "localhost" if host in ("", "0.0.0.0", "::") else host
    if ":" in url_host:
        url_host = f"[{url_host}]"
    url = f"http://{url_host}:{port}/"
    listen = f"{host}:{port}"
    if host in ("", "0.0.0.0", "::"):
        listen += " (all interfaces)"

    db_path = cfg.get("db_path", "mkio.db")
    database = "in-memory (nothing persists)" if db_path == ":memory:" else str(Path(db_path).resolve())
    config_desc = "<dict>" if isinstance(config, dict) else str(Path(config).resolve())

    lines = [
        f"mkfix {__version__}",
        f"  Web UI:    {url}",
        f"  Listening: {listen}",
        f"  Config:    {config_desc}",
        f"  Database:  {database}",
    ]

    sessions = list(engine.sessions.values()) if engine is not None else []
    lines.append(f"  Sessions:  {len(sessions)} enabled")
    for session in sessions:
        c = session.config
        if c.get("host"):
            role = f"initiator -> {c['host']}:{c['port']}"
        else:
            role = f"acceptor on port {c['port']}"
        lines.append(
            f"    {c['session_id']}: {c['sender_comp_id']} -> {c['target_comp_id']}"
            f" ({c.get('fix_version', '')}, {role})"
        )
    lines.append("  Press Ctrl+C to stop.")
    return "\n".join(lines)


def _load_config(config: str | Path | dict[str, Any]) -> dict[str, Any]:
    """Load config, resolving mkui static path and relative directories."""
    config_dir = Path(config).parent.resolve() if isinstance(config, (str, Path)) else Path.cwd()
    cfg = load_config(config)
    cfg["version"] = __version__

    statics = cfg.get("static", {})
    for route, directory in list(statics.items()):
        if directory == "__mkui__":
            import mkui
            statics[route] = str(mkui.static_dir)
        else:
            resolved = (config_dir / directory).resolve()
            statics[route] = str(resolved)

    return cfg


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="mkfix",
        description="FIX protocol testing engine built on mkio and mkui",
    )
    parser.add_argument(
        "config", nargs="?", default=None,
        help="path to mkfix.toml config file (default: auto-detect)",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=None,
        help="override listening port",
    )
    parser.add_argument(
        "--host", default=None,
        help="override listening host",
    )
    parser.add_argument(
        "-d", "--db", default=None, metavar="PATH",
        help="database filename (.db added if no extension; use ':memory:' for in-memory)",
    )
    parser.add_argument(
        "--version", action="version", version=f"mkfix {__version__}",
    )
    args = parser.parse_args()

    db_path = args.db
    if db_path is not None and db_path != ":memory:" and not Path(db_path).suffix:
        db_path += ".db"

    config_path = args.config or _find_config()
    serve(config_path, host=args.host, port=args.port, db_path=db_path)


def _find_config() -> str:
    """Look for mkfix.toml in current directory, then package directory."""
    cwd = Path.cwd() / "mkfix.toml"
    if cwd.exists():
        return str(cwd)

    pkg = Path(__file__).parent / "mkfix.toml"
    if pkg.exists():
        return str(pkg)

    print("Error: mkfix.toml not found. Provide a config path as argument.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
