"""``mkfix check`` and ``mkfix run``: macros from the command line.

``check`` is offline — the parser and checker, nothing else — and answers
the way a compiler does: one line per problem, ``FILE:LINE:COL: message``,
exit 1 when any is an error. Templates and sessions are not known here, so
a ``using`` name or a ``run on SESSION`` passes; the server checks them
again when the macro is run.

``run`` talks to a running server through its ``fix_cmd`` service, the
way the UI does: the file is saved under its name (Import's rule: the
file's stem), then armed or run for its side — a market macro that only
sends, or a client one, on the session given. ``--wait`` follows the run
until it is over, printing its log as it comes, and exits by the verdict;
``--for`` bounds the wait and stops the run at the end of it, which is how
a macro that waits for orders is given a turn.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from mkfix.archive import server_url
from mkfix.macro import check, errors

_EXIT_PASSED, _EXIT_FAILED, _EXIT_INTERRUPTED, _EXIT_USAGE = 0, 1, 2, 3
_OVER = ("finished", "stopped", "interrupted")


def _parser(cmd: str) -> argparse.ArgumentParser:
    if cmd == "check":
        p = argparse.ArgumentParser(
            prog="mkfix check",
            description="Check macro files without a server: parse them, check their meaning, and print every "
                        "problem as FILE:LINE:COL: message. Exit 1 when any file has an error.",
            epilog="examples:\n  mkfix check slow-fill.macro\n  mkfix check --side client *.macro",
            formatter_class=argparse.RawDescriptionHelpFormatter)
        p.add_argument("files", nargs="+", metavar="FILE", help="a .macro file")
        p.add_argument("--side", choices=("client", "market"), default=None,
                       help="the side the macros must be for, as an editor's side would (default: each file's "
                            "first block decides)")
        return p
    p = argparse.ArgumentParser(
        prog="mkfix run",
        description="Save a macro file on a running server under the file's name and run it — a client macro "
                    "or a market one that sends is run on --session, a market one that waits is armed — "
                    "the way ▶ in the editor does. Prints the run's number; with --wait, follows the run to "
                    "its end, printing its log, and exits 0 when it passed, 1 when it failed, 2 when the server "
                    "stopped it.",
        epilog="examples:\n"
               "  mkfix run order-burst.macro --session LOOP-CLI --wait\n"
               "  mkfix run slow-fill.macro                    # a market macro: armed, and left armed\n"
               "  mkfix run slow-fill.macro --for 30s          # armed for thirty seconds, then stopped\n"
               "  mkfix run mine.macro --session S1 --speed 5 --seed 7 --wait -p 9090",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("file", metavar="FILE", help="a .macro file; the macro is saved under the file's name")
    p.add_argument("--session", default="", help="the session to run on (a `run` block that names none needs it)")
    p.add_argument("--name", default=None, help="save the macro under this name instead of the file's")
    p.add_argument("--speed", type=float, default=1.0, help="run the macro's waits this many times faster (default 1)")
    p.add_argument("--seed", type=int, default=None, help="the run's seed (default: the macro's, or random)")
    p.add_argument("--wait", action="store_true", help="follow the run until it is over and exit by its verdict")
    p.add_argument("--for", dest="duration", default=None, metavar="DURATION",
                   help="follow the run for this long (30s, 2m), then stop it; implies --wait")
    p.add_argument("-p", "--port", type=int, default=8080, help="the server's web port (default 8080)")
    p.add_argument("--host", default="localhost", help="the server's host (default localhost)")
    p.add_argument("--url", default=None, help="the server's URL, instead of --host and --port")
    return p


# -- check ------------------------------------------------------------------------------

def check_files(paths: list[str], side: str | None = None, out: Any = None) -> int:
    """Check each file; print its problems; the exit code."""
    out = out or sys.stdout
    worst = _EXIT_PASSED
    for name in paths:
        path = Path(name)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"{name}: {exc.strerror or exc}", file=out)
            worst = max(worst, _EXIT_USAGE)
            continue
        _, diagnostics = check(text, side=side)
        for d in diagnostics:
            print(f"{name}:{d.line}:{d.col + 1}: {d.severity}: {d.message}", file=out)
        bad = errors(diagnostics)
        if bad:
            worst = max(worst, _EXIT_FAILED)
        print(f"{name}: {'no problems' if not diagnostics else f'{len(bad)} error(s), {len(diagnostics) - len(bad)} warning(s)'}",
              file=out)
    return worst


# -- run --------------------------------------------------------------------------------

def _duration(text: str) -> float:
    units = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    for unit, factor in sorted(units.items(), key=lambda u: -len(u[0])):
        if text.endswith(unit):
            return float(text[: -len(unit)]) * factor
    return float(text)


async def run_file(path: str, *, session: str = "", name: str | None = None, speed: float = 1.0,
                   seed: int | None = None, wait: bool = False, duration: float | None = None,
                   url: str = "http://localhost:8080", out: Any = None) -> int:
    """Save and run one macro file on the server at ``url``; the exit code."""
    from mkio.client import MkioClient
    out = out or sys.stdout
    text = Path(path).read_text(encoding="utf-8")
    macro, diagnostics = check(text)
    bad = errors(diagnostics)
    if bad:
        for d in bad:
            print(f"{path}:{d.line}:{d.col + 1}: error: {d.message}", file=out)
        return _EXIT_USAGE
    name = name or Path(path).stem
    ws_url = url.replace("http://", "ws://", 1).replace("https://", "wss://", 1).rstrip("/") + "/ws"

    async with MkioClient(ws_url, reconnect=False) as client:
        async def cmd(command: str, **data: Any) -> dict[str, Any]:
            # fix_cmd takes the UI's transaction-shaped messages (`op`), not a reqrep request.
            reply = await client.send("fix_cmd", {"command": command, **data}, op=command)
            if reply.get("type") == "error":
                raise RuntimeError(reply.get("message", command))
            return reply

        saved = await cmd("save_macro", name=name, source=text, side=macro.side)
        if saved.get("problems"):
            for d in saved.get("diagnostics", []):
                print(f"{path}:{d['line']}:{d['col'] + 1}: {d['severity']}: {d['message']}", file=out)
            return _EXIT_USAGE
        command = "run_macro" if macro.side == "client" else "arm_macro"
        started = await cmd(command, name=name, session=session, seed=seed if seed is not None else "", speed=speed)
        run_id = started["run_id"]
        sends = any(block.kind == "client" for block in macro.blocks)
        print(f"{name}: {'run' if sends else 'armed'} #{run_id} on {session or 'every session'} "
              f"(seed {started.get('seed')}, speed {speed:g})", file=out)
        if not wait and duration is None:
            return _EXIT_PASSED

        # A run is followed until it is over. One that sends and also waits
        # (`on sent order` beside its `run`) is over, for the command line,
        # when nothing of it is sending any more: it is stopped then, as it
        # is when --for runs out — this is a test run, not a desk left armed.
        deadline = None if duration is None else time.monotonic() + duration
        after = 0
        while True:
            report = await cmd("run_report", run_id=run_id, after=after)
            for line in report["log"]:
                who = f"{line['cl_ord_id']} " if line["cl_ord_id"] else ""
                print(f"  {who}line {line['line']}: {line['text']}", file=out)
                after = line["id"]
            run = report["run"]
            if run["status"] in _OVER:
                break
            done = report["sends"] and not report["sending"]
            if done or (deadline is not None and time.monotonic() >= deadline):
                await cmd("stop_run", run_id=run_id)
                continue
            await asyncio.sleep(0.25)
        run = report["run"]
        summary = f"{name} #{run_id}: {run['status']}"
        if run["verdict"]:
            summary += f", {run['verdict']}"
        summary += f" · {run['orders']} order{'s' if run['orders'] != 1 else ''}"
        if run["passed"] or run["failed"]:
            summary += f", {run['passed']} passed, {run['failed']} failed"
        print(summary, file=out)
        for order in report["orders"]:
            if order["status"] == "failed":
                print(f"  {order['subject']} {order['cl_ord_id']}: {order['message']}", file=out)
        if run["status"] == "interrupted":
            return _EXIT_INTERRUPTED
        return _EXIT_FAILED if run["verdict"] == "failed" else _EXIT_PASSED


def main(cmd: str, argv: list[str]) -> None:
    args = _parser(cmd).parse_args(argv)
    if cmd == "check":
        sys.exit(check_files(args.files, args.side))
    url = args.url or server_url({"host": args.host, "port": args.port})
    duration = _duration(args.duration) if args.duration else None
    try:
        code = asyncio.run(run_file(args.file, session=args.session, name=args.name, speed=args.speed,
                                    seed=args.seed, wait=args.wait, duration=duration, url=url))
    except (OSError, RuntimeError) as exc:
        print(f"mkfix run: {exc}", file=sys.stderr)
        sys.exit(_EXIT_USAGE)
    sys.exit(code)
