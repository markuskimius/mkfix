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

A market recording follows received orders (from their arrival) and the
IOIs, adverts and allocations sent by hand (from their sending); a client
recording follows orders sent by hand (from their `new`) and the three
families as they arrive. Whatever a macro owns is nobody's to record. Each
subject's timeline becomes a block of its kind: `on order`/`on ioi`… for
what was received, `run` with the sending verb for what was sent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from mkfix.fix.instrument import COVERED, OPEN_CLOSE, PUT_OR_CALL
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
# What the counterparty does that a macro can wait for, by the kind of block
# a timeline becomes: the events of the received (`on …`) or sent (`run`)
# block of each subject, bar the ones a script seldom names.
_NEVER_HEARD = {"er", "acked", "message", "manual", "session down", "session up", "error", "signal"}
_HEARD = {
    (subject, kind): tuple(e.name for e in vocab.EVENTS.values()
                           if (subject, kind) in e.places and e.name not in _NEVER_HEARD)
    for subject in vocab.SUBJECTS for kind in (vocab.MARKET, vocab.CLIENT)
}
_CREATORS = set(vocab.CREATORS.values())
# The column a received subject's `where` narrows on when its symbol is not enough.
_QTY_COL = {vocab.ORDER: "order_qty", vocab.IOI: "ioi_qty", vocab.ADVERT: "quantity", vocab.ALLOCATION: "quantity",
            vocab.RFQ: "order_qty", vocab.QUOTE: "order_qty", vocab.RFQ_REQUEST: "num_symbols"}
# The column that names a subject's instrument (an RFQ request names several).
_SYMBOL_COL = {vocab.RFQ_REQUEST: "symbols"}
_QUIET = 0.05          # a delay shorter than this is not worth a line


@dataclass
class _Step:
    at: float
    kind: str                      # heard | did
    name: str                      # the event, or the verb
    terms: dict[str, Any] = field(default_factory=dict)
    trade: dict[str, Any] | None = None
    stamp: str = ""
    target: str = ""               # the trade target already worked out (a macro from history)


@dataclass
class _Timeline:
    order: dict[str, Any]          # the subject's row (an order's, or an IOI's, advert's, allocation's)
    started: float
    steps: list[_Step] = field(default_factory=list)
    subject: str = vocab.ORDER

    @property
    def sent(self) -> bool:
        """Whether this is one we sent (a `run` block) or received (an `on` block)."""
        return self.order["direction"] == "TX"


