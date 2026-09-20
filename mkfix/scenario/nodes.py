"""What a parsed scenario is made of. Lines count from 1, columns from 0."""

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
    """An mkio expression as it stands in the script. The positions inside
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
class Block:
    kind: str                         # vocab.MARKET | CLIENT | ATTACHED
    line: int
    col: int
    where: Expr | None = None
    session: str | None = None
    body: list[Statement] = field(default_factory=list)


@dataclass(slots=True)
class Scenario:
    name: str = ""
    seed: int | None = None
    on_error: str = "fail"            # fail | continue
    blocks: list[Block] = field(default_factory=list)

    @property
    def side(self) -> str:
        """client | market — what its blocks make it; '' with no blocks, and
        the first block's side for a script that (wrongly) mixes the two."""
        from .vocab import side_of
        return side_of(self.blocks[0].kind) if self.blocks else ""

    @property
    def needs_session(self) -> bool:
        """True when a `run` block leaves its session to be chosen at Run…"""
        return any(b.kind == "client" and not b.session for b in self.blocks)


def walk(body: list[Statement]):
    """Every statement under ``body``, depth first, in source order."""
    for st in body:
        yield st
        if isinstance(st, (When, While, Repeat)):
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
    if isinstance(st, Action):
        if st.target and st.target.where:
            yield st.target.where
        for term in st.terms:
            if term.value is not None:
                yield term.value
