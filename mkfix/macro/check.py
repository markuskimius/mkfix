"""What is wrong with a macro that parsed: meaning, not form.

Everything is found before anything runs — a verb on the wrong side of an
order, a term a verb does not take, an event that cannot happen there, a
misspelt column (`order.leave_qty` would otherwise be NULL and fail silently
in `==` or `MIN(…)`), an unknown function, a template that does not exist.
"""

from __future__ import annotations

import difflib
from typing import Any, Iterable, Mapping

from mkio import expr

from mkfix.fix.instrument import INSTRUMENT_COLS, normalize_instrument

from . import vocab
from .functions import ENV
from .nodes import (
    After, Define, Do, Instrument, LegLine, OrderLine, Term, LEGGED,
    Action, Block, Diagnostic, Expect, Expr, If, Let, Repeat, Macro, Share, Signal, Statement, Wait, When,
    expressions, walk,
)
from .parser import parse

_SIDE_BLOCKS = {
    "market": "`on order`, `on rfq`, a `run` that sends IOIs, adverts, allocations, quotes or RFQ requests, "
              "and `on sent ioi/advert/allocation/quote/rfq request`",
    "client": "a `run` that sends orders or RFQs, `on sent order/rfq`, and `on ioi/advert/allocation/quote/rfq request`",
}


def _where(places: frozenset[tuple[str, str]]) -> str:
    """The blocks a verb or event may stand in, named."""
    return " or ".join(vocab.block_name(kind, subject) for subject, kind in sorted(places))


def _close(word: str, known: Iterable[str]) -> str:
    match = difflib.get_close_matches(word, list(known), n=1, cutoff=0.6)
    return f" — did you mean {match[0]!r}?" if match else ""


def instrument_payload(values: Mapping[str, Any]) -> dict[str, Any]:
    """An instrument as `send_new_order` takes it — its symbol and the
    columns it gives, spelled as the rows keep them — from a saved row or a
    declaration's values. Blank columns are left out, so what a `new` says
    inline is added to it."""
    row = normalize_instrument(values)
    return {**({"symbol": values["symbol"]} if values.get("symbol") else {}),
            **{c: row[c] for c in INSTRUMENT_COLS if row[c] not in (None, "")},
            # A strategy's legs, as a list: what `new multileg` sends.
            **({"legs": _legs_list(values["legs"])} if values.get("legs") else {})}


def _legs_list(legs: Any) -> list[dict[str, Any]]:
    import json
    return json.loads(legs) if isinstance(legs, str) else list(legs)


def _literal_terms(terms: list[Term], verb: str) -> dict[str, Any]:
    """Written-out terms (a declaration's) as payload values: words as their codes."""
    values: dict[str, Any] = {}
    for term in terms:
        if not term.key:
            continue
        enum = vocab.enum_of(verb, term.name)
        if term.word is not None:
            values[term.key] = vocab.enum_code(enum, term.word)
        else:
            value = getattr(term.value.node, "value", None)
            values[term.key] = vocab.enum_code(enum, value) if enum and isinstance(value, str) else value
    return values


def declared_payload(decl: Instrument) -> dict[str, Any]:
    """The instrument an `instrument 'NAME' …` line declares. Its terms are
    literals (the checker holds them to it), so this needs no scope."""
    values = _literal_terms(decl.terms, "instrument")
    if decl.legs:
        values["legs"] = [_literal_terms(leg.terms, "leg") for leg in decl.legs]
    return instrument_payload(values)


