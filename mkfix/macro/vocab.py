"""The macro language's words, in one place.

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

# What a block is about — its *subject*: an order, or one of the three
# families 0.63 added. Each is a row of its table (`SUBJECT_TABLES`) with
# an ID column of its own, and a macro instance is bound to one of them.
ORDER, IOI, ADVERT, ALLOCATION = "order", "ioi", "advert", "allocation"
SUBJECTS = (ORDER, IOI, ADVERT, ALLOCATION)
SUBJECT_TABLES = {ORDER: "fix_orders", IOI: "fix_iois", ADVERT: "fix_adverts", ALLOCATION: "fix_allocations"}
SUBJECT_IDS = {ORDER: "cl_ord_id", IOI: "ioi_id", ADVERT: "adv_id", ALLOCATION: "alloc_id"}
# The verb that sends a `run` block's subject and so binds the block to it.
CREATORS = {ORDER: "new", IOI: "ioi", ADVERT: "advert", ALLOCATION: "allocate"}

# Which role a block plays for its subject. A macro may hold blocks of each.
MARKET = "market"        # on order where …      — answers what we receive
CLIENT = "client"        # run on SESSION        — sends the subject and manages it
ATTACHED = "attached"    # on sent order where … — manages one sent some other way
SIDES = (MARKET, CLIENT, ATTACHED)
_SENDING = (CLIENT, ATTACHED)

# A macro is for one side, and the UI keeps the two apart: the *market*
# side receives orders and sends IOIs, adverts and allocations; the
# *client* side sends orders and receives the other three. So `on order`
# is a market block while `on ioi` is a client one, and `run` is either by
# what it sends.
MACRO_SIDES = ("client", "market")
# A third kind of macro is for both at once: an *end-to-end* macro holds
# blocks of either side and plays them in one run — a test that sends an
# order and answers it, in one file, with one verdict. What its macros say
# to each other (`signal`, `share`) crosses the sides because it never
# leaves the run.
E2E = "end-to-end"
MACRO_KINDS = (*MACRO_SIDES, E2E)
KIND_NAMES = {"client": "Client", "market": "Market", E2E: "End-to-end"}


def side_of(kind: str, subject: str = ORDER) -> str:
    """The macro side a block belongs to, by what it is about and whether
    it receives that thing (`on …`) or sends it (`run`, `on sent …`)."""
    received = kind == MARKET
    if subject == ORDER:
        return "market" if received else "client"
    return "client" if received else "market"


# The block headers, by the kind and subject they open. `run` names its
# subject by the verb that sends it (`new`, `ioi`, `advert`, `allocate`).
BLOCK_HEADERS: dict[str, tuple[str, str | None]] = {
    "on order": (MARKET, ORDER), "on sent order": (ATTACHED, ORDER), "run": (CLIENT, None),
    # A `run` started by a signal of the run's own macros instead of by Run…: once for each.
    "on signal": (CLIENT, None),
    "on ioi": (MARKET, IOI), "on sent ioi": (ATTACHED, IOI),
    "on advert": (MARKET, ADVERT), "on sent advert": (ATTACHED, ADVERT),
    "on allocation": (MARKET, ALLOCATION), "on sent allocation": (ATTACHED, ALLOCATION),
}


PLURALS = {ORDER: "orders", IOI: "IOIs", ADVERT: "adverts", ALLOCATION: "allocations"}


def block_name(kind: str, subject: str) -> str:
    """How the checker names a block: "an `on ioi` block"."""
    if kind == CLIENT:
        return f"a `run` block that sends {PLURALS[subject]}"
    return f"an `on {'sent ' if kind == ATTACHED else ''}{subject}` block"


def _places(subjects: tuple[str, ...], kinds: tuple[str, ...]) -> frozenset[tuple[str, str]]:
    return frozenset((s, k) for s in subjects for k in kinds)


@dataclass(frozen=True, slots=True)
class Verb:
    """An action: what a blotter button does."""
    name: str
    op: str                                   # FixEngine.perform's name for it
    sides: tuple[str, ...]                    # the kinds of block it stands in, for its subject
    terms: dict[str, str]                     # term as written -> payload key
    required: tuple[str, ...] = ()            # terms it cannot go without (a template may supply them)
    trade: bool = False                       # acts on a trade: takes a trade target
    doc: str = ""
    subject: str = ORDER                      # what it acts on
    scope: str = ""                           # the template scope `using` reads
    also: frozenset[tuple[str, str]] = frozenset()   # further (subject, kind) places it may stand in

    @property
    def places(self) -> frozenset[tuple[str, str]]:
        return _places((self.subject,), self.sides) | self.also


@dataclass(frozen=True, slots=True)
class Event:
    name: str
    sides: tuple[str, ...]
    trade: bool = False                       # carries the trade it is about
    doc: str = ""
    subjects: tuple[str, ...] = (ORDER,)
    also: frozenset[tuple[str, str]] = frozenset()

    @property
    def places(self) -> frozenset[tuple[str, str]]:
        return _places(self.subjects, self.sides) | self.also


_TEXT = {"text": "text", "extra": "extra_tags"}
_ORDER_TERMS = {
    "symbol": "symbol", "side": "side", "qty": "qty", "type": "ord_type", "price": "price",
    "tif": "tif", "expire": "expire_time", "client": "client", "handl_inst": "handl_inst", **_TEXT,
}

_IOI_TERMS = {
    "symbol": "symbol", "side": "side", "qty": "qty", "price": "price", "valid": "valid_until",
    "quality": "qlty_ind", "natural": "natural_flag", "qualifiers": "qualifiers", "currency": "currency",
    "client": "client", **_TEXT,
}
_ADVERT_TERMS = {
    "symbol": "symbol", "side": "side", "qty": "qty", "price": "price", "currency": "currency",
    "trade_date": "trade_date", "last_mkt": "last_mkt", "client": "client", **_TEXT,
}
_ALLOC_TERMS = {
    "symbol": "symbol", "side": "side", "qty": "qty", "avg_price": "avg_price", "trade_date": "trade_date",
    "alloc_type": "alloc_type", "orders": "orders", "execs": "execs", "accounts": "allocs", "client": "client",
    **_TEXT,
}

VERBS: dict[str, Verb] = {v.name: v for v in (
    Verb("new", "send_new_order", (CLIENT,), _ORDER_TERMS, ("symbol", "side", "qty"), scope="order",
         # From an `on ioi` block it answers the IOI: an order nobody's macro owns, carrying the IOI's ID in tag 23.
         also=_places((IOI,), (MARKET,)),
         doc="Send a new order (NewOrderSingle). In a `run` block the order it creates is the block's order; "
             "in an `on ioi` block it answers the IOI (tag 23) and belongs to no macro."),
    Verb("replace", "send_cancel_replace", _SENDING, {k: v for k, v in _ORDER_TERMS.items() if k not in ("symbol", "side")},
         scope="order",
         doc="Ask to replace the order (OrderCancelReplaceRequest). Terms left out keep the order's last accepted value."),
    Verb("cancel", "send_cancel", _SENDING, _TEXT, scope="cancel",
         doc="Ask to cancel the order (OrderCancelRequest)."),
    Verb("dk", "dk_trade", _SENDING, {"reason": "dk_reason", **_TEXT}, ("reason",), trade=True, scope="dk",
         doc="Dispute a received trade (DontKnowTrade)."),
    Verb("accept", "accept_request", (MARKET,), _TEXT, scope="accept",
         doc="Accept whatever is pending on the order: the new order, or a cancel or replace request."),
    Verb("reject", "reject_request", (MARKET,), _TEXT, scope="reject",
         doc="Reject whatever is pending: ExecutionReport Rejected for a new order, OrderCancelReject for a request."),
    Verb("fill", "fill_order", (MARKET,), {"qty": "qty", "price": "price", **_TEXT}, ("qty", "price"), scope="fill",
         doc="Fill the order, in part or in full (ExecutionReport with a trade)."),
    Verb("unsol cxl", "unsolicited_cancel", (MARKET,), _TEXT, scope="unsolicited",
         doc="Cancel the order though nobody asked (ExecutionReport Canceled without OrigClOrdID)."),
    Verb("restate", "restate_order", (MARKET,), {"qty": "qty", "price": "price", "reason": "restate_reason", **_TEXT}, ("qty",),
         scope="restate", doc="Change the order's terms unasked (ExecutionReport Restated)."),
    Verb("correct", "correct_trade", (MARKET,), {"qty": "qty", "price": "price", **_TEXT}, ("qty", "price"), trade=True,
         scope="correct", doc="Correct a trade we sent."),
    Verb("bust", "bust_trade", (MARKET,), _TEXT, trade=True, scope="bust",
         doc="Bust (cancel) a trade we sent."),
    Verb("renotify", "renotify_trade", (MARKET,), _TEXT, trade=True, scope="renotify",
         doc="Send a disputed trade's report again under a new ExecID."),
    # IOIs, adverts and allocations: the market side sends them from a `run`
    # block (or minds ones sent by hand, `on sent …`), the client side
    # receives them (`on …`). Nothing answers an IOI or an advert; an
    # allocation is accepted or rejected.
    Verb("ioi", "send_ioi", (CLIENT,), _IOI_TERMS, ("symbol", "side", "qty"), subject=IOI, scope="ioi",
         doc="Send a new IOI (Indication of Interest). The IOI it creates is this block's IOI."),
    Verb("replace ioi", "replace_ioi", _SENDING, _IOI_TERMS, subject=IOI, scope="ioi",
         doc="Replace the IOI under a new IOIID naming the old one. Terms left out keep the IOI's values."),
    Verb("cancel ioi", "cancel_ioi", _SENDING, _TEXT, subject=IOI, scope="cancel",
         doc="Cancel the IOI under a new IOIID naming the old one."),
    Verb("advert", "send_advert", (CLIENT,), _ADVERT_TERMS, ("symbol", "side", "qty"), subject=ADVERT, scope="advert",
         doc="Send a new Advertisement. The advert it creates is this block's advert."),
    Verb("replace advert", "replace_advert", _SENDING, _ADVERT_TERMS, subject=ADVERT, scope="advert",
         doc="Replace the advert under a new AdvId naming the old one. Terms left out keep the advert's values."),
    Verb("cancel advert", "cancel_advert", _SENDING, _TEXT, subject=ADVERT, scope="cancel",
         doc="Cancel the advert under a new AdvId naming the old one."),
    Verb("allocate", "send_allocation", (CLIENT,), _ALLOC_TERMS, ("symbol", "side", "qty", "accounts"),
         subject=ALLOCATION, scope="allocation",
         doc="Send a new AllocationInstruction. The allocation it creates is this block's allocation; "
             "`accounts`, `orders` and `execs` are lines, one instance each: 'ACC1 60 10.5; ACC2 40'."),
    Verb("replace allocation", "replace_allocation", _SENDING, _ALLOC_TERMS, subject=ALLOCATION, scope="allocation",
         doc="Ask to replace the allocation under a new AllocID naming the old one; the row moves when the Ack accepts it."),
    Verb("cancel allocation", "cancel_allocation", _SENDING, _TEXT, subject=ALLOCATION, scope="cancel",
         doc="Ask to cancel the allocation under a new AllocID naming the old one."),
    Verb("accept allocation", "accept_allocation", (MARKET,), {"status": "alloc_status", **_TEXT},
         subject=ALLOCATION, scope="alloc_accept",
         doc="Accept what is pending on a received allocation — the new one, or a replace or cancel request — "
             "with an Ack of AllocStatus accepted, received or incomplete."),
    Verb("reject allocation", "reject_allocation", (MARKET,), {"status": "alloc_status", "reason": "alloc_rej_code", **_TEXT},
         subject=ALLOCATION, scope="alloc_reject",
         doc="Refuse what is pending on a received allocation with an Ack of AllocStatus block or account level "
             "reject and an AllocRejCode."),
)}

EVENTS: dict[str, Event] = {e.name: e for e in (
    Event("cancel", (MARKET,), also=_places((ALLOCATION,), (MARKET,)),
         doc="The counterparty asked to cancel the order (or, on a received allocation, the allocation)."),
    Event("replace", (MARKET,), also=_places((ALLOCATION,), (MARKET,)),
         doc="The counterparty asked to replace the order (or, on a received allocation, the allocation)."),
    Event("dk", (MARKET,), trade=True, doc="The counterparty disputed a trade we sent (DontKnowTrade)."),
    Event("ack", _SENDING, doc="The order was accepted (ExecutionReport New)."),
    Event("pending", _SENDING, doc="A request was received but not yet decided (PendingNew, PendingCancel, PendingReplace)."),
    Event("fill", _SENDING, trade=True, doc="A fill, partial or complete."),
    Event("filled", _SENDING, trade=True, doc="The fill that completed the order."),
    Event("replaced", _SENDING, also=_places((IOI, ADVERT), (MARKET,)),
         doc="A replace request was accepted — or a received IOI or advert was replaced by its sender."),
    Event("canceled", _SENDING, also=_places((IOI, ADVERT), (MARKET,)),
         doc="The order was canceled, asked for or not — or a received IOI or advert was canceled by its sender."),
    Event("rejected", _SENDING, also=_places((ALLOCATION,), _SENDING),
         doc="The order was rejected — or the allocation's Ack refused it (AllocStatus block or account level reject)."),
    Event("cancel rejected", _SENDING, doc="A cancel or replace request was refused (OrderCancelReject); see event.response_to and event.reason."),
    Event("restated", _SENDING, doc="The counterparty changed the order's terms unasked."),
    Event("expired", _SENDING, doc="The order expired."),
    Event("done for day", _SENDING, doc="The order is done for the day."),
    Event("corrected", _SENDING, trade=True, doc="A trade we received was corrected."),
    Event("busted", _SENDING, trade=True, doc="A trade we received was busted."),
    Event("er", _SENDING, doc="Any ExecutionReport, named or not: test event.tag['150']."),
    Event("accepted", _SENDING, subjects=(ALLOCATION,), doc="The allocation's Ack accepted it (AllocStatus 0)."),
    Event("received", _SENDING, subjects=(ALLOCATION,), doc="The allocation's Ack says received, not yet accepted (AllocStatus 3)."),
    Event("incomplete", _SENDING, subjects=(ALLOCATION,), doc="The allocation's Ack says incomplete (AllocStatus 4)."),
    Event("acked", _SENDING, subjects=(ALLOCATION,), doc="Any Ack of the allocation, named or not: test event.tag['87']."),
    Event("message", SIDES, subjects=SUBJECTS, doc="Any application message about the order (or IOI, advert, allocation)."),
    Event("manual", SIDES, subjects=SUBJECTS, doc="Someone acted on it by hand; event.op names the action."),
    Event("session down", SIDES, subjects=SUBJECTS, doc="Its session lost its connection."),
    Event("session up", SIDES, subjects=SUBJECTS, doc="Its session is connected again."),
    Event("error", SIDES, subjects=SUBJECTS, doc="An action of this macro was refused; event.text says why."),
    Event("signal", SIDES, subjects=SUBJECTS,
          doc="Another macro of this run said `signal`: `signal 'NAME'` is that one, `signal` alone any; "
              "event.name, event.value, and event.sender — the row of the macro that said it."),
)}

# Statement keywords, each with its form and one line of help.
STATEMENTS: dict[str, tuple[str, str]] = {
    "seed": ("seed N", "Seeds RANDOM() and timing jitter, so a run repeats exactly."),
    "on error": ("on error continue", "A refused action raises an `error` event instead of failing the order's macro."),
    "on order": ("on order [where EXPR]", "A market block run for every received order the expression matches."),
    "on sent order": ("on sent order [where EXPR]", "A client block run for every order sent some other way — by hand, or by Message Replay."),
    "run": ("run [on SESSION]", "A block that sends its own order (`new`), IOI (`ioi`), advert (`advert`) or allocation (`allocate`); starts when you press Run. Without `on SESSION` the session is chosen at Run…, so one macro can run on several at once."),
    "on ioi": ("on ioi [where EXPR]", "A client block run for every received IOI the expression matches; `new` in it answers the IOI."),
    "on sent ioi": ("on sent ioi [where EXPR]", "A market block run for every IOI sent by hand."),
    "on advert": ("on advert [where EXPR]", "A client block run for every received advert the expression matches."),
    "on sent advert": ("on sent advert [where EXPR]", "A market block run for every advert sent by hand."),
    "on allocation": ("on allocation [where EXPR]", "A client block run for every received allocation the expression matches; it accepts or rejects it."),
    "on sent allocation": ("on sent allocation [where EXPR]", "A market block run for every allocation sent by hand."),
    "after": ("after DURATION [± DURATION]", "Wait that long. The optional part is random jitter either way."),
    "wait": ("wait EVENT [or EVENT…] [where EXPR] [or timeout DURATION]", "Wait for an event; carry on either way."),
    "expect": ("expect EVENT [or EVENT…] [where EXPR] within DURATION [else fail 'WHY']", "Wait for an event, and fail the order's macro if it does not come in time."),
    "when": ("when EVENT [or EVENT…] [and EXPR]", "From here to the end of the enclosing block, run the indented lines whenever the event happens."),
    "if": ("if EXPR", "Run the indented lines when the expression is true; `else if` and `else` may follow."),
    "else": ("else | else if EXPR", "The alternative of the `if` above it."),
    "while": ("while EXPR", "Repeat the indented lines while the expression is true."),
    "repeat": ("repeat N [at RATE | every DURATION] [with NAME = LIST]", "Run the indented lines N times — `at 10/s` paces them, `with` hands each pass the next value of a list; `n` counts from 0."),
    "let": ("let NAME = EXPR", "Name a value for the lines that follow."),
    "stop": ("stop", "End this order's macro without a verdict of its own."),
    "pass": ("pass ['WHY']", "End this order's macro as passed."),
    "fail": ("fail 'WHY'", "End this order's macro as failed."),
    "log": ("log EXPR", "Write a value to the run's log."),
    "signal": ("signal 'NAME' [with EXPR]", "Tell every other macro of this run: each hears the event `signal 'NAME'`, "
                                           "with the value as event.value and this macro's row as event.sender."),
    "share": ("share NAME = EXPR", "Set a value every macro of this run reads as shared.NAME. At the top of the "
                                   "macro, before its blocks, it is the value the run starts with."),
    "on signal": ("on signal 'NAME' [where EXPR]", "A block that sends something of its own, like `run`, started once "
                                                   "for every signal of that name; `event` is the signal."),
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
    "ioi side": {"buy": "1", "sell": "2", "undisclosed": "7", "cross": "8"},
    "adv side": {"buy": "B", "sell": "S", "cross": "X", "trade": "T"},
    "quality": {"high": "H", "medium": "M", "low": "L"},
    "natural": {"yes": "Y", "no": "N"},
    "alloc type": {"calculated": "1", "preliminary": "2", "ready_to_book": "5", "warehouse": "7",
                   "request_to_intermediary": "8"},
    "alloc status": {"accepted": "0", "block_level_reject": "1", "account_level_reject": "2", "received": "3",
                     "incomplete": "4", "rejected_by_intermediary": "5"},
    "alloc reject reason": {"unknown_account": "0", "incorrect_quantity": "1", "incorrect_average_price": "2",
                            "unknown_executing_broker": "3", "commission_difference": "4", "unknown_order_id": "5",
                            "unknown_list_id": "6", "other": "7", "incorrect_allocated_quantity": "8",
                            "calculation_difference": "9", "unknown_or_stale_exec_id": "10", "mismatched_data": "11",
                            "unknown_cl_ord_id": "12", "warehouse_request_rejected": "13"},
}

# A term's words depend on the verb: `side` is an order's on `new`, an
# IOI's on `ioi`, AdvSide's on `advert`; `reason` is a DK's, a restatement's
# or an allocation reject's.
_ENUM_OF: dict[str, dict[str, str]] = {
    "new": {"side": "side", "type": "type", "tif": "tif", "handl_inst": "handl_inst"},
    "replace": {"type": "type", "tif": "tif", "handl_inst": "handl_inst"},
    "dk": {"reason": "dk reason"},
    "restate": {"reason": "restate reason"},
    "ioi": {"side": "ioi side", "quality": "quality", "natural": "natural"},
    "replace ioi": {"side": "ioi side", "quality": "quality", "natural": "natural"},
    "advert": {"side": "adv side"},
    "replace advert": {"side": "adv side"},
    "allocate": {"side": "side", "alloc_type": "alloc type"},
    "replace allocation": {"side": "side", "alloc_type": "alloc type"},
    "accept allocation": {"status": "alloc status"},
    "reject allocation": {"status": "alloc status", "reason": "alloc reject reason"},
}


def enum_of(verb: str, term: str) -> str | None:
    """The ENUMS key a verb's term draws its words from, or None."""
    return _ENUM_OF.get(verb, {}).get(term)


