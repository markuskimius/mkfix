"""The order and trade actions, by name.

One table serves everything that acts on an order: the UI's ``fix_cmd``
commands and scripted macros both go through ``FixEngine.perform``, so a
script can do exactly what a button can, behind the same checks. Each entry
turns a payload of loosely typed fields — a dialog submits strings — into
the engine call and names what it returns.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, TYPE_CHECKING

from mkfix.fix.instrument import INSTRUMENT_COLS, POSITION_COLS

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
    # An RFQ row is found by side, not direction (the client's are sent
    # RFQs and received quotes): `_find_family_row` takes the side there.
    "hit_quote": ("fix_rfqs", "quote_id", "client"), "counter_quote": ("fix_rfqs", "quote_id", "client"),
    "pass_quote": ("fix_rfqs", "quote_id", "client"),
    "quote_rfq": ("fix_rfqs", "quote_req_id", "market"), "reject_rfq": ("fix_rfqs", "quote_req_id", "market"),
    "requote": ("fix_rfqs", "quote_id", "market"), "cancel_quote": ("fix_rfqs", "quote_id", "market"),
    "unsubscribe_rfq_request": ("fix_rfq_requests", "rfq_req_id", "TX"),
    "execute_list": ("fix_lists", "list_id", "TX"), "cancel_list": ("fix_lists", "list_id", "TX"),
    "request_list_status": ("fix_lists", "list_id", "TX"),
    "accept_list": ("fix_lists", "list_id", "RX"), "reject_list": ("fix_lists", "list_id", "RX"),
    "send_list_status": ("fix_lists", "list_id", "RX"), "fill_list": ("fix_lists", "list_id", "RX"),
    "cancel_list_orders": ("fix_lists", "list_id", "RX"), "add_list_order": ("fix_lists", "list_id", "TX"),
}
CREATES = {"send_ioi": ("fix_iois", "ioi_id"), "send_advert": ("fix_adverts", "adv_id"),
           "send_allocation": ("fix_allocations", "alloc_id"),
           "send_rfq": ("fix_rfqs", "quote_req_id"), "send_quote": ("fix_rfqs", "quote_id"),
           "send_rfq_request": ("fix_rfq_requests", "rfq_req_id"), "send_new_list": ("fix_lists", "list_id")}
# The actions the macro language has no verb for: none since 0.78 gave the
# lists theirs. The recorder and vocabulary tests key off it.
UNSCRIPTED: frozenset[str] = frozenset()


def _action(name: str) -> Callable[[Action], Action]:
    def register(fn: Action) -> Action:
        ACTIONS[name] = fn
        return fn
    return register


def _price(d: dict[str, Any]) -> float | None:
    return float(d["price"]) if d.get("price") else None


def _common(d: dict[str, Any]) -> dict[str, Any]:
    return {"extra_tags": d.get("extra_tags", ""), "text": d.get("text", "")}


def _instrument(d: dict[str, Any]) -> dict[str, Any]:
    """The instrument terms a payload gives (instrument.py's columns)."""
    return {c: d[c] for c in INSTRUMENT_COLS + POSITION_COLS if d.get(c) not in (None, "")}


def _family_instrument(d: dict[str, Any]) -> dict[str, Any]:
    """An IOI's, advert's, allocation's, RFQ's or quote's: the instrument alone."""
    return {c: d[c] for c in INSTRUMENT_COLS if d.get(c) not in (None, "")}


@_action("send_new_order")
async def _send_new_order(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"cl_ord_id": await e.send_new_order(
        session_id=d["session_id"], symbol=d["symbol"], side=d["side"], qty=float(d["qty"]),
        ord_type=d.get("ord_type", "2"), price=_price(d), tif=d.get("tif", "0"),
        expire_time=d.get("expire_time", ""), expire_date=d.get("expire_date", ""),
        expire_precision=d.get("expire_precision", ""), client=d.get("client", ""),
        handl_inst=d.get("handl_inst") or "1", source=d.get("_source", "manual"), tag=d.get("_tag", ""),
        instrument=_instrument(d), **_common(d))}


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
    return {"ioi_id": await e.send_ioi(session_id=d["session_id"], source=d.get("_source", "manual"),
                                   tag=d.get("_tag", ""), instrument=_family_instrument(d), **_ioi_terms(d))}


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
    return {"adv_id": await e.send_advert(session_id=d["session_id"], source=d.get("_source", "manual"),
                                   tag=d.get("_tag", ""), instrument=_family_instrument(d), **_advert_terms(d))}


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
    return {"alloc_id": await e.send_allocation(session_id=d["session_id"], source=d.get("_source", "manual"),
                                   tag=d.get("_tag", ""), instrument=_family_instrument(d), **_allocation_terms(d))}


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


# ── RFQs and quotes ───────────────────────────────────────────────────

def _opt(d: dict[str, Any], key: str) -> float | None:
    return float(d[key]) if d.get(key) not in (None, "") else None


def _quote_terms(d: dict[str, Any]) -> dict[str, Any]:
    return dict(bid_px=_opt(d, "bid_px"), offer_px=_opt(d, "offer_px"), bid_size=_opt(d, "bid_size"),
                offer_size=_opt(d, "offer_size"), valid_for=_opt(d, "valid_for"),
                valid_until=d.get("valid_until", ""), quote_type=d.get("quote_type", ""), **_common(d))


@_action("send_rfq")
async def _send_rfq(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"quote_req_id": await e.send_rfq(
        session_id=d["session_id"], symbol=d["symbol"], side=d.get("side", ""), qty=_opt(d, "qty") or 0.0,
        quote_request_type=d.get("quote_request_type", ""), quote_type=d.get("quote_type", ""),
        currency=d.get("currency", ""), client=d.get("client", ""), source=d.get("_source", "manual"),
        tag=d.get("_tag", ""), instrument=_family_instrument(d), **_common(d))}


@_action("hit_quote")
async def _hit_quote(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"cl_ord_id": await e.hit_quote(
        session_id=d["session_id"], quote_id=d["quote_id"], side=d.get("side", ""), qty=_opt(d, "qty"),
        price=_opt(d, "price"), ord_type=d.get("ord_type", ""), source=d.get("_source", "manual"),
        tag=d.get("_tag", ""), **_common(d))}


@_action("counter_quote")
async def _counter_quote(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"quote_resp_id": await e.counter_quote(
        session_id=d["session_id"], quote_id=d["quote_id"], bid_px=_opt(d, "bid_px"), offer_px=_opt(d, "offer_px"),
        bid_size=_opt(d, "bid_size"), offer_size=_opt(d, "offer_size"), **_common(d))}


@_action("pass_quote")
async def _pass_quote(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"quote_resp_id": await e.pass_quote(session_id=d["session_id"], quote_id=d["quote_id"], **_common(d))}


@_action("quote_rfq")
async def _quote_rfq(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"quote_id": await e.quote_rfq(session_id=d["session_id"], quote_req_id=d["quote_req_id"],
                                          currency=d.get("currency", ""), **_quote_terms(d))}


@_action("send_quote")
async def _send_quote(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"quote_id": await e.send_quote(
        session_id=d["session_id"], symbol=d["symbol"], side=d.get("side", ""), qty=_opt(d, "qty"),
        currency=d.get("currency", ""), client=d.get("client", ""), source=d.get("_source", "manual"),
        tag=d.get("_tag", ""), instrument=_family_instrument(d), **_quote_terms(d))}


@_action("requote")
async def _requote(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"quote_id": await e.requote(session_id=d["session_id"], quote_id=d["quote_id"], **_quote_terms(d))}


@_action("reject_rfq")
async def _reject_rfq(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    await e.reject_rfq(session_id=d["session_id"], quote_req_id=d["quote_req_id"],
                       reason=d.get("quote_rej_reason", ""), **_common(d))
    return {}


@_action("cancel_quote")
async def _cancel_quote(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    await e.cancel_quote(session_id=d["session_id"], quote_id=d["quote_id"], **_common(d))
    return {}


@_action("send_rfq_request")
async def _send_rfq_request(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"rfq_req_id": await e.send_rfq_request(
        session_id=d["session_id"], symbols=d["symbols"], subscription_type=d.get("subscription_type") or "1",
        quote_request_type=d.get("quote_request_type", ""), quote_type=d.get("quote_type", ""),
        client=d.get("client", ""), extra_tags=d.get("extra_tags", ""), source=d.get("_source", "manual"),
        tag=d.get("_tag", ""))}


@_action("unsubscribe_rfq_request")
async def _unsubscribe_rfq_request(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"rfq_req_id": await e.unsubscribe_rfq_request(
        session_id=d["session_id"], rfq_req_id=d["rfq_req_id"], extra_tags=d.get("extra_tags", ""))}


def _yes(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "y", "on")


@_action("send_new_list")
async def _send_new_list(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"list_id": await e.send_new_list(
        session_id=d["session_id"], orders=d.get("list_orders") or "", mode=d.get("mode") or "E",
        bid_type=d.get("bid_type", ""), exec_inst_type=d.get("exec_inst_type", ""),
        tot_orders=_yes(d.get("tot_orders")) or _count(d.get("tot_orders")) > 0, tot_count=_count(d.get("tot_orders")),
        client=d.get("client", ""), source=d.get("_source", "manual"), tag=d.get("_tag", ""), **_common(d))}


def _count(value: Any) -> int:
    """`tot_orders` as a count of more than one: the TotNoOrders(68) a list
    sent order by order announces before all of it has gone."""
    try:
        n = int(float(str(value)))
    except ValueError:
        return 0
    return n if n > 1 else 0


@_action("add_list_order")
async def _add_list_order(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"cl_ord_id": await e.send_new_order(
        session_id=d["session_id"], symbol=d["symbol"], side=d["side"], qty=float(d["qty"]),
        ord_type=d.get("ord_type", "2"), price=_price(d), tif=d.get("tif", "0"), client=d.get("client", ""),
        handl_inst=d.get("handl_inst") or "1", source=d.get("_source", "manual"), tag=d.get("_tag", ""),
        instrument=_instrument(d), list_id=d["list_id"], **_common(d))}


def _on_list(name: str, **defaults: str) -> None:
    """A list action naming the list, with text and extra tags and its own keys."""
    @_action(name)
    async def run(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
        more = {k: d.get(k, v) for k, v in defaults.items()}
        result = await getattr(e, name)(session_id=d["session_id"], list_id=d["list_id"], **more, **_common(d))
        return {"list_id": d["list_id"], "result": result}


_on_list("execute_list")
_on_list("cancel_list", as_orders="")
_on_list("accept_list")
_on_list("reject_list")
_on_list("send_list_status", status_type="6", list_status="")
_on_list("fill_list", price="")
_on_list("cancel_list_orders")


@_action("request_list_status")
async def _request_list_status(e: FixEngine, d: dict[str, Any]) -> dict[str, Any]:
    return {"list_id": await e.request_list_status(session_id=d["session_id"], list_id=d["list_id"], **_common(d))}
