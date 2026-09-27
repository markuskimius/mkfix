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
