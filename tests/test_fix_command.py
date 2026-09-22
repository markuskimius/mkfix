"""Tests for FixCommandService command dispatch and envelope handling."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from mkfix.fix.engine import FixEngine
from mkfix.fix.events import EventBus
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
    # The action table is real — it is what turns a dialog's strings into the
    # engine calls these tests assert on — over an engine of mocks.
    engine.events = EventBus()
    engine.perform = lambda op, data, source="manual": FixEngine.perform(engine, op, data, source)
    for method in (
        "start_session", "stop_session", "reload_session", "reset_sequence",
        "start_replay", "pause_replay", "resume_replay", "stop_replay", "delete_replay",
    ):
        setattr(engine, method, AsyncMock())
    engine.send_new_order = AsyncMock(return_value="RTXX00000001")
    engine.send_cancel = AsyncMock(return_value="RTXX00000002")
    engine.send_cancel_replace = AsyncMock(return_value="RTXX00000003")
    engine.accept_order = AsyncMock(return_value="ORXX00000001")
    engine.reject_order = AsyncMock()
    engine.fill_order = AsyncMock(return_value="EXXX00000001")
    engine.correct_trade = AsyncMock(return_value="EXXX00000002")
    engine.bust_trade = AsyncMock(return_value="EXXX00000003")
    engine.renotify_trade = AsyncMock(return_value="EXXX00000007")
    engine.dk_trade = AsyncMock()
    engine.accept_cancel = AsyncMock(return_value="EXXX00000004")
    engine.unsolicited_cancel = AsyncMock(return_value="EXXX00000008")
    engine.restate_order = AsyncMock(return_value="EXXX00000009")
    engine.accept_replace = AsyncMock(return_value="EXXX00000005")
    engine.reject_cancel = AsyncMock()
    engine.accept_request = AsyncMock(return_value="EXXX00000006")
    engine.reject_request = AsyncMock()
    engine.save_template = AsyncMock(return_value="tmpl")
    engine.save_dictionary = AsyncMock(return_value="MYDICT")
    engine.delete_dictionary = AsyncMock()
    engine.get_dictionary = MagicMock(return_value={
        "name": "MYDICT", "kind": "custom", "base_version": "FIX.4.2",
        "doc": {}, "dictionary": {"version": "MYDICT"},
    })
    engine.list_dictionaries = MagicMock(return_value=[
        {"name": "FIX.4.2", "kind": "standard", "base_version": ""},
    ])
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
            ord_type="2", price=150.25, tif="0", extra_tags="",
            expire_time="", expire_date="", expire_precision="", client="",
            handl_inst="1", text="", source="manual", tag="",
        )
        resp = _sent(ws)
        assert resp["ok"] is True
        assert resp["cl_ord_id"] == "RTXX00000001"

    @pytest.mark.asyncio
    async def test_extra_tags_passed_through(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "send_new_order",
            "data": {"session_id": "S1", "symbol": "AAPL", "side": "1",
                     "qty": "100", "extra_tags": "5001=X|382=2|375=A|375=B"},
        })
        kwargs = engine.send_new_order.await_args.kwargs
        assert kwargs["extra_tags"] == "5001=X|382=2|375=A|375=B"

    @pytest.mark.asyncio
    async def test_client_reaches_every_order_send(self):
        """The New and Replace dialogs' Client field, and the client Cancel
        passes as rowData, reach the engine verbatim; the engine stamps it
        on the session's client tag."""
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        base = {"session_id": "S1", "symbol": "AAPL", "side": "1", "qty": "100", "client": "ACME"}
        await svc.on_message(ws, {"ref": "r", "op": "send_new_order", "data": base})
        assert engine.send_new_order.await_args.kwargs["client"] == "ACME"
        await svc.on_message(ws, {"ref": "r", "op": "send_cancel_replace",
                                  "data": {**base, "orig_cl_ord_id": "C1"}})
        assert engine.send_cancel_replace.await_args.kwargs["client"] == "ACME"
        await svc.on_message(ws, {"ref": "r", "op": "send_cancel",
                                  "data": {**base, "orig_cl_ord_id": "C1"}})
        assert engine.send_cancel.await_args.kwargs["client"] == "ACME"

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
            extra_tags="", client="", text="",
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
            side="1", qty=200.0, ord_type="2", price=151.0, tif=None, extra_tags="",
            expire_time="", expire_date="", expire_precision="", client="",
            handl_inst="1", text="",
        )

    @pytest.mark.asyncio
    async def test_send_cancel_replace_passes_dialog_terms(self):
        """The Replace dialog submits every New-dialog field; tif and
        extra_tags must reach the engine verbatim."""
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "send_cancel_replace",
            "data": {"session_id": "S1", "orig_cl_ord_id": "C1", "symbol": "AAPL",
                     "side": "1", "qty": "200", "ord_type": "1", "price": "",
                     "tif": "3", "extra_tags": "5001=X"},
        })
        engine.send_cancel_replace.assert_awaited_once_with(
            session_id="S1", orig_cl_ord_id="C1", symbol="AAPL",
            side="1", qty=200.0, ord_type="1", price=None, tif="3", extra_tags="5001=X",
            expire_time="", expire_date="", expire_precision="", client="",
            handl_inst="1", text="",
        )

    @pytest.mark.asyncio
    async def test_send_new_order_ignores_account(self):
        """Account left the New dialog — it rides as an extra tag (1=...)."""
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "send_new_order",
            "data": {"session_id": "S1", "symbol": "AAPL", "side": "1",
                     "qty": "100", "account": "ACCT", "extra_tags": "1=ACCT"},
        })
        kwargs = engine.send_new_order.await_args.kwargs
        assert "account" not in kwargs
        assert kwargs["extra_tags"] == "1=ACCT"

    @pytest.mark.asyncio
    async def test_accept_order(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "accept_order",
            "data": {"session_id": "S1", "cl_ord_id": "C100"},
        })
        engine.accept_order.assert_awaited_once_with(
            session_id="S1", cl_ord_id="C100", extra_tags="", text="",
        )
        resp = _sent(ws)
        assert resp["ok"] is True
        assert resp["order_id"] == "ORXX00000001"

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
            session_id="S1", cl_ord_id="C100", text="", extra_tags="",
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
            session_id="S1", cl_ord_id="C100", qty=40.0, price=150.25, extra_tags="", text="",
        )
        resp = _sent(ws)
        assert resp["ok"] is True
        assert resp["exec_id"] == "EXXX00000001"

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
            session_id="S1", exec_id="E1", qty=50.0, price=151.0, extra_tags="", text="",
        )
        assert _sent(ws)["exec_id"] == "EXXX00000002"

    @pytest.mark.asyncio
    async def test_bust_trade(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "bust_trade",
            "data": {"session_id": "S1", "exec_id": "E1"},
        })
        engine.bust_trade.assert_awaited_once_with(
            session_id="S1", exec_id="E1", extra_tags="", text="",
        )
        assert _sent(ws)["exec_id"] == "EXXX00000003"

    @pytest.mark.asyncio
    async def test_renotify_trade(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "renotify_trade",
            "data": {"session_id": "S1", "exec_id": "E1", "extra_tags": "20=0|19="},
        })
        engine.renotify_trade.assert_awaited_once_with(
            session_id="S1", exec_id="E1", extra_tags="20=0|19=", text="",
        )
        assert _sent(ws)["exec_id"] == "EXXX00000007"

    @pytest.mark.asyncio
    async def test_dk_trade(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "dk_trade",
            "data": {"session_id": "S1", "exec_id": "E1", "dk_reason": "B",
                     "text": "wrong side"},
        })
        engine.dk_trade.assert_awaited_once_with(
            session_id="S1", exec_id="E1", reason="B", text="wrong side", extra_tags="",
        )
        assert _sent(ws)["ok"] is True

    @pytest.mark.asyncio
    async def test_accept_request(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "accept_request",
            "data": {"session_id": "S1", "cl_ord_id": "C1"},
        })
        engine.accept_request.assert_awaited_once_with(
            session_id="S1", cl_ord_id="C1", extra_tags="", text="",
        )
        assert _sent(ws)["exec_id"] == "EXXX00000006"

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
            session_id="S1", cl_ord_id="C1", text="", extra_tags="",
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
        engine.accept_cancel.assert_awaited_once_with(
            session_id="S1", cl_ord_id="C1", extra_tags="", text="",
        )
        assert _sent(ws)["exec_id"] == "EXXX00000004"

    @pytest.mark.asyncio
    async def test_unsolicited_cancel(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "unsolicited_cancel",
            "data": {"session_id": "S1", "cl_ord_id": "C1", "extra_tags": "378=4"},
        })
        engine.unsolicited_cancel.assert_awaited_once_with(
            session_id="S1", cl_ord_id="C1", extra_tags="378=4", text="",
        )
        assert _sent(ws)["exec_id"] == "EXXX00000008"

    @pytest.mark.asyncio
    async def test_restate_order_coerces_and_returns_execid(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "restate_order",
            "data": {"session_id": "S1", "cl_ord_id": "C1", "qty": "80", "price": "149.5",
                     "restate_reason": "3"},
        })
        engine.restate_order.assert_awaited_once_with(
            session_id="S1", cl_ord_id="C1", qty=80.0, price=149.5, reason="3",
            extra_tags="", text="",
        )
        assert _sent(ws)["exec_id"] == "EXXX00000009"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("price", ["", None])
    async def test_restate_order_blank_price_means_none(self, price):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        data = {"session_id": "S1", "cl_ord_id": "C1", "qty": "80", "price": price}
        await svc.on_message(ws, {"ref": "r", "op": "restate_order", "data": data})
        kwargs = engine.restate_order.await_args.kwargs
        assert (kwargs["price"], kwargs["reason"]) == (0.0, "")
        assert _sent(ws)["ok"] is True

    @pytest.mark.asyncio
    async def test_accept_replace(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "accept_replace",
            "data": {"session_id": "S1", "cl_ord_id": "C1"},
        })
        engine.accept_replace.assert_awaited_once_with(
            session_id="S1", cl_ord_id="C1", extra_tags="", text="",
        )
        assert _sent(ws)["exec_id"] == "EXXX00000005"

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
            session_id="S1", cl_ord_id="C1", text="", extra_tags="",
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
        "pause_replay", "resume_replay", "stop_replay", "delete_replay",
    ])
    async def test_replay_commands_coerce_job_id(self, command):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {"ref": "r", "op": command, "data": {"job_id": "3"}})
        getattr(engine, command).assert_awaited_once_with(3)

    @pytest.mark.asyncio
    async def test_start_replay_passes_the_direction_and_answers_the_count(self):
        engine = _make_engine()
        engine.start_replay = AsyncMock(return_value={"selected": 4, "direction": "A→B"})
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {"ref": "r", "op": "start_replay", "data": {"job_id": "3", "direction": "A→B"}})
        engine.start_replay.assert_awaited_once_with(3, direction="A→B")
        assert _sent(ws)["selected"] == 4

    @pytest.mark.asyncio
    async def test_load_and_configure_replay_pass_the_dialogs_fields(self):
        engine = _make_engine()
        engine.load_replay = AsyncMock(return_value={"job_id": 9, "count": 28, "name": "day"})
        engine.configure_replay = AsyncMock()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {"ref": "r", "op": "load_replay",
                                  "data": {"name": "", "file_path": "", "example": "two-sided-day"}})
        engine.load_replay.assert_awaited_once_with(name="", file_path="", example="two-sided-day")
        assert _sent(ws)["job_id"] == 9
        await svc.on_message(ws, {"ref": "r", "op": "configure_replay", "data": {
            "job_id": "9", "target_session": "Client", "speed": "2", "msg_filter": "D,G",
            "time_from": "09:00", "time_to": "", "max_gap": "5"}})
        engine.configure_replay.assert_awaited_once_with(
            9, target_session="Client", speed="2", msg_filter="D,G", time_from="09:00", time_to="", max_gap="5")


