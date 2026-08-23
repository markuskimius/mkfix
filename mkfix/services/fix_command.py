"""Custom mkio service: bridges UI commands to the FIX engine."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from mkio.services.base import Service
from mkio.ws_protocol import make_result, make_error

if TYPE_CHECKING:
    from aiohttp.web import WebSocketResponse
    from mkfix.fix.engine import FixEngine


class FixCommandService(Service):
    """Receives commands from the UI via WebSocket and dispatches to the FIX engine.

    Commands are sent as transaction-style messages with an "op" field:
        {"service": "fix_cmd", "op": "start_session", "data": {"session_id": "..."}}
    """

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self._engine: FixEngine | None = None

    def set_engine(self, engine: FixEngine) -> None:
        self._engine = engine

    async def on_message(self, ws: WebSocketResponse, msg: dict[str, Any]) -> None:
        ref = msg.get("ref")
        txnid = msg.get("txnid")
        data = msg.get("data", {})
        command = msg.get("op", data.get("command", ""))

        await self.notify_monitors("in", msg)

        if not self._engine:
            await ws.send_bytes(make_error(ref, "FIX engine not initialized", txnid=txnid))
            return

        try:
            result = await self._dispatch(command, data)
            resp = make_result(ref, self.name, result, txnid=txnid)
            await ws.send_bytes(resp)
            await self.notify_monitors("out", result)
        except Exception as e:
            await ws.send_bytes(make_error(ref, str(e), txnid=txnid))

    async def _dispatch(self, command: str, data: dict[str, Any]) -> dict[str, Any]:
        engine = self._engine

        if command == "start_session":
            await engine.start_session(data["session_id"])
            return {"ok": True}

        elif command == "stop_session":
            await engine.stop_session(data["session_id"])
            return {"ok": True}

        elif command == "send_new_order":
            cl_ord_id = await engine.send_new_order(
                session_id=data["session_id"],
                symbol=data["symbol"],
                side=data["side"],
                qty=float(data["qty"]),
                ord_type=data.get("ord_type", "2"),
                price=float(data["price"]) if data.get("price") else None,
                tif=data.get("tif", "0"),
                extra_tags=data.get("extra_tags", ""),
            )
            return {"ok": True, "cl_ord_id": cl_ord_id}

        elif command == "send_cancel":
            cl_ord_id = await engine.send_cancel(
                session_id=data["session_id"],
                orig_cl_ord_id=data["orig_cl_ord_id"],
                symbol=data["symbol"],
                side=data["side"],
                qty=float(data.get("qty", 0)),
                extra_tags=data.get("extra_tags", ""),
            )
            return {"ok": True, "cl_ord_id": cl_ord_id}

        elif command == "send_cancel_replace":
            cl_ord_id = await engine.send_cancel_replace(
                session_id=data["session_id"],
                orig_cl_ord_id=data["orig_cl_ord_id"],
                symbol=data["symbol"],
                side=data["side"],
                qty=float(data["qty"]),
                ord_type=data.get("ord_type", "2"),
                price=float(data["price"]) if data.get("price") else None,
                tif=data.get("tif"),
                extra_tags=data.get("extra_tags", ""),
            )
            return {"ok": True, "cl_ord_id": cl_ord_id}

        elif command == "accept_order":
            order_id = await engine.accept_order(
                session_id=data["session_id"],
                cl_ord_id=data["cl_ord_id"],
                extra_tags=data.get("extra_tags", ""),
            )
            return {"ok": True, "order_id": order_id}

        elif command == "reject_order":
            await engine.reject_order(
                session_id=data["session_id"],
                cl_ord_id=data["cl_ord_id"],
                text=data.get("text", ""),
                extra_tags=data.get("extra_tags", ""),
            )
            return {"ok": True}

        elif command == "fill_order":
            exec_id = await engine.fill_order(
                session_id=data["session_id"],
                cl_ord_id=data["cl_ord_id"],
                qty=float(data["qty"]),
                price=float(data["price"]),
                extra_tags=data.get("extra_tags", ""),
            )
            return {"ok": True, "exec_id": exec_id}

        elif command == "accept_request":
            exec_id = await engine.accept_request(
                session_id=data["session_id"],
                cl_ord_id=data["cl_ord_id"],
                extra_tags=data.get("extra_tags", ""),
            )
            return {"ok": True, "exec_id": exec_id}

        elif command == "reject_request":
            await engine.reject_request(
                session_id=data["session_id"],
                cl_ord_id=data["cl_ord_id"],
                text=data.get("text", ""),
                extra_tags=data.get("extra_tags", ""),
            )
            return {"ok": True}

        elif command == "accept_cancel":
            exec_id = await engine.accept_cancel(
                session_id=data["session_id"],
                cl_ord_id=data["cl_ord_id"],
                extra_tags=data.get("extra_tags", ""),
            )
            return {"ok": True, "exec_id": exec_id}

        elif command == "accept_replace":
            exec_id = await engine.accept_replace(
                session_id=data["session_id"],
                cl_ord_id=data["cl_ord_id"],
                extra_tags=data.get("extra_tags", ""),
            )
            return {"ok": True, "exec_id": exec_id}

        elif command == "reject_cancel":
            await engine.reject_cancel(
                session_id=data["session_id"],
                cl_ord_id=data["cl_ord_id"],
                text=data.get("text", ""),
                extra_tags=data.get("extra_tags", ""),
            )
            return {"ok": True}

        elif command == "correct_trade":
            exec_id = await engine.correct_trade(
                session_id=data["session_id"],
                exec_id=data["exec_id"],
                qty=float(data["qty"]),
                price=float(data["price"]),
                extra_tags=data.get("extra_tags", ""),
            )
            return {"ok": True, "exec_id": exec_id}

        elif command == "bust_trade":
            exec_id = await engine.bust_trade(
                session_id=data["session_id"],
                exec_id=data["exec_id"],
                extra_tags=data.get("extra_tags", ""),
            )
            return {"ok": True, "exec_id": exec_id}

        elif command == "reset_sequence":
            await engine.reset_sequence(
                session_id=data["session_id"],
                tx=int(data.get("tx_seq_num", 1)),
                rx=int(data.get("rx_seq_num", 1)),
            )
            return {"ok": True}

        elif command == "reload_session":
            await engine.reload_session(data["session_id"])
            return {"ok": True}

        elif command == "start_replay":
            await engine.start_replay(int(data["job_id"]))
            return {"ok": True}

        elif command == "pause_replay":
            await engine.pause_replay(int(data["job_id"]))
            return {"ok": True}

        elif command == "resume_replay":
            await engine.resume_replay(int(data["job_id"]))
            return {"ok": True}

        elif command == "stop_replay":
            await engine.stop_replay(int(data["job_id"]))
            return {"ok": True}

        else:
            raise ValueError(f"Unknown command: {command}")