def enum_code(enum: str, value: Any) -> str:
    """The FIX code for a word or name of ``enum``; anything else is a code already."""
    text = str(value)
    word = "_".join(text.replace("-", " ").lower().split())
    return ENUMS[enum].get(word, text)


# -- What an expression may name ------------------------------------------------

def _columns(table: str) -> dict[str, None]:
    config = tomllib.loads((Path(__file__).parent.parent / "mkfix.toml").read_text(encoding="utf-8"))
    return {name: None for name in config["tables"][table]["columns"]}


SUBJECT_FIELDS: dict[str, dict[str, None]] = {subject: _columns(table) for subject, table in SUBJECT_TABLES.items()}
ORDER_FIELDS = SUBJECT_FIELDS[ORDER]
TRADE_FIELDS = _columns("fix_executions")
_VERSION_FIELDS = {"_mkio_version": None, "_mkio_ref": None}


def event_fields(subject: str = ORDER) -> dict[str, Any]:
    return {
        "kind": None, "kinds": {"*": None}, "source": None, "request": None,
        "tag": {"*": None},                        # the message's tags: event.tag['150'], event.tag.150
        "prev": SUBJECT_FIELDS[subject],           # the subject as it stood before this event
        "response_to": None, "reason": None,       # cancel rejected
        "text": None, "op": None,                  # error, manual
        # signal: what was said, with what, and by whom — the sender's row, whatever it is a row of
        "name": None, "value": None, "sender": {"*": None}, "subject": None, "n": None,
    }


