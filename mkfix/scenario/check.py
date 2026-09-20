"""What is wrong with a script that parsed: meaning, not form.

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
    Action, Block, Diagnostic, Expect, Expr, If, Let, Repeat, Scenario, Statement, Wait, When,
    expressions, walk,
)
from .parser import parse

# Which verbs save their terms under which template scope (`using 'name'`).
_TEMPLATE_SCOPE = {
    "new": "order", "replace": "order", "cancel": "cancel", "accept": "accept", "reject": "reject",
    "fill": "fill", "unsol cxl": "unsolicited", "restate": "restate", "dk": "dk", "correct": "correct",
    "bust": "bust", "renotify": "renotify",
}
_SIDE_NAMES = {
    vocab.MARKET: "an `on order` block", vocab.CLIENT: "a `run on` block",
    vocab.ATTACHED: "an `on sent order` block",
}


def _close(word: str, known: Iterable[str]) -> str:
    match = difflib.get_close_matches(word, list(known), n=1, cutoff=0.6)
    return f" — did you mean {match[0]!r}?" if match else ""


class _Checker:
    def __init__(self, templates: Mapping[str, Iterable[str]] | None, sessions: Iterable[str] | None) -> None:
        self.templates = {scope: set(names) for scope, names in templates.items()} if templates is not None else None
        self.sessions = set(sessions) if sessions is not None else None
        self.out: list[Diagnostic] = []

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

    def scenario(self, scenario: Scenario) -> None:
        if not scenario.blocks:
            self.report(1, 0, 1, "A script needs at least one block: `on order`, `on sent order` or `run on SESSION`")
        for block in scenario.blocks:
            self.block(block)

    def block(self, block: Block) -> None:
        if block.where is not None:
            self.expression(block.where, vocab.match_schema())
        if block.session is not None and self.sessions is not None and block.session not in self.sessions:
            self.report(block.line, block.col, block.col + len("run on ") + len(block.session),
                        f"No session named {block.session!r}{_close(block.session, self.sessions)}")
        names = [st.name for st in walk(block.body) if isinstance(st, Let)]
        names += [st.var for st in walk(block.body) if isinstance(st, Repeat) and st.var]
        schema = vocab.scope_schema(names)
        self.body(block.body, block, schema, trade_in_hand=False)
        if block.kind == vocab.CLIENT:
            self.sending(block)

    def sending(self, block: Block) -> None:
        """A `run on` block is one order's script when `new` stands among its
        own lines. When `new` sits inside a `repeat`, each pass is a script
        with an order of its own, and the lines around that `repeat` run
        before any order exists: they may set things up, not act or wait."""
        if not any(isinstance(st, Action) and st.verb == "new" for st in walk(block.body)):
            self.report(block.line, block.col, block.col + len("run on"),
                        "A `run on` block sends its own orders: it needs a `new`")
            return
        if any(isinstance(st, Action) and st.verb == "new" for st in block.body):
            return

        def outside(body: list[Statement]) -> None:
            for st in body:
                if isinstance(st, Repeat) and any(isinstance(x, Action) and x.verb == "new" for x in walk(st.body)):
                    continue                                   # the scripts themselves
                if isinstance(st, (Action, Wait, Expect, When)):
                    self.report(st.line, st.col, st.col + 4,
                                "This line runs before any order exists: move it inside the `repeat` that sends")
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
                carries = all(vocab.EVENTS[name].trade for name in st.events if name in vocab.EVENTS)
                self.body(st.body, block, schema, trade_in_hand=bool(st.events) and carries)
            elif isinstance(st, If):
                for _, branch in st.branches:
                    self.body(branch, block, schema, trade_in_hand)
                if st.orelse:
                    self.body(st.orelse, block, schema, trade_in_hand)
            elif hasattr(st, "body"):
                self.body(st.body, block, schema, trade_in_hand)

    def events(self, st: Wait | Expect | When, block: Block) -> None:
        for name in st.events:
            event = vocab.EVENTS[name]
            if block.kind not in event.sides:
                there = ", ".join(sorted(n for n, e in vocab.EVENTS.items() if block.kind in e.sides))
                self.report(st.line, st.col, st.col + 4,
                            f"`{name}` never happens in {_SIDE_NAMES[block.kind]}. Events there: {there}")

    def action(self, st: Action, block: Block, trade_in_hand: bool) -> None:
        verb = vocab.VERBS[st.verb]
        end = st.col + len(st.verb)
        if block.kind not in verb.sides:
            where = " or ".join(_SIDE_NAMES[s] for s in verb.sides)
            extra = " — one order per script; send more from a `run on` block" if st.verb == "new" else ""
            self.report(st.line, st.col, end, f"`{st.verb}` belongs in {where}, not {_SIDE_NAMES[block.kind]}{extra}")
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
                                f"{literal!r} is no {term.name} this script knows. Words: {', '.join(vocab.ENUMS[enum])}; "
                                f"or give the FIX code{_close('_'.join(literal.lower().split()), vocab.ENUMS[enum])}",
                                severity="warning")
        if st.template is None:
            missing = [name for name in verb.required if name not in seen]
            if missing:
                self.report(st.line, st.col, end, f"`{st.verb}` needs {', '.join(missing)}")
        elif self.templates is not None:
            scope = _TEMPLATE_SCOPE[st.verb]
            if st.template not in self.templates.get(scope, set()):
                self.report(st.line, st.col, end,
                            f"No {scope} template named {st.template!r}{_close(st.template, self.templates.get(scope, ()))}")


def check(text: str, *, templates: Mapping[str, Iterable[str]] | None = None,
          sessions: Iterable[str] | None = None) -> tuple[Scenario, list[Diagnostic]]:
    """Parse and check ``text``. ``templates`` (scope -> names) and ``sessions``
    are checked against when given; a caller that does not know them says
    None and those references pass."""
    scenario, diagnostics = parse(text)
    checker = _Checker(templates, sessions)
    checker.scenario(scenario)
    every = sorted({*diagnostics, *checker.out}, key=lambda d: (d.line, d.col, d.message))
    return scenario, every


def errors(diagnostics: Iterable[Diagnostic]) -> list[Diagnostic]:
    """The diagnostics that stop a script being armed or run."""
    return [d for d in diagnostics if d.severity == "error"]