class Recorder:
    def __init__(self, engine: FixEngine, side: str, session: str = "",
                 clock: Callable[[], float] = time.monotonic, delays: bool = False) -> None:
        if side not in vocab.MACRO_SIDES:
            raise ValueError(f"A recording is of the client side or the market side, not {side!r}")
        self.engine, self.side, self.session, self.clock = engine, side, session, clock
        self.delays = delays            # write the time taken to answer an event too; settable until `stop`
        self.timelines: dict[tuple[str, int], _Timeline] = {}
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

    def _sends(self, subject: str) -> bool:
        """Whether this side sends the subject: the client side sends orders
        and receives the rest, the market side the other way round."""
        return (subject in vocab.CLIENT_SENDS) == (self.side == "client")

    def _on_event(self, ev: EngineEvent) -> None:
        if ev.table:
            subject, row = vocab.subject_of_row(ev.table, ev.row), ev.row
            prefix = vocab.SUBJECT_WORDS.get(subject, subject) + " "
            kinds = tuple(k[len(prefix):] if k.startswith(prefix) else k for k in ev.kinds)
        else:
            subject, row, kinds = vocab.ORDER, ev.order, ev.kinds
        if row is None or not subject or (self.session and ev.session_id != self.session):
            return
        sends = self._sends(subject)
        if row["direction"] != ("TX" if sends else "RX") or row.get("macro"):
            return
        now, key = self.clock(), (subject, row["id"])
        line = self.timelines.get(key)
        if line is None:
            # What we receive is followed from its arrival, what we send from
            # the sending; one already under way when recording began has no
            # beginning to write down.
            word = vocab.SUBJECT_WORDS[subject]
            born = (kinds[0] == f"sent {word}" and ev.source == "manual") if sends \
                else (kinds[0] == word and ev.source == "wire")
            if born:
                self.timelines[key] = _Timeline(dict(row), now, subject=subject)
            return
        if ev.source == "manual" and kinds[0] == "action":
            verb = _VERB_OF.get(ev.detail.get("op", ""))
            if verb and verb not in _CREATORS:
                # The trade as it stood: a correction is named by the terms it found, not the ones it left.
                line.steps.append(_Step(now, "did", verb, self._terms(verb, ev),
                                        ev.detail.get("trade_before") or ev.trade, _fix_timestamp()))
        elif ev.source == "wire":
            heard = "filled" if "filled" in kinds else kinds[0]
            if heard in _HEARD[(subject, vocab.CLIENT if sends else vocab.MARKET)]:
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
        # What was received and never touched — an arrival, a cancel heard,
        # nothing done — is no block: a block needs a line.
        lines = [line for line in self.timelines.values()
                 if line.sent or any(s.kind == "did" for s in line.steps)]
        stamp = self.started_at
        return await self._write(
            lines, f"recorded {stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:17]} UTC",
            nothing="# Nothing was recorded: no order "
                    + ("arrived and was worked" if self.side == "market" else "was sent by hand") + " while recording.")

    async def _write(self, lines: list[_Timeline], how: str, nothing: str, notes: tuple[str, ...] = (),
                     took: str = "you took") -> dict[str, Any]:
        """The timelines as a macro: `{source, orders, actions}`. ``how``
        says where they came from, in the header's first line; ``notes`` are
        further header lines, and ``nothing`` the one for no timelines."""
        actions = sum(1 for line in lines for s in line.steps if s.kind == "did")
        sessions = sorted({line.order["session_id"] for line in lines})
        counts = {subject: sum(1 for line in lines if line.subject == subject) for subject in vocab.SUBJECTS}
        what = ", ".join(f"{n} {vocab.PLURALS[subject] if n != 1 else vocab.PLURALS[subject][:-1]}"     # 1 IOI, not 1 ioi
                         for subject, n in counts.items() if n) or "0 orders"
        head = [f"# {self.side.capitalize()} side, {how}" + (f" on {', '.join(sessions)}" if sessions else "")
                + f": {what}, {actions} action{'s' if actions != 1 else ''}.",
                "# A first draft, literal about what happened: read it, and loosen what is too exact —",
                "# a quantity, a bound, the `where`. It checks clean, so it runs as it stands.",
                (f"# Each action runs when what it answered comes, after the time {took} to answer it."
                 if self.delays else
                 "# Each action runs the moment what it answered comes; `after` is only where nothing came between two."),
                *notes, ""]
        if not lines:
            head += [nothing, ""]
            return {"source": "\n".join(head), "orders": 0, "actions": 0, "blocks": [], "counts": counts,
                    "sessions": sessions,
                    "declarations": []}
        blocks: list[str] = []
        self._declared = {}
        for subject in vocab.SUBJECTS:
            mine = [line for line in lines if line.subject == subject]
            if not mine:
                continue
            blocks += await self._sent_blocks(mine, subject) if self._sends(subject) \
                else await self._received_blocks(mine, subject)
        declarations = list(self._declared.values())
        top = "\n".join(declarations) + "\n\n" if declarations else ""
        return {"source": "\n".join(head) + "\n" + top + "\n\n".join(blocks) + "\n", "orders": len(lines),
                "actions": actions, "blocks": blocks, "counts": counts, "sessions": sessions,
                "declarations": declarations}

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
        handled, handlers = self._handlers(steps, line.subject) if not line.sent else (set(), [])
        out: list[str] = []
        for event, answer, think, final in handlers:
            out.append(f"when {event}")
            if self.delays and think >= _QUIET:
                out.append(f"    after {_duration(think)}")
            out.append("    " + await self._action(line, answer))
            if final:
                out.append("    stop")
        heard_since = not line.sent                  # the arrival itself
        last = since
        for n, step in enumerate(steps):
            if n in handled:
                continue
            if step.kind == "heard":
                if line.sent:
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
        if line.sent and steps and steps[-1].kind == "heard":
            out.append("pass")
        return out

    # What answers a request: the first thing done after it, if it is one of these.
    _ANSWERS = {
        vocab.ORDER: {"cancel": ("accept", "reject"), "replace": ("accept", "reject"),
                      "dk": ("renotify", "correct", "bust")},
        vocab.ALLOCATION: {"cancel": ("accept allocation", "reject allocation"),
                           "replace": ("accept allocation", "reject allocation")},
        vocab.RFQ: {"countered": ("quote", "requote", "reject rfq", "cancel quote")},
    }

    def _handlers(self, steps: list[_Step], subject: str = vocab.ORDER) -> tuple[set[int], list[tuple[str, _Step, float, bool]]]:
        """The requests that were answered the same way every time, as
        `(event, the answer, the time first taken to give it, nothing was
        done after)`, and the steps they account for. One answered two ways
        — the first replace accepted, the second refused — is no rule: it
        stays in the main flow, in the order it happened."""
        answers = self._ANSWERS.get(subject, {})
        found: dict[str, list[tuple[int, _Step | None]]] = {}
        for n, step in enumerate(steps):
            if step.kind == "heard" and step.name in answers:
                nxt = steps[n + 1] if n + 1 < len(steps) else None
                answered = nxt is not None and nxt.kind == "did" and nxt.name in answers[step.name]
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
            final = event == "cancel" and first.name.startswith("accept") and \
                not any(s.kind == "did" for s in steps[last_n + 1:])
            handlers.append((event, first, first.at - steps[first_n].at, final))
        return handled, handlers

    async def _action(self, line: _Timeline, step: _Step) -> str:
        parts = [step.name]
        if vocab.VERBS[step.name].trade:
            parts.append(step.target or await self._target(line, step))
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
            sql = ("SELECT * FROM fix_executions WHERE session_id = ? AND direction = 'RX' AND order_id = ? "
                   "ORDER BY id")
            params = (order["session_id"], order["order_id"])
        cursor = await self.engine.db.read_conn.execute(sql, params)
        rows = [dict(r) for r in await cursor.fetchall()]
        await cursor.close()
        return rows

    async def _received_blocks(self, lines: list[_Timeline], subject: str = vocab.ORDER) -> list[str]:
        """One block per way of working what was received: orders (IOIs,
        adverts, allocations) worked the same way — the same statements,
        whatever the delays — share a block."""
        qty_col, symbol_col = _QTY_COL[subject], _SYMBOL_COL.get(subject, "symbol")
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
        symbols_of = [sorted({m.order[symbol_col] for m in members}) for _, members in groups]
        made: list[tuple[bool, str, list[str]]] = []
        for n, (body, members) in enumerate(groups):
            symbols = symbols_of[n]
            where = f"{symbol_col} == {_quote(symbols[0])}" if len(symbols) == 1 \
                else f"{symbol_col} in [{', '.join(map(_quote, symbols))}]"
            # A symbol worked two ways: the quantity is the next thing that told the orders apart.
            shared = any(set(symbols) & set(other) for k, other in enumerate(symbols_of) if k != n)
            if shared:
                qtys = sorted({m.order[qty_col] for m in members}, key=str)
                where += f" and {qty_col} == {_literal(qtys[0])}" if len(qtys) == 1 else \
                    f" and {qty_col} in [{', '.join(_literal(q) for q in qtys)}]"
            made.append((shared, where, body))
        blocks, seen = [], set()
        for _, where, body in sorted(made, key=lambda m: not m[0]):      # the narrower `where` first: blocks are tried from the top
            note = [f"# The same {vocab.PLURALS[subject]} as a block above, worked differently: "
                    "tell them apart in the `where`."] if where in seen else []
            seen.add(where)
            blocks.append("\n".join([*note, f"on {vocab.SUBJECT_WORDS[subject]} where {where}",
                                     *("    " + line for line in body)]))
        return blocks

    _market_blocks = _received_blocks

    @staticmethod
    def _creator_terms(subject: str, o: dict[str, Any]) -> dict[str, Any]:
        """The sending verb's terms, from the row as it was sent."""
        if subject == vocab.ORDER:
            return {"symbol": o["symbol"], "side": o["side_code"], "qty": o["entered_qty"] or o["order_qty"],
                    "type": o["ord_type_code"], "price": o["entered_price"], "tif": o["tif_code"],
                    "expire": o.get("expire_time") or o.get("expire_date"), "client": o.get("client"),
                    "handl_inst": o.get("handl_inst_code") if o.get("handl_inst_code") != "1" else "",
                    **_instrument_terms(o),
                    "text": o.get("sent_text"), "extra": o.get("extra_tags")}
        if subject == vocab.IOI:
            return {"symbol": o["symbol"], "side": o["side_code"], "qty": o["ioi_qty"], "price": o["price"] or None,
                    "valid": o["valid_until"], "quality": o["qlty_ind_code"], "natural": o["natural_flag"],
                    "qualifiers": o["qualifiers"], "currency": o["currency"], "client": o.get("client"),
                    "text": o["text"], "extra": o["extra_tags"], **_instrument_terms(o, position=False)}
        if subject == vocab.ADVERT:
            return {"symbol": o["symbol"], "side": o["side_code"], "qty": o["quantity"], "price": o["price"] or None,
                    "currency": o["currency"], "trade_date": o["trade_date"], "last_mkt": o["last_mkt"],
                    "client": o.get("client"), "text": o["text"], "extra": o["extra_tags"], **_instrument_terms(o, position=False)}
        if subject == vocab.RFQ:
            return {"symbol": o["symbol"], "side": o["side_code"], "qty": o["order_qty"] or None,
                    "request_type": o["quote_request_type_code"], "quote_type": o["quote_type_code"],
                    "currency": o["currency"], "client": o.get("client"), "text": o.get("sent_text"),
                    "extra": o["extra_tags"], **_instrument_terms(o, position=False)}
        if subject == vocab.QUOTE:
            return {"symbol": o["symbol"], "side": o["side_code"], "qty": o["order_qty"] or None,
                    "currency": o["currency"], "client": o.get("client"), "bid": o["bid_px"], "offer": o["offer_px"],
                    "bid_size": o["bid_size"], "offer_size": o["offer_size"], "quote_type": o["quote_type_code"],
                    "text": o.get("sent_text"), "extra": o["quote_extra_tags"], **_instrument_terms(o, position=False)}
        if subject == vocab.RFQ_REQUEST:
            return {"symbols": o["symbols"], "subscription": o["subscription_type_code"],
                    "request_type": o["quote_request_type_code"], "quote_type": o["quote_type_code"],
                    "client": o.get("client"), "extra": o["extra_tags"]}
        # An allocation sent without a trade date goes out with the day's: written down, the
        # macro would send that day's for ever. The day it was sent is no term; any other was given.
        sent_on = str(o.get("transact_time") or o.get("timestamp") or "")[:8]
        return {"symbol": o["symbol"], "side": o["side_code"], "qty": o["quantity"], "avg_price": o["avg_price"] or None,
                "trade_date": o["trade_date"] if o["trade_date"] != sent_on else "",
                "alloc_type": o["alloc_type_code"], "orders": o["orders"],
                "execs": o["execs"], "accounts": o["allocs"], "client": o.get("client"), "text": o["sent_text"],
                "extra": o["extra_tags"], **_instrument_terms(o, position=False)}

    async def _saved_instruments(self) -> dict[str, dict[str, Any]]:
        from .check import instrument_payload
        cursor = await self.engine.db.read_conn.execute("SELECT * FROM fix_instruments ORDER BY name")
        rows = await cursor.fetchall()
        await cursor.close()
        return {r["name"]: instrument_payload(dict(r)) for r in rows}

    def _named(self, terms: dict[str, Any], order: dict[str, Any], saved: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """An order in an instrument saved in Config › Instruments names it,
        `instrument: 'NAME'`, and the macro declares it at the top, so the
        text still runs on a server that has no such instrument."""
        from .check import instrument_payload
        mine = instrument_payload(order)
        name = next((n for n, payload in saved.items() if payload == mine and len(payload) > 1), None)
        if name is None:
            return terms
        self._declared[name] = "instrument " + _quote(name) + " " + ", ".join(
            f"{k}: {_value('instrument', k, v)}" for k, v in _instrument_terms({**mine, "symbol": order["symbol"]},
                                                                               symbol=True).items()
            if v not in (None, ""))
        dropped = set(_instrument_terms(mine, symbol=True))       # Open/Close and Covered are the order's
        return {"instrument": name, **{k: v for k, v in terms.items() if k not in dropped}}

    async def _sent_blocks(self, lines: list[_Timeline], subject: str = vocab.ORDER) -> list[str]:
        """One `run` block per thing sent, led by its sending verb and the
        terms it went out with, then what was heard and done."""
        first = min(line.started for line in lines)
        verb = vocab.CREATORS[subject]
        saved = await self._saved_instruments() if subject != vocab.RFQ_REQUEST else {}
        blocks = []
        for line in sorted(lines, key=lambda t: t.started):
            terms = self._creator_terms(subject, line.order)
            if saved:
                terms = self._named(terms, line.order, saved)
            new = f"{verb} " + ", ".join(f"{k}: {_value(verb, k, v)}" for k, v in terms.items() if v not in (None, ""))
            lead = [f"after {_duration(line.started - first)}"] if line.started - first >= _QUIET else []
            body = await self._body(line, line.started)
            blocks.append("\n".join(["run", *("    " + s for s in [*lead, new, *body])]))
        return blocks

    _client_blocks = _sent_blocks


class EndToEndRecorder:
    """Both sides recorded at once, written as one end-to-end macro: what
    was sent by hand and what was done by hand to what arrived, the client
    side's blocks first. It is two recorders — each side's rules are its
    own — and one text, which run on the same two sessions plays both
    parts against each other."""

    side = vocab.E2E

    def __init__(self, engine: FixEngine, session: str = "", market_session: str = "",
                 clock: Callable[[], float] = time.monotonic, delays: bool = False) -> None:
        self.session, self.market_session = session, market_session
        self.client = Recorder(engine, "client", session, clock, delays)
        self.market = Recorder(engine, "market", market_session, clock, delays)
        self.started_at = self.client.started_at

    @property
    def recording(self) -> bool:
        return self.client.recording or self.market.recording

    @property
    def actions(self) -> int:
        return self.client.actions + self.market.actions

    def status(self) -> dict[str, Any]:
        return {"recording": self.recording, "side": self.side, "session": self.session,
                "market_session": self.market_session,
                "orders": len(self.client.timelines) + len(self.market.timelines), "actions": self.actions,
                "since": self.started_at}

    async def stop(self, name: str = "recorded", delays: bool | None = None) -> dict[str, Any]:
        client, market = await self.client.stop(name, delays), await self.market.stop(name, delays)
        keeps = self.client.delays
        blocks = [*client["blocks"], *market["blocks"]]
        counts = {s: client["counts"][s] + market["counts"][s] for s in vocab.SUBJECTS}
        what = ", ".join(f"{n} {vocab.PLURALS[s] if n != 1 else vocab.PLURALS[s][:-1]}"
                         for s, n in counts.items() if n) or "0 orders"
        sessions = sorted({*client["sessions"], *market["sessions"]})
        actions = client["actions"] + market["actions"]
        stamp = self.started_at
        head = [f"# End-to-end, recorded {stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:17]} UTC"
                + (f" on {', '.join(sessions)}" if sessions else "")
                + f": {what}, {actions} action{'s' if actions != 1 else ''}.",
                "# Both sides of what you did by hand, in one macro: what you sent, and what you did to what arrived.",
                "# A first draft, literal about what happened: read it, and loosen what is too exact —",
                "# a quantity, a bound, the `where`. It checks clean, so it runs as it stands.",
                ("# Each action runs when what it answered comes, after the time you took to answer it."
                 if keeps else
                 "# Each action runs the moment what it answered comes; `after` is only where nothing came between two."),
                ""]
        if not blocks:
            head += ["# Nothing was recorded: nothing was sent by hand, and nothing that arrived was worked.", ""]
            return {"source": "\n".join(head), "orders": 0, "actions": 0}
        declarations = list(dict.fromkeys([*client["declarations"], *market["declarations"]]))
        top = "\n".join(declarations) + "\n\n" if declarations else ""
        return {"source": "\n".join(head) + "\n" + top + "\n\n".join(blocks) + "\n",
                "orders": client["orders"] + market["orders"], "actions": actions}


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


def _literal(value: Any) -> str:
    """A number as a number, anything else quoted (an IOI's `L`)."""
    try:
        return _number(value)
    except (TypeError, ValueError):
        return _quote(value)


def _value(verb: str, term: str, value: Any) -> str:
    enum = vocab.enum_of(verb, term)
    if enum:
        word = next((w for w, code in vocab.ENUMS[enum].items() if code == str(value)), None)
        return word or _quote(value)
    if term == "valid":
        return _seconds_term(value)
    if term in ("qty", "price", "avg_price", "bid", "offer", "bid_size", "offer_size", "strike", "multiplier"):
        try:
            return _number(value)
        except (TypeError, ValueError):
            return _quote(value)
    return _quote(value)


def _instrument_terms(row: dict[str, Any], symbol: bool = False, position: bool = True) -> dict[str, Any]:
    """A row's instrument as a sending verb's terms (a declaration's, with
    its symbol; a family's, without an order's Open/Close and Covered), the
    words it keeps turned back into the codes the terms' words stand for."""
    code = lambda words, col: words.get(row.get(col) or "", row.get(col))  # noqa: E731
    terms = {"sec_type": row.get("security_type"), "maturity": row.get("maturity"), "strike": row.get("strike_price"),
             "put_call": code(PUT_OR_CALL, "put_or_call"), "cfi": row.get("cfi_code"),
             "underlying": row.get("underlying_symbol"), "underlying_type": row.get("underlying_security_type"),
             "underlying_maturity": row.get("underlying_maturity"), "multiplier": row.get("multiplier"),
             "exchange": row.get("security_exchange"), "security_id": row.get("security_id"),
             "id_source": row.get("security_id_source")}
    if symbol:
        return {"symbol": row.get("symbol"), **terms}
    if not position:
        return terms
    return {**terms, "open_close": code(OPEN_CLOSE, "open_close"), "covered": code(COVERED, "covered_uncovered")}


def _seconds_term(value: Any) -> str:
    """A `valid` term as a duration: the seconds a dialog took, written the way a person would."""
    try:
        return _duration(float(value))
    except (TypeError, ValueError):
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
