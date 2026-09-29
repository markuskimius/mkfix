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
# 0.73: the RFQ families. An RFQ and an unsolicited quote are rows of one
# table (fix_rfqs, told apart by `origin`), an RFQ request of its own.
RFQ, QUOTE, RFQ_REQUEST = "rfq", "quote", "rfq_request"
# 0.78: a list — its own row, its orders ordinary orders (fix/lists.py).
LIST = "list"
# How long a received list's orders must have stopped coming before it is
# `received`, in macro seconds: a NewOrderList is received at once.
LIST_QUIET = 5.0
SUBJECTS = (ORDER, IOI, ADVERT, ALLOCATION, RFQ, QUOTE, RFQ_REQUEST, LIST)
SUBJECT_TABLES = {ORDER: "fix_orders", IOI: "fix_iois", ADVERT: "fix_adverts", ALLOCATION: "fix_allocations",
                  RFQ: "fix_rfqs", QUOTE: "fix_rfqs", RFQ_REQUEST: "fix_rfq_requests", LIST: "fix_lists"}
SUBJECT_IDS = {ORDER: "cl_ord_id", IOI: "ioi_id", ADVERT: "adv_id", ALLOCATION: "alloc_id",
               RFQ: "quote_req_id", QUOTE: "quote_id", RFQ_REQUEST: "rfq_req_id", LIST: "list_id"}
# The verb that sends a `run` block's subject and so binds the block to it.
CREATORS = {ORDER: "new", IOI: "ioi", ADVERT: "advert", ALLOCATION: "allocate",
            RFQ: "rfq", QUOTE: "new quote", RFQ_REQUEST: "rfq request", LIST: "new list"}
# How a subject is written: in block headers (`on rfq request`) and at the
# head of the engine's events about it (`rfq request unsubscribed`).
SUBJECT_WORDS = {**{s: s for s in SUBJECTS}, RFQ_REQUEST: "rfq request"}
# The subjects the client side sends, the way it sends orders; the market
# side sends the rest.
CLIENT_SENDS = frozenset({ORDER, RFQ, LIST})
# The tag and column a message answering a subject names it by: an order
# answering an IOI (23) or a quote (117), an RFQ answering an RFQ request (644).
ANSWER_TAGS = {("new", IOI): ("23", "ioi_id"), ("new", QUOTE): ("117", "quote_id"),
               ("new", RFQ): ("117", "quote_id"), ("rfq", RFQ_REQUEST): ("644", "rfq_req_id")}


def subject_of_row(table: str, row: dict[str, Any] | None) -> str:
    """The subject a row of ``table`` is: fix_rfqs holds two, by `origin`."""
    if table == "fix_rfqs":
        return (row or {}).get("origin") or ""
    return next((s for s, t in SUBJECT_TABLES.items() if t == table), "")

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
    if subject in CLIENT_SENDS:
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
    "on rfq": (MARKET, RFQ), "on sent rfq": (ATTACHED, RFQ),
    "on quote": (MARKET, QUOTE), "on sent quote": (ATTACHED, QUOTE),
    "on rfq request": (MARKET, RFQ_REQUEST), "on sent rfq request": (ATTACHED, RFQ_REQUEST),
    "on list": (MARKET, LIST), "on sent list": (ATTACHED, LIST),
}


PLURALS = {ORDER: "orders", IOI: "IOIs", ADVERT: "adverts", ALLOCATION: "allocations", RFQ: "RFQs",
           QUOTE: "quotes", RFQ_REQUEST: "RFQ requests", LIST: "lists"}


def block_name(kind: str, subject: str) -> str:
    """How the checker names a block: "an `on ioi` block"."""
    if kind == CLIENT:
        return f"a `run` block that sends {PLURALS[subject]}"
    return f"an `on {'sent ' if kind == ATTACHED else ''}{SUBJECT_WORDS[subject]}` block"


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
    key: str = ""                             # the payload key naming its row, read from the row's column of that name

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

