"""What the engine tells listeners about orders and trades.

Every inbound order message and every action is announced after its
database writes have committed, so a listener that reads the order finds
what the event describes. Nothing listens until something subscribes — the
scenario runner will — and with no listener the engine skips the row reads
an event costs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from mkfix.fix.message import FixMessage

log = logging.getLogger(__name__)

# ExecType(150) — or OrdStatus(39) where a dialect has no 150 — to the event
# an ExecutionReport is for the order it reports on.
_REPORT_KINDS = {
    "0": "ack", "8": "rejected", "4": "canceled", "5": "replaced",
    "6": "pending", "E": "pending", "A": "pending",
    "C": "expired", "3": "done for day", "D": "restated",
    "1": "fill", "2": "fill", "F": "fill",
    "G": "corrected", "H": "busted",
}
# ExecTransType(20), the way FIX 4.2 and earlier cancel or correct a trade.
_TRANS_KINDS = {"1": "busted", "2": "corrected"}


def report_kinds(msg: FixMessage) -> tuple[str, ...]:
    """The events an inbound ExecutionReport is, most specific first; every
    report is also an ``er``. A fill that completes the order is ``filled``
    as well."""
    kind = _TRANS_KINDS.get(msg.get("20", "")) or _REPORT_KINDS.get(msg.get("150", "") or msg.get("39", ""))
    kinds = [kind] if kind else []
    if kind == "fill" and msg.get("39", "") == "2":
        kinds.append("filled")
    return (*kinds, "er")


@dataclass(frozen=True, slots=True)
class EngineEvent:
    """One thing that happened to an order.

    ``kinds`` names it from most to least specific (``("fill", "filled",
    "er")``); ``kind`` is the first. ``order`` is the order's row after the
    event and ``prev`` the row before it — None when there was none — so a
    listener can tell a report that moved CumQty from one that restated it.
    ``source`` is ``wire`` for what the counterparty sent, ``manual`` for an
    action from the UI and ``scenario`` for one a script took.
    """
    kinds: tuple[str, ...]
    session_id: str
    source: str = "wire"
    order: dict[str, Any] | None = None
    prev: dict[str, Any] | None = None
    trade: dict[str, Any] | None = None
    msg: FixMessage | None = None
    request: str = ""                     # the ClOrdID a request or its answer names
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return self.kinds[0]

    @property
    def order_key(self) -> int | None:
        """The order's immutable row id: what survives every ClOrdID rename."""
        return self.order["id"] if self.order else None


Listener = Callable[[EngineEvent], None]


class EventBus:
    """Synchronous fan-out. A listener must not block — it runs on the
    session's read loop or inside an action — and one that raises is logged
    and skipped: nothing a listener does can fail the engine."""

    def __init__(self) -> None:
        self._listeners: list[Listener] = []

    @property
    def active(self) -> bool:
        return bool(self._listeners)

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def emit(self, event: EngineEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                log.exception("event listener failed on %s", event.kind)
