"""What a parsed macro is made of. Lines count from 1, columns from 0."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A problem at a place: what the editor underlines."""
    line: int
    col: int
    end: int                      # column just past the offending text
    message: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"line {self.line}, col {self.col + 1}: {self.message}"


@dataclass(slots=True)
class Expr:
    """An mkio expression as it stands in the macro. The positions inside
    ``node`` are columns of ``line``."""
    source: str
    node: Any
    line: int
    col: int
    template: bool = False        # a quoted string holding ${…}: evaluated as a template


@dataclass(slots=True)
class Term:
    name: str                     # as written: qty, type, reason
    key: str                      # the action payload's key for it; '' when the verb has no such term
    value: Expr | None            # None when the value is an enum word
    word: str | None              # side: buy
    line: int
    col: int


@dataclass(slots=True)
class TradeTarget:
    which: str                    # last | first | where
    where: Expr | None
    line: int
    col: int


@dataclass(slots=True)
class Statement:
    line: int
    col: int


@dataclass(slots=True)
class Action(Statement):
    verb: str = ""
    target: TradeTarget | None = None
    template: str | None = None   # using 'name'
    terms: list[Term] = field(default_factory=list)
    # `new list`: its `order` lines (a `repeat` may hold them); `add order`:
    # the order's own macro. Empty for every other verb.
    body: list[Statement] = field(default_factory=list)


@dataclass(slots=True)
class OrderLine(Statement):
    """One order of the `new list` it stands under: `new`'s terms, and the
    order's own macro in ``body`` (another subject's, so `walk` stays out)."""
    terms: list[Term] = field(default_factory=list)
    body: list[Statement] = field(default_factory=list)


@dataclass(slots=True)
class After(Statement):
    delay: Expr | None = None
    jitter: Expr | None = None


@dataclass(slots=True)
class Wait(Statement):
    events: list[str] = field(default_factory=list)
    where: Expr | None = None
    timeout: Expr | None = None


@dataclass(slots=True)
class Expect(Statement):
    events: list[str] = field(default_factory=list)
    where: Expr | None = None
    within: Expr | None = None
    message: Expr | None = None   # else fail '…'


@dataclass(slots=True)
class When(Statement):
    events: list[str] = field(default_factory=list)
    guard: Expr | None = None
    body: list[Statement] = field(default_factory=list)


@dataclass(slots=True)
class If(Statement):
    branches: list[tuple[Expr, list[Statement]]] = field(default_factory=list)
    orelse: list[Statement] | None = None


@dataclass(slots=True)
class While(Statement):
    test: Expr | None = None
    body: list[Statement] = field(default_factory=list)


@dataclass(slots=True)
class Repeat(Statement):
    count: Expr | None = None
    interval: float | None = None     # at 10/s -> 0.1
    every: Expr | None = None         # every 250ms
    var: str | None = None            # with sym = […]
    values: Expr | None = None
    body: list[Statement] = field(default_factory=list)


@dataclass(slots=True)
class Let(Statement):
    name: str = ""
    value: Expr | None = None


@dataclass(slots=True)
class Stop(Statement):
    pass


@dataclass(slots=True)
class Finish(Statement):
    verdict: str = "pass"             # pass | fail
    message: Expr | None = None


@dataclass(slots=True)
class Log(Statement):
    message: Expr | None = None


@dataclass(slots=True)
class Signal(Statement):
    name: str = ""                    # signal 'NAME'
    value: Expr | None = None         # with EXPR


@dataclass(slots=True)
class Share(Statement):
    name: str = ""                    # share NAME = EXPR
    value: Expr | None = None


@dataclass(slots=True)
class Instrument(Statement):
    name: str = ""                    # instrument 'NAME' symbol: …, sec_type: …
    terms: list[Term] = field(default_factory=list)


