"""The order and trade actions, by name.

One table serves everything that acts on an order: the UI's ``fix_cmd``
commands and scripted macros both go through ``FixEngine.perform``, so a
script can do exactly what a button can, behind the same checks. Each entry
turns a payload of loosely typed fields — a dialog submits strings — into
the engine call and names what it returns.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from mkfix.fix.engine import FixEngine

Action = Callable[["FixEngine", dict[str, Any]], Awaitable[dict[str, Any]]]

ACTIONS: dict[str, Action] = {}

# The payload key naming the order (a ClOrdID) or trade (an ExecID) an action
# is about; send_new_order has neither until it has run.
ORDER_KEY = {
    "send_cancel": "orig_cl_ord_id", "send_cancel_replace": "orig_cl_ord_id",
    "accept_order": "cl_ord_id", "reject_order": "cl_ord_id", "fill_order": "cl_ord_id",
    "unsolicited_cancel": "cl_ord_id", "restate_order": "cl_ord_id",
    "accept_request": "cl_ord_id", "reject_request": "cl_ord_id",
    "accept_cancel": "cl_ord_id", "accept_replace": "cl_ord_id", "reject_cancel": "cl_ord_id",
}
TRADE_KEY = {"correct_trade": "exec_id", "bust_trade": "exec_id", "renotify_trade": "exec_id",
             "dk_trade": "exec_id"}


def _action(name: str) -> Callable[[Action], Action]:
    def register(fn: Action) -> Action:
        ACTIONS[name] = fn
        return fn
    return register


def _price(d: dict[str, Any]) -> float | None:
    return float(d["price"]) if d.get("price") else None


def _common(d: dict[str, Any]) -> dict[str, Any]:
    return {"extra_tags": d.get("extra_tags", ""), "text": d.get("text", "")}


@_action("send_new_order")
async def _send_new_order(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"cl_ord_id": await e.send_new_order(
        session_id=d["session_id"], symbol=d["symbol"], side=d["side"], qty=float(d["qty"]),
        ord_type=d.get("ord_type", "2"), price=_price(d), tif=d.get("tif", "0"),
        expire_time=d.get("expire_time", ""), expire_date=d.get("expire_date", ""),
        expire_precision=d.get("expire_precision", ""), client=d.get("client", ""),
        handl_inst=d.get("handl_inst") or "1", source=d.get("_source", "manual"), tag=d.get("_tag", ""),
        **_common(d))}


@_action("send_cancel")
async def _send_cancel(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"cl_ord_id": await e.send_cancel(
        session_id=d["session_id"], orig_cl_ord_id=d["orig_cl_ord_id"], symbol=d["symbol"],
        side=d["side"], qty=float(d.get("qty", 0)), client=d.get("client", ""), **_common(d))}


@_action("send_cancel_replace")
async def _send_cancel_replace(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"cl_ord_id": await e.send_cancel_replace(
        session_id=d["session_id"], orig_cl_ord_id=d["orig_cl_ord_id"], symbol=d["symbol"],
        side=d["side"], qty=float(d["qty"]), ord_type=d.get("ord_type", "2"), price=_price(d),
        tif=d.get("tif"), expire_time=d.get("expire_time", ""), expire_date=d.get("expire_date", ""),
        expire_precision=d.get("expire_precision", ""), client=d.get("client", ""),
        handl_inst=d.get("handl_inst") or "1", **_common(d))}


def _on_order(name: str, returns: str | None) -> None:
    """An action naming a received order and carrying only text and extra tags."""
    @_action(name)
    async def run(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
        value = await getattr(e, name)(session_id=d["session_id"], cl_ord_id=d["cl_ord_id"], **_common(d))
        return {returns: value} if returns else {}


_on_order("accept_order", "order_id")
_on_order("reject_order", None)
_on_order("unsolicited_cancel", "exec_id")
_on_order("accept_request", "exec_id")
_on_order("reject_request", None)
_on_order("accept_cancel", "exec_id")
_on_order("accept_replace", "exec_id")
_on_order("reject_cancel", None)


@_action("fill_order")
async def _fill_order(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"exec_id": await e.fill_order(
        session_id=d["session_id"], cl_ord_id=d["cl_ord_id"], qty=float(d["qty"]),
        price=float(d["price"]), **_common(d))}


@_action("restate_order")
async def _restate_order(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"exec_id": await e.restate_order(
        session_id=d["session_id"], cl_ord_id=d["cl_ord_id"], qty=float(d["qty"]),
        price=float(d.get("price") or 0), reason=d.get("restate_reason", ""), **_common(d))}


@_action("correct_trade")
async def _correct_trade(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"exec_id": await e.correct_trade(
        session_id=d["session_id"], exec_id=d["exec_id"], qty=float(d["qty"]),
        price=float(d["price"]), **_common(d))}


def _on_trade(name: str) -> None:
    @_action(name)
    async def run(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
        return {"exec_id": await getattr(e, name)(session_id=d["session_id"], exec_id=d["exec_id"], **_common(d))}


_on_trade("bust_trade")
_on_trade("renotify_trade")


@_action("dk_trade")
async def _dk_trade(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    await e.dk_trade(session_id=d["session_id"], exec_id=d["exec_id"],
                     reason=d.get("dk_reason", ""), **_common(d))
    return {}
