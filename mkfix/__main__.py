"""mkfix CLI and server entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from aiohttp import web

from mkfix import __version__
from mkio.config import load_config
from mkio.server import (
    _on_startup as mkio_on_startup,
    _on_shutdown as mkio_on_shutdown,
    _api_services,
    _api_service_detail,
    _ws_handler,
    _make_index_handler,
)

from mkfix.fix.engine import FixEngine


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

    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError:
        pass

    app = web.Application()
    app["config"] = cfg

    app.on_startup.append(mkio_on_startup)
    app.on_startup.append(_start_fix_engine)
    app.on_shutdown.append(_stop_fix_engine)
    app.on_shutdown.append(mkio_on_shutdown)

    # API routes
    app.router.add_get("/api/services", _api_services)
    app.router.add_get("/api/services/{service_name}", _api_service_detail)

    # WebSocket routes
    app.router.add_get("/ws", _ws_handler)
    app.router.add_get("/ws/{service_name}", _ws_handler)

    # Serve mkio.js client library
    js_path = Path(__import__("mkio").__file__).parent / "client" / "mkio.js"
    if js_path.exists():
        async def serve_js(request: web.Request) -> web.FileResponse:
            return web.FileResponse(
                js_path, headers={"Content-Type": "application/javascript"}
            )
        app.router.add_get("/mkio.js", serve_js)

    # Static file routes — register "/" last so explicit routes take priority
    deferred_root = None
    for route, directory in cfg.get("static", {}).items():
        path = Path(directory).resolve()
        if route == "/":
            deferred_root = path
        else:
            app.router.add_static(route, path)

    if deferred_root is not None:
        app.router.add_get("/", _make_index_handler(deferred_root))
        app.router.add_static("/", deferred_root)

    web.run_app(
        app,
        host=cfg.get("host", "0.0.0.0"),
        port=cfg.get("port", 8080),
        shutdown_timeout=cfg.get("shutdown_timeout", 0),
    )


def _load_config(config: str | Path | dict[str, Any]) -> dict[str, Any]:
    """Load config, resolving mkui static path and relative directories."""
    config_dir = Path(config).parent.resolve() if isinstance(config, (str, Path)) else Path.cwd()
    cfg = load_config(config)

    statics = cfg.get("static", {})
    for route, directory in list(statics.items()):
        if directory == "__mkui__":
            import mkui
            statics[route] = str(mkui.static_dir)
        else:
            resolved = (config_dir / directory).resolve()
            statics[route] = str(resolved)

    return cfg


async def _start_fix_engine(app: web.Application) -> None:
    """Create and start the FIX engine after mkio services are ready."""
    engine = FixEngine(
        db=app["db"],
        writer=app["writer"],
        bus=app["bus"],
    )
    app["fix_engine"] = engine

    # Wire the engine into the FixCommandService
    from mkfix.services.fix_command import FixCommandService
    for svc in app["services"].values():
        if isinstance(svc, FixCommandService):
            svc.set_engine(engine)

    await engine.start()


async def _stop_fix_engine(app: web.Application) -> None:
    """Stop the FIX engine before mkio shutdown."""
    engine = app.get("fix_engine")
    if engine:
        await engine.stop()


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
