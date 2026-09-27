"""The IOI, Advertisement and Allocation families: what a message of each
maps to on its table row, how their repeating groups are read and written,
and the line grammars the dialogs take a group in.

Each family is a chain of IDs, the way an order is a ClOrdID chain: an IOI
(35=6) is New/Replace/Cancel by IOITransType(28) with IOIRefID(26) naming
the one it supersedes; an Advertisement (35=7) the same by AdvTransType(5)
and AdvRefID(3); an AllocationInstruction (35=J) by AllocTransType(71) and
RefAllocID(72), and it alone is answered — by an AllocationInstructionAck
(35=P) carrying AllocStatus(87) and, refused, AllocRejCode(88). One row per
chain, the ID column holding the chain's latest ID, versioned so the chain
is the row's history. The engine (engine.py) owns the rows; this module is
the pure part.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from mkfix.fix.dictionary import FixDictionary
from mkfix.fix.message import FixMessage

# The tags each family's engine handler maps into columns (and puts back on
# the wire itself): everything else on a received message is a custom tag
# kept as the row's extra_tags, the way CONSUMED_ORDER_TAGS works.
CONSUMED_IOI_TAGS = frozenset({
    "23", "26", "28", "55", "54", "27", "44", "15", "62", "25", "130", "199", "104", "58", "60",
})
CONSUMED_ADVERT_TAGS = frozenset({
    "2", "3", "5", "55", "4", "53", "44", "15", "75", "30", "58", "60",
})
CONSUMED_ALLOC_TAGS = frozenset({
    "70", "72", "71", "626", "55", "54", "53", "6", "75", "58", "60",
    "73", "11", "37", "124", "17", "32", "31", "78", "79", "80", "366",
})
CONSUMED_ALLOC_ACK_TAGS = frozenset({"70", "75", "60", "87", "88", "58"})

# AllocStatus(87) to the row status a sent allocation takes from its Ack —
# and a received one from the Accept/Reject that answered it. The codes
# for a refusal are three (block level, account level, by an intermediary);
# the row says Rejected and alloc_status keeps which.
ALLOC_STATUS_OF = {
    "0": "Accepted", "1": "Rejected", "2": "Rejected", "3": "Received", "4": "Incomplete", "5": "Rejected",
}
ALLOC_ACCEPTING = frozenset({"0", "3", "4"})

# An allocation's three repeating groups: the counter, the member tags the
# row keeps, and the column they are kept in — as lines (see `format_lines`).
ALLOC_GROUPS = {
    "orders": ("73", ("11", "37")),
    "execs": ("124", ("17", "32", "31")),
    "allocs": ("78", ("79", "80", "366")),
}

_SPLIT_ENTRIES = re.compile(r"[\n;]+")
_SPLIT_TOKENS = re.compile(r"[,\s]+")


def group_instances(msg: FixMessage, counter: str, members: tuple[str, ...],
                    dictionary: FixDictionary) -> list[dict[str, str]]:
    """The instances of a repeating group as a parsed message carries them:
    from the counter tag on, each instance opened by ``members[0]`` and
    ended by the next one or by a tag outside the group (the dictionary's
    member list says which tags are inside). Only ``members`` are read;
    the group's other members are skipped."""
    items = msg._items()
    start = next((i for i, (t, _) in enumerate(items) if t == counter), None)
    if start is None:
        return []
    inside = set(dictionary.groups.get(counter, {}).get("members", ())) | set(members)
    instances: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for tag, value in items[start + 1:]:
        if tag == members[0]:
            current = {tag: value}
            instances.append(current)
        elif tag not in inside:
            break
        elif current is not None and tag in members:
            current[tag] = value
    return instances


def group_pairs(counter: str, members: tuple[str, ...], instances: list[dict[str, str]],
                dictionary: FixDictionary) -> list[tuple[str, str]]:
    """The ordered wire pairs of a repeating group: the counter, then each
    instance's members in the order given, blanks left out. Nothing when the
    dictionary does not define the counter (a custom dictionary may drop
    it) or there is no instance."""
    if not instances or not dictionary.defines(counter):
        return []
    pairs: list[tuple[str, str]] = [(counter, str(len(instances)))]
    for instance in instances:
        for tag in members:
            value = instance.get(tag, "")
            if value != "" and dictionary.defines(tag):
                pairs.append((tag, value))
    return pairs


