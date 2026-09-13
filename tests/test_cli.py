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
    assert "IDs:       RT/OR/EX/TR + Q7 + 8-digit counter (saved code)" in text


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
    assert "IDs:       RT/OR/EX/TR + ME + 8-digit counter (from username)" in text
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
