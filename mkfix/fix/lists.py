"""Lists: a basket of orders under one ListID(66), sent two ways — one
NewOrderList (35=E) holding the orders, or the orders themselves (35=D)
each carrying the ListID — and answered order by order, with ListStatus
(35=N) speaking for the list. This is the pure part; engine.py sends and
records.

Before FIX 4.2 a NewOrderList carries one order in its body, with
ListSeqNo(67) and ListNoOrds(68); from 4.2 the orders ride in NoOrders(73),
each instance led by ClOrdID(11). A received list is every order of its
ListID on the session, however it came and whenever.
"""

from __future__ import annotations

from typing import Any

from mkfix.fix.message import FixMessage

MODES = ("E", "D")
# The member tags a ListStatus reports per order, where the version has them.
STATUS_MEMBER_TAGS = ("11", "14", "39", "151", "84", "6")
# ListOrderStatus(431) codes an answer sends.
EXECUTING, RECEIVED_FOR_EXECUTION, ALL_DONE, REJECT = "3", "2", "6", "7"
# ListStatusType(429) codes.
ACK, RESPONSE, EXEC_STARTED, ALL_DONE_TYPE = "1", "2", "4", "5"
# An order no longer working.
DONE_STATUSES = ("Filled", "Canceled", "Rejected", "Expired", "DoneForDay", "Done for day")


def list_members(msg: FixMessage) -> list[list[tuple[str, str]]]:
    """A NewOrderList's orders as the ordered pairs each carries: the
    NoOrders(73) instances, split where each ClOrdID(11) begins one, or —
    before 4.2, with no group — the body's one order."""
    items = list(msg._items())
    at = next((i for i, (tag, _) in enumerate(items) if tag == "73"), None)
    if at is None:
        return [[(t, v) for t, v in items if t not in ("8", "9", "10", "35")]]
    members: list[list[tuple[str, str]]] = []
    for tag, value in items[at + 1:]:
        if tag == "10":
            break
        if tag == "11" or not members:
            members.append([])
        members[-1].append((tag, value))
    return members


def member_message(list_msg: FixMessage, pairs: list[tuple[str, str]]) -> FixMessage:
    """One order of a received list as the NewOrderSingle it stands for: the
    header of the list's message, the order's pairs, and the ListID."""
    header = {t: v for t, v in list_msg._items() if t in ("8", "49", "56", "34", "52")}
    fields = {**header, "35": "D", **dict(pairs), "66": list_msg.get("66", "")}
    return FixMessage(fields, pairs=[*header.items(), ("35", "D"), *pairs, ("66", fields["66"])])


def member_pairs(sent: FixMessage, dictionary: Any, seq: int) -> list[tuple[str, str]]:
    """A NewOrderSingle as sent, as the pairs its NoOrders(73) instance
    carries: its body, ClOrdID first, then ListSeqNo(67)."""
    body = [(t, v) for t, v in sent._items()
            if not dictionary.is_header(t) and not dictionary.is_trailer(t) and t not in ("35", "66", "67", "68")]
    first = [p for p in body if p[0] == "11"]
    return [*first, ("67", str(seq)), *[p for p in body if p[0] != "11"]]


def status_name(dictionary: Any, code: str) -> str:
    """ListOrderStatus(431) by name; its code where the version has none."""
    return dictionary.enum_name("431", code) if code else ""


def is_done(status: str) -> bool:
    return status in DONE_STATUSES
