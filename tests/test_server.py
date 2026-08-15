"""Integration tests: full mkfix server over HTTP and WebSocket."""

import asyncio
import json
import socket
import subprocess
import sys
import time

import aiohttp
import pytest
import pytest_asyncio

from mkfix import __version__

MAJOR_MINOR = ".".join(__version__.split(".")[:2])


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "mkfix", "-d", ":memory:", "-p", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            if proc.poll() is not None:
                out = proc.stdout.read().decode()
                raise RuntimeError(f"server exited early:\n{out}")
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("server did not start within 10s")
    yield base
    proc.terminate()
    proc.wait(timeout=5)


async def _recv_json(ws):
    msg = await asyncio.wait_for(ws.receive(), timeout=5)
    return json.loads(msg.data)


class TestHttp:
    @pytest.mark.asyncio
    async def test_routes(self, server):
        async with aiohttp.ClientSession() as s:
            for path, expect in [
                ("/", "mkfix"),
                ("/static/app.json", "menubar"),
                ("/static/mkfix.css", "mkfix"),
                ("/mkio.js", "mkio"),
                ("/mkui/src/index.js", "Mkui"),
            ]:
                async with s.get(server + path) as resp:
                    assert resp.status == 200, path
                    assert expect in await resp.text(), path

    @pytest.mark.asyncio
    async def test_api_services_includes_fix_cmd(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.get(server + "/api/services") as resp:
                names = [svc["name"] for svc in await resp.json()]
        assert "fix_cmd" in names
        assert "sessions_query" in names
        assert "messages_stream" in names


class TestWebSocket:
    @pytest.mark.asyncio
    async def test_mkio_identity(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await ws.send_json({"service": "_mkio", "type": "request",
                                    "ref": "r1", "data": {"version": MAJOR_MINOR}})
                row = (await _recv_json(ws))["row"]
        assert row["name"] == "mkfix"
        assert row["compatible"] is True

    @pytest.mark.asyncio
    async def test_session_lifecycle(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await ws.send_json({
                    "service": "session_mgmt", "type": "transaction", "op": "add",
                    "data": {"session_id": "T1", "sender_comp_id": "A",
                             "target_comp_id": "B", "port": _free_port()},
                    "ref": "r1",
                })
                resp = await _recv_json(ws)
                assert resp["type"] == "result", resp

                await ws.send_json({
                    "service": "fix_cmd", "type": "transaction",
                    "op": "start_session", "data": {"session_id": "T1"},
                    "ref": "r2", "txnid": "t2",
                })
                resp = await _recv_json(ws)
                assert resp["type"] == "result", resp
                assert resp["txnid"] == "t2"

                await asyncio.sleep(0.3)
                await ws.send_json({"service": "session_state_query", "type": "subscribe",
                                    "protocol": "query", "subid": "s1", "ref": "q1"})
                snap = await _recv_json(ws)
                statuses = {r["session_id"]: r["status"] for r in snap["rows"]}
        assert statuses.get("T1") == "LISTENING"

    @pytest.mark.asyncio
    async def test_fix_cmd_error_keeps_connection(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await ws.send_json({"service": "fix_cmd", "type": "transaction",
                                    "op": "bogus", "data": {}, "ref": "r1"})
                resp = await _recv_json(ws)
                assert resp["type"] == "error"

                await ws.send_json({"service": "fix_cmd", "type": "transaction",
                                    "op": "bogus", "data": {}, "ref": "r2"})
                resp = await _recv_json(ws)
                assert resp["type"] == "error"
                assert resp["ref"] == "r2"
