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


def _start(port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "mkfix", "-p", str(port), "-d", ":memory:"],
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

    engine = SimpleNamespace(sessions={
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
    assert "2 enabled" in text
    assert "acc: ME -> THEM (FIX.4.2, acceptor on port 9876)" in text
    assert "ini: ME -> EXCH (FIX.4.4, initiator -> 10.0.0.5:9877)" in text
