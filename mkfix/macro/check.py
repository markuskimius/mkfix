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

from . import vocab
from .functions import ENV
from .nodes import (
    Action, Block, Diagnostic, Expect, Expr, If, Let, Repeat, Macro, Share, Signal, Statement, Wait, When,
    expressions, walk,
)
from .parser import parse

_SIDE_BLOCKS = {
    "market": "`on order`, a `run` that sends IOIs, adverts or allocations, and `on sent ioi/advert/allocation`",
    "client": "a `run` that sends orders, `on sent order`, and `on ioi/advert/allocation`",
}


def _where(places: frozenset[tuple[str, str]]) -> str:
    """The blocks a verb or event may stand in, named."""
    return " or ".join(vocab.block_name(kind, subject) for subject, kind in sorted(places))


def _close(word: str, known: Iterable[str]) -> str:
    match = difflib.get_close_matches(word, list(known), n=1, cutoff=0.6)
    return f" — did you mean {match[0]!r}?" if match else ""


class _Checker:
    def __init__(self, templates: Mapping[str, Iterable[str]] | None, sessions: Iterable[str] | None,
                 side: str | None = None) -> None:
        self.side = side
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
        for block in macro.blocks:
            self.block(block)

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
        creators = set(vocab.CREATORS.values())
        header = "on signal" if block.signal is not None else "run"
        if not any(isinstance(st, Action) and st.verb == creator for st in walk(block.body)):
            self.report(block.line, block.col, block.col + len(header),
                        f"A{'n' if header[0] == 'o' else ''} `{header}` block sends something of its own: it needs a "
                        "`new`, `ioi`, `advert` or `allocate`"
                        + (". To react to a signal in a macro that already has its order, write "
                           "`when signal 'NAME'` inside that block" if block.signal is not None else ""))
            return
        for st in walk(block.body):
            if isinstance(st, Action) and st.verb in creators and st.verb != creator:
                self.report(st.line, st.col, st.col + len(st.verb),
                            f"This `run` block sends {vocab.PLURALS[block.subject]} (`{creator}`): `{st.verb}` "
                            "belongs in a `run` block of its own")
        if any(isinstance(st, Action) and st.verb == creator for st in block.body):
            return

        def outside(body: list[Statement]) -> None:
            for st in body:
                if isinstance(st, Repeat) and any(isinstance(x, Action) and x.verb == creator for x in walk(st.body)):
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

    def body(self, body: list[Statement], block: Block, schema: Mapping[str, Any], trade_in_hand: bool) -> None:
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
            elif hasattr(st, "body"):
                self.body(st.body, block, schema, trade_in_hand)

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
            if block.kind == vocab.CLIENT and st.verb in vocab.CREATORS.values():
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
            enum = vocab.enum_of(st.verb, term.name)
            literal = getattr(term.value.node, "value", None) if term.value is not None else None
            if enum and isinstance(literal, str) and not term.value.template:
                code = vocab.enum_code(enum, literal)
                if code == literal and literal not in vocab.ENUMS[enum].values() and len(literal) > 2:
                    self.report(term.value.line, term.value.col, term.value.col + len(term.value.source),
                                f"{literal!r} is no {term.name} this macro knows. Words: {', '.join(vocab.ENUMS[enum])}; "
                                f"or give the FIX code{_close('_'.join(literal.lower().split()), vocab.ENUMS[enum])}",
                                severity="warning")
        if st.template is None:
            missing = [name for name in verb.required if name not in seen]
            if missing:
                self.report(st.line, st.col, end, f"`{st.verb}` needs {', '.join(missing)}")
        elif self.templates is not None:
            scope = verb.scope
            if st.template not in self.templates.get(scope, set()):
                self.report(st.line, st.col, end,
                            f"No {scope} template named {st.template!r}{_close(st.template, self.templates.get(scope, ()))}")


def check(text: str, *, templates: Mapping[str, Iterable[str]] | None = None,
          sessions: Iterable[str] | None = None, side: str | None = None) -> tuple[Macro, list[Diagnostic]]:
    """Parse and check ``text``. ``templates`` (scope -> names) and ``sessions``
    are checked against when given; a caller that does not know them says
    None and those references pass. ``side`` (client | market |
    end-to-end) is what the macro must be — the pane it is being edited in;
    without it the blocks decide: of one side it is that side's, of both
    it is end-to-end."""
    macro, diagnostics = parse(text)
    if side and side not in vocab.MACRO_KINDS:
        raise ValueError(f"A macro is a client, a market or an end-to-end macro, not {side!r}")
    macro.declared = side or ""
    checker = _Checker(templates, sessions, side)
    checker.macro(macro)
    every = sorted({*diagnostics, *checker.out}, key=lambda d: (d.line, d.col, d.message))
    return macro, every


def errors(diagnostics: Iterable[Diagnostic]) -> list[Diagnostic]:
    """The diagnostics that stop a macro being armed or run."""
    return [d for d in diagnostics if d.severity == "error"]