def parse_lines(text: str, members: tuple[str, ...]) -> list[dict[str, str]]:
    """A group as a dialog types it: one instance per line (or `;`), its
    members in order separated by commas or spaces — `ACC1 100 10.5`,
    `RTMA00000001,OR12`. Trailing members may be left out; more tokens than
    members is an error, as is a blank first member."""
    instances: list[dict[str, str]] = []
    for entry in _SPLIT_ENTRIES.split(text or ""):
        tokens = [t for t in _SPLIT_TOKENS.split(entry.strip()) if t]
        if not tokens:
            continue
        if len(tokens) > len(members):
            raise ValueError(f"Too many values in {entry.strip()!r}: at most {len(members)} "
                             f"({', '.join(members)})")
        instances.append(dict(zip(members, tokens)))
    return instances


def format_lines(instances: list[dict[str, str]], members: tuple[str, ...]) -> str:
    """Inverse of `parse_lines`, one instance per `; `-joined entry, so the
    column reads in a cell and the Replace dialog can prefill from it."""
    entries = []
    for instance in instances:
        values = [instance.get(t, "") for t in members]
        while values and values[-1] == "":
            values.pop()
        entries.append(" ".join(values))
    return "; ".join(e for e in entries if e)


def qualifiers_of(msg: FixMessage) -> str:
    """IOIQualifier(104) values, comma-joined."""
    return ",".join(v for t, v in msg._items() if t == "104")


def parse_qualifiers(text: str) -> list[str]:
    return [q.strip() for q in (text or "").split(",") if q.strip()]


def ioi_columns(msg: FixMessage, dictionary: FixDictionary) -> dict[str, Any]:
    """The term columns of an IOI row, from a message on the wire (received,
    or a sent one previewed with its extras applied)."""
    trans, side, qlty = msg.get("28", ""), msg.get("54", ""), msg.get("25", "")
    return {
        "ioi_id": msg.get("23", ""),
        "ioi_ref_id": msg.get("26", ""),
        "ioi_trans_type": dictionary.enum_name("28", trans),
        "ioi_trans_type_code": trans,
        "symbol": msg.get("55", ""),
        "side": dictionary.enum_name("54", side),
        "side_code": side,
        "ioi_qty": msg.get("27", ""),
        "price": msg.get_float("44", 0.0),
        "currency": msg.get("15", ""),
        "valid_until": msg.get("62", ""),
        "qlty_ind": dictionary.enum_name("25", qlty),
        "qlty_ind_code": qlty,
        "natural_flag": msg.get("130", ""),
        "qualifiers": qualifiers_of(msg),
        "text": msg.get("58", ""),
        "transact_time": msg.get("60", ""),
    }


def advert_columns(msg: FixMessage, dictionary: FixDictionary) -> dict[str, Any]:
    trans, side = msg.get("5", ""), msg.get("4", "")
    return {
        "adv_id": msg.get("2", ""),
        "adv_ref_id": msg.get("3", ""),
        "adv_trans_type": dictionary.enum_name("5", trans),
        "adv_trans_type_code": trans,
        "symbol": msg.get("55", ""),
        "side": dictionary.enum_name("4", side),
        "side_code": side,
        "quantity": msg.get_float("53", 0.0),
        "price": msg.get_float("44", 0.0),
        "currency": msg.get("15", ""),
        "trade_date": msg.get("75", ""),
        "last_mkt": msg.get("30", ""),
        "text": msg.get("58", ""),
        "transact_time": msg.get("60", ""),
    }


def allocation_columns(msg: FixMessage, dictionary: FixDictionary) -> dict[str, Any]:
    """The term columns of an allocation row from an AllocationInstruction;
    the three groups as lines, NoAllocs(78) counted."""
    trans, side, kind = msg.get("71", ""), msg.get("54", ""), msg.get("626", "")
    columns: dict[str, Any] = {
        "alloc_id": msg.get("70", ""),
        "ref_alloc_id": msg.get("72", ""),
        "alloc_trans_type": dictionary.enum_name("71", trans),
        "alloc_trans_type_code": trans,
        "alloc_type": dictionary.enum_name("626", kind) if kind else "",
        "alloc_type_code": kind,
        "symbol": msg.get("55", ""),
        "side": dictionary.enum_name("54", side),
        "side_code": side,
        "quantity": msg.get_float("53", 0.0),
        "avg_price": msg.get_float("6", 0.0),
        "trade_date": msg.get("75", ""),
        "text": msg.get("58", ""),
        "transact_time": msg.get("60", ""),
    }
    for column, (counter, members) in ALLOC_GROUPS.items():
        instances = group_instances(msg, counter, members, dictionary)
        columns[column] = format_lines(instances, members)
        if column == "allocs":
            columns["num_allocs"] = len(instances)
    return columns


