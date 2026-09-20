"""The scenario language's words, in one place.

The parser reads statements, verbs and events from here, the checker reads
the terms each verb takes and the fields an expression may name, and the
editor is handed the same tables (`vocabulary()`), so completion, hover help
and the reference pages cannot drift from what the parser accepts. Every
word carries the one line of help shown for it.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Which side of an order a block plays. A scenario may hold blocks of each.
MARKET = "market"        # on order where …      — answers orders we receive
CLIENT = "client"        # run on SESSION        — sends orders and manages them
ATTACHED = "attached"    # on sent order where … — manages an order sent some other way
SIDES = (MARKET, CLIENT, ATTACHED)
_SENDING = (CLIENT, ATTACHED)

# A script is for one side of an order, and the UI keeps the two apart: a
# *market* scenario answers orders we receive (`on order`); a *client*
# scenario sends orders or minds ones sent by hand (`run`, `on sent order`).
SCENARIO_SIDES = ("client", "market")


def side_of(kind: str) -> str:
    """The scenario side a kind of block belongs to."""
    return "market" if kind == MARKET else "client"


@dataclass(frozen=True, slots=True)
class Verb:
    """An action: what a blotter button does."""
    name: str
    op: str                                   # FixEngine.perform's name for it
    sides: tuple[str, ...]
    terms: dict[str, str]                     # term as written -> payload key
    required: tuple[str, ...] = ()            # terms it cannot go without (a template may supply them)
    trade: bool = False                       # acts on a trade: takes a trade target
    doc: str = ""


@dataclass(frozen=True, slots=True)
class Event:
    name: str
    sides: tuple[str, ...]
    trade: bool = False                       # carries the trade it is about
    doc: str = ""


_TEXT = {"text": "text", "extra": "extra_tags"}
_ORDER_TERMS = {
    "symbol": "symbol", "side": "side", "qty": "qty", "type": "ord_type", "price": "price",
    "tif": "tif", "expire": "expire_time", "client": "client", "handl_inst": "handl_inst", **_TEXT,
}

VERBS: dict[str, Verb] = {v.name: v for v in (
    Verb("new", "send_new_order", (CLIENT,), _ORDER_TERMS, ("symbol", "side", "qty"),
         doc="Send a new order (NewOrderSingle). The order it creates is this block's order."),
    Verb("replace", "send_cancel_replace", _SENDING, {k: v for k, v in _ORDER_TERMS.items() if k not in ("symbol", "side")},
         doc="Ask to replace the order (OrderCancelReplaceRequest). Terms left out keep the order's last accepted value."),
    Verb("cancel", "send_cancel", _SENDING, _TEXT,
         doc="Ask to cancel the order (OrderCancelRequest)."),
    Verb("dk", "dk_trade", _SENDING, {"reason": "dk_reason", **_TEXT}, ("reason",), trade=True,
         doc="Dispute a received trade (DontKnowTrade)."),
    Verb("accept", "accept_request", (MARKET,), _TEXT,
         doc="Accept whatever is pending on the order: the new order, or a cancel or replace request."),
    Verb("reject", "reject_request", (MARKET,), _TEXT,
         doc="Reject whatever is pending: ExecutionReport Rejected for a new order, OrderCancelReject for a request."),
    Verb("fill", "fill_order", (MARKET,), {"qty": "qty", "price": "price", **_TEXT}, ("qty", "price"),
         doc="Fill the order, in part or in full (ExecutionReport with a trade)."),
    Verb("unsol cxl", "unsolicited_cancel", (MARKET,), _TEXT,
         doc="Cancel the order though nobody asked (ExecutionReport Canceled without OrigClOrdID)."),
    Verb("restate", "restate_order", (MARKET,), {"qty": "qty", "price": "price", "reason": "restate_reason", **_TEXT}, ("qty",),
         doc="Change the order's terms unasked (ExecutionReport Restated)."),
    Verb("correct", "correct_trade", (MARKET,), {"qty": "qty", "price": "price", **_TEXT}, ("qty", "price"), trade=True,
         doc="Correct a trade we sent."),
    Verb("bust", "bust_trade", (MARKET,), _TEXT, trade=True,
         doc="Bust (cancel) a trade we sent."),
    Verb("renotify", "renotify_trade", (MARKET,), _TEXT, trade=True,
         doc="Send a disputed trade's report again under a new ExecID."),
)}

EVENTS: dict[str, Event] = {e.name: e for e in (
    Event("cancel", (MARKET,), doc="The counterparty asked to cancel the order."),
    Event("replace", (MARKET,), doc="The counterparty asked to replace the order."),
    Event("dk", (MARKET,), trade=True, doc="The counterparty disputed a trade we sent (DontKnowTrade)."),
    Event("ack", _SENDING, doc="The order was accepted (ExecutionReport New)."),
    Event("pending", _SENDING, doc="A request was received but not yet decided (PendingNew, PendingCancel, PendingReplace)."),
    Event("fill", _SENDING, trade=True, doc="A fill, partial or complete."),
    Event("filled", _SENDING, trade=True, doc="The fill that completed the order."),
    Event("replaced", _SENDING, doc="A replace request was accepted."),
    Event("canceled", _SENDING, doc="The order was canceled, asked for or not."),
    Event("rejected", _SENDING, doc="The order was rejected."),
    Event("cancel rejected", _SENDING, doc="A cancel or replace request was refused (OrderCancelReject); see event.response_to and event.reason."),
    Event("restated", _SENDING, doc="The counterparty changed the order's terms unasked."),
    Event("expired", _SENDING, doc="The order expired."),
    Event("done for day", _SENDING, doc="The order is done for the day."),
    Event("corrected", _SENDING, trade=True, doc="A trade we received was corrected."),
    Event("busted", _SENDING, trade=True, doc="A trade we received was busted."),
    Event("er", _SENDING, doc="Any ExecutionReport, named or not: test event.tag['150']."),
    Event("message", SIDES, doc="Any application message about the order."),
    Event("manual", SIDES, doc="Someone acted on the order by hand; event.op names the action."),
    Event("session down", SIDES, doc="The order's session lost its connection."),
    Event("session up", SIDES, doc="The order's session is connected again."),
    Event("error", SIDES, doc="An action of this script was refused; event.text says why."),
)}

# Statement keywords, each with its form and one line of help.
STATEMENTS: dict[str, tuple[str, str]] = {
    "scenario": ("scenario NAME", "Names the scenario. First line of every script."),
    "seed": ("seed N", "Seeds RANDOM() and timing jitter, so a run repeats exactly."),
    "on error": ("on error continue", "A refused action raises an `error` event instead of failing the order's script."),
    "on order": ("on order [where EXPR]", "A block run for every received order the expression matches."),
    "on sent order": ("on sent order [where EXPR]", "A block run for every order sent some other way — by hand, or by Message Replay."),
    "run": ("run [on SESSION]", "A block that sends its own orders; starts when you press Run. Without `on SESSION` the session is chosen at Run…, so one script can run on several at once."),
    "after": ("after DURATION [± DURATION]", "Wait that long. The optional part is random jitter either way."),
    "wait": ("wait EVENT [or EVENT…] [where EXPR] [or timeout DURATION]", "Wait for an event; carry on either way."),
    "expect": ("expect EVENT [or EVENT…] [where EXPR] within DURATION [else fail 'WHY']", "Wait for an event, and fail the order's script if it does not come in time."),
    "when": ("when EVENT [or EVENT…] [and EXPR]", "From here to the end of the enclosing block, run the indented lines whenever the event happens."),
    "if": ("if EXPR", "Run the indented lines when the expression is true; `else if` and `else` may follow."),
    "else": ("else | else if EXPR", "The alternative of the `if` above it."),
    "while": ("while EXPR", "Repeat the indented lines while the expression is true."),
    "repeat": ("repeat N [at RATE | every DURATION] [with NAME = LIST]", "Run the indented lines N times — `at 10/s` paces them, `with` hands each pass the next value of a list; `n` counts from 0."),
    "let": ("let NAME = EXPR", "Name a value for the lines that follow."),
    "stop": ("stop", "End this order's script without a verdict of its own."),
    "pass": ("pass ['WHY']", "End this order's script as passed."),
    "fail": ("fail 'WHY'", "End this order's script as failed."),
    "log": ("log EXPR", "Write a value to the run's log."),
}

# Words that may stand where a trade verb needs to know which trade.
TRADE_TARGETS = {
    "last trade": "The order's most recent trade.",
    "first trade": "The order's first trade.",
    "trade where": "trade where EXPR — the first of the order's trades the expression is true for (`trade` is the candidate).",
}

# Fixed choices a term may be given as a bare word (`side: buy`) or a quoted
# name ('Price exceeds limit'); any other value is the FIX code itself. The
# codes are the dialogs' hand-listed options, which a test holds these to.
ENUMS: dict[str, dict[str, str]] = {
    "side": {"buy": "1", "sell": "2", "sell_short": "5", "sell_short_exempt": "6"},
    "type": {"market": "1", "limit": "2", "market_on_close": "5", "limit_on_close": "B", "funari": "I"},
    "tif": {"day": "0", "gtc": "1", "at_the_opening": "2", "ioc": "3", "fok": "4", "gtx": "5", "gtd": "6",
            "at_the_close": "7"},
    "handl_inst": {"automated_private": "1", "automated_public": "2", "manual": "3"},
    "dk reason": {"unknown_symbol": "A", "wrong_side": "B", "quantity_exceeds_order": "C",
                  "no_matching_order": "D", "price_exceeds_limit": "E", "calculation_difference": "F",
                  "no_matching_execution_report": "G", "other": "Z"},
    "restate reason": {"gt_corporate_action": "0", "gt_renewal": "1", "verbal_change": "2", "repricing": "3",
                       "broker_option": "4", "partial_decline": "5", "trading_halt": "6", "system_failure": "7",
                       "market_option": "8", "canceled_not_best": "9", "warehouse_recap": "10",
                       "peg_refresh": "11", "other": "99"},
}


def enum_of(verb: str, term: str) -> str | None:
    """The ENUMS key a verb's term draws its words from, or None."""
    if term == "reason":
        return {"dk": "dk reason", "restate": "restate reason"}.get(verb)
    return term if term in ENUMS else None


