"""Custom mkio service: bridges UI commands to the FIX engine."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from mkio.services.base import Service
from mkio.ws_protocol import make_result, make_error

from mkfix.fix.actions import ACTIONS

if TYPE_CHECKING:
    from aiohttp.web import WebSocketResponse
    from mkfix.fix.engine import FixEngine


# The dialogs that load and save templates: their op, the template scope,
# and the payload keys kept as the template's terms. An op's `save_as`
# names the template to keep them under — written before the send. Only an
# order template records a session: the New dialog's own field, which a
# pick fills; every other dialog acts on its row's session.
ORDER_TERMS = ("session_id", "symbol", "side", "ord_type", "qty", "price", "tif", "extra_tags", "client",
               "handl_inst", "text")
TEMPLATE_TERMS: dict[str, tuple[str, tuple[str, ...]]] = {
    "send_new_order": ("order", ORDER_TERMS),
    "send_cancel_replace": ("order", ORDER_TERMS),
    "send_cancel": ("cancel", ("text", "extra_tags")),
    "accept_request": ("accept", ("text", "extra_tags")),
    "reject_request": ("reject", ("text", "extra_tags")),
    "fill_order": ("fill", ("qty", "price", "text", "extra_tags")),
    "unsolicited_cancel": ("unsolicited", ("text", "extra_tags")),
    "restate_order": ("restate", ("qty", "price", "restate_reason", "text", "extra_tags")),
    "dk_trade": ("dk", ("dk_reason", "text", "extra_tags")),
    "correct_trade": ("correct", ("qty", "price", "text", "extra_tags")),
    "bust_trade": ("bust", ("text", "extra_tags")),
    "renotify_trade": ("renotify", ("text", "extra_tags")),
}


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

        if data.get("save_as") and command in TEMPLATE_TERMS:
            scope, keys = TEMPLATE_TERMS[command]
            await engine.save_template(
                scope, data["save_as"], **{k: data.get(k, "") for k in keys})

        if command in ACTIONS:
            return {"ok": True, **await engine.perform(command, data)}

        scenarios = engine.scenarios
        if command == "check_scenario":
            return {"ok": True, **await scenarios.check(data.get("source", ""), data.get("side", ""))}
        elif command == "save_scenario":
            return {"ok": True, **await scenarios.save(data["name"], data.get("source", ""), data.get("side", ""))}
        elif command == "delete_scenario":
            await scenarios.delete(data["name"])
            return {"ok": True}
        elif command == "scenario_vocab":
            from mkfix.scenario import vocabulary
            return {"ok": True, "vocabulary": vocabulary()}
        elif command == "list_examples":
            return {"ok": True, "examples": scenarios.examples(data.get("side", ""))}
        elif command == "get_example":
            return {"ok": True, **scenarios.example(data["name"])}
        elif command == "arm_scenario" or command == "run_scenario":
            # Two names for one thing, so neither side's button can start the
            # other's script: arming waits for orders, running sends them.
            return {"ok": True, **await scenarios.arm(
                data["name"], side="market" if command == "arm_scenario" else "client",
                session=data.get("session", ""), seed=int(data["seed"]) if data.get("seed") else None,
                speed=float(data.get("speed") or 1.0))}
        elif command == "stop_scenario":
            return {"ok": True, **await scenarios.stop_scenario(data["name"])}
        elif command == "move_run":
            await scenarios.move_run(data["run_id"], data.get("direction", "up"))
            return {"ok": True}
        elif command == "stop_run":
            await scenarios.stop_run(data["run_id"])
            return {"ok": True}
        elif command == "pause_run":
            await scenarios.pause_run(data["run_id"])
            return {"ok": True}
        elif command == "resume_run":
            await scenarios.resume_run(data["run_id"])
            return {"ok": True}
        elif command == "setup_loopback":
            return {"ok": True, **await scenarios.setup_loopback(data.get("port"))}
        elif command == "record_start":
            return {"ok": True, **scenarios.record_start(data.get("side", ""), data.get("session", ""))}
        elif command == "record_stop":
            return {"ok": True, **await scenarios.record_stop(data.get("side", ""), data.get("name", "recorded"))}
        elif command == "record_status":
            return {"ok": True, **scenarios.record_status(data.get("side", ""))}
        elif command == "run_loopback_tour":
            return {"ok": True, **await scenarios.run_tour(data.get("port"))}
        elif command == "detach_instance":
            await scenarios.detach(data["order_row"])
            return {"ok": True}

        if command == "start_session":
            await engine.start_session(data["session_id"])
            return {"ok": True}

        elif command == "stop_session":
            await engine.stop_session(data["session_id"])
            return {"ok": True}

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

        elif command == "save_dictionary":
            name = await engine.save_dictionary(
                name=data["name"],
                base_version=data.get("base_version", ""),
                doc=data.get("doc", "{}"),
            )
            return {"ok": True, "name": name}

        elif command == "delete_dictionary":
            await engine.delete_dictionary(data["name"])
            return {"ok": True}

        elif command == "get_dictionary":
            return {"ok": True, **engine.get_dictionary(data["name"])}

        elif command == "list_dictionaries":
            return {"ok": True, "dictionaries": engine.list_dictionaries()}

        else:
            raise ValueError(f"Unknown command: {command}")
