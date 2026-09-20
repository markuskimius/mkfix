"""Text to tree.

A script is lines: a statement per line, blocks by indentation, `#` to the
end of a line for comments. Whatever computes a value is an mkio expression,
parsed in place with `expr.parse_prefix`, which ends where the expression
does — at a comma, a keyword of ours, the `±` of a jitter or the `#` of a
comment — so the statement grammar never has to find the end itself.

Parsing never stops at the first problem: a bad line is reported and
skipped, so the editor can underline everything wrong at once. `parse`
returns the tree it could build and the diagnostics; the checker adds the
problems that are about meaning rather than form.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from mkio import expr

from . import vocab
from .nodes import (
    Action, After, Block, Diagnostic, Expect, Expr, Finish, If, Let, Log, Repeat, Scenario, Statement,
    Stop, Term, TradeTarget, Wait, When, While,
)

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]*")
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_RATE = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(ms|s|m|h)\b")
_JITTER = re.compile(r"±|\+/-")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}

# Longest first, so `cancel rejected` is not read as `cancel`.
_EVENTS = sorted(vocab.EVENTS, key=lambda name: -len(name.split()))
_VERBS = sorted(vocab.VERBS, key=lambda name: -len(name.split()))
_SIMPLE = ("after", "wait", "expect", "when", "if", "else", "while", "repeat", "let", "stop", "pass", "fail", "log")
_HEADERS = ("scenario", "seed", "on", "run")


class _Problem(Exception):
    def __init__(self, message: str, col: int, end: int | None = None) -> None:
        super().__init__(message)
        self.message, self.col, self.end = message, col, end


def _code_end(text: str) -> int:
    """Where the comment starts: the first `#` outside a quoted string."""
    quote = ""
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif c == "#":
            return i
        i += 1
    return len(text)


class _Line:
    """A cursor over one line's code."""

    def __init__(self, no: int, text: str, indent: int) -> None:
        self.no, self.text, self.indent = no, text, indent
        self.pos = indent

    # -- looking ---------------------------------------------------------------

    def skip(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] == " ":
            self.pos += 1

    @property
    def done(self) -> bool:
        self.skip()
        return self.pos >= len(self.text)

    def words_ahead(self, count: int) -> list[str]:
        """Up to ``count`` bare words from here, lower-cased, without moving."""
        out, pos = [], self.pos
        for _ in range(count):
            while pos < len(self.text) and self.text[pos] == " ":
                pos += 1
            m = _WORD.match(self.text, pos)
            if not m:
                break
            out.append(m.group().lower())
            pos = m.end()
        return out

    def at_phrase(self, phrase: str) -> bool:
        words = phrase.split()
        return self.words_ahead(len(words)) == words

    # -- taking ----------------------------------------------------------------

    def take_phrase(self, phrase: str) -> bool:
        if not self.at_phrase(phrase):
            return False
        for _ in phrase.split():
            self.skip()
            self.pos = _WORD.match(self.text, self.pos).end()
        return True

    def take_char(self, ch: str) -> bool:
        self.skip()
        if self.text.startswith(ch, self.pos):
            self.pos += len(ch)
            return True
        return False

    def take_re(self, pattern: re.Pattern[str]) -> re.Match[str] | None:
        self.skip()
        m = pattern.match(self.text, self.pos)
        if m:
            self.pos = m.end()
        return m

    def first_of(self, phrases: list[str]) -> str | None:
        for phrase in phrases:
            if self.take_phrase(phrase):
                return phrase
        return None

    def expr(self, what: str, stop: tuple[str, ...] = (), until: int | None = None) -> Expr:
        """The expression here. ``until`` fences it off from text the
        expression grammar would misread (the `+/-` spelling of a jitter)."""
        self.skip()
        text = self.text if until is None else self.text[:until]
        if self.pos >= len(text):
            raise _Problem(f"Expected {what}", self.pos)
        start = self.pos
        try:
            node, end = expr.parse_prefix(text, start, stop=stop)
        except expr.ExprError as e:
            raise _Problem(e.message, e.pos if e.pos is not None else start) from None
        self.pos = end
        return Expr(self.text[start:end].rstrip(), node, self.no, start)

    def end(self) -> None:
        if not self.done:
            raise _Problem(f"Unexpected {self.text[self.pos:].split()[0]!r}", self.pos, len(self.text))