class _Checker:
    def __init__(self, templates: Mapping[str, Iterable[str]] | None, sessions: Iterable[str] | None,
                 side: str | None = None, instruments: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self.side = side
        # Saved instruments by name, as `instrument_payload` gives them; None when unknown.
        self.instruments = dict(instruments) if instruments is not None else None
        self.declared: dict[str, dict[str, Any]] = {}
        self.templates = {scope: set(names) for scope, names in templates.items()} if templates is not None else None
        self.sessions = set(sessions) if sessions is not None else None
        self.out: list[Diagnostic] = []
        self.shared: list[str] = []          # the names the macro's `share` lines set, anywhere in it
        self.signals: set[str] = set()       # the signals its `signal` lines send

    def report(self, line: int, col: int, end: int, message: str, severity: str = "error") -> None:
        self.out.append(Diagnostic(line, col, max(end, col + 1), message, severity))

    # -- expressions ------------------------------------------------------------

    def expression(self, e: Expr, schema: Mapping[str, Any]) -> None:
        def at(problem: expr.ExprError) -> None:
            col = problem.pos if problem.pos is not None else e.col
            self.report(e.line, col, e.col + len(e.source) if col == e.col else col + 1, problem.message)

        try:
            expr.compile_node(e.node, ENV)
        except expr.ExprError as problem:
            at(problem)
            return
        for problem in expr.check_fields(e.node, schema):
            at(problem)
        value = getattr(e.node, "value", None)
        if isinstance(value, str) and expr.has_expressions(value):
            try:
                template = expr.compile_template(value, ENV)
            except expr.ExprError as problem:
                self.report(e.line, e.col, e.col + len(e.source), f"In the text's ${{…}}: {problem.message}")
                return
            e.template = True
            for kind, part in template.parts:
                if kind == "expr":
                    for problem in expr.check_fields(part.ast, schema):
                        self.report(e.line, e.col, e.col + len(e.source), f"In the text's ${{…}}: {problem.message}")

    # -- blocks -----------------------------------------------------------------

    def macro(self, macro: Macro) -> None:
        if not macro.blocks:
            self.report(1, 0, 1, "A macro needs at least one block: `on order`, `on sent order`, `run`, "
                                 "`on ioi`, `on advert`, `on allocation`…")
        # One side per macro. A client macro sends orders and acts on
        # them, a market macro acts on orders received; the panes, the
        # runs and what may run at once are all kept apart by that. An
        # end-to-end macro is the one kind that holds both.
        side = self.side or macro.side
        for block in macro.blocks:
            if side != vocab.E2E and vocab.side_of(block.kind, block.subject) != side:
                other = vocab.side_of(block.kind, block.subject)
                self.report(block.line, block.col, block.col + 3,
                            f"This is a {side} macro, and this block belongs in a {other} macro: a macro is "
                            f"for one side. Keep {_SIDE_BLOCKS[side]} here and move this to a {other} macro — "
                            "or keep both in an end-to-end macro")
        # What is shared and what is signalled is the run's, so the macro's:
        # a block reads a name another block sets, and waits for a signal
        # another block sends.
        every = [st for block in macro.blocks for st in walk(block.body)]
        self.shared = list(dict.fromkeys(st.name for st in [*macro.shares, *every] if isinstance(st, Share)))
        self.signals = {st.name for st in every if isinstance(st, Signal)}
        for st in macro.shares:
            # Before any macro of the run exists: only what is shared already can be read.
            self.expression(st.value, {"shared": {name: None for name in self.shared}})
        for decl in macro.instruments:
            self.declaration(decl)
        self.defines, self.called, self.calling = list(macro.defines), set(), []
        for d in macro.defines:
            self.define(d)
        for block in macro.blocks:
            self.block(block)
        for d in macro.defines:
            if d.name not in self.called:
                self.report(d.line, d.col, d.col + len("define ") + len(d.name),
                            f"`{d.name}` is defined but never called: `do {d.name}(…)`", severity="warning")

    def declaration(self, decl: Instrument) -> None:
        """`instrument 'NAME' …`: its own terms, written out."""
        end = decl.col + len("instrument") + len(decl.name) + 3
        if decl.name in self.declared:
            self.report(decl.line, decl.col, end, f"Instrument {decl.name!r} is declared twice")
        seen: set[str] = set()
        for term in decl.terms:
            if term.name not in vocab.DECLARED_TERMS:
                self.report(term.line, term.col, term.col + len(term.name),
                            f"An instrument has no term {term.name!r}. It takes: {', '.join(vocab.DECLARED_TERMS)}"
                            f"{_close(term.name, vocab.DECLARED_TERMS)}")
                continue
            if term.name in seen:
                self.report(term.line, term.col, term.col + len(term.name), f"{term.name!r} is given twice")
            seen.add(term.name)
            if term.value is not None and type(term.value.node).__name__ != "Literal":
                self.report(term.value.line, term.value.col, term.value.col + len(term.value.source),
                            "An instrument's terms are written out — 'ES', 50, future — not computed")
            else:
                self.enum_words("instrument", term)
        if "symbol" not in seen:
            self.report(decl.line, decl.col, end, f"Instrument {decl.name!r} needs its symbol: symbol: 'ES'")
        for leg in decl.legs:
            self.leg_line(leg, declared=True)
        if len(decl.legs) == 1:
            self.report(decl.line, decl.col, end, f"Strategy {decl.name!r} has one leg: a strategy has two or more")
        payload = declared_payload(decl)
        saved = (self.instruments or {}).get(decl.name)
        if saved is not None and saved != payload:
            self.report(decl.line, decl.col, end,
                        f"Config › Instruments saves a different {decl.name!r}; this macro uses its own",
                        severity="warning")
        self.declared.setdefault(decl.name, payload)

    def instrument_named(self, term: Term) -> None:
        """`instrument: 'NAME'` names one declared above or saved."""
        value = getattr(term.value.node, "value", None) if term.value is not None else term.word
        if term.value is not None and type(term.value.node).__name__ != "Literal":
            return                                   # computed: resolved when it runs
        at = (term.value.line, term.value.col, term.value.col + len(term.value.source)) if term.value else \
            (term.line, term.col, term.col + len(term.name))
        if not isinstance(value, str):
            self.report(*at, "An instrument is named in quotes: instrument: 'ESZ6'")
        elif value not in self.declared and self.instruments is not None and value not in self.instruments:
            known = [*self.declared, *self.instruments]
            self.report(*at, f"No instrument named {value!r}: declare it at the top "
                             f"(instrument {value!r} symbol: …) or save it in Config › Instruments"
                             f"{_close(value, known)}")

    def enum_words(self, verb: str, term: Term) -> None:
        """A quoted value where the term takes words: a code passes, anything else is warned about."""
        enum = vocab.enum_of(verb, term.name)
        literal = getattr(term.value.node, "value", None) if term.value is not None else None
        if enum and isinstance(literal, str) and not term.value.template:
            code = vocab.enum_code(enum, literal)
            if code == literal and literal not in vocab.ENUMS[enum].values() and len(literal) > 2:
                self.report(term.value.line, term.value.col, term.value.col + len(term.value.source),
                            f"{literal!r} is no {term.name} this macro knows. Words: {', '.join(vocab.ENUMS[enum])}; "
                            f"or give the FIX code{_close('_'.join(literal.lower().split()), vocab.ENUMS[enum])}",
                            severity="warning")

    def heard(self, name: str, line: int, col: int, end: int) -> None:
        """A signal waited for by name that nothing in the macro sends never
        comes: signals stay within the run."""
        if name not in self.signals:
            sent = ", ".join(repr(s) for s in sorted(self.signals)) or "none"
            self.report(line, col, end, f"Nothing in this macro signals {name!r}, so this never happens: a signal "
                                        f"is heard by the macros of its own run. Signals sent here: {sent}"
                                        f"{_close(name, self.signals)}", severity="warning")

    def block(self, block: Block) -> None:
        if block.signal is not None:
            self.heard(block.signal, block.line, block.col, block.col + len("on signal"))
            if block.where is not None:
                # The signal is all there is to look at: the block's subject is still to be sent.
                started = vocab.scope_schema((), block.subject, self.shared)
                self.expression(block.where, {k: v for k, v in started.items()
                                              if k in ("event", "shared", *vocab.PEERS.values())})
        elif block.where is not None:
            self.expression(block.where, vocab.match_schema(block.subject))
        if block.session is not None and self.sessions is not None and block.session not in self.sessions:
            self.report(block.line, block.col, block.col + len("run on ") + len(block.session),
                        f"No session named {block.session!r}{_close(block.session, self.sessions)}")
        names = [st.name for st in walk(block.body) if isinstance(st, Let)]
        names += [st.var for st in walk(block.body) if isinstance(st, Repeat) and st.var]
        names += [p for st in walk(block.body) if isinstance(st, Do) and st.define for p in st.define.params]
        schema = vocab.scope_schema(names, block.subject, self.shared)
        self.body(block.body, block, schema, trade_in_hand=False)
        if block.kind == vocab.CLIENT:
            self.sending(block)

    def sending(self, block: Block) -> None:
        """A `run on` block is one subject's macro when the verb that sends
        it — `new`, `ioi`, `advert`, `allocate` — stands among its own
        lines. When that verb sits inside a `repeat`, each pass is a macro
        with a subject of its own, and the lines around that `repeat` run
        before any exists: they may set things up, not act or wait. The
        first sending verb decides the subject (the parser set it); a
        second kind of sending verb belongs in a block of its own."""
        creator = vocab.CREATORS[block.subject]
        creators = set(vocab.SENDERS)

        def sends(st: Statement) -> bool:
            return isinstance(st, Action) and vocab.SENDERS.get(st.verb) == block.subject

        header = "on signal" if block.signal is not None else "run"
        if not any(sends(st) for st in walk(block.body)):
            self.report(block.line, block.col, block.col + len(header),
                        f"A{'n' if header[0] == 'o' else ''} `{header}` block sends something of its own: it needs a "
                        "`new`, `ioi`, `advert`, `allocate`, `rfq`, `new quote` or `rfq request`"
                        + (". To react to a signal in a macro that already has its order, write "
                           "`when signal 'NAME'` inside that block" if block.signal is not None else ""))
            return
        for st in walk(block.body):
            if isinstance(st, Action) and st.verb in creators and not sends(st) \
                    and (block.subject, vocab.CLIENT) not in vocab.VERBS[st.verb].places:
                self.report(st.line, st.col, st.col + len(st.verb),
                            f"This `run` block sends {vocab.PLURALS[block.subject]} (`{creator}`): `{st.verb}` "
                            "belongs in a `run` block of its own")
        if any(sends(st) for st in block.body):
            return

        def outside(body: list[Statement]) -> None:
            for st in body:
                if isinstance(st, Repeat) and any(sends(x) for x in walk(st.body)):
                    continue                                   # the macros themselves
                if isinstance(st, (Action, Wait, Expect, When)):
                    self.report(st.line, st.col, st.col + 4,
                                f"This line runs before any {block.subject} exists: move it inside the `repeat` "
                                "that sends")
                elif isinstance(st, If):
                    for _, branch in st.branches:
                        outside(branch)
                    outside(st.orelse or [])
                elif hasattr(st, "body"):
                    outside(st.body)
        outside(block.body)

    def body(self, body: list[Statement], block: Block, schema: Mapping[str, Any], trade_in_hand: bool,
             list_body: bool = False) -> None:
        for st in body:
            for e in expressions(st):
                self.expression(e, schema)
            if isinstance(st, (Wait, Expect, When)):
                self.events(st, block)
            if isinstance(st, Action):
                self.action(st, block, trade_in_hand)
            if isinstance(st, When):
                carries = all(event.trade for event in map(vocab.event_of, st.events) if event is not None)
                self.body(st.body, block, schema, trade_in_hand=bool(st.events) and carries)
            elif isinstance(st, If):
                for _, branch in st.branches:
                    self.body(branch, block, schema, trade_in_hand)
                if st.orelse:
                    self.body(st.orelse, block, schema, trade_in_hand)
            elif isinstance(st, OrderLine):
                self.order_line(st, block, schema, list_body)
            elif isinstance(st, Do):
                self.do(st, block, schema, trade_in_hand)
            elif isinstance(st, LegLine):
                self.report(st.line, st.col, st.col + 3, "A `leg` line belongs under `new multileg`, a `replace` "
                                                         "of a multileg order, or a declared strategy")
            elif isinstance(st, Action) and st.verb in LEGGED:
                self.legs(st, schema)
            elif isinstance(st, Action) and st.verb == "new list":
                self.new_list(st, block, schema)
            elif isinstance(st, Action) and st.verb == "add order":
                if st.body:
                    self.member(st.body, st, schema)
            elif hasattr(st, "body"):
                self.body(st.body, block, schema, trade_in_hand, list_body)

    def do(self, st: Do, block: Block, schema: Mapping[str, Any], trade_in_hand: bool) -> None:
        """`do NAME(…)`: a define of this macro, given one argument a
        parameter, its lines checked here — in this block, about its subject —
        as if written in place. A define may call another, not itself."""
        end = st.col + len("do ") + len(st.name)
        define = st.define
        self.called.add(st.name)
        if define is None:
            known = [d.name for d in self.defines]
            self.report(st.line, st.col, end, f"No define named {st.name!r}: `define {st.name}(…)` belongs at the "
                                              f"top of the macro{_close(st.name, known)}")
            return
        if len(st.args) != len(define.params):
            want = f"{len(define.params)} argument{'s' if len(define.params) != 1 else ''}"
            self.report(st.line, st.col, end, f"`{st.name}` takes {want} ({', '.join(define.params) or 'none'}), "
                                              f"not {len(st.args)}")
        if st.name in self.calling:
            self.report(st.line, st.col, end, f"`{st.name}` calls itself"
                        + (f" (through {', '.join(self.calling[self.calling.index(st.name) + 1:])})"
                           if self.calling[-1] != st.name else "") + ": a define runs once where it is called")
            return
        self.calling.append(st.name)
        try:
            self.body(define.body, block, schema, trade_in_hand)
        finally:
            self.calling.pop()

    def define(self, d: Define) -> None:
        """`define NAME(…)`: once, and sending nothing — what a block sends
        stays in the block."""
        end = d.col + len("define ") + len(d.name)
        if sum(1 for x in self.defines if x.name == d.name) > 1 and d is not next(x for x in self.defines
                                                                                   if x.name == d.name):
            self.report(d.line, d.col, end, f"{d.name!r} is defined twice")
        for st in walk(d.body):
            if isinstance(st, Action) and st.verb in vocab.SENDERS:
                self.report(st.line, st.col, st.col + len(st.verb),
                            f"`{st.verb}` sends a block's subject: it belongs in the block, not in `define {d.name}`")

    def legs(self, st: Action, schema: Mapping[str, Any]) -> None:
        """The `leg` lines under `new multileg` or `replace`: two at least,
        and on `new multileg` either they or a strategy, not both."""
        legs = [x for x in st.body if isinstance(x, LegLine)]
        for x in st.body:
            if not isinstance(x, LegLine):
                self.report(x.line, x.col, x.col + 4, f"Only `leg` lines stand under `{st.verb}`")
        for leg in legs:
            for e in expressions(leg):
                self.expression(e, schema)
            self.leg_line(leg)
        end = st.col + len(st.verb)
        strategy = next((t for t in st.terms if t.name == "instrument"), None)
        if st.verb == "new multileg" and strategy is not None:
            if legs:
                self.report(st.line, st.col, end, "`new multileg` takes its legs from the strategy or from its "
                                                  "`leg` lines, not both")
            self.strategy_named(strategy)
        elif st.verb == "new multileg" and len(legs) < 2 and st.template is None:
            self.report(st.line, st.col, end, "`new multileg` needs its legs: two `leg` lines or more under it, "
                                              "or a strategy — instrument: 'NAME'")
        elif st.body and len(legs) < 2:
            self.report(st.line, st.col, end, "A multileg order has two legs or more")

    def leg_line(self, leg: LegLine, declared: bool = False) -> None:
        seen: set[str] = set()
        for term in leg.terms:
            if term.name not in vocab.LEG_TERMS:
                self.report(term.line, term.col, term.col + len(term.name),
                            f"A leg has no term {term.name!r}. It takes: {', '.join(vocab.LEG_TERMS)}"
                            f"{_close(term.name, vocab.LEG_TERMS)}")
                continue
            if term.name in seen:
                self.report(term.line, term.col, term.col + len(term.name), f"{term.name!r} is given twice")
            seen.add(term.name)
            if declared and term.name == "instrument":
                self.report(term.line, term.col, term.col + len(term.name),
                            "A declared strategy's legs are written out: no `instrument` in them")
            elif declared and term.value is not None and type(term.value.node).__name__ != "Literal":
                self.report(term.value.line, term.value.col, term.value.col + len(term.value.source),
                            "An instrument's terms are written out — 'ES', 50, future — not computed")
            else:
                self.enum_words("leg", term)
            if term.name == "instrument":
                self.instrument_named(term)
                self.not_a_strategy(term)
        missing = [n for n in ("symbol", "side") if n not in seen and not (n == "symbol" and "instrument" in seen)]
        if missing:
            self.report(leg.line, leg.col, leg.col + 3, f"A leg needs {' and '.join(missing)}")

    def strategy_named(self, term: Term) -> None:
        """`new multileg instrument: 'NAME'` names a strategy: one with legs."""
        self.instrument_named(term)
        value = getattr(term.value.node, "value", None) if term.value is not None else None
        if not isinstance(value, str):
            return
        payload = self.declared.get(value) or (self.instruments or {}).get(value)
        if payload is not None and not payload.get("legs"):
            self.report(term.value.line, term.value.col, term.value.col + len(term.value.source),
                        f"{value!r} is no strategy: it has no legs. A strategy is an instrument with `leg` lines "
                        "(or saved with legs in Config › Instruments)")

    def instruments_named(self, term: Term) -> None:
        """`instruments: 'A, B'`: each declared or saved, none a strategy."""
        value = getattr(term.value.node, "value", None) if term.value is not None else None
        if not isinstance(value, str) or term.value.template:
            return                                   # computed: resolved when it runs
        for name in (n.strip() for n in value.split(",")):
            if not name:
                continue
            one = Term(term.name, term.key, None, term.value, term.line, term.col)
            one.value = Expr(repr(name), expr.parse(repr(name)), term.value.line, term.value.col)
            self.instrument_named(one)
            self.not_a_strategy(one)

    def not_a_strategy(self, term: Term) -> None:
        value = getattr(term.value.node, "value", None) if term.value is not None else None
        payload = (self.declared.get(value) or (self.instruments or {}).get(value)) if isinstance(value, str) else None
        if payload is not None and payload.get("legs"):
            self.report(term.value.line, term.value.col, term.value.col + len(term.value.source),
                        f"{value!r} is a strategy: it is sent with `new multileg instrument: {value!r}`")

    def new_list(self, st: Action, block: Block, schema: Mapping[str, Any]) -> None:
        """`new list`: its `order` lines, one at least; a list sent as one
        NewOrderList goes at once, so pacing its orders does nothing."""
        self.body(st.body, block, schema, False, list_body=True)
        if not any(isinstance(x, OrderLine) for x in walk(st.body)):
            self.report(st.line, st.col, st.col + len(st.verb),
                        "`new list` needs its orders: `order symbol: …, side: …, qty: …` lines under it")
        mode = next((t for t in st.terms if t.name == "mode"), None)
        sent = "list" if mode is None else mode.word or getattr(mode.value.node, "value", None)
        if sent in ("list", "E"):
            for x in walk(st.body):
                if isinstance(x, After) or (isinstance(x, Repeat) and (x.interval is not None or x.every is not None)):
                    self.report(x.line, x.col, x.col + 5,
                                "A list sent as one NewOrderList (mode: list) goes at once: this pacing does nothing. "
                                "`mode: orders` sends each order as it comes", severity="warning")

    def order_line(self, st: OrderLine, block: Block, schema: Mapping[str, Any], list_body: bool) -> None:
        """An `order` line: `new`'s terms, under a `new list` only."""
        if not list_body:
            self.report(st.line, st.col, st.col + 5, "An `order` line belongs under a `new list`")
            return
        as_new = Block(vocab.CLIENT, st.line, st.col, subject=vocab.ORDER)
        self.action(Action(st.line, st.col, verb="new", terms=st.terms), as_new, False)
        if st.body:
            self.member(st.body, st, schema)

    def member(self, body: list[Statement], at: Statement, outer: Mapping[str, Any]) -> None:
        """An order's own macro, under its `order` line or `add order`: a
        macro about an order already sent, as an `on sent order` block is."""
        block = Block(vocab.ATTACHED, at.line, at.col, subject=vocab.ORDER, body=body)
        names = [x.name for x in walk(body) if isinstance(x, Let)] + \
            [p for x in walk(body) if isinstance(x, Do) and x.define for p in x.define.params] + \
            [x.var for x in walk(body) if isinstance(x, Repeat) and x.var] + \
            [k for k, v in outer.items() if v is None and k not in vocab.CONTEXT_DOCS]
        self.body(body, block, vocab.scope_schema(names, vocab.ORDER, self.shared), trade_in_hand=False)

    def events(self, st: Wait | Expect | When, block: Block) -> None:
        place = (block.subject, block.kind)
        for name in st.events:
            event = vocab.event_of(name)
            if name.startswith(vocab.SIGNAL + ":"):
                self.heard(name.split(":", 1)[1], st.line, st.col, st.col + 4)
            if place not in event.places:
                there = ", ".join(sorted(n for n, e in vocab.EVENTS.items() if place in e.places))
                self.report(st.line, st.col, st.col + 4,
                            f"`{vocab.event_text(name)}` never happens in {vocab.block_name(block.kind, block.subject)}. "
                            f"Events there: {there}")

    def action(self, st: Action, block: Block, trade_in_hand: bool) -> None:
        verb = vocab.VERBS[st.verb]
        end = st.col + len(st.verb)
        if (block.subject, block.kind) not in verb.places:
            if block.kind == vocab.CLIENT and st.verb in vocab.SENDERS:
                return                                          # `sending` said which block it belongs in
            extra = " — one order per macro; send more from a `run on` block" \
                if st.verb == "new" and block.subject == vocab.ORDER else ""
            self.report(st.line, st.col, end, f"`{st.verb}` belongs in {_where(verb.places)}, not "
                                              f"{vocab.block_name(block.kind, block.subject)}{extra}")
            return

        if st.target is not None and not verb.trade:
            self.report(st.target.line, st.target.col, st.target.col + 5, f"`{st.verb}` acts on the order, not on a trade")
        if verb.trade and st.target is None and not trade_in_hand:
            self.report(st.line, st.col, end,
                        f"Which trade? `{st.verb} last trade`, `{st.verb} first trade` or `{st.verb} trade where …` "
                        f"— or write it under a `when` for an event that carries a trade")

        seen: set[str] = set()
        for term in st.terms:
            if term.name not in verb.terms:
                self.report(term.line, term.col, term.col + len(term.name),
                            f"`{st.verb}` has no term {term.name!r}. It takes: {', '.join(verb.terms)}"
                            f"{_close(term.name, verb.terms)}")
                continue
            if term.name in seen:
                self.report(term.line, term.col, term.col + len(term.name), f"{term.name!r} is given twice")
            seen.add(term.name)
            self.enum_words(st.verb, term)
            if term.name == "instrument" and st.verb != "new multileg":     # its strategy: `legs`
                self.instrument_named(term)
                self.not_a_strategy(term)
            if term.name == "instruments":
                self.instruments_named(term)
        if st.template is None:
            # A named instrument brings its symbol.
            missing = [name for name in verb.required if name not in seen
                       and not (name == "symbol" and "instrument" in seen)
                       and not (name == "price" and "leg" in seen)]     # a leg's fill: its LegPrice
            if st.verb == "rfq request" and not seen & {"symbols", "instruments"}:
                missing.append("symbols or instruments")
            if missing:
                self.report(st.line, st.col, end, f"`{st.verb}` needs {', '.join(missing)}")
        elif self.templates is not None:
            scope = verb.scope
            if st.template not in self.templates.get(scope, set()):
                self.report(st.line, st.col, end,
                            f"No {scope} template named {st.template!r}{_close(st.template, self.templates.get(scope, ()))}")


def check(text: str, *, templates: Mapping[str, Iterable[str]] | None = None,
          sessions: Iterable[str] | None = None, side: str | None = None,
          instruments: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[Macro, list[Diagnostic]]:
    """Parse and check ``text``. ``templates`` (scope -> names), ``sessions``
    and ``instruments`` (name -> `instrument_payload`) are checked against when given; a caller that does not know them says
    None and those references pass. ``side`` (client | market |
    end-to-end) is what the macro must be — the pane it is being edited in;
    without it the blocks decide: of one side it is that side's, of both
    it is end-to-end."""
    macro, diagnostics = parse(text)
    if side and side not in vocab.MACRO_KINDS:
        raise ValueError(f"A macro is a client, a market or an end-to-end macro, not {side!r}")
    macro.declared = side or ""
    checker = _Checker(templates, sessions, side, instruments)
    checker.macro(macro)
    every = sorted({*diagnostics, *checker.out}, key=lambda d: (d.line, d.col, d.message))
    return macro, every


def errors(diagnostics: Iterable[Diagnostic]) -> list[Diagnostic]:
    """The diagnostics that stop a macro being armed or run."""
    return [d for d in diagnostics if d.severity == "error"]