class TestSaveAsTemplate:
    """A dialog's `save_as` keeps the op's terms as a template of the op's
    scope, written before the send (the engine's write-before-send rule
    holds for a template too); without it nothing is saved."""

    @pytest.mark.asyncio
    async def test_fill_saves_its_terms_then_sends(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        order = []
        engine.save_template.side_effect = lambda *a, **k: order.append("save")
        engine.fill_order.side_effect = lambda **k: order.append("send") or "EXXX00000001"
        await svc.on_message(ws, {
            "ref": "r", "op": "fill_order",
            "data": {"session_id": "S1", "cl_ord_id": "C1", "qty": "50", "price": "150.5",
                     "extra_tags": "5001=X", "save_as": "half", "_template": "3"},
        })
        engine.save_template.assert_awaited_once_with(
            "fill", "half", qty="50", price="150.5", text="", extra_tags="5001=X")
        assert order == ["save", "send"]
        assert _sent(ws)["ok"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("op,scope,data,terms", [
        ("send_new_order", "order",
         {"session_id": "S1", "symbol": "AAPL", "side": "1", "qty": "100", "ord_type": "2",
          "price": "150.25", "tif": "0", "handl_inst": "3", "text": "work it"},
         {"session_id": "S1", "symbol": "AAPL", "side": "1", "ord_type": "2", "qty": "100",
          "price": "150.25", "tif": "0", "extra_tags": "", "client": "",
          "handl_inst": "3", "text": "work it"}),
        ("send_cancel_replace", "order",
         {"session_id": "S1", "orig_cl_ord_id": "C1", "symbol": "AAPL", "side": "1", "qty": "120",
          "ord_type": "2", "price": "151", "tif": "0"},
         {"session_id": "S1", "symbol": "AAPL", "side": "1", "ord_type": "2", "qty": "120",
          "price": "151", "tif": "0", "extra_tags": "", "client": "", "handl_inst": "", "text": ""}),
        ("send_cancel", "cancel",
         {"session_id": "S1", "orig_cl_ord_id": "C1", "symbol": "AAPL", "side": "1", "qty": "100",
          "text": "bye", "extra_tags": "5001=X"},
         {"text": "bye", "extra_tags": "5001=X"}),
        ("accept_request", "accept", {"session_id": "S1", "cl_ord_id": "C1", "extra_tags": "5001=A"},
         {"text": "", "extra_tags": "5001=A"}),
        ("reject_request", "reject", {"session_id": "S1", "cl_ord_id": "C1", "text": "busy"},
         {"text": "busy", "extra_tags": ""}),
        ("unsolicited_cancel", "unsolicited", {"session_id": "S1", "cl_ord_id": "C1", "text": "halted"},
         {"text": "halted", "extra_tags": ""}),
        ("restate_order", "restate",
         {"session_id": "S1", "cl_ord_id": "C1", "qty": "80", "price": "149.5", "restate_reason": "3"},
         {"qty": "80", "price": "149.5", "restate_reason": "3", "text": "", "extra_tags": ""}),
        ("dk_trade", "dk", {"session_id": "S1", "exec_id": "E1", "dk_reason": "B", "text": "?"},
         {"dk_reason": "B", "text": "?", "extra_tags": ""}),
        ("correct_trade", "correct", {"session_id": "S1", "exec_id": "E1", "qty": "40", "price": "149"},
         {"qty": "40", "price": "149", "text": "", "extra_tags": ""}),
        ("bust_trade", "bust", {"session_id": "S1", "exec_id": "E1", "text": "oops"},
         {"text": "oops", "extra_tags": ""}),
        ("renotify_trade", "renotify", {"session_id": "S1", "exec_id": "E1", "extra_tags": "20=0|19="},
         {"text": "", "extra_tags": "20=0|19="}),
    ])
    async def test_each_dialog_op_saves_its_own_terms(self, op, scope, data, terms):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {"ref": "r", "op": op, "data": {**data, "save_as": "t1"}})
        engine.save_template.assert_awaited_once_with(scope, "t1", **terms)
        assert _sent(ws)["ok"] is True

    @pytest.mark.asyncio
    async def test_blank_save_as_saves_nothing(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "bust_trade",
            "data": {"session_id": "S1", "exec_id": "E1", "save_as": ""},
        })
        engine.save_template.assert_not_awaited()
        engine.bust_trade.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failed_save_stops_the_send(self):
        engine = _make_engine()
        engine.save_template.side_effect = ValueError("A template needs a name")
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r", "op": "bust_trade",
            "data": {"session_id": "S1", "exec_id": "E1", "save_as": "  "},
        })
        engine.bust_trade.assert_not_awaited()
        assert _sent(ws)["type"] == "error"