@dataclass(slots=True)
class Block:
    kind: str                         # vocab.MARKET | CLIENT | ATTACHED
    line: int
    col: int
    where: Expr | None = None
    session: str | None = None
    body: list[Statement] = field(default_factory=list)
    subject: str = "order"            # vocab.SUBJECTS: what the block is about; a `run`'s by the verb that sends it
    signal: str | None = None         # on signal 'NAME': the signal that starts it, once for each


@dataclass(slots=True)
class Macro:
    name: str = ""                    # the saved name, set by whoever loads it; the text carries none
    seed: int | None = None
    on_error: str = "fail"            # fail | continue
    blocks: list[Block] = field(default_factory=list)
    shares: list[Share] = field(default_factory=list)   # `share NAME = EXPR` before the blocks: what a run starts with
    instruments: list[Instrument] = field(default_factory=list)   # `instrument 'NAME' …` before the blocks
    declared: str = ""                # the side it was checked for — the editor it is kept in — when one was given

    @property
    def sides(self) -> set[str]:
        """The sides its blocks are of."""
        from .vocab import side_of
        return {side_of(b.kind, b.subject) for b in self.blocks}

    @property
    def side(self) -> str:
        """client | market | end-to-end — the side it was checked for, else
        what its blocks make it: end-to-end when they are of both sides,
        '' with no blocks."""
        from .vocab import E2E
        if self.declared:
            return self.declared
        sides = self.sides
        return E2E if len(sides) > 1 else next(iter(sides), "")

    def needs(self, side: str) -> bool:
        """Whether a `run` block of ``side`` leaves its session to be
        chosen at Run… An `on signal` block sends where the macro that
        signalled is."""
        from .vocab import side_of
        return any(b.kind == "client" and b.signal is None and not b.session
                   and side_of(b.kind, b.subject) == side for b in self.blocks)

    @property
    def needs_session(self) -> bool:
        """True when a `run` block leaves its session to be chosen at Run… —
        in an end-to-end macro, one that sends orders: the client session."""
        from .vocab import E2E
        return self.needs("client") if self.side == E2E else self.needs("client") or self.needs("market")

    @property
    def needs_market_session(self) -> bool:
        """In an end-to-end macro, whether a `run` block that sends IOIs,
        adverts or allocations leaves its session to Run…: the second of
        the two sessions such a run has."""
        from .vocab import E2E
        return self.side == E2E and self.needs("market")

    @property
    def sends(self) -> bool:
        """It has a `run` block: something goes out the moment it is run."""
        return any(b.kind == "client" and b.signal is None for b in self.blocks)


def walk(body: list[Statement]):
    """Every statement under ``body``, depth first, in source order."""
    for st in body:
        yield st
        if isinstance(st, (When, While, Repeat)) or (isinstance(st, Action) and st.verb == "new list"):
            yield from walk(st.body)
        elif isinstance(st, If):
            for _, branch in st.branches:
                yield from walk(branch)
            if st.orelse:
                yield from walk(st.orelse)


def expressions(st: Statement):
    """The expressions a statement itself holds (not those of its body)."""
    for name in ("delay", "jitter", "where", "timeout", "within", "message", "guard", "test",
                 "count", "every", "values", "value"):
        e = getattr(st, name, None)
        if isinstance(e, Expr):
            yield e
    if isinstance(st, If):
        for test, _ in st.branches:
            yield test
    if isinstance(st, OrderLine):
        for term in st.terms:
            if term.value is not None:
                yield term.value
    if isinstance(st, Action):
        if st.target and st.target.where:
            yield st.target.where
        for term in st.terms:
            if term.value is not None:
                yield term.value


def member_macros(body: list[Statement]):
    """The orders' own macros under ``body``: each `order` line of a `new
    list`, and each `add order`, with a block under it — (statement, body)."""
    for st in walk(body):
        if isinstance(st, OrderLine) and st.body:
            yield st, st.body
        elif isinstance(st, Action) and st.verb == "add order" and st.body:
            yield st, st.body
