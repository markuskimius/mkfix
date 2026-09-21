"""Recording: work orders by hand, get the macro that would have done it.

A `Recorder` listens on the engine's event bus while it records. For every
order of its side it keeps a timeline — what the counterparty did and what
was done by hand, each with its moment — and `stop()` writes the timelines
out in the macro language, one block an order:

- an action with nothing from the counterparty since the last one becomes
  `after DELAY` and the action;
- an action that followed something from the counterparty was an answer to
  it: `wait EVENT`, `after DELAY` (the time taken to answer), the action.

What comes out is a first draft, literal about what happened: the delays
are the ones taken, the `where` is the order that was seen. It is meant to
be read and loosened — a `wait` turned into a `when`, a quantity into an
expression — and it always checks clean, so it can be run as it stands.

A market recording follows received orders (from their arrival) and a client
recording follows orders sent by hand (from their `new`); orders a macro
owns are nobody's to record.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from mkfix.fix.message import _fix_timestamp

from . import vocab

if TYPE_CHECKING:
    from mkfix.fix.engine import FixEngine
    from mkfix.fix.events import EngineEvent

# perform()'s ops by the verb that writes them; the UI's dispatchers and the
# specific ops they route to are the same verb.
_VERB_OF = {v.op: v.name for v in vocab.VERBS.values()} | {
    "accept_order": "accept", "accept_cancel": "accept", "accept_replace": "accept",
    "reject_order": "reject", "reject_cancel": "reject",
}
# What the counterparty does that a macro can wait for, per side.
_HEARD = {
    "market": ("cancel", "replace", "dk"),
    "client": tuple(e.name for e in vocab.EVENTS.values()
                    if vocab.CLIENT in e.sides and e.name not in ("er", "message", "manual", "session down",
                                                                 "session up", "error")),
}
_QUIET = 0.05          # a delay shorter than this is not worth a line


@dataclass
class _Step:
    at: float
    kind: str                      # heard | did
    name: str                      # the event, or the verb
    terms: dict[str, Any] = field(default_factory=dict)
    trade: dict[str, Any] | None = None
    stamp: str = ""


@dataclass
class _Timeline:
    order: dict[str, Any]
    started: float
    steps: list[_Step] = field(default_factory=list)


class Recorder:
    def __init__(self, engine: FixEngine, side: str, session: str = "",
                 clock: Callable[[], float] = time.monotonic) -> None:
        if side not in vocab.MACRO_SIDES:
            raise ValueError(f"A recording is of the client side or the market side, not {side!r}")
        self.engine, self.side, self.session, self.clock = engine, side, session, clock
        self.timelines: dict[int, _Timeline] = {}
        self.started = clock()
        self.started_at = _fix_timestamp()
        self._unsubscribe: Callable[[], None] | None = engine.events.subscribe(self._on_event)

    # -- listening ---------------------------------------------------------------------------

    @property
    def recording(self) -> bool:
        return self._unsubscribe is not None

    @property
    def actions(self) -> int:
        return sum(1 for t in self.timelines.values() for s in t.steps if s.kind == "did")

    def status(self) -> dict[str, Any]:
        return {"recording": self.recording, "side": self.side, "session": self.session,
                "orders": len(self.timelines), "actions": self.actions, "since": self.started_at}

    def _on_event(self, ev: EngineEvent) -> None:
        order = ev.order
        if order is None or (self.session and ev.session_id != self.session):
            return
        if order["direction"] != ("RX" if self.side == "market" else "TX") or order.get("macro"):
            return
        now, key = self.clock(), order["id"]
        line = self.timelines.get(key)
        if line is None:
            # A market order is followed from its arrival, a client order from
            # the `new` that sent it; one already under way when recording
            # began has no beginning to write down.
            born = ("order" in ev.kinds and ev.source == "wire") if self.side == "market" \
                else (ev.kinds[0] == "sent order" and ev.source == "manual")
            if born:
                self.timelines[key] = _Timeline(dict(order), now)
            return
        if ev.source == "manual" and ev.kinds[0] == "action":
            verb = _VERB_OF.get(ev.detail.get("op", ""))
            if verb and verb != "new":
                # The trade as it stood: a correction is named by the terms it found, not the ones it left.
                line.steps.append(_Step(now, "did", verb, self._terms(verb, ev),
                                        ev.detail.get("trade_before") or ev.trade, _fix_timestamp()))
        elif ev.source == "wire":
            heard = "filled" if "filled" in ev.kinds else ev.kinds[0]
            if heard in _HEARD[self.side]:
                line.steps.append(_Step(now, "heard", heard))

    @staticmethod
    def _terms(verb: str, ev: EngineEvent) -> dict[str, Any]:
        data, prev = ev.detail.get("data") or {}, ev.prev or {}
        terms = {term: data.get(key) for term, key in vocab.VERBS[verb].terms.items()
                 if data.get(key) not in (None, "")}
        if verb == "replace":
            # Only what the request changed: the rest keeps the order's values, as the language has it.
            same = {"qty": prev.get("order_qty"), "price": prev.get("price"), "type": prev.get("ord_type_code"),
                    "tif": prev.get("tif_code"), "client": prev.get("client"), "handl_inst": prev.get("handl_inst_code"),
                    "expire": prev.get("expire_time") or prev.get("expire_date")}
            changed = {k: v for k, v in terms.items() if k in ("text", "extra") or not _equal(v, same.get(k))}
            terms = changed if any(k not in ("text", "extra") for k in changed) else {"qty": terms.get("qty"), **changed}
        if verb == "new" or verb == "replace":
            if terms.get("handl_inst") == "1":
                terms.pop("handl_inst")
        return terms

    # -- writing -----------------------------------------------------------------------------

    async def stop(self, name: str = "recorded") -> dict[str, Any]:
        """Stop listening and write what was recorded: `{source, orders, actions}`."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        lines = [line for line in self.timelines.values() if self.side == "client" or line.steps]
        sessions = sorted({line.order["session_id"] for line in lines})
        head = [f"# Recorded {self.started_at[:4]}-{self.started_at[4:6]}-{self.started_at[6:8]} "
                f"{self.started_at[9:17]} UTC" + (f" on {', '.join(sessions)}" if sessions else "")
                + f": {len(lines)} order{'s' if len(lines) != 1 else ''}, {self.actions} action{'s' if self.actions != 1 else ''}.",
                "# A first draft, literal about what happened: read it, and loosen what is too exact —",
                "# a delay, a quantity, the `where`. It checks clean, so it runs as it stands.", "",
                f"macro {name}", ""]
        if not lines:
            head += ["# Nothing was recorded: no order "
                     + ("arrived and was worked" if self.side == "market" else "was sent by hand") + " while recording.", ""]
            return {"source": "\n".join(head), "orders": 0, "actions": 0}
        blocks = await self._market_blocks(lines) if self.side == "market" else await self._client_blocks(lines)
        return {"source": "\n".join(head) + "\n" + "\n\n".join(blocks) + "\n", "orders": len(lines),
                "actions": self.actions}

    async def _body(self, line: _Timeline, since: float) -> list[str]:
        out: list[str] = []
        heard: _Step | None = None
        for n, step in enumerate(line.steps):
            if step.kind == "heard":
                heard = step
                continue
            if heard is not None:
                out.append(f"wait {heard.name}")
                since, heard = heard.at, None
            if step.at - since >= _QUIET:
                out.append(f"after {_duration(step.at - since)}")
            out.append(await self._action(line, step))
            since = step.at
        if heard is not None and self.side == "client":
            # What the counterparty last did is what a client macro would check for.
            out.append(f"expect {heard.name} within {_duration(max(2.0, 2 * (heard.at - since)), coarse=True)}")
            out.append("pass")
        return out

    async def _action(self, line: _Timeline, step: _Step) -> str:
        parts = [step.name]
        if vocab.VERBS[step.name].trade:
            parts.append(await self._target(line, step))
        terms = ", ".join(f"{k}: {_value(step.name, k, v)}" for k, v in step.terms.items())
        # `trade where EXPR` ends at a comma; `last trade` and a plain verb run straight into the terms.
        joint = ", " if " where " in parts[-1] else " "
        return " ".join(parts) + (joint + terms if terms else "")

    async def _target(self, line: _Timeline, step: _Step) -> str:
        trade = step.trade
        if trade is None:
            return "last trade"
        known = [t for t in await self._trades(line.order) if t["id"] == trade["id"] or t["timestamp"] <= step.stamp]
        ids = [t["id"] for t in known] or [trade["id"]]
        if trade["id"] == max(ids):
            return "last trade"
        if trade["id"] == min(ids):
            return "first trade"
        return (f"trade where trade.last_qty == {_number(trade['last_qty'])} "
                f"and trade.last_price == {_number(trade['last_price'])}")

    async def _trades(self, order: dict[str, Any]) -> list[dict[str, Any]]:
        if order["direction"] == "RX":
            sql = "SELECT * FROM fix_executions WHERE session_id = ? AND direction = 'TX' AND order_id = ? ORDER BY id"
            params: tuple[Any, ...] = (order["session_id"], order["order_id"])
        else:
            sql = ("SELECT * FROM fix_executions WHERE session_id = ? AND direction = 'RX' AND cl_ord_id IN "
                   "(SELECT cl_ord_id FROM fix_orders WHERE id = ? UNION "
                   "SELECT cl_ord_id FROM fix_orders__history WHERE id = ?) ORDER BY id")
            params = (order["session_id"], order["id"], order["id"])
        cursor = await self.engine.db.read_conn.execute(sql, params)
        rows = [dict(r) for r in await cursor.fetchall()]
        await cursor.close()
        return rows

    async def _market_blocks(self, lines: list[_Timeline]) -> list[str]:
        """One block per way of working an order: orders worked the same way
        (the same statements, whatever the delays) share a block."""
        groups: list[tuple[list[str], list[_Timeline]]] = []
        for line in lines:
            body = await self._body(line, line.started)
            shape = [s for s in body if not s.startswith("after ")]
            for other, members in groups:
                if [s for s in other if not s.startswith("after ")] == shape:
                    members.append(line)
                    break
            else:
                groups.append((body, [line]))
        symbols_of = [sorted({m.order["symbol"] for m in members}) for _, members in groups]
        made: list[tuple[bool, str, list[str]]] = []
        for n, (body, members) in enumerate(groups):
            symbols = symbols_of[n]
            where = f"symbol == {_quote(symbols[0])}" if len(symbols) == 1 else f"symbol in [{', '.join(map(_quote, symbols))}]"
            # A symbol worked two ways: the quantity is the next thing that told the orders apart.
            shared = any(set(symbols) & set(other) for k, other in enumerate(symbols_of) if k != n)
            if shared:
                qtys = sorted({m.order["order_qty"] for m in members})
                where += f" and order_qty == {_number(qtys[0])}" if len(qtys) == 1 else \
                    f" and order_qty in [{', '.join(_number(q) for q in qtys)}]"
            made.append((shared, where, body))
        blocks, seen = [], set()
        for _, where, body in sorted(made, key=lambda m: not m[0]):      # the narrower `where` first: blocks are tried from the top
            note = ["# The same orders as a block above, worked differently: tell them apart in the `where`."] if where in seen else []
            seen.add(where)
            blocks.append("\n".join([*note, f"on order where {where}", *("    " + line for line in body)]))
        return blocks

    async def _client_blocks(self, lines: list[_Timeline]) -> list[str]:
        first = min(line.started for line in lines)
        blocks = []
        for line in sorted(lines, key=lambda t: t.started):
            o = line.order
            terms = {"symbol": o["symbol"], "side": o["side_code"], "qty": o["entered_qty"] or o["order_qty"],
                     "type": o["ord_type_code"], "price": o["entered_price"], "tif": o["tif_code"],
                     "expire": o.get("expire_time") or o.get("expire_date"), "client": o.get("client"),
                     "handl_inst": o.get("handl_inst_code") if o.get("handl_inst_code") != "1" else "",
                     "text": o.get("sent_text"), "extra": o.get("extra_tags")}
            new = "new " + ", ".join(f"{k}: {_value('new', k, v)}" for k, v in terms.items() if v not in (None, ""))
            lead = [f"after {_duration(line.started - first)}"] if line.started - first >= _QUIET else []
            body = await self._body(line, line.started)
            blocks.append("\n".join(["run", *("    " + s for s in [*lead, new, *body])]))
        return blocks


def _equal(a: Any, b: Any) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a if a is not None else "") == str(b if b is not None else "")


def _number(value: Any) -> str:
    number = float(value)
    return str(int(number)) if number == int(number) else f"{number:.10g}"


def _quote(text: Any) -> str:
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _value(verb: str, term: str, value: Any) -> str:
    enum = vocab.enum_of(verb, term)
    if enum:
        word = next((w for w, code in vocab.ENUMS[enum].items() if code == str(value)), None)
        return word or _quote(value)
    if term in ("qty", "price"):
        try:
            return _number(value)
        except (TypeError, ValueError):
            return _quote(value)
    return _quote(value)


def _duration(seconds: float, coarse: bool = False) -> str:
    """As a person would write it: 250ms, 1.5s, 2m — rounded, since the hand was not that exact."""
    if coarse:
        seconds = float(-(-seconds // 1))
    if seconds < 1:
        return f"{max(10, round(seconds * 100) * 10)}ms"
    if seconds < 120:
        return f"{_number(round(seconds, 1))}s"
    return f"{_number(round(seconds / 60, 1))}m"