def enum_code(enum: str, value: Any) -> str:
    """The FIX code for a word or name of ``enum``; anything else is a code already."""
    text = str(value)
    word = "_".join(text.replace("-", " ").lower().split())
    return ENUMS[enum].get(word, text)


# -- What an expression may name ------------------------------------------------

def _columns(table: str) -> dict[str, None]:
    config = tomllib.loads((Path(__file__).parent.parent / "mkfix.toml").read_text(encoding="utf-8"))
    return {name: None for name in config["tables"][table]["columns"]}


ORDER_FIELDS = _columns("fix_orders")
TRADE_FIELDS = _columns("fix_executions")
_VERSION_FIELDS = {"_mkio_version": None, "_mkio_ref": None}

EVENT_FIELDS: dict[str, Any] = {
    "kind": None, "kinds": {"*": None}, "source": None, "request": None,
    "tag": {"*": None},               # the message's tags: event.tag['150'], event.tag.150
    "prev": ORDER_FIELDS,             # the order as it stood before this event
    "response_to": None, "reason": None,   # cancel rejected
    "text": None, "op": None,         # error, manual
}

CONTEXT_DOCS = {
    "order": "The order's row, as the blotter shows it: order.leaves_qty, order.pending_action, order.entered_qty…",
    "trade": "The trade in hand: the event's, or the one a trade target chose.",
    "trades": "Every trade of the order, oldest first.",
    "history": "Every recorded version of the order's row, oldest first.",
    "event": "What just happened: event.kind, event.tag['150'], event.request, event.prev (the order before it)…",
    "elapsed": "Seconds since this order's script started.",
    "since": "Seconds since the last event this script waited for or reacted to.",
    "n": "The pass of the innermost `repeat`, from 0.",
}