class TestDictionaryCommands:
    @pytest.mark.asyncio
    async def test_save_dictionary(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r1", "op": "save_dictionary",
            "data": {"name": "MYDICT", "base_version": "FIX.4.2", "doc": "{}"},
        })
        resp = _sent(ws)
        assert resp["type"] == "result"
        assert resp["ok"] is True
        assert resp["name"] == "MYDICT"
        engine.save_dictionary.assert_awaited_once_with(
            name="MYDICT", base_version="FIX.4.2", doc="{}")

    @pytest.mark.asyncio
    async def test_delete_dictionary(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r2", "op": "delete_dictionary", "data": {"name": "MYDICT"},
        })
        resp = _sent(ws)
        assert resp["type"] == "result"
        engine.delete_dictionary.assert_awaited_once_with("MYDICT")

    @pytest.mark.asyncio
    async def test_delete_bound_dictionary_errors(self):
        engine = _make_engine()
        engine.delete_dictionary = AsyncMock(
            side_effect=ValueError("Dictionary 'MYDICT' is bound to session(s): S1"))
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r3", "op": "delete_dictionary", "data": {"name": "MYDICT"},
        })
        resp = _sent(ws)
        assert resp["type"] == "error"
        assert "bound to session" in resp["message"]

    @pytest.mark.asyncio
    async def test_get_dictionary(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {
            "ref": "r4", "op": "get_dictionary", "data": {"name": "MYDICT"},
        })
        resp = _sent(ws)
        assert resp["type"] == "result"
        assert resp["kind"] == "custom"
        assert resp["dictionary"] == {"version": "MYDICT"}
        engine.get_dictionary.assert_called_once_with("MYDICT")

    @pytest.mark.asyncio
    async def test_list_dictionaries(self):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {"ref": "r5", "op": "list_dictionaries", "data": {}})
        resp = _sent(ws)
        assert resp["type"] == "result"
        assert resp["dictionaries"][0]["name"] == "FIX.4.2"