EVENT_FIELDS: dict[str, Any] = event_fields()

CONTEXT_DOCS = {
    "order": "The order's row, as the blotter shows it: order.leaves_qty, order.pending_action, order.entered_qty…",
    "ioi": "In an IOI block, the IOI's row: ioi.ioi_id, ioi.symbol, ioi.ioi_qty, ioi.status…",
    "advert": "In an advert block, the advert's row: advert.adv_id, advert.symbol, advert.quantity…",
    "allocation": "In an allocation block, the allocation's row: allocation.alloc_id, allocation.pending_action, "
                  "allocation.allocs, allocation.alloc_status…",
    "trade": "The trade in hand: the event's, or the one a trade target chose.",
    "trades": "Every trade of the order, oldest first.",
    "history": "Every recorded version of the row, oldest first.",
    "event": "What just happened: event.kind, event.tag['150'], event.request, event.prev (the row before it)…",
    "elapsed": "Seconds since this macro started.",
    "since": "Seconds since the last event this macro waited for or reacted to.",
    "n": "The pass of the innermost `repeat`, from 0.",
    "shared": "The values the run's macros share: shared.NAME is what `share NAME = …` last set, NULL before.",
    "orders": "Every order this run's macros hold, as it stands now, oldest first.",
    "iois": "Every IOI this run's macros hold, as it stands now, oldest first.",
    "adverts": "Every advert this run's macros hold, as it stands now, oldest first.",
    "allocations": "Every allocation this run's macros hold, as it stands now, oldest first.",
}
# The names a `let` or a `with` may not take. The ones 0.68 added — `shared`
# and the run's rows — are not among them: a macro written before them that
# calls something `orders` keeps its name, which wins.
RESERVED = frozenset(CONTEXT_DOCS) - {"shared", "orders", "iois", "adverts", "allocations"}
# The run's rows of each kind, by the name an expression reads them under.
PEERS = {ORDER: "orders", IOI: "iois", ADVERT: "adverts", ALLOCATION: "allocations"}
SIGNAL = "signal"