def _suggest(word: str, known: list[str]) -> str:
    close = difflib.get_close_matches(word, known, n=1, cutoff=0.7)
    return f" — did you mean {close[0]!r}?" if close else ""


class _Parser:
    def __init__(self, text: str) -> None:
        self.diagnostics: list[Diagnostic] = []
        self.lines: list[_Line] = []
        for no, raw in enumerate(text.splitlines(), 1):
            code = raw[:_code_end(raw)].rstrip()
            if not code.strip():
                continue
            lead = code[: len(code) - len(code.lstrip())]
            if "\t" in lead:
                self.report(no, 0, len(lead), "Indent with spaces, not tabs")
                code = lead.replace("\t", "    ") + code.lstrip()
                lead = code[: len(code) - len(code.lstrip())]
            self.lines.append(_Line(no, code, len(lead)))
        self.i = 0

    def report(self, line: int, col: int, end: int | None, message: str) -> None:
        self.diagnostics.append(Diagnostic(line, col, end if end is not None and end > col else col + 1, message))

    def problem(self, line: _Line, p: _Problem) -> None:
        col = p.col
        while col < len(line.text) and line.text[col] == " ":
            col += 1                  # point at the word, not the gap before it
        self.report(line.no, col, p.end if p.end is not None else len(line.text), p.message)

    # -- the file ----------------------------------------------------------------

    def parse(self) -> Scenario:
        scenario = Scenario()
        named = False
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.indent:
                self.report(line.no, 0, line.indent, "Unexpected indent: this line belongs to no block")
                self.i += 1
                continue
            self.i += 1
            try:
                if line.take_phrase("scenario"):
                    m = line.take_re(_NAME)
                    if not m:
                        raise _Problem("Expected the scenario's name", line.pos)
                    if named:
                        raise _Problem("A script names itself once", line.indent, len(line.text))
                    line.pos = m.end()
                    line.end()
                    scenario.name, named = m.group().strip(), True
                elif line.take_phrase("seed"):
                    m = line.take_re(re.compile(r"\d+"))
                    if not m:
                        raise _Problem("Expected a whole number", line.pos)
                    line.end()
                    scenario.seed = int(m.group())
                elif line.take_phrase("on error"):
                    choice = line.first_of(["continue", "fail"])
                    if not choice:
                        raise _Problem("Expected `continue` or `fail`", line.pos)
                    line.end()
                    scenario.on_error = choice
                elif line.at_phrase("on order") or line.at_phrase("on sent order") or line.at_phrase("run on"):
                    scenario.blocks.append(self.block(line))
                else:
                    word = (line.words_ahead(1) or [line.text.split()[0]])[0]
                    raise _Problem(
                        f"Expected `scenario`, `seed`, `on error`, or a block (`on order`, `on sent order`, "
                        f"`run on`), got {word!r}", line.indent, len(line.text))
            except _Problem as p:
                self.problem(line, p)
                self.skip_body(line.indent)
        if not named:
            self.report(1, 0, 1, "A script starts with `scenario NAME`")
        return scenario

    def block(self, line: _Line) -> Block:
        if line.take_phrase("run on"):
            m = line.take_re(_SESSION)
            if not m:
                raise _Problem("Expected a session name", line.pos)
            line.end()
            block = Block(vocab.CLIENT, line.no, line.indent, session=m.group())
        else:
            kind = vocab.ATTACHED if line.take_phrase("on sent order") else vocab.MARKET
            if kind == vocab.MARKET:
                line.take_phrase("on order")
            block = Block(kind, line.no, line.indent)
            if line.take_phrase("where"):
                block.where = line.expr("an expression")
            line.end()
        block.body = self.body(line)
        return block

    # -- blocks by indentation -----------------------------------------------------

    def skip_body(self, indent: int) -> None:
        while self.i < len(self.lines) and self.lines[self.i].indent > indent:
            self.i += 1

    def body(self, header: _Line) -> list[Statement]:
        """The indented lines under ``header``."""
        if self.i >= len(self.lines) or self.lines[self.i].indent <= header.indent:
            self.report(header.no, header.indent, len(header.text), "Expected an indented block under this line")
            return []
        indent = self.lines[self.i].indent
        out: list[Statement] = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.indent < indent:
                if line.indent > header.indent:
                    self.report(line.no, 0, line.indent, "This indent matches no enclosing block")
                    self.i += 1
                    continue
                break
            self.i += 1
            if line.indent > indent:
                self.report(line.no, 0, line.indent, "Unexpected indent")
                self.skip_body(indent)
                continue
            try:
                st = self.statement(line, out)
                if st is not None:
                    out.append(st)
            except _Problem as p:
                self.problem(line, p)
                self.skip_body(line.indent)
        return out

    # -- statements -----------------------------------------------------------------

    def statement(self, line: _Line, before: list[Statement]) -> Statement | None:
        at = (line.no, line.indent)

        if line.take_phrase("else"):
            if not before or not isinstance(before[-1], If) or before[-1].orelse is not None:
                raise _Problem("`else` must follow an `if` at the same indent", line.indent, len(line.text))
            owner = before[-1]
            if line.take_phrase("if"):
                test = line.expr("a condition")
                line.end()
                owner.branches.append((test, self.body(line)))
            else:
                line.end()
                owner.orelse = self.body(line)
            return None

        if line.take_phrase("after"):
            ascii_jitter = line.text.find("+/-", line.pos)
            st = After(*at, delay=line.expr("a duration", until=ascii_jitter if ascii_jitter >= 0 else None))
            if line.take_re(_JITTER):
                st.jitter = line.expr("a duration")
            line.end()
            return st

        if line.take_phrase("wait"):
            w = Wait(*at, events=self.events(line))
            if line.take_phrase("where"):
                w.where = line.expr("a condition", stop=("or timeout",))
            if line.take_phrase("or timeout"):
                w.timeout = line.expr("a duration")
            line.end()
            return w

        if line.take_phrase("expect"):
            e = Expect(*at, events=self.events(line))
            if line.take_phrase("where"):
                e.where = line.expr("a condition")
            if not line.take_phrase("within"):
                raise _Problem("Expected `within DURATION`: an expectation needs a time limit", line.pos)
            e.within = line.expr("a duration")
            if line.take_phrase("else"):
                if not line.take_phrase("fail"):
                    raise _Problem("Expected `fail 'WHY'`", line.pos)
                e.message = line.expr("the reason")
            line.end()
            return e

        if line.take_phrase("when"):
            wh = When(*at, events=self.events(line))
            if line.take_phrase("and"):
                wh.guard = line.expr("a condition")
            line.end()
            wh.body = self.body(line)
            return wh

        if line.take_phrase("if"):
            test = line.expr("a condition")
            line.end()
            return If(*at, branches=[(test, self.body(line))])

        if line.take_phrase("while"):
            wl = While(*at, test=line.expr("a condition"))
            line.end()
            wl.body = self.body(line)
            return wl

        if line.take_phrase("repeat"):
            return self.repeat(line, at)

        if line.take_phrase("let"):
            m = line.take_re(_WORD)
            if not m:
                raise _Problem("Expected a name", line.pos)
            if m.group() in vocab.CONTEXT_DOCS:
                raise _Problem(f"{m.group()!r} is the script's own name for something", m.start(), m.end())
            if not line.take_char("="):
                raise _Problem("Expected `=`", line.pos)
            st = Let(*at, name=m.group(), value=line.expr("a value"))
            line.end()
            return st

        if line.take_phrase("stop"):
            line.end()
            return Stop(*at)

        for verdict in ("pass", "fail"):
            if line.take_phrase(verdict):
                f = Finish(*at, verdict=verdict)
                if not line.done:
                    f.message = line.expr("the reason")
                elif verdict == "fail":
                    raise _Problem("Expected the reason: fail 'WHY'", line.pos)
                line.end()
                return f

        if line.take_phrase("log"):
            lg = Log(*at, message=line.expr("a value"))
            line.end()
            return lg

        verb = line.first_of(_VERBS)
        if verb:
            return self.action(line, at, verb)

        words = line.words_ahead(1)
        word = words[0] if words else line.text.strip().split()[0]
        if word in _HEADERS:
            raise _Problem(f"`{word}` belongs at the start of a line, outside any block", line.indent, len(line.text))
        known = [*_SIMPLE, *(v.split()[0] for v in vocab.VERBS)]
        raise _Problem(f"Unknown statement {word!r}{_suggest(word, known)}", line.indent, line.indent + len(word))

    def events(self, line: _Line) -> list[str]:
        out: list[str] = []
        while True:
            line.skip()
            name = line.first_of(_EVENTS)
            if not name:
                words = line.words_ahead(1)
                got = repr(words[0]) if words else "the end of the line"
                hint = _suggest(words[0], list(vocab.EVENTS)) if words else ""
                raise _Problem(f"Expected an event, got {got}{hint}", line.pos, line.pos + (len(words[0]) if words else 1))
            out.append(name)
            if line.at_phrase("or timeout") or not line.take_phrase("or"):
                return out

    def repeat(self, line: _Line, at: tuple[int, int]) -> Repeat:
        r = Repeat(*at, count=line.expr("how many times"))
        if line.take_phrase("at"):
            m = line.take_re(_RATE)
            if not m:
                raise _Problem("Expected a rate such as 10/s", line.pos)
            if float(m.group(1)) <= 0:
                raise _Problem("A rate must be more than zero", m.start(), m.end())
            r.interval = _UNIT_SECONDS[m.group(2)] / float(m.group(1))
        elif line.take_phrase("every"):
            r.every = line.expr("a duration")
        if line.take_phrase("with"):
            m = line.take_re(_WORD)
            if not m:
                raise _Problem("Expected a name", line.pos)
            if m.group() in vocab.CONTEXT_DOCS:
                raise _Problem(f"{m.group()!r} is the script's own name for something", m.start(), m.end())
            if not line.take_char("="):
                raise _Problem("Expected `=`", line.pos)
            r.var, r.values = m.group(), line.expr("a list")
        line.end()
        r.body = self.body(line)
        return r

    def action(self, line: _Line, at: tuple[int, int], verb: str) -> Action:
        act = Action(*at, verb=verb)
        line.skip()
        start = line.pos
        if line.take_phrase("last trade"):
            act.target = TradeTarget("last", None, line.no, start)
        elif line.take_phrase("first trade"):
            act.target = TradeTarget("first", None, line.no, start)
        elif line.take_phrase("trade where"):
            act.target = TradeTarget("where", line.expr("a condition"), line.no, start)
        if act.target:
            line.take_char(",")
        if line.take_phrase("using"):
            name = line.expr("a template's name in quotes")
            if not isinstance(getattr(name.node, "value", None), str):
                raise _Problem("A template is named in quotes: using 'half-fill'", name.col, line.pos)
            act.template = name.node.value
            line.take_char(",")
        spec = vocab.VERBS[verb]
        while not line.done:
            m = line.take_re(_WORD)
            if not m:
                raise _Problem("Expected a term such as `qty: 100`", line.pos)
            if not line.take_char(":"):
                raise _Problem(f"Expected `:` after {m.group()!r}", line.pos)
            name = m.group().lower()
            term = Term(name, spec.terms.get(name, ""), None, None, line.no, m.start())
            enum = vocab.enum_of(verb, name)
            ahead = line.words_ahead(1)
            if enum and ahead and ahead[0] in vocab.ENUMS[enum] and self._word_alone(line):
                line.take_re(_WORD)
                term.word = ahead[0]
            else:
                term.value = line.expr("a value")
            act.terms.append(term)
            if not line.take_char(","):
                break
        line.end()
        return act

    @staticmethod
    def _word_alone(line: _Line) -> bool:
        """The word ahead is the whole value: a comma or the end follows it."""
        line.skip()
        m = _WORD.match(line.text, line.pos)
        rest = line.text[m.end():].lstrip() if m else "x"
        return rest == "" or rest.startswith(",")


def parse(text: str) -> tuple[Scenario, list[Diagnostic]]:
    """The tree that could be built from ``text``, and what was wrong with it."""
    parser = _Parser(text)
    scenario = parser.parse()
    return scenario, sorted(parser.diagnostics, key=lambda d: (d.line, d.col))
