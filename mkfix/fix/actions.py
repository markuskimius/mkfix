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
# The IOI, advert and allocation actions: the table, payload key and
# direction of the row each acts on, and for the sends, the table and the
# key the result names the new row by.
SUBJECT_KEY = {
    "replace_ioi": ("fix_iois", "ioi_id", "TX"), "cancel_ioi": ("fix_iois", "ioi_id", "TX"),
    "replace_advert": ("fix_adverts", "adv_id", "TX"), "cancel_advert": ("fix_adverts", "adv_id", "TX"),
    "replace_allocation": ("fix_allocations", "alloc_id", "TX"),
    "cancel_allocation": ("fix_allocations", "alloc_id", "TX"),
    "accept_allocation": ("fix_allocations", "alloc_id", "RX"),
    "reject_allocation": ("fix_allocations", "alloc_id", "RX"),
}
CREATES = {"send_ioi": ("fix_iois", "ioi_id"), "send_advert": ("fix_adverts", "adv_id"),
           "send_allocation": ("fix_allocations", "alloc_id")}
# The actions the macro language has no verb for yet (0.63: the IOI, advert
# and allocation ops). perform() runs them and announces them like the
# rest; the recorder and the vocabulary leave them out until the language
# learns them.
UNSCRIPTED = frozenset(SUBJECT_KEY) | frozenset(CREATES)


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


# ── IOIs, adverts and allocations ─────────────────────────────────────

def _ioi_terms(d: dict[str, Any]) -> dict[str, Any]:
    return dict(symbol=d["symbol"], side=d["side"], qty=str(d.get("qty", "")), price=_price(d),
                valid_until=d.get("valid_until", ""), qlty_ind=d.get("qlty_ind", ""),
                natural_flag=d.get("natural_flag", ""), qualifiers=d.get("qualifiers", ""),
                currency=d.get("currency", ""), client=d.get("client", ""), **_common(d))


@_action("send_ioi")
async def _send_ioi(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"ioi_id": await e.send_ioi(session_id=d["session_id"], **_ioi_terms(d))}


@_action("replace_ioi")
async def _replace_ioi(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"ioi_id": await e.replace_ioi(session_id=d["session_id"], ioi_id=d["ioi_id"], **_ioi_terms(d))}


@_action("cancel_ioi")
async def _cancel_ioi(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"ioi_id": await e.cancel_ioi(session_id=d["session_id"], ioi_id=d["ioi_id"], **_common(d))}


def _advert_terms(d: dict[str, Any]) -> dict[str, Any]:
    return dict(symbol=d["symbol"], side=d["side"], qty=float(d["qty"]), price=_price(d),
                currency=d.get("currency", ""), trade_date=d.get("trade_date", ""),
                last_mkt=d.get("last_mkt", ""), client=d.get("client", ""), **_common(d))


@_action("send_advert")
async def _send_advert(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"adv_id": await e.send_advert(session_id=d["session_id"], **_advert_terms(d))}


@_action("replace_advert")
async def _replace_advert(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"adv_id": await e.replace_advert(session_id=d["session_id"], adv_id=d["adv_id"], **_advert_terms(d))}


@_action("cancel_advert")
async def _cancel_advert(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"adv_id": await e.cancel_advert(session_id=d["session_id"], adv_id=d["adv_id"], **_common(d))}


def _allocation_terms(d: dict[str, Any]) -> dict[str, Any]:
    return dict(symbol=d["symbol"], side=d["side"], qty=float(d["qty"]), avg_price=float(d.get("avg_price") or 0),
                trade_date=d.get("trade_date", ""), alloc_type=d.get("alloc_type", ""),
                orders=d.get("orders", ""), execs=d.get("execs", ""), allocs=d.get("allocs", ""),
                client=d.get("client", ""), **_common(d))


@_action("send_allocation")
async def _send_allocation(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"alloc_id": await e.send_allocation(session_id=d["session_id"], **_allocation_terms(d))}


@_action("replace_allocation")
async def _replace_allocation(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"alloc_id": await e.replace_allocation(session_id=d["session_id"], alloc_id=d["alloc_id"],
                                                   **_allocation_terms(d))}


@_action("cancel_allocation")
async def _cancel_allocation(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"alloc_id": await e.cancel_allocation(session_id=d["session_id"], alloc_id=d["alloc_id"], **_common(d))}


@_action("accept_allocation")
async def _accept_allocation(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"alloc_id": await e.accept_allocation(
        session_id=d["session_id"], alloc_id=d["alloc_id"], alloc_status=d.get("alloc_status") or "0",
        **_common(d))}


@_action("reject_allocation")
async def _reject_allocation(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"alloc_id": await e.reject_allocation(
        session_id=d["session_id"], alloc_id=d["alloc_id"], alloc_status=d.get("alloc_status") or "1",
        alloc_rej_code=d.get("alloc_rej_code", ""), **_common(d))}
