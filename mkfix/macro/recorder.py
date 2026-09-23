"""Recording: work orders by hand, get the macro that would have done it.

A `Recorder` listens on the engine's event bus while it records. For every
order of its side it keeps a timeline — what the counterparty did and what
was done by hand, each with its moment — and `stop()` writes the timelines
out in the macro language, one block an order:

- an action that followed something from the counterparty was an answer to
  it, and is triggered by it: every event heard since the last action is
  waited for, in order, and the action runs when the last comes;
- only an action with nothing heard since the one before has nothing but
  time to go by: `after DELAY` and the action;
- on the market side a request answered the same way every time is written
  as the rule it was: a `when` handler beside the main flow.

`delays` keeps the time taken to answer as well (see `_body`).

What comes out is a first draft, literal about what happened: the events
are the ones heard, the `where` is the order that was seen. It is meant to
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
                 clock: Callable[[], float] = time.monotonic, delays: bool = False) -> None:
        if side not in vocab.MACRO_SIDES:
            raise ValueError(f"A recording is of the client side or the market side, not {side!r}")
        self.engine, self.side, self.session, self.clock = engine, side, session, clock
        self.delays = delays            # write the time taken to answer an event too; settable until `stop`
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

    async def stop(self, name: str = "recorded", delays: bool | None = None) -> dict[str, Any]:
        """Stop listening and write what was recorded: `{source, orders,
        actions}`. ``delays`` keeps the time taken to answer each event; the
        timeline holds it either way, so it is a choice for the end."""
        if delays is not None:
            self.delays = delays
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        lines = [line for line in self.timelines.values() if self.side == "client" or line.steps]
        sessions = sorted({line.order["session_id"] for line in lines})
        head = [f"# {self.side.capitalize()} side, recorded {self.started_at[:4]}-{self.started_at[4:6]}-{self.started_at[6:8]} "
                f"{self.started_at[9:17]} UTC" + (f" on {', '.join(sessions)}" if sessions else "")
                + f": {len(lines)} order{'s' if len(lines) != 1 else ''}, {self.actions} action{'s' if self.actions != 1 else ''}.",
                "# A first draft, literal about what happened: read it, and loosen what is too exact —",
                "# a quantity, a bound, the `where`. It checks clean, so it runs as it stands.",
                ("# Each action runs when what it answered comes, after the time you took to answer it."
                 if self.delays else
                 "# Each action runs the moment what it answered comes; `after` is only where nothing came between two."), ""]
        if not lines:
            head += ["# Nothing was recorded: no order "
                     + ("arrived and was worked" if self.side == "market" else "was sent by hand") + " while recording.", ""]
            return {"source": "\n".join(head), "orders": 0, "actions": 0}
        blocks = await self._market_blocks(lines) if self.side == "market" else await self._client_blocks(lines)
        return {"source": "\n".join(head) + "\n" + "\n\n".join(blocks) + "\n", "orders": len(lines),
                "actions": self.actions}

    async def _body(self, line: _Timeline, since: float) -> list[str]:
        """One order's statements. What happened decides when the next thing
        is done, not the clock:

        - an action that followed something from the counterparty is written
          after a wait for it — every event heard since the last action, in
          the order heard — and runs the moment the last of them comes. A
          market order's arrival is such an event: the first action on it has
          no delay. A client macro `expect`s within a generous bound, so a
          venue that never answers fails the run instead of hanging it; a
          market macro `wait`s, as long as the client takes;
        - only an action with nothing heard since the one before it has
          nothing but time to go by, and is written `after DELAY`;
        - on the market side, a request that was answered the same way every
          time it came is a rule, and is written as one: `when cancel` /
          `accept`, beside the main flow, instead of a wait inside it.

        With ``self.delays`` the time taken to answer is kept as well: an
        `after` follows the wait (and leads a `when`'s answer)."""
        steps = line.steps
        handled, handlers = self._handlers(steps) if self.side == "market" else (set(), [])
        out: list[str] = []
        for event, answer, think, final in handlers:
            out.append(f"when {event}")
            if self.delays and think >= _QUIET:
                out.append(f"    after {_duration(think)}")
            out.append("    " + await self._action(line, answer))
            if final:
                out.append("    stop")
        heard_since = self.side == "market"          # the arrival itself
        last = since
        for n, step in enumerate(steps):
            if n in handled:
                continue
            if step.kind == "heard":
                if self.side == "client":
                    bound = _duration(max(5.0, 3 * (step.at - last)), coarse=True)
                    out.append(f"expect {step.name} within {bound}")
                elif any(s.kind == "did" and k not in handled for k, s in enumerate(steps) if k > n):
                    out.append(f"wait {step.name}")        # a wait nothing follows is not worth a line
                heard_since, last = True, step.at
                continue
            if (not heard_since or self.delays) and step.at - last >= _QUIET:
                out.append(f"after {_duration(step.at - last)}")
            out.append(await self._action(line, step))
            heard_since, last = False, step.at
        if self.side == "client" and steps and steps[-1].kind == "heard":
            out.append("pass")
        return out

    # What answers a request: the first thing done after it, if it is one of these.
    _ANSWERS = {"cancel": ("accept", "reject"), "replace": ("accept", "reject"), "dk": ("renotify", "correct", "bust")}

    def _handlers(self, steps: list[_Step]) -> tuple[set[int], list[tuple[str, _Step, float, bool]]]:
        """The requests that were answered the same way every time, as
        `(event, the answer, the time first taken to give it, nothing was
        done after)`, and the steps they account for. One answered two ways
        — the first replace accepted, the second refused — is no rule: it
        stays in the main flow, in the order it happened."""
        found: dict[str, list[tuple[int, _Step | None]]] = {}
        for n, step in enumerate(steps):
            if step.kind == "heard" and step.name in self._ANSWERS:
                nxt = steps[n + 1] if n + 1 < len(steps) else None
                answered = nxt is not None and nxt.kind == "did" and nxt.name in self._ANSWERS[step.name]
                found.setdefault(step.name, []).append((n, nxt if answered else None))
        handled: set[int] = set()
        handlers = []
        for event, seen in found.items():
            answers = [a for _, a in seen]
            if any(a is None for a in answers) or len({(a.name, tuple(sorted(a.terms.items()))) for a in answers}) != 1:
                continue
            first_n, first = seen[0]
            handled |= {k for n, _ in seen for k in (n, n + 1)}
            last_n = seen[-1][0] + 1
            # An accepted cancel ends the order; with nothing done after it, the macro says so, so that
            # what the main flow still has to do is not done to a cancelled order when the cancel comes sooner.
            final = event == "cancel" and first.name == "accept" and not any(s.kind == "did" for s in steps[last_n + 1:])
            handlers.append((event, first, first.at - steps[first_n].at, final))
        return handled, handlers

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