# An order's instrument (mkfix/fix/instrument.py): `instrument` names one
# saved in Config › Instruments or declared at the top of the macro, the
# rest are its columns, given inline to add to or override it. A replace
# cannot change them. Open/Close and Covered are the order's, not the
# instrument's.
_INSTRUMENT_TERMS = {
    "instrument": "_instrument", "sec_type": "security_type", "maturity": "maturity", "strike": "strike_price",
    "put_call": "put_or_call", "cfi": "cfi_code", "underlying": "underlying_symbol",
    "underlying_type": "underlying_security_type", "underlying_maturity": "underlying_maturity",
    "multiplier": "multiplier", "exchange": "security_exchange", "security_id": "security_id",
    "id_source": "security_id_source",
}
_POSITION_TERMS = {"open_close": "open_close", "covered": "covered_uncovered"}
# What `instrument 'NAME' …` at the top of a macro may give: a saved
# instrument's terms, its symbol among them.
DECLARED_TERMS = {"symbol": "symbol", **{k: v for k, v in _INSTRUMENT_TERMS.items() if k != "instrument"}}

_LIST_TERMS = {"mode": "mode", "bid_type": "bid_type", "execution": "exec_inst_type", "tot_orders": "tot_orders",
               "client": "client", **_TEXT}

_IOI_TERMS = {
    "symbol": "symbol", "side": "side", "qty": "qty", "price": "price", "valid": "valid_until",
    "quality": "qlty_ind", "natural": "natural_flag", "qualifiers": "qualifiers", "currency": "currency",
    "client": "client", **_TEXT,
}
_ADVERT_TERMS = {
    "symbol": "symbol", "side": "side", "qty": "qty", "price": "price", "currency": "currency",
    "trade_date": "trade_date", "last_mkt": "last_mkt", "client": "client", **_TEXT,
}
_RFQ_TERMS = {
    "symbol": "symbol", "side": "side", "qty": "qty", "request_type": "quote_request_type",
    "quote_type": "quote_type", "currency": "currency", "client": "client", **_TEXT,
}
_PRICE_TERMS = {"bid": "bid_px", "offer": "offer_px", "bid_size": "bid_size", "offer_size": "offer_size"}
_QUOTE_TERMS = {**_PRICE_TERMS, "valid": "valid_for", "quote_type": "quote_type", **_TEXT}
_NEW_QUOTE_TERMS = {"symbol": "symbol", "side": "side", "qty": "qty", "currency": "currency", "client": "client",
                    **_QUOTE_TERMS}
_RFQ_REQUEST_TERMS = {"symbols": "symbols", "subscription": "subscription_type", "request_type": "quote_request_type",
                      "quote_type": "quote_type", "client": "client", "extra": "extra_tags"}
_ALLOC_TERMS = {
    "symbol": "symbol", "side": "side", "qty": "qty", "avg_price": "avg_price", "trade_date": "trade_date",
    "alloc_type": "alloc_type", "orders": "orders", "execs": "execs", "accounts": "allocs", "client": "client",
    **_TEXT,
}