def scope_schema(names: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
    """The schema `mkio.expr.check_fields` checks a block's expressions
    against; ``names`` are the script's own `let`/`with` names."""
    return {
        "order": ORDER_FIELDS, "trade": TRADE_FIELDS, "trades": {"*": TRADE_FIELDS},
        "history": {"*": {**ORDER_FIELDS, **_VERSION_FIELDS}}, "event": EVENT_FIELDS,
        "elapsed": None, "since": None, "n": None,
        **{name: None for name in names},
    }


def match_schema() -> dict[str, Any]:
    """A block header's `where`: the order's columns stand as names themselves
    (`symbol == 'IBM'`), and `order.symbol` works too."""
    return {**ORDER_FIELDS, "order": ORDER_FIELDS}


def vocabulary() -> dict[str, Any]:
    """Everything above as plain data, for the editor and the reference pages."""
    from mkio import expr
    from . import functions  # noqa: F401  (registers the scenario library)
    env = expr.Env(extra=[functions.LIBRARY])
    return {
        "statements": {k: {"form": form, "doc": doc} for k, (form, doc) in STATEMENTS.items()},
        "verbs": {v.name: {"op": v.op, "sides": list(v.sides), "terms": list(v.terms),
                           "required": list(v.required), "trade": v.trade, "doc": v.doc} for v in VERBS.values()},
        "events": {e.name: {"sides": list(e.sides), "trade": e.trade, "doc": e.doc} for e in EVENTS.values()},
        "trade_targets": TRADE_TARGETS,
        "enums": ENUMS,
        "context": CONTEXT_DOCS,
        "fields": {"order": sorted(ORDER_FIELDS), "trade": sorted(TRADE_FIELDS),
                   "event": sorted(EVENT_FIELDS)},
        "functions": {name: {"doc": f.doc, "params": list(f.params)} for name, f in sorted(env.functions().items())},
    }
