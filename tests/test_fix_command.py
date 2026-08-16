"""Tests for FixCommandService command dispatch and envelope handling."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from mkfix.services.fix_command import FixCommandService


def _make_service(engine=None):
    svc = FixCommandService(
        config={}, db=MagicMock(), change_bus=MagicMock(), writer=MagicMock()
    )
    svc.name = "fix_cmd"
    if engine is not None:
        svc.set_engine(engine)
    return svc


def _make_engine():
    engine = MagicMock()
    for method in (
        "start_session", "stop_session", "reload_session", "reset_sequence",
        "start_replay", "pause_replay", "resume_replay", "stop_replay",
    ):
        setattr(engine, method, AsyncMock())
    engine.send_new_order = AsyncMock(return_value="MKFIX-00000001")
    engine.send_cancel = AsyncMock(return_value="MKFIX-00000002")
    engine.send_cancel_replace = AsyncMock(return_value="MKFIX-00000003")
    engine.accept_order = AsyncMock(return_value="MKFIX-O-00000001")
    engine.reject_order = AsyncMock()
    engine.fill_order = AsyncMock(return_value="MKFIX-E-00000001")
    engine.correct_trade = AsyncMock(return_value="MKFIX-E-00000002")
    engine.bust_trade = AsyncMock(return_value="MKFIX-E-00000003")
    engine.accept_cancel = AsyncMock(return_value="MKFIX-E-00000004")
    engine.accept_replace = AsyncMock(return_value="MKFIX-E-00000005")
    engine.reject_cancel = AsyncMock()
    engine.accept_request = AsyncMock(return_value="MKFIX-E-00000006")
    engine.reject_request = AsyncMock()
    return engine


def _make_ws():
    ws = MagicMock()
    ws.send_bytes = AsyncMock()
    return ws


def _sent(ws):
    assert ws.send_bytes.await_count == 1
    return json.loads(ws.send_bytes.await_args.args[0])


class TestEnvelope:
    @pytest.mark.asyncio
    async def test_error_when_engine_missing(self):
        svc = _make_service()
        ws = _make_ws()
        await svc.on_message(ws, {"ref": "r1", "txnid": "t1", "op": "start_session"})
        resp = _sent(ws)
        assert resp["type"] == "error"
        assert resp["ref"] == "r1"
        assert resp["txnid"] == "t1"

    @pytest.mark.asyncio
    async def test_txnid_echoed_on_result(self):
        svc = _make_service(_make_engine())
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r2", "txnid": "t2", "op": "start_session",
            "data": {"session_id": "S1"},
        })
        resp = _sent(ws)
        assert resp["type"] == "result"
        assert resp["ref"] == "r2"
        assert resp["txnid"] == "t2"
        assert resp["ok"] is True

    @pytest.mark.asyncio
    async def test_txnid_optional(self):
        svc = _make_service(_make_engine())
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r3", "op": "start_session", "data": {"session_id": "S1"},
        })
        resp = _sent(ws)
        assert resp["type"] == "result"
        assert "txnid" not in resp or resp["txnid"] is None

    @pytest.mark.asyncio
    async def test_unknown_command_is_error_not_exception(self):
        svc = _make_service(_make_engine())
        ws = _make_ws()
        await svc.on_message(ws, {"ref": "r4", "op": "bogus", "data": {}})
        resp = _sent(ws)
        assert resp["type"] == "error"
        assert "bogus" in resp["message"]

    @pytest.mark.asyncio
    async def test_engine_exception_becomes_error_envelope(self):
        engine = _make_engine()
        engine.start_session = AsyncMock(side_effect=ValueError("Unknown session: X"))
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {"ref": "r5", "op": "start_session", "data": {"session_id": "X"}})
        resp = _sent(ws)
        assert resp["type"] == "error"
        assert "Unknown session" in resp["message"]

    @pytest.mark.asyncio
    async def test_command_falls_back_to_data_field(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r6", "data": {"command": "stop_session", "session_id": "S1"},
        })
        engine.stop_session.assert_awaited_once_with("S1")
        assert _sent(ws)["type"] == "result"


class TestDispatch:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("command,method", [
        ("start_session", "start_session"),
        ("stop_session", "stop_session"),
        ("reload_session", "reload_session"),
    ])
    async def test_session_commands(self, command, method):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {"ref": "r", "op": command, "data": {"session_id": "S1"}})
        getattr(engine, method).assert_awaited_once_with("S1")

    @pytest.mark.asyncio
    async def test_send_new_order_coerces_and_returns_clordid(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "send_new_order",
            "data": {"session_id": "S1", "symbol": "AAPL", "side": "1",
                     "qty": "100", "price": "150.25"},
        })
        engine.send_new_order.assert_awaited_once_with(
            session_id="S1", symbol="AAPL", side="1", qty=100.0,
            ord_type="2", price=150.25, tif="0", account=None,
        )
        resp = _sent(ws)
        assert resp["ok"] is True
        assert resp["cl_ord_id"] == "MKFIX-00000001"

    @pytest.mark.asyncio
    async def test_send_new_order_market_has_no_price(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "send_new_order",
            "data": {"session_id": "S1", "symbol": "AAPL", "side": "1",
                     "qty": "100", "ord_type": "1", "price": ""},
        })
        kwargs = engine.send_new_order.await_args.kwargs
        assert kwargs["ord_type"] == "1"
        assert kwargs["price"] is None

    @pytest.mark.asyncio
    async def test_send_cancel(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "send_cancel",
            "data": {"session_id": "S1", "orig_cl_ord_id": "C1",
                     "symbol": "AAPL", "side": "1", "qty": "100.0"},
        })
        engine.send_cancel.assert_awaited_once_with(
            session_id="S1", orig_cl_ord_id="C1", symbol="AAPL", side="1", qty=100.0,
        )

    @pytest.mark.asyncio
    async def test_send_cancel_replace(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "send_cancel_replace",
            "data": {"session_id": "S1", "orig_cl_ord_id": "C1", "symbol": "AAPL",
                     "side": "1", "qty": "200", "ord_type": "2", "price": "151.00"},
        })
        engine.send_cancel_replace.assert_awaited_once_with(
            session_id="S1", orig_cl_ord_id="C1", symbol="AAPL",
            side="1", qty=200.0, ord_type="2", price=151.0,
        )

    @pytest.mark.asyncio
    async def test_accept_order(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "accept_order",
            "data": {"session_id": "S1", "cl_ord_id": "C100"},
        })
        engine.accept_order.assert_awaited_once_with(session_id="S1", cl_ord_id="C100")
        resp = _sent(ws)
        assert resp["ok"] is True
        assert resp["order_id"] == "MKFIX-O-00000001"

    @pytest.mark.asyncio
    async def test_reject_order_defaults_text(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "reject_order",
            "data": {"session_id": "S1", "cl_ord_id": "C100"},
        })
        engine.reject_order.assert_awaited_once_with(
            session_id="S1", cl_ord_id="C100", text="",
        )

    @pytest.mark.asyncio
    async def test_fill_order_coerces_and_returns_execid(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "fill_order",
            "data": {"session_id": "S1", "cl_ord_id": "C100",
                     "qty": "40", "price": "150.25"},
        })
        engine.fill_order.assert_awaited_once_with(
            session_id="S1", cl_ord_id="C100", qty=40.0, price=150.25,
        )
        resp = _sent(ws)
        assert resp["ok"] is True
        assert resp["exec_id"] == "MKFIX-E-00000001"

    @pytest.mark.asyncio
    async def test_correct_trade(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "correct_trade",
            "data": {"session_id": "S1", "exec_id": "E1",
                     "qty": "50", "price": "151.00"},
        })
        engine.correct_trade.assert_awaited_once_with(
            session_id="S1", exec_id="E1", qty=50.0, price=151.0,
        )
        assert _sent(ws)["exec_id"] == "MKFIX-E-00000002"

    @pytest.mark.asyncio
    async def test_bust_trade(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "bust_trade",
            "data": {"session_id": "S1", "exec_id": "E1"},
        })
        engine.bust_trade.assert_awaited_once_with(session_id="S1", exec_id="E1")
        assert _sent(ws)["exec_id"] == "MKFIX-E-00000003"

    @pytest.mark.asyncio
    async def test_accept_request(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "accept_request",
            "data": {"session_id": "S1", "cl_ord_id": "C1"},
        })
        engine.accept_request.assert_awaited_once_with(session_id="S1", cl_ord_id="C1")
        assert _sent(ws)["exec_id"] == "MKFIX-E-00000006"

    @pytest.mark.asyncio
    async def test_reject_request_defaults_text(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "reject_request",
            "data": {"session_id": "S1", "cl_ord_id": "C1"},
        })
        engine.reject_request.assert_awaited_once_with(
            session_id="S1", cl_ord_id="C1", text="",
        )
        assert _sent(ws)["ok"] is True

    @pytest.mark.asyncio
    async def test_accept_cancel(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "accept_cancel",
            "data": {"session_id": "S1", "cl_ord_id": "C1"},
        })
        engine.accept_cancel.assert_awaited_once_with(session_id="S1", cl_ord_id="C1")
        assert _sent(ws)["exec_id"] == "MKFIX-E-00000004"

    @pytest.mark.asyncio
    async def test_accept_replace(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "accept_replace",
            "data": {"session_id": "S1", "cl_ord_id": "C1"},
        })
        engine.accept_replace.assert_awaited_once_with(session_id="S1", cl_ord_id="C1")
        assert _sent(ws)["exec_id"] == "MKFIX-E-00000005"

    @pytest.mark.asyncio
    async def test_reject_cancel_defaults_text(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "reject_cancel",
            "data": {"session_id": "S1", "cl_ord_id": "C1"},
        })
        engine.reject_cancel.assert_awaited_once_with(
            session_id="S1", cl_ord_id="C1", text="",
        )
        assert _sent(ws)["ok"] is True

    @pytest.mark.asyncio
    async def test_reset_sequence_coerces_ints(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "reset_sequence",
            "data": {"session_id": "S1", "tx_seq_num": "5", "rx_seq_num": "7"},
        })
        engine.reset_sequence.assert_awaited_once_with(session_id="S1", tx=5, rx=7)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", [
        "start_replay", "pause_replay", "resume_replay", "stop_replay",
    ])
    async def test_replay_commands_coerce_job_id(self, command):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {"ref": "r", "op": command, "data": {"job_id": "3"}})
        getattr(engine, command).assert_awaited_once_with(3)