VERBS: dict[str, Verb] = {v.name: v for v in (
    Verb("new", "send_new_order", (CLIENT,), {**_ORDER_TERMS, **_INSTRUMENT_TERMS, **_POSITION_TERMS},
         ("symbol", "side", "qty"), scope="order",
         # From an `on ioi` block it answers the IOI: an order nobody's macro owns, carrying the IOI's ID in
         # tag 23; from a quote's (or an RFQ's) it takes the quote, naming it in tag 117.
         also=_places((IOI, QUOTE), (MARKET,)) | _places((RFQ,), _SENDING),
         doc="Send a new order (NewOrderSingle). In a `run` block the order it creates is the block's order; "
             "in an `on ioi` block it answers the IOI (tag 23), in a quote's or an RFQ's it takes the quote "
             "(tag 117, any FIX version), and belongs to no macro."),
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
    Verb("ioi", "send_ioi", (CLIENT,), {**_IOI_TERMS, **_INSTRUMENT_TERMS}, ("symbol", "side", "qty"), subject=IOI, scope="ioi",
         doc="Send a new IOI (Indication of Interest). The IOI it creates is this block's IOI."),
    Verb("replace ioi", "replace_ioi", _SENDING, _IOI_TERMS, subject=IOI, scope="ioi",
         doc="Replace the IOI under a new IOIID naming the old one. Terms left out keep the IOI's values."),
    Verb("cancel ioi", "cancel_ioi", _SENDING, _TEXT, subject=IOI, scope="cancel",
         doc="Cancel the IOI under a new IOIID naming the old one."),
    Verb("advert", "send_advert", (CLIENT,), {**_ADVERT_TERMS, **_INSTRUMENT_TERMS}, ("symbol", "side", "qty"), subject=ADVERT, scope="advert",
         doc="Send a new Advertisement. The advert it creates is this block's advert."),
    Verb("replace advert", "replace_advert", _SENDING, _ADVERT_TERMS, subject=ADVERT, scope="advert",
         doc="Replace the advert under a new AdvId naming the old one. Terms left out keep the advert's values."),
    Verb("cancel advert", "cancel_advert", _SENDING, _TEXT, subject=ADVERT, scope="cancel",
         doc="Cancel the advert under a new AdvId naming the old one."),
    Verb("allocate", "send_allocation", (CLIENT,), {**_ALLOC_TERMS, **_INSTRUMENT_TERMS}, ("symbol", "side", "qty", "accounts"),
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
    # RFQs and quotes: the client sends an RFQ from a `run` block and takes,
    # counters or passes the quote that answers it — or a quote sent
    # unasked, in an `on quote` block; the market answers an RFQ in an
    # `on rfq` block and sends quotes unasked from a `run` block.
    Verb("rfq", "send_rfq", (CLIENT,), {**_RFQ_TERMS, **_INSTRUMENT_TERMS}, ("symbol",), subject=RFQ, scope="rfq",
         also=_places((RFQ_REQUEST,), (MARKET,)),
         doc="Send a QuoteRequest. In a `run` block the RFQ it creates is the block's RFQ; in an `on rfq request` "
             "block it answers the request (tag 644) and belongs to no macro."),
    Verb("hit", "hit_quote", _SENDING, {"side": "side", "qty": "qty", "price": "price", **_TEXT},
         subject=RFQ, scope="hit", key="quote_id", also=_places((QUOTE,), (MARKET,)),
         doc="Take the quote with a QuoteResponse Hit (FIX 4.4+): an order on both sides, nobody's macro's. "
             "The side defaults to the request's, the quantity to the quoted size, the price to the side taken."),
    Verb("counter", "counter_quote", _SENDING, {**_PRICE_TERMS, **_TEXT}, subject=RFQ, scope="counter",
         key="quote_id", also=_places((QUOTE,), (MARKET,)),
         doc="Counter the quote with a QuoteResponse Counter (FIX 4.4+); terms left out keep the quote's."),
    Verb("pass quote", "pass_quote", _SENDING, _TEXT, subject=RFQ, scope="pass", key="quote_id",
         also=_places((QUOTE,), (MARKET,)), doc="Decline the quote with a QuoteResponse Pass (FIX 4.4+)."),
    Verb("quote", "quote_rfq", (MARKET,), _QUOTE_TERMS, subject=RFQ, scope="quote", key="quote_req_id",
         doc="Quote the RFQ — a first quote, or a requote replacing the one standing, which answers a counter. "
             "`valid` is how long it stands; terms left out keep the standing quote's."),
    Verb("reject rfq", "reject_rfq", (MARKET,), {"reason": "quote_rej_reason", **_TEXT}, ("reason",), subject=RFQ,
         scope="quote_reject", key="quote_req_id", doc="Refuse the RFQ with a QuoteRequestReject (FIX 4.3+)."),
    Verb("new quote", "send_quote", (CLIENT,), {**_NEW_QUOTE_TERMS, **_INSTRUMENT_TERMS}, ("symbol",), subject=QUOTE, scope="new_quote",
         doc="Send a quote nobody asked for. The quote it creates is this block's quote; one already standing "
             "on the symbol is replaced by it."),
    Verb("requote", "requote", _SENDING, _QUOTE_TERMS, subject=QUOTE, scope="quote", key="quote_id",
         also=_places((RFQ,), (MARKET,)),
         doc="Replace the quote with a new QuoteID; terms left out keep the quote's. Answers a pending counter."),
    Verb("cancel quote", "cancel_quote", _SENDING, _TEXT, subject=QUOTE, scope="cancel", key="quote_id",
         also=_places((RFQ,), (MARKET,)), doc="Withdraw the quote with a QuoteCancel (FIX 4.2+)."),
    Verb("rfq request", "send_rfq_request", (CLIENT,), _RFQ_REQUEST_TERMS, ("symbols",), subject=RFQ_REQUEST,
         scope="rfq_request",
         doc="Ask to be sent the RFQs for some instruments (RFQRequest, FIX 4.3+): `symbols` one per line or "
             "split by `;`. The request it creates is this block's."),
    Verb("unsubscribe", "unsubscribe_rfq_request", _SENDING, {"extra": "extra_tags"}, subject=RFQ_REQUEST,
         scope="unsubscribe", key="rfq_req_id", doc="End the RFQ request's subscription."),
    Verb("new list", "send_new_list", (CLIENT,), _LIST_TERMS, subject=LIST, scope="list",
         doc="Send a list: the `order` lines under it (a `repeat` may hold them). `mode: list` sends one NewOrderList, "
             "`mode: orders` each order as it comes, carrying the ListID — so `after` and a paced `repeat` space them."),
    Verb("add order", "add_list_order", _SENDING, {**_ORDER_TERMS, **_INSTRUMENT_TERMS, **_POSITION_TERMS},
         ("symbol", "side", "qty"), subject=LIST, scope="order",
         doc="One more order of the list, carrying its ListID. An indented block under it is that order's own macro."),
    Verb("execute list", "execute_list", _SENDING, _TEXT, subject=LIST, scope="list_request",
         doc="Tell the counterparty to execute the list (ListExecute), for one sent to wait for it."),
    Verb("cancel list", "cancel_list", _SENDING, {"as": "as_orders", **_TEXT}, subject=LIST, scope="cancel",
         doc="Cancel the list: a ListCancelRequest (`as: list`), or a cancel for each working order (`as: orders`, "
             "the default for a list sent as orders)."),
    Verb("request list status", "request_list_status", _SENDING, _TEXT, subject=LIST, scope="list_request",
         doc="Ask for the list's status (ListStatusRequest)."),
    Verb("accept list", "accept_list", (MARKET,), _TEXT, subject=LIST, scope="accept",
         doc="Accept what the list has pending: its new orders (and a ListStatus for a NewOrderList), an execute, "
             "or a cancel."),
    Verb("reject list", "reject_list", (MARKET,), _TEXT, subject=LIST, scope="reject",
         doc="Refuse what the list has pending: its new orders rejected, or the execute or cancel refused."),
    Verb("list status", "send_list_status", (MARKET,), {"status_type": "status_type", "list_status": "list_status",
                                                        **_TEXT}, subject=LIST, scope="list_status",
         doc="Send a ListStatus unasked (Alert by default)."),
    Verb("fill all", "fill_list", (MARKET,), {"price": "price", **_TEXT}, subject=LIST, scope="list_fill",
         doc="Fill every working order of the list for what it has left, at its limit or `price`."),
    Verb("cancel list orders", "cancel_list_orders", (MARKET,), _TEXT, subject=LIST, scope="unsolicited",
         doc="Cancel every working order of the list, unasked."),
)}

EVENTS: dict[str, Event] = {e.name: e for e in (
    Event("cancel", (MARKET,), also=_places((ALLOCATION, LIST), (MARKET,)),
         doc="The counterparty asked to cancel the order (or, on a received allocation or list, that)."),
    Event("replace", (MARKET,), also=_places((ALLOCATION,), (MARKET,)),
         doc="The counterparty asked to replace the order (or, on a received allocation, the allocation)."),
    Event("dk", (MARKET,), trade=True, doc="The counterparty disputed a trade we sent (DontKnowTrade)."),
    Event("ack", _SENDING, doc="The order was accepted (ExecutionReport New)."),
    Event("pending", _SENDING, doc="A request was received but not yet decided (PendingNew, PendingCancel, PendingReplace)."),
    Event("fill", _SENDING, trade=True, doc="A fill, partial or complete."),
    Event("filled", _SENDING, trade=True, doc="The fill that completed the order."),
    Event("replaced", _SENDING, also=_places((IOI, ADVERT), (MARKET,)),
         doc="A replace request was accepted — or a received IOI or advert was replaced by its sender."),
    Event("canceled", _SENDING, also=_places((IOI, ADVERT, QUOTE), (MARKET,)) | _places((RFQ,), _SENDING),
         doc="The order was canceled, asked for or not — or a received IOI, advert or quote was canceled by its "
             "sender, or the quote on an RFQ we sent."),
    Event("rejected", _SENDING, also=_places((ALLOCATION, RFQ, LIST), _SENDING),
         doc="The order was rejected — or the allocation's Ack refused it (AllocStatus block or account level "
             "reject), or the RFQ was (QuoteRequestReject), or the list (ListStatus Reject)."),
    Event("cancel rejected", _SENDING, doc="A cancel or replace request was refused (OrderCancelReject); see event.response_to and event.reason."),
    Event("restated", _SENDING, doc="The counterparty changed the order's terms unasked."),
    Event("expired", _SENDING, also=_places((RFQ, QUOTE), SIDES),
          doc="The order expired — or the quote's ValidUntilTime passed, or its counterparty said it expired."),
    Event("done for day", _SENDING, doc="The order is done for the day."),
    Event("corrected", _SENDING, trade=True, doc="A trade we received was corrected."),
    Event("busted", _SENDING, trade=True, doc="A trade we received was busted."),
    Event("er", _SENDING, doc="Any ExecutionReport, named or not: test event.tag['150']."),
    Event("accepted", _SENDING, subjects=(ALLOCATION, LIST),
          doc="The allocation's Ack accepted it (AllocStatus 0) — or a ListStatus acknowledged the list."),
    Event("received", _SENDING, subjects=(ALLOCATION,), also=_places((LIST,), (MARKET,)),
          doc="The allocation's Ack says received, not yet accepted (AllocStatus 3). On a received list: all of it "
              "is here — a NewOrderList at once, orders carrying the ListID after 5 seconds without another."),
    Event("incomplete", _SENDING, subjects=(ALLOCATION,), doc="The allocation's Ack says incomplete (AllocStatus 4)."),
    Event("acked", _SENDING, subjects=(ALLOCATION,), doc="Any Ack of the allocation, named or not: test event.tag['87']."),
    Event("quoted", _SENDING, subjects=(RFQ,), doc="The first quote answering the RFQ arrived: rfq.bid_px, rfq.offer_px…"),
    Event("requoted", _SENDING, subjects=(RFQ,), also=_places((QUOTE,), (MARKET,)),
          doc="A new quote replaced the one standing (a counter answered, a stream ticking)."),
    Event("status", _SENDING, subjects=(RFQ, LIST), also=_places((QUOTE,), (MARKET,)),
          doc="A QuoteStatusReport about the quote: rfq.quote_status (quote.quote_status) — or any ListStatus about "
              "the list: list.status."),
    Event("executing", _SENDING, subjects=(LIST,), doc="A ListStatus says the list is executing."),
    Event("done", _SENDING, subjects=(LIST,), doc="The list's last working order finished."),
    Event("joined", (MARKET,), subjects=(LIST,), doc="Another order of the list arrived, after it was received."),
    Event("execute", (MARKET,), subjects=(LIST,), doc="The counterparty asked to execute the list (ListExecute)."),
    Event("status request", (MARKET,), subjects=(LIST,),
          doc="The counterparty asked for the list's status (answered at once, from the tables)."),
    Event("hit", (MARKET,), subjects=(RFQ,), also=_places((QUOTE,), _SENDING),
          doc="The counterparty took our quote — a QuoteResponse Hit or an order naming it; the order arrives "
              "as a received order of its own."),
    Event("countered", (MARKET,), subjects=(RFQ,), also=_places((QUOTE,), _SENDING),
          doc="The counterparty countered our quote: its prices are pending_bid_px, pending_offer_px…"),
    Event("passed", (MARKET,), subjects=(RFQ,), also=_places((QUOTE,), _SENDING),
          doc="The counterparty passed on our quote."),
    Event("response", (MARKET,), subjects=(RFQ,), also=_places((QUOTE,), _SENDING),
          doc="Any other QuoteResponse to our quote (Cover, Done Away…): test event.tag['694']."),
    Event("unsubscribed", (MARKET,), subjects=(RFQ_REQUEST,), doc="The RFQ request's sender ended its subscription."),
    Event("answered", _SENDING, subjects=(RFQ_REQUEST,),
          doc="An RFQ naming the request arrived: rfq_request.quote_requests counts them, "
              "rfq_request.last_quote_req_id is the latest."),
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
    "run": ("run [on SESSION]", "A block that sends its own order (`new`), IOI (`ioi`), advert (`advert`), allocation (`allocate`), RFQ (`rfq`), quote (`new quote`) or RFQ request (`rfq request`); starts when you press Run. Without `on SESSION` the session is chosen at Run…, so one macro can run on several at once."),
    "on ioi": ("on ioi [where EXPR]", "A client block run for every received IOI the expression matches; `new` in it answers the IOI."),
    "on sent ioi": ("on sent ioi [where EXPR]", "A market block run for every IOI sent by hand."),
    "on advert": ("on advert [where EXPR]", "A client block run for every received advert the expression matches."),
    "on sent advert": ("on sent advert [where EXPR]", "A market block run for every advert sent by hand."),
    "on allocation": ("on allocation [where EXPR]", "A client block run for every received allocation the expression matches; it accepts or rejects it."),
    "on sent allocation": ("on sent allocation [where EXPR]", "A market block run for every allocation sent by hand."),
    "on rfq": ("on rfq [where EXPR]", "A market block run for every received RFQ the expression matches; it quotes or rejects it."),
    "on sent rfq": ("on sent rfq [where EXPR]", "A client block run for every RFQ sent by hand."),
    "on quote": ("on quote [where EXPR]", "A client block run for every quote received unasked; it takes, counters or passes it."),
    "on sent quote": ("on sent quote [where EXPR]", "A market block run for every quote sent by hand."),
    "on rfq request": ("on rfq request [where EXPR]", "A client block run for every received RFQ request; `rfq` in it answers the request."),
    "on sent rfq request": ("on sent rfq request [where EXPR]", "A market block run for every RFQ request sent by hand."),
    "on list": ("on list [where EXPR]", "A market block run for every received list, once it is received: a "
                                        "NewOrderList at once, orders carrying a ListID after 5 seconds without another."),
    "on sent list": ("on sent list [where EXPR]", "A client block run for every list sent by hand."),
    "order": ("order TERMS", "One order of the `new list` above it: `new`'s terms. An indented block under it is "
                             "that order's own macro, started once it is sent."),
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
    "instrument": ("instrument 'NAME' symbol: …, sec_type: …, maturity: …",
                   "Name an instrument for `new … instrument: 'NAME'`, at the top of the macro, before its blocks. "
                   "It wins over one saved under the same name in Config › Instruments, so the macro runs on "
                   "any server."),
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
    "type": {"market": "1", "limit": "2", "market_on_close": "5", "limit_on_close": "B", "funari": "I",
             "previously_quoted": "D"},
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
    "request type": {"manual": "1", "automatic": "2"},
    "quote type": {"indicative": "0", "tradeable": "1", "restricted_tradeable": "2", "counter": "3"},
    "quote reject reason": {"unknown_symbol": "1", "exchange_closed": "2", "quote_request_exceeds_limit": "3",
                            "too_late_to_enter": "4", "invalid_price": "5", "not_authorized": "6",
                            "no_match_for_inquiry": "7", "no_market_for_instrument": "8", "no_inventory": "9",
                            "pass": "10", "other": "99"},
    "subscription": {"subscribe": "1", "snapshot": "0"},
    "hit side": {"buy": "1", "sell": "2"},
    "sec type": {"stock": "CS", "option": "OPT", "future": "FUT", "option_on_future": "OOF"},
    "put call": {"call": "1", "put": "0"},
    "open close": {"open": "O", "close": "C", "rolled": "R", "fifo": "F"},
    "covered": {"covered": "0", "uncovered": "1"},
    "id source": {"cusip": "1", "sedol": "2", "isin": "4", "ric": "5", "exchange_symbol": "8", "bloomberg_symbol": "A"},
    "list mode": {"list": "E", "orders": "D"},
    "bid type": {"no_bidding": "3", "non_disclosed": "1", "disclosed": "2"},
    "execution": {"immediate": "1", "wait": "2"},
    "tot orders": {"yes": "1"},
    "cancel as": {"list": "0", "orders": "1"},
    "status type": {"alert": "6", "ack": "1", "response": "2", "timed": "3", "exec_started": "4", "all_done": "5"},
    "list status": {"in_bidding_process": "1", "received_for_execution": "2", "executing": "3", "canceling": "4",
                    "alert": "5", "all_done": "6", "reject": "7"},
}
_INSTRUMENT_ENUMS = {"sec_type": "sec type", "underlying_type": "sec type", "put_call": "put call",
                     "id_source": "id source"}

# A term's words depend on the verb: `side` is an order's on `new`, an
# IOI's on `ioi`, AdvSide's on `advert`; `reason` is a DK's, a restatement's
# or an allocation reject's.
_ENUM_OF: dict[str, dict[str, str]] = {
    "new": {"side": "side", "type": "type", "tif": "tif", "handl_inst": "handl_inst", **_INSTRUMENT_ENUMS,
            "open_close": "open close", "covered": "covered"},
    # `instrument 'NAME' …`, the declaration at the top of a macro.
    "instrument": _INSTRUMENT_ENUMS,
    "new list": {"mode": "list mode", "bid_type": "bid type", "execution": "execution", "tot_orders": "tot orders"},
    "add order": {"side": "side", "type": "type", "tif": "tif", "handl_inst": "handl_inst", **_INSTRUMENT_ENUMS,
                  "open_close": "open close", "covered": "covered"},
    "cancel list": {"as": "cancel as"},
    "list status": {"status_type": "status type", "list_status": "list status"},
    "replace": {"type": "type", "tif": "tif", "handl_inst": "handl_inst"},
    "dk": {"reason": "dk reason"},
    "restate": {"reason": "restate reason"},
    "ioi": {"side": "ioi side", "quality": "quality", "natural": "natural", **_INSTRUMENT_ENUMS},
    "replace ioi": {"side": "ioi side", "quality": "quality", "natural": "natural"},
    "advert": {"side": "adv side", **_INSTRUMENT_ENUMS},
    "replace advert": {"side": "adv side"},
    "allocate": {"side": "side", "alloc_type": "alloc type", **_INSTRUMENT_ENUMS},
    "replace allocation": {"side": "side", "alloc_type": "alloc type"},
    "accept allocation": {"status": "alloc status"},
    "reject allocation": {"status": "alloc status", "reason": "alloc reject reason"},
    "rfq": {"side": "hit side", "request_type": "request type", "quote_type": "quote type", **_INSTRUMENT_ENUMS},
    "hit": {"side": "hit side"},
    "quote": {"quote_type": "quote type"},
    "requote": {"quote_type": "quote type"},
    "new quote": {"side": "hit side", "quote_type": "quote type", **_INSTRUMENT_ENUMS},
    "reject rfq": {"reason": "quote reject reason"},
    "rfq request": {"subscription": "subscription", "request_type": "request type", "quote_type": "quote type"},
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
    "rfq": "In an RFQ block, the negotiation's row: rfq.quote_req_id, rfq.status, and the standing quote — "
           "rfq.quote_id, rfq.bid_px, rfq.offer_px, rfq.valid_until…",
    "quote": "In a quote block, the quote's row: quote.quote_id, quote.symbol, quote.bid_px, quote.offer_px, "
             "quote.status…",
    "rfq_request": "In an RFQ request block, the request's row: rfq_request.rfq_req_id, rfq_request.symbols, "
                   "rfq_request.quote_requests…",
    "rfqs": "Every RFQ this run's macros hold, as it stands now, oldest first.",
    "quotes": "Every quote this run's macros hold, as it stands now, oldest first.",
    "rfq_requests": "Every RFQ request this run's macros hold, as it stands now, oldest first.",
    "list": "In a list block, the list's row: list.list_id, list.mode, list.status, list.order_count, "
            "list.pending_action…",
    "lists": "Every list this run's macros hold, as it stands now, oldest first.",
}
# The names a `let` or a `with` may not take. The ones 0.68 added — `shared`
# and the run's rows — are not among them: a macro written before them that
# calls something `orders` keeps its name, which wins.
RESERVED = frozenset(CONTEXT_DOCS) - {"shared", "orders", "iois", "adverts", "allocations", "rfqs", "quotes",
                                      "rfq_requests", "lists"}
# The run's rows of each kind, by the name an expression reads them under.
PEERS = {ORDER: "orders", IOI: "iois", ADVERT: "adverts", ALLOCATION: "allocations", RFQ: "rfqs", QUOTE: "quotes",
         RFQ_REQUEST: "rfq_requests", LIST: "lists"}
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
        "words": SUBJECT_WORDS,
        "blocks": {header: {"kind": kind, "subject": subject,
                            "side": side_of(kind, subject) if subject else None}
                   for header, (kind, subject) in BLOCK_HEADERS.items()},
        "context": CONTEXT_DOCS,
        "fields": {**{subject: sorted(fields) for subject, fields in SUBJECT_FIELDS.items()},
                   "trade": sorted(TRADE_FIELDS), "event": sorted(EVENT_FIELDS)},
        "functions": {name: {"doc": f.doc, "params": list(f.params)} for name, f in sorted(env.functions().items())},
    }