def signal_event(name: str) -> str:
    """How `signal 'NAME'` stands in a statement's list of events: a kind
    of its own, beside the bare `signal` every signal also is."""
    return f"{SIGNAL}:{name}"


def event_text(name: str) -> str:
    """An event as the macro wrote it: `signal 'NAME'`, or the word."""
    return f"{SIGNAL} {name.split(':', 1)[1]!r}" if name.startswith(SIGNAL + ":") else name


def event_of(name: str) -> "Event | None":
    """The vocabulary's event for a name as a statement holds it."""
    return EVENTS.get(SIGNAL if name.startswith(SIGNAL + ":") else name)


def scope_schema(names: tuple[str, ...] | list[str] = (), subject: str = ORDER,
                 shared: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
    """The schema `mkio.expr.check_fields` checks a block's expressions
    against; ``names`` are the macro's own `let`/`with` names and ``shared``
    the ones its `share` lines set. The subject's row stands under its own
    name (`order`, `ioi`, `advert`, `allocation`); trades are an order's
    alone; the run's rows stand under their plurals."""
    fields = SUBJECT_FIELDS[subject]
    return {
        subject: fields, "trade": TRADE_FIELDS, "trades": {"*": TRADE_FIELDS},
        "history": {"*": {**fields, **_VERSION_FIELDS}}, "event": event_fields(subject),
        "elapsed": None, "since": None, "n": None,
        "shared": {name: None for name in shared},
        **{name: {"*": SUBJECT_FIELDS[kind]} for kind, name in PEERS.items()},
        **{name: None for name in names},
    }


def match_schema(subject: str = ORDER) -> dict[str, Any]:
    """A block header's `where`: the row's columns stand as names themselves
    (`symbol == 'IBM'`), and `order.symbol` (or `ioi.symbol`…) works too."""
    return {**SUBJECT_FIELDS[subject], subject: SUBJECT_FIELDS[subject]}


def vocabulary() -> dict[str, Any]:
    """Everything above as plain data, for the editor and the reference pages."""
    from mkio import expr
    from . import functions  # noqa: F401  (registers the macro library)
    env = expr.Env(extra=[functions.LIBRARY])
    return {
        "statements": {k: {"form": form, "doc": doc} for k, (form, doc) in STATEMENTS.items()},
        "verbs": {v.name: {"op": v.op, "sides": list(v.sides), "subject": v.subject, "scope": v.scope,
                           "places": sorted(map(list, v.places)), "terms": list(v.terms),
                           "required": list(v.required), "trade": v.trade, "doc": v.doc} for v in VERBS.values()},
        "events": {e.name: {"sides": list(e.sides), "subjects": list(e.subjects),
                            "places": sorted(map(list, e.places)), "trade": e.trade, "doc": e.doc}
                   for e in EVENTS.values()},
        "trade_targets": TRADE_TARGETS,
        "enums": ENUMS,
        "enum_of": _ENUM_OF,
        "subjects": list(SUBJECTS),
        "blocks": {header: {"kind": kind, "subject": subject,
                            "side": side_of(kind, subject) if subject else None}
                   for header, (kind, subject) in BLOCK_HEADERS.items()},
        "context": CONTEXT_DOCS,
        "fields": {**{subject: sorted(fields) for subject, fields in SUBJECT_FIELDS.items()},
                   "trade": sorted(TRADE_FIELDS), "event": sorted(EVENT_FIELDS)},
        "functions": {name: {"doc": f.doc, "params": list(f.params)} for name, f in sorted(env.functions().items())},
    }