class TestHandlingAndTextDispatch:
    """Every dialog's Text(58), and New/Replace's HandlInst(21), reach the
    engine as typed; a blank HandlInst (an old template's fill, a scripted
    call) falls back to the engine's 1."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("op,data", [
        ("send_new_order", {"session_id": "S1", "symbol": "AAPL", "side": "1", "qty": "100"}),
        ("send_cancel_replace", {"session_id": "S1", "orig_cl_ord_id": "C1", "symbol": "AAPL",
                                 "side": "1", "qty": "100"}),
    ])
    async def test_order_ops_pass_handl_inst_and_text(self, op, data):
        engine = _make_engine()
        svc = _make_service(engine)
        await svc.on_message(_make_ws(), {"ref": "r", "op": op,
                                          "data": {**data, "handl_inst": "3", "text": "work it"}})
        kwargs = getattr(engine, op).await_args.kwargs
        assert (kwargs["handl_inst"], kwargs["text"]) == ("3", "work it")
        engine = _make_engine()
        svc = _make_service(engine)
        await svc.on_message(_make_ws(), {"ref": "r", "op": op, "data": {**data, "handl_inst": ""}})
        kwargs = getattr(engine, op).await_args.kwargs
        assert (kwargs["handl_inst"], kwargs["text"]) == ("1", "")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("op,data", [
        ("send_cancel", {"session_id": "S1", "orig_cl_ord_id": "C1", "symbol": "AAPL", "side": "1"}),
        ("accept_request", {"session_id": "S1", "cl_ord_id": "C1"}),
        ("accept_order", {"session_id": "S1", "cl_ord_id": "C1"}),
        ("accept_cancel", {"session_id": "S1", "cl_ord_id": "C1"}),
        ("accept_replace", {"session_id": "S1", "cl_ord_id": "C1"}),
        ("reject_request", {"session_id": "S1", "cl_ord_id": "C1"}),
        ("fill_order", {"session_id": "S1", "cl_ord_id": "C1", "qty": "1", "price": "1"}),
        ("unsolicited_cancel", {"session_id": "S1", "cl_ord_id": "C1"}),
        ("restate_order", {"session_id": "S1", "cl_ord_id": "C1", "qty": "1", "price": "1"}),
        ("correct_trade", {"session_id": "S1", "exec_id": "E1", "qty": "1", "price": "1"}),
        ("bust_trade", {"session_id": "S1", "exec_id": "E1"}),
        ("renotify_trade", {"session_id": "S1", "exec_id": "E1"}),
    ])
    async def test_every_send_op_passes_text(self, op, data):
        engine = _make_engine()
        svc = _make_service(engine)
        ws = _make_ws()
        await svc.on_message(ws, {"ref": "r", "op": op, "data": {**data, "text": "note"}})
        assert _sent(ws).get("ok") is True
        assert getattr(engine, op).await_args.kwargs["text"] == "note"
        assert "handl_inst" not in getattr(engine, op).await_args.kwargs
