"""Tests for CLI argument handling."""

import subprocess
import sys
from pathlib import Path

import pytest

from mkfix import __version__


def test_help():
    result = subprocess.run(
        [sys.executable, "-m", "mkfix", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "FIX protocol testing engine" in result.stdout
    assert "--port" in result.stdout
    assert "--db" in result.stdout
    assert "--instance-code" in result.stdout
    assert "mkfix archive -h" in result.stdout
    assert "mkfix restore -h" in result.stdout
    assert "mkfix check FILE..." in result.stdout and "mkfix run FILE" in result.stdout
    assert "mkfix check -h" in result.stdout and "mkfix run -h" in result.stdout


def test_help_states_the_shipped_defaults():
    """The defaults the help quotes live in mkfix.toml, not the parser."""
    import tomllib
    from mkfix.archive import _parser

    cfg = tomllib.loads(
        (Path(__file__).parent.parent / "mkfix" / "mkfix.toml").read_text(encoding="utf-8"))
    result = subprocess.run(
        [sys.executable, "-m", "mkfix", "--help"],
        capture_output=True, text=True,
    )
    text = " ".join(result.stdout.split())
    assert f"{cfg['port']} built in" in text
    assert f"{cfg['host']}, all interfaces, built in" in text
    assert f"{cfg['db_path']} in the current directory built in" in text
    archive = " ".join(_parser("archive").format_help().split())
    assert f"{cfg['port']} built in" in archive
    assert f"{cfg['db_path']} built in" in archive


def test_help_examples_parse(monkeypatch):
    """Each server example in the help epilog is a command line main()
    accepts and hands to serve()."""
    import shlex
    import mkfix.__main__ as main_mod

    lines = [l.strip() for l in main_mod._EPILOG.split("examples:")[1].splitlines() if l.strip()]
    assert lines
    calls = []
    monkeypatch.setattr(main_mod, "serve", lambda *a, **kw: calls.append((a, kw)))
    for line in lines:
        monkeypatch.setattr(sys, "argv", shlex.split(line.split("   ")[0]))
        main_mod.main()
    assert len(calls) == len(lines)
    assert any(kw["db_path"] == "mytest.db" for _, kw in calls)
    assert any(kw["instance_code"] == "Q7" for _, kw in calls)
    assert any(a[0] == "myconfig.toml" for a, _ in calls)


@pytest.mark.parametrize("cmd", ["archive", "restore"])
def test_subcommand_help(cmd):
    """The subcommands are dispatched ahead of argparse, so their help is
    the only place their options are listed."""
    result = subprocess.run(
        [sys.executable, "-m", "mkfix", cmd, "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert f"usage: mkfix {cmd}" in result.stdout
    assert "--tables" in result.stdout
    assert "examples:" in result.stdout


@pytest.mark.parametrize("bad", ["A", "ABC", "A-"])
def test_bad_instance_code_is_a_usage_error(bad):
    result = subprocess.run(
        [sys.executable, "-m", "mkfix", "-d", ":memory:", "-i", bad],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 2
    assert "instance code must be exactly 2" in result.stderr
    assert "Traceback" not in result.stderr


def test_serve_rejects_bad_instance_code_before_starting():
    """A malformed code must fail before any config, port, or database is
    touched, so a typo can't leave a server running with the wrong default."""
    from mkfix.__main__ import serve

    with pytest.raises(ValueError, match="instance code"):
        serve("/nonexistent/config.toml", instance_code="bad")


def test_version():
    result = subprocess.run(
        [sys.executable, "-m", "mkfix", "--version"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert f"mkfix {__version__}" in result.stdout


def test_bad_config():
    result = subprocess.run(
        [sys.executable, "-m", "mkfix", "/nonexistent/config.toml"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(port: int, *extra: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "mkfix", "-p", str(port), "-d", ":memory:", *extra],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def _read_until(proc: subprocess.Popen, marker: str, limit: int = 40) -> list[str]:
    lines: list[str] = []
    for _ in range(limit):
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line)
        if marker in line:
            break
    return lines


def test_startup_banner_names_web_url():
    """Once the port is bound, the server must say where its UI is."""
    port = _free_port()
    proc = _start(port)
    try:
        lines = _read_until(proc, "Sessions:")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    text = "".join(lines)
    assert f"mkfix {__version__}" in text
    assert f"http://localhost:{port}/" in text
    assert "in-memory" in text
    assert "0 enabled" in text


def _banner_of(port: int, *extra: str) -> str:
    proc = _start(port, *extra)
    try:
        return "".join(_read_until(proc, "Sessions:"))
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_startup_honors_instance_code():
    """The banner must show the code generated IDs will carry, so an
    override is confirmed before the first order goes out."""
    text = _banner_of(_free_port(), "-i", "Q7")
    assert "IDs:       RT/OR/EX/TR/IO/AD/AL/RQ/QT/QR + Q7 + 8-digit counter (saved code)" in text


def test_instance_code_persists_across_restarts(tmp_path):
    """-i is remembered in the database: a later run without it keeps the
    code, and -i '' returns to the username default."""
    import getpass
    from mkfix.fix.idgen import _instance_code

    default = _instance_code(getpass.getuser())
    db = str(tmp_path / "persist.db")

    def banner(*extra: str) -> str:
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-m", "mkfix", "-p", str(port), "-d", db, *extra],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            return "".join(_read_until(proc, "Sessions:"))
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    assert f"+ {default} + 8-digit counter (from username)" in banner()
    assert "+ Q7 + 8-digit counter (saved code)" in banner("-i", "Q7")
    assert "+ Q7 + 8-digit counter (saved code)" in banner()
    assert "+ Z9 + 8-digit counter (saved code)" in banner("-i", "Z9")
    assert f"+ {default} + 8-digit counter (from username)" in banner("-i", "")
    assert f"+ {default} + 8-digit counter (from username)" in banner()


def test_startup_fails_cleanly_on_busy_port():
    """A second instance on the same port must exit 1 with a one-line error,
    not hang on the database threads mkio's startup hooks left open."""
    port = _free_port()
    first = _start(port)
    try:
        _read_until(first, "Web UI:")
        second = subprocess.run(
            [sys.executable, "-m", "mkfix", "-p", str(port), "-d", ":memory:"],
            capture_output=True, text=True, timeout=20,
        )
    finally:
        first.terminate()
        first.wait(timeout=10)
    assert second.returncode == 1
    assert f"cannot listen on 0.0.0.0:{port}" in second.stderr
    assert "Traceback" not in second.stderr


def test_startup_honors_host_override():
    """The URL must name the host actually bound, not always localhost."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "mkfix", "-p", str(port), "--host", "127.0.0.1",
         "-d", ":memory:"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        text = "".join(_read_until(proc, "Sessions:"))
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    assert f"http://127.0.0.1:{port}/" in text
    assert f"Listening: 127.0.0.1:{port}\n" in text


def test_check_port_rejects_busy_port():
    import socket
    from mkfix.__main__ import _check_port

    with socket.socket() as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        with pytest.raises(SystemExit) as exc:
            _check_port("127.0.0.1", port)
        assert exc.value.code == 1
    _check_port("127.0.0.1", port)


def test_banner_url_for_wildcard_and_ipv6_hosts():
    from mkfix.__main__ import _banner

    for host in ("0.0.0.0", "", "::"):
        text = _banner({"host": host, "port": 8080, "db_path": ":memory:"}, {}, None)
        assert "http://localhost:8080/" in text
        assert "(all interfaces)" in text
        assert "in-memory" in text
        assert "Config:    <dict>" in text
        assert "0 enabled" in text

    text = _banner({"host": "::1", "port": 8080, "db_path": "x.db"}, "x.toml", None)
    assert "http://[::1]:8080/" in text
    assert "(all interfaces)" not in text
    assert str(Path("x.db").resolve()) in text
    assert str(Path("x.toml").resolve()) in text


def test_banner_lists_enabled_sessions():
    from types import SimpleNamespace
    from mkfix.__main__ import _banner

    ids = SimpleNamespace(instance_id="ME", instance_source="username")
    engine = SimpleNamespace(ids=ids, sessions={
        "acc": SimpleNamespace(config={
            "session_id": "acc", "fix_version": "FIX.4.2", "sender_comp_id": "ME",
            "target_comp_id": "THEM", "host": "", "port": 9876,
        }),
        "ini": SimpleNamespace(config={
            "session_id": "ini", "fix_version": "FIX.4.4", "sender_comp_id": "ME",
            "target_comp_id": "EXCH", "host": "10.0.0.5", "port": 9877,
        }),
    })
    cfg = {"host": "127.0.0.1", "port": 9090, "db_path": "x.db"}
    text = _banner(cfg, "mkfix.toml", engine)
    assert "http://127.0.0.1:9090/" in text
    assert "IDs:       RT/OR/EX/TR/IO/AD/AL/RQ/QT/QR + ME + 8-digit counter (from username)" in text
    assert "2 enabled" in text
    assert "acc: ME -> THEM (FIX.4.2, acceptor on port 9876)" in text
    assert "ini: ME -> EXCH (FIX.4.4, initiator -> 10.0.0.5:9877)" in text


def test_check_port_probes_like_the_server_on_windows(monkeypatch):
    """SO_REUSEADDR means something else on Windows — bind over a live
    listener — so there the probe must set nothing, as asyncio's server
    does, or a busy port passes the check and fails only at start()."""
    import socket
    from mkfix.__main__ import _check_port

    options: list[tuple] = []

    class Spy(socket.socket):
        def setsockopt(self, *args):
            options.append(args)
            super().setsockopt(*args)

    monkeypatch.setattr(socket, "socket", Spy)
    monkeypatch.setattr(sys, "platform", "win32")
    _check_port("127.0.0.1", _free_port())
    assert options == []

    monkeypatch.setattr(sys, "platform", "linux")
    _check_port("127.0.0.1", _free_port())
    assert options == [(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]


def test_serve_returns_on_cancellation_without_signal_handlers(monkeypatch, capsys):
    """On Windows the ProactorEventLoop has no add_signal_handler, and
    asyncio.run() delivers Ctrl+C as a cancellation of the main task; serve()
    must then stop the server and return, instead of dying right after
    binding the port — which is what the bare add_signal_handler did."""
    import asyncio
    import mkfix.__main__ as main_mod

    monkeypatch.setitem(sys.modules, "uvloop", None)
    loop = asyncio.new_event_loop()
    loop_cls = type(loop)
    loop.close()

    def unsupported(self, sig, callback, *args):
        raise NotImplementedError

    monkeypatch.setattr(loop_cls, "add_signal_handler", unsupported)

    apps = []
    real_create_app = main_mod.create_app

    def capturing_create_app(cfg):
        apps.append(real_create_app(cfg))
        return apps[-1]

    real_banner = main_mod._banner

    def banner_then_interrupt(*args):
        # The banner is composed after start() and before the wait, so a
        # cancel scheduled here lands in app.wait(), where Ctrl+C would.
        task = asyncio.current_task()
        asyncio.get_running_loop().call_later(0.05, task.cancel)
        return real_banner(*args)

    monkeypatch.setattr(main_mod, "create_app", capturing_create_app)
    monkeypatch.setattr(main_mod, "_banner", banner_then_interrupt)

    port = _free_port()
    config = Path(main_mod.__file__).parent / "mkfix.toml"
    main_mod.serve(config, host="127.0.0.1", port=port, db_path=":memory:")

    assert len(apps) == 1 and apps[0].db is None
    assert f"http://127.0.0.1:{port}/" in capsys.readouterr().out


def test_serve_runs_on_the_loop_mkio_picks(monkeypatch, capsys):
    """serve() mirrors mkio's run() and so builds its loop the same way:
    through loop_factory and the `event_loop` key. On Windows that is
    asyncio's selector loop by default, whose transport teardown does not
    log a traceback for every connection a browser opens and drops; here
    the key names it outright, and the server must come up on it."""
    import asyncio
    import mkfix.__main__ as main_mod

    seen = []
    real_create_app = main_mod.create_app

    def create_app_on_the_selector_loop(cfg):
        cfg["event_loop"] = "selector"
        app = real_create_app(cfg)

        async def note_loop_and_stop():
            seen.append(type(asyncio.get_running_loop()))
            asyncio.get_running_loop().call_later(0.05, lambda: asyncio.ensure_future(app.stop()))

        app.on_startup(note_loop_and_stop)
        return app

    monkeypatch.setattr(main_mod, "create_app", create_app_on_the_selector_loop)
    port = _free_port()
    config = Path(main_mod.__file__).parent / "mkfix.toml"
    main_mod.serve(config, host="127.0.0.1", port=port, db_path=":memory:")

    assert len(seen) == 1 and issubclass(seen[0], asyncio.SelectorEventLoop)
    assert f"http://127.0.0.1:{port}/" in capsys.readouterr().out


# -- mkfix check, mkfix run -----------------------------------------------------------

EXAMPLES = Path(__file__).parent.parent / "mkfix" / "macro" / "examples"


def _mkfix(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "mkfix", *args], capture_output=True, text=True, timeout=timeout)


@pytest.mark.parametrize("cmd", ["check", "run"])
def test_macro_subcommand_help(cmd):
    result = _mkfix(cmd, "--help")
    assert result.returncode == 0
    assert f"usage: mkfix {cmd}" in result.stdout and "examples:" in result.stdout
    if cmd == "run":
        for option in ("--session", "--speed", "--seed", "--wait", "--for", "--port", "--url", "--name"):
            assert option in result.stdout, option


def test_macro_help_examples_parse():
    """Every example line in the two help screens is a command line its
    parser accepts."""
    import shlex
    from mkfix.macro.cli import _parser
    checked = 0
    for cmd in ("check", "run"):
        parser = _parser(cmd)
        for line in parser.epilog.splitlines():
            line = line.split("#")[0].strip()
            if line.startswith(f"mkfix {cmd} "):
                parser.parse_args(shlex.split(line)[2:])
                checked += 1
    assert checked >= 5


def test_check_passes_every_bundled_example():
    files = sorted(str(p) for p in EXAMPLES.glob("*.macro"))
    result = _mkfix("check", *files)
    assert result.returncode == 0, result.stdout
    assert result.stdout.count(": no problems") == len(files) >= 21


def test_check_reports_problems_like_a_compiler(tmp_path):
    bad = tmp_path / "bad.macro"
    bad.write_text("on order\n    fill qty: 1\n    bogus\n", encoding="utf-8")
    good = tmp_path / "good.macro"
    good.write_text("on order\n    accept\n", encoding="utf-8")
    result = _mkfix("check", str(good), str(bad))
    assert result.returncode == 1
    assert f"{bad}:2:5: error: `fill` needs price" in result.stdout, "columns count from 1, as editors do"
    assert f"{bad}:3:5: error: Unknown statement 'bogus'" in result.stdout
    assert f"{bad}: 2 error(s), 0 warning(s)" in result.stdout and f"{good}: no problems" in result.stdout


def test_check_holds_a_file_to_a_side_and_a_warning_is_no_failure(tmp_path):
    result = _mkfix("check", "--side", "client", str(EXAMPLES / "auto-ack.macro"))
    assert result.returncode == 1 and "This is a client macro" in result.stdout
    assert _mkfix("check", "--side", "market", str(EXAMPLES / "auto-ack.macro")).returncode == 0
    # a file that holds both sides is an end-to-end macro: clean by itself, a problem in the editor of a side
    both = str(EXAMPLES / "told-to-reject.macro")
    assert _mkfix("check", both).returncode == 0 and _mkfix("check", "--side", "end-to-end", both).returncode == 0
    held = _mkfix("check", "--side", "client", both)
    assert held.returncode == 1 and "or keep both in an end-to-end macro" in held.stdout
    assert _mkfix("check", "--side", "end-to-end", str(EXAMPLES / "auto-ack.macro")).returncode == 0
    assert _mkfix("check", "--side", "both", both).returncode == 2, "argparse names the three choices"
    warned = tmp_path / "warned.macro"
    warned.write_text("on order\n    restate qty: 1, reason: 'no such reason'\n", encoding="utf-8")
    result = _mkfix("check", str(warned))
    assert result.returncode == 0 and ": warning: " in result.stdout and "0 error(s), 1 warning(s)" in result.stdout


def test_check_says_a_file_it_cannot_read():
    result = _mkfix("check", "no-such-file.macro")
    assert result.returncode == 3 and "no-such-file.macro: " in result.stdout


def test_run_needs_a_server_and_a_clean_macro(tmp_path):
    macro = tmp_path / "m.macro"
    macro.write_text("on order\n    accept\n", encoding="utf-8")
    result = _mkfix("run", str(macro), "-p", str(_free_port()))
    assert result.returncode == 3 and result.stderr.startswith("mkfix run: ")
    bad = tmp_path / "bad.macro"
    bad.write_text("on order\n    bogus\n", encoding="utf-8")
    result = _mkfix("run", str(bad), "-p", str(_free_port()))
    assert result.returncode == 3 and f"{bad}:2:5: error: Unknown statement 'bogus'" in result.stdout, \
        "a macro with problems is never sent to the server"


def test_run_follows_a_run_to_its_verdict(tmp_path):
    """Against a real server: the loopback sessions, a venue armed from the
    command line, then client and market macros run with --wait and --for,
    each ending with its verdict as the exit code."""
    import asyncio
    from mkio.client import MkioClient
    port, fix_port = _free_port(), _free_port()
    proc = _start(port)
    try:
        _read_until(proc, "Press Ctrl+C")

        async def loopback():
            async with MkioClient(f"ws://localhost:{port}/ws", reconnect=False) as client:
                reply = await client.send("fix_cmd", {"command": "setup_loopback", "port": fix_port}, op="setup_loopback")
                assert reply.get("ok") and reply["started"] == ["LOOP-MKT", "LOOP-CLI"], reply
        asyncio.run(loopback())
        import time
        time.sleep(1.5)                                   # the initiator logs on

        # Real answers take real time: at speed 4 the macros' 2 s bounds are still half a second.
        venue = _mkfix("run", str(EXAMPLES / "loopback-venue.macro"), "--session", "LOOP-MKT", "--speed", "4",
                       "-p", str(port))
        assert venue.returncode == 0 and "loopback-venue: armed #1 on LOOP-MKT" in venue.stdout, venue.stdout + venue.stderr

        client = _mkfix("run", str(EXAMPLES / "loopback-client.macro"), "--session", "LOOP-CLI", "--speed", "4",
                        "--wait", "-p", str(port))
        assert client.returncode == 0, client.stdout + client.stderr
        assert "loopback-client: run #2 on LOOP-CLI (seed 1, speed 4)" in client.stdout
        assert "loopback-client #2: stopped, passed · 5 orders, 5 passed, 0 failed" in client.stdout, \
            "its `on sent order` block was still waiting, so the run is stopped once nothing is sending"
        assert client.stdout.count("passed: ") == 5

        failing = tmp_path / "failing.macro"
        failing.write_text("run\n    new symbol: 'ZZZ', side: buy, qty: 1, price: 1\n    fail 'on purpose'\n", encoding="utf-8")
        failed = _mkfix("run", str(failing), "--session", "LOOP-CLI", "--wait", "-p", str(port))
        assert failed.returncode == 1 and "failing #3: finished, failed · 1 order, 0 passed, 1 failed" in failed.stdout
        assert "on purpose" in failed.stdout

        check = _mkfix("run", str(EXAMPLES / "allocation-check.macro"), "-p", str(port))
        assert check.returncode == 0 and "allocation-check: armed #4 on every session" in check.stdout
        desk = _mkfix("run", str(EXAMPLES / "allocation-desk.macro"), "--session", "LOOP-MKT", "--speed", "4",
                      "--for", "10s", "--name", "desk", "-p", str(port))
        assert desk.returncode == 0 and "desk: run #5 on LOOP-MKT" in desk.stdout, desk.stdout + desk.stderr
        assert "desk #5: stopped, passed · 2 orders, 2 passed, 0 failed" in desk.stdout

        unknown = _mkfix("run", str(failing), "--session", "NOPE", "-p", str(port))
        assert unknown.returncode == 3 and "NOPE" in unknown.stderr

        # An end-to-end macro: both sides in one file, a session for each, and over when what it sent is done —
        # with the venue of the first step still armed on LOOP-MKT, which it comes before.
        test = _mkfix("run", str(EXAMPLES / "told-to-reject.macro"), "--session", "LOOP-CLI", "--market-session",
                      "LOOP-MKT", "--wait", "-p", str(port))
        assert test.returncode == 0, test.stdout + test.stderr
        assert "told-to-reject: run #6 on LOOP-CLI and LOOP-MKT" in test.stdout
        assert "told-to-reject #6: finished, passed · 4 orders, 2 passed, 0 failed" in test.stdout
        assert "passed: refused as told: halted after " in test.stdout and "signal 'halted'" in test.stdout
        one_side = _mkfix("run", str(EXAMPLES / "told-to-reject.macro"), "--market-session", "LOOP-MKT", "-p", str(port))
        assert one_side.returncode == 3 and "choose the client session to send on" in one_side.stderr
        not_both = _mkfix("run", str(failing), "--session", "LOOP-CLI", "--market-session", "LOOP-MKT", "-p", str(port))
        assert not_both.returncode == 3 and "is a client macro: it has one session" in not_both.stderr
    finally:
        proc.terminate()
        proc.wait(timeout=10)
