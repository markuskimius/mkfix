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
                # sessions_query serves the blotter: it joins fix_session_state
                # onto the session row, so the status must come through.
                await ws.send_json({"service": "sessions_query", "type": "subscribe",
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


class TestStalledBlotter:
    """A page that stops reading — a laptop asleep, a tab frozen in the
    background — must not freeze the blotters of every other page, and its
    eventual disconnect must not end the service's live updates for good.
    Both happened before mkio gave each connection its own send queue: the
    symptom was a blotter that stopped updating and stayed stopped across
    page reloads, until the server was restarted."""

    @pytest.mark.asyncio
    async def test_other_pages_keep_updating(self, server):
        subscribe = {"service": "templates_query", "type": "subscribe",
                     "protocol": "query", "subid": "mkui-table-1"}
        rows = 250
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws", max_msg_size=0) as healthy, \
                    s.ws_connect(server + "/ws", max_msg_size=0) as stalled, \
                    s.ws_connect(server + "/ws") as tx:
                for ws in (healthy, stalled):
                    await ws.send_json(subscribe)
                    assert (await _recv_json(ws))["type"] == "snapshot"
                stalled._conn.transport.pause_reading()

                names: list[str] = []

                async def read() -> None:
                    async for msg in healthy:
                        names.append(json.loads(msg.data)["row"]["name"])

                reader = asyncio.create_task(read())

                async def add(name: str) -> None:
                    await tx.send_json({
                        "service": "templates", "type": "transaction", "op": "add", "ref": name,
                        "data": {"scope": "order", "name": name, "text": "X" * 20000},
                    })
                    assert (await _recv_json(tx))["type"] == "result"

                # ~5 MB to each subscriber: far past what a socket buffers.
                for i in range(rows):
                    await add(f"stall-{i}")
                await asyncio.sleep(0.5)
                assert len(names) == rows

                # The stalled page goes away mid-stream; the feed must survive it.
                stalled._conn.transport.abort()
                await asyncio.sleep(0.3)
                await add("after-the-drop")
                await asyncio.sleep(0.5)
                reader.cancel()
        assert names[-1] == "after-the-drop"


class TestDeliveryConfig:
    """The blotters' protection against a page that stops reading lives in
    mkio's connection settings; mkfix ships their defaults and the README
    documents them, so both are pinned here."""

    def test_shipped_config_carries_mkio_delivery_defaults(self):
        from pathlib import Path

        from mkfix.__main__ import _load_config
        cfg = _load_config(Path(__file__).parent.parent / "mkfix" / "mkfix.toml")
        assert cfg["ws_heartbeat_s"] == 30
        assert cfg["ws_send_buffer_mb"] == 16

    def test_readme_names_the_keys_with_their_defaults(self):
        from pathlib import Path
        readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
        assert "ws_heartbeat_s = 30" in readme
        assert "ws_send_buffer_mb = 16" in readme

    def test_a_config_may_override_them(self, tmp_path):
        from pathlib import Path

        from mkfix.__main__ import _load_config
        shipped = (Path(__file__).parent.parent / "mkfix" / "mkfix.toml").read_text(encoding="utf-8")
        custom = tmp_path / "custom.toml"
        custom.write_text("ws_heartbeat_s = 0\nws_send_buffer_mb = 64\n" + shipped, encoding="utf-8")
        cfg = _load_config(custom)
        assert cfg["ws_heartbeat_s"] == 0 and cfg["ws_send_buffer_mb"] == 64

    @pytest.mark.asyncio
    async def test_server_speaks_the_protocol_with_reset_nacks(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server + "/ws") as ws:
                await ws.send_json({"service": "_mkio", "type": "request", "reqid": "p"})
                row = (await _recv_json(ws))["row"]
                major, minor = (int(x) for x in row["protocol"].split(".")[:2])
                assert (major, minor) >= (1, 3)
                # A getmore for a page sequence the server does not hold is a
                # reset — subscribe again — not a refusal.
                await ws.send_json({"service": "orders_query", "type": "getmore", "subid": "gone"})
                nack = await _recv_json(ws)
        assert nack["type"] == "nack" and nack["code"] == "reset"
