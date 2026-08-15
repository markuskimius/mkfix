"""mkfix CLI and server entry point."""

from __future__ import annotations

import argparse
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
    app.run()


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
