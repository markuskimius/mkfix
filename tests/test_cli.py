"""Tests for CLI argument handling."""

import subprocess
import sys

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