def ack_columns(msg: FixMessage, dictionary: FixDictionary) -> dict[str, Any]:
    """What an AllocationInstructionAck says about the allocation it answers:
    the status by name and code, the reject code by name and code, its text."""
    status, reason = msg.get("87", ""), msg.get("88", "")
    return {
        "alloc_status": dictionary.enum_name("87", status),
        "alloc_status_code": status,
        "alloc_rej_reason": dictionary.enum_name("88", reason) if reason else "",
        "alloc_rej_code": reason,
        "text": msg.get("58", ""),
    }


# ── RFQs and quotes ───────────────────────────────────────────────────
# One row per negotiation on fix_rfqs: a QuoteRequest (35=R) and the
# quotes that answer it — `origin` 'rfq' — or a chain of unsolicited
# quotes on one instrument, `origin` 'quote'. The row's quote columns are
# the quote standing now; a requote is a new version of the row, so the
# row's history is the negotiation. `direction` is who opened the chain:
# TX a request (or unsolicited quote) this engine sent, RX one it received.

RFQ_TABLE = "fix_rfqs"
CONSUMED_RFQ_TAGS = frozenset({"131", "644", "146", "55", "54", "38", "303", "537", "15", "60", "58"})
CONSUMED_QUOTE_TAGS = frozenset({
    "131", "117", "537", "55", "54", "38", "132", "133", "134", "135", "62", "15", "60", "58",
})
CONSUMED_RESPONSE_TAGS = frozenset({
    "693", "117", "694", "11", "131", "55", "54", "38", "40", "44", "132", "133", "134", "135", "60", "58",
})

# QuoteRespType(694) to the status it leaves a quote in; others by name.
RESP_STATUS_OF = {"1": "Hit", "2": "Countered", "3": "Expired", "4": "Covered", "5": "DoneAway",
                  "6": "Passed", "8": "Expired", "11": "Hit"}
# QuoteStatus(297) on a QuoteStatusReport that ends the quote.
QUOTE_STATUS_ENDS = {"1": "Canceled", "2": "Canceled", "3": "Canceled", "4": "Canceled", "5": "Rejected",
                     "6": "Canceled", "7": "Expired", "17": "Canceled"}
# The statuses in which a quote stands and can be taken, countered or passed.
LIVE_QUOTE = ("Quoted", "Countered")
# Nothing more happens to a negotiation in these.
FINAL_RFQ = ("Hit", "Passed", "Rejected", "Failed")


def _price_of(msg: FixMessage, tag: str) -> float | None:
    value = msg.get(tag, "")
    try:
        return float(value) if value != "" else None
    except ValueError:
        return None


def rfq_columns(msg: FixMessage, dictionary: FixDictionary) -> dict[str, Any]:
    """The request's columns of an RFQ row, from a QuoteRequest: the body's
    tags through 4.1, the first NoRelatedSym(146) instance's from 4.2
    (a request of several instruments keeps the first; the rest stay in
    the recorded message)."""
    instance = msg.fields
    if "146" in msg.fields:
        members = ("55", "54", "38", "303", "537", "15", "60")
        found = group_instances(msg, "146", members, dictionary)
        instance = found[0] if found else {}
    side, rtype, qtype = instance.get("54", ""), instance.get("303", ""), instance.get("537", "")
    qty = instance.get("38", "")
    return {
        "quote_req_id": msg.get("131", ""),
        "rfq_req_id": msg.get("644", ""),
        "symbol": instance.get("55", ""),
        "side": dictionary.enum_name("54", side) if side else "",
        "side_code": side,
        "order_qty": float(qty) if qty else 0.0,
        "quote_request_type": dictionary.enum_name("303", rtype) if rtype else "",
        "quote_request_type_code": rtype,
        "quote_type": dictionary.enum_name("537", qtype) if qtype else "",
        "quote_type_code": qtype,
        "currency": instance.get("15", ""),
        "text": msg.get("58", ""),
        "transact_time": instance.get("60", "") or msg.get("60", ""),
    }


def quote_columns(msg: FixMessage, dictionary: FixDictionary) -> dict[str, Any]:
    """The quote's columns of an RFQ row, from a Quote."""
    qtype = msg.get("537", "")
    return {
        "quote_id": msg.get("117", ""),
        "bid_px": _price_of(msg, "132"),
        "offer_px": _price_of(msg, "133"),
        "bid_size": _price_of(msg, "134"),
        "offer_size": _price_of(msg, "135"),
        "valid_until": msg.get("62", ""),
        "quote_type": dictionary.enum_name("537", qtype) if qtype else "",
        "quote_type_code": qtype,
        "text": msg.get("58", ""),
        "transact_time": msg.get("60", ""),
    }


def response_columns(msg: FixMessage, dictionary: FixDictionary) -> dict[str, Any]:
    """What a QuoteResponse says: its ID and type, a counter's prices."""
    kind = msg.get("694", "")
    return {
        "quote_resp_id": msg.get("693", ""),
        "quote_resp_type": dictionary.enum_name("694", kind) if kind else "",
        "quote_resp_type_code": kind,
        "bid_px": _price_of(msg, "132"),
        "offer_px": _price_of(msg, "133"),
        "bid_size": _price_of(msg, "134"),
        "offer_size": _price_of(msg, "135"),
        "text": msg.get("58", ""),
    }


def status_of_response(kind: str, dictionary: FixDictionary) -> str:
    return RESP_STATUS_OF.get(kind) or dictionary.enum_name("694", kind) or kind


def quote_side(row: dict[str, Any]) -> str:
    """Which side of the negotiation a row is on here: the client asks
    (sent RFQs, received quotes), the market quotes (received RFQs, sent
    quotes)."""
    asked = row["origin"] == "rfq"
    return "client" if asked == (row["direction"] == "TX") else "market"


def parse_stamp(value: str) -> datetime | None:
    """A FIX UTC stamp (`YYYYMMDD-HH:MM:SS[.fff…]`) as an aware datetime;
    None for anything else."""
    head, _, fraction = (value or "").partition(".")
    try:
        stamp = datetime.strptime(head, "%Y%m%d-%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    digits = "".join(c for c in fraction if c.isdigit())[:6]
    return stamp.replace(microsecond=int(digits.ljust(6, "0"))) if digits else stamp


# ── RFQ requests ──────────────────────────────────────────────────────
# An RFQRequest (35=AH, FIX 4.3+) is a quoting party asking to be sent the
# quote requests for a list of instruments: RFQReqID(644), the instruments
# in NoRelatedSym(146), SubscriptionRequestType(263) — 0 a snapshot, 1
# snapshot and updates, 2 the unsubscribe of the RFQReqID named. Nothing
# answers it: the QuoteRequests that follow, carrying the 644, are the
# answer. One row per RFQReqID on fix_rfq_requests, the market side
# sending and the client side receiving.

CONSUMED_RFQ_REQUEST_TAGS = frozenset({"644", "263", "146", "55", "303", "537"})
RFQ_REQUEST_STATUS_OF = {"0": "Snapshot", "1": "Active", "2": "Unsubscribed"}


def rfq_request_columns(msg: FixMessage, dictionary: FixDictionary) -> dict[str, Any]:
    """The columns of an RFQ request row: its instruments as `; `-joined
    symbols, the request and quote types of the first instrument (the
    dialog gives one to every instrument), the subscription type."""
    instances = group_instances(msg, "146", ("55", "303", "537"), dictionary)
    first = instances[0] if instances else {}
    rtype, qtype, sub = first.get("303", ""), first.get("537", ""), msg.get("263", "")
    return {
        "rfq_req_id": msg.get("644", ""),
        "symbols": "; ".join(i["55"] for i in instances if i.get("55")),
        "num_symbols": len(instances),
        "quote_request_type": dictionary.enum_name("303", rtype) if rtype else "",
        "quote_request_type_code": rtype,
        "quote_type": dictionary.enum_name("537", qtype) if qtype else "",
        "quote_type_code": qtype,
        "subscription_type": dictionary.enum_name("263", sub) if sub else "",
        "subscription_type_code": sub,
    }


def parse_symbols(text: str) -> list[str]:
    """Instruments as a dialog types them: one per line, or split by `;`,
    commas or spaces."""
    return [s for s in re.split(r"[\s,;]+", text or "") if s]
