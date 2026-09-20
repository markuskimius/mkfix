"""One order's script, running.

An `Instance` is a block bound to an order. Its *main flow* runs the block's
lines top to bottom; every `when` it passes becomes a live handler, and each
event a handler takes runs that handler's lines as a flow of its own, beside
the main one. All the flows of an instance share its names (`let` has one
flat scope) and its order; each has its own `event`, `trade`, `since` and
`n`, so a handler cannot trample what the main flow is looking at.

A flow is only ever parked in three places — an `after`, a `wait`/`expect`,
or a paused run — and everything between two parks runs without yielding to
another flow of the same instance, except across an action, which awaits the
engine. Actions of one instance go one at a time.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mkio import expr

from . import vocab
from .functions import ENV, RNG_NAME
from .nodes import (
    Action, After, Block, Expect, Expr, Finish, If, Let, Log, Repeat, Statement, Stop, Wait, When, While,
)

if TYPE_CHECKING:
    from .runner import Run, ScenarioRunner

TIMEOUT = object()
RUNAWAY_LOOPS = 1000          # passes of a loop without once waiting

# Instance.status
RUNNING, LISTENING = "running", "listening"
PASSED, FAILED, STOPPED, COMPLETED, DETACHED = "passed", "failed", "stopped", "completed", "detached"
LIVE = (RUNNING, LISTENING)


class ScriptError(Exception):
    """The script cannot go on: an expression failed, an action was refused,
    a limit was passed. Fails the instance, naming the line."""

    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line, self.message = line, message


class _Stop(Exception):
    pass


class _Verdict(Exception):
    def __init__(self, verdict: str, message: str) -> None:
        self.verdict, self.message = verdict, message


@dataclass(eq=False)
class Flow:
    name: str
    event: dict[str, Any] | None = None
    trade: dict[str, Any] | None = None
    since_at: float = 0.0
    n: int = 0
    cursor: int = 0               # how much of the instance's event log this flow has looked at
    parks: int = 0
    line: int = 0
    waiting_for: str = ""
    task: asyncio.Task | None = None
    waiter: asyncio.Future | None = None


@dataclass(eq=False)
class _Handler:
    when: When
    depth: int


def event_map(kinds: tuple[str, ...] | list[str], source: str = "script", *, request: str = "",
              msg: Any = None, prev: dict[str, Any] | None = None, **detail: Any) -> dict[str, Any]:
    """What a script sees as `event`."""
    tags = dict(msg.fields) if msg is not None else {}
    return {
        "kind": kinds[0], "kinds": list(kinds), "source": source, "request": request, "tag": tags,
        "prev": prev, "response_to": detail.get("response_to"), "reason": detail.get("reason"),
        "text": detail.get("text") or tags.get("58"), "op": detail.get("op"),
    }


def contains_new(body: list[Statement]) -> bool:
    from .nodes import walk
    return any(isinstance(st, Action) and st.verb == "new" for st in walk(body))


class Instance:
    """``order`` is None for a script that has yet to send its order — it is
    bound by `new` — and for a *generator*: the lines of a `run on` block
    outside the `repeat` that sends, which own no order at all and only
    start the scripts that do."""

    def __init__(self, runner: ScenarioRunner, run: Run, block: Block, order: dict[str, Any] | None, index: int,
                 *, body: list[Statement] | None = None, vars: dict[str, Any] | None = None, n: int = 0,
                 generator: bool = False) -> None:
        self.runner, self.run, self.block, self.index = runner, run, block, index
        self.order = order
        self.key: int | None = order["id"] if order else None
        self.body = block.body if body is None else body
        self.generator = generator
        self.vars: dict[str, Any] = dict(vars or {})
        self.rng = random.Random(f"{run.seed}:{index}")
        self.log_events: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        self.handlers: list[_Handler] = []
        self.flows: list[Flow] = []
        self.status = RUNNING
        self.message = ""
        self.actions = 0
        self.started_at = runner.clock.now()
        self._action_lock = asyncio.Lock()
        self._compiled: dict[int, tuple[Any, set[str]]] = {}
        self.main = Flow("main", since_at=self.started_at, n=n)
        self.tag = f"{run.id}:{index}"          # how the runner knows the order this script sends

    # -- life ------------------------------------------------------------------------

    @property
    def live(self) -> bool:
        return self.status in LIVE

    @property
    def line(self) -> int:
        return self.main.line

    @property
    def waiting_for(self) -> str:
        return self.main.waiting_for if self.status == RUNNING else ("events" if self.status == LISTENING else "")

    def start(self) -> None:
        self._spawn(self.main, self._run_main())

    def _spawn(self, flow: Flow, coro: Any) -> None:
        self.flows.append(flow)
        self.runner.scheduler.started(flow)
        flow.task = asyncio.get_running_loop().create_task(self._guarded(flow, coro))

    async def _guarded(self, flow: Flow, coro: Any) -> None:
        try:
            await coro
        except _Stop:
            self.finish(STOPPED, "")
        except _Verdict as v:
            self.finish(v.verdict, v.message)
        except ScriptError as e:
            self.finish(FAILED, str(e))
        except asyncio.CancelledError:
            pass
        except Exception as e:                      # a bug of ours must not take the engine's loop down
            self.finish(FAILED, f"line {flow.line}: internal error: {e!r}")
        finally:
            if flow in self.flows:
                self.flows.remove(flow)
            self.runner.scheduler.finished(flow)

    def bind(self, order: dict[str, Any]) -> None:
        """This script's order exists: from here on its events come here."""
        self.order, self.key = order, order["id"]
        self.runner.owners[self.key] = self
        self.runner._changed(self)

    async def _run_main(self) -> None:
        await self._body(self.main, self.body, 0)
        if self.handlers:
            # Live `when` lines outlive the last plain line: the desk goes on
            # answering cancels after the order has filled.
            self._set_status(LISTENING)
        else:
            self.finish(COMPLETED, "")

    def finish(self, status: str, message: str) -> None:
        if not self.live:
            return
        self.message = message
        self._set_status(status)
        current = asyncio.current_task()
        for flow in list(self.flows):
            if flow.task is not None and flow.task is not current:
                flow.task.cancel()
        self.handlers.clear()
        self.runner._finished(self)

    def _set_status(self, status: str) -> None:
        self.status = status
        self.runner._changed(self)

    # -- events ------------------------------------------------------------------------

    def deliver(self, event: dict[str, Any], trade: dict[str, Any] | None = None) -> None:
        """An event for this order. Synchronous: it may be called from the
        session's read loop. Waiting flows look again; the handlers get a
        flow of their own to decide who takes it."""
        if not self.live:
            return
        self.log_events.append((event, trade))
        for flow in self.flows:
            if flow.waiter is not None:
                self.runner.scheduler.wake(flow, flow.waiter, None)
        if any(set(h.when.events) & set(event["kinds"]) for h in self.handlers):
            flow = Flow(f"when {event['kind']}", event=event, trade=trade,
                        since_at=self.runner.clock.now(), cursor=len(self.log_events))
            self._spawn(flow, self._dispatch(flow, list(self.handlers)))

    async def _dispatch(self, flow: Flow, handlers: list[_Handler]) -> None:
        for h in handlers:
            if not set(h.when.events) & set(flow.event["kinds"]):
                continue
            flow.line = h.when.line
            if h.when.guard is None or expr.truthy(await self.value(flow, h.when.guard)):
                await self._body(flow, h.when.body, h.depth + 1)
                return                                  # the first `when` that matches wins

    # -- statements ----------------------------------------------------------------------

    async def _body(self, flow: Flow, body: list[Statement], depth: int) -> None:
        declared = len(self.handlers)
        try:
            for st in body:
                await self._gate(flow)
                flow.line = st.line
                await self._statement(flow, st, depth)
        finally:
            if depth > 0:
                del self.handlers[declared:]            # a `when` lives as long as the block it is in

    async def _statement(self, flow: Flow, st: Statement, depth: int) -> None:
        if isinstance(st, Action):
            await self._act(flow, st)
        elif isinstance(st, After):
            delay = await self._seconds(flow, st.delay)
            if st.jitter is not None:
                delay += (self.rng.random() * 2 - 1) * await self._seconds(flow, st.jitter)
            await self._sleep(flow, delay, f"after {st.delay.source}")
        elif isinstance(st, Wait):
            timeout = None if st.timeout is None else await self._seconds(flow, st.timeout)
            await self._wait(flow, st.events, st.where, timeout)
        elif isinstance(st, Expect):
            found = await self._wait(flow, st.events, st.where, await self._seconds(flow, st.within))
            if not found:
                why = await self.value(flow, st.message) if st.message is not None else \
                    f"expected {' or '.join(st.events)} within {st.within.source}"
                raise _Verdict(FAILED, f"line {st.line}: {why}")
        elif isinstance(st, When):
            self.handlers.append(_Handler(st, depth))
        elif isinstance(st, If):
            for test, branch in st.branches:
                if expr.truthy(await self.value(flow, test)):
                    await self._body(flow, branch, depth + 1)
                    break
            else:
                if st.orelse:
                    await self._body(flow, st.orelse, depth + 1)
        elif isinstance(st, While):
            await self._loop(flow, st, depth)
        elif isinstance(st, Repeat):
            await self._repeat(flow, st, depth)
        elif isinstance(st, Let):
            self.vars[st.name] = await self.value(flow, st.value)
        elif isinstance(st, Log):
            self.runner._log(self, st.line, expr.to_string(await self.value(flow, st.message)))
        elif isinstance(st, Stop):
            raise _Stop()
        elif isinstance(st, Finish):
            message = expr.to_string(await self.value(flow, st.message)) if st.message is not None else ""
            raise _Verdict(PASSED if st.verdict == "pass" else FAILED, message)

    async def _loop(self, flow: Flow, st: While, depth: int) -> None:
        dry, parks = 0, flow.parks
        while expr.truthy(await self.value(flow, st.test)):
            await self._body(flow, st.body, depth + 1)
            dry, parks = (dry + 1, parks) if flow.parks == parks else (0, flow.parks)
            if dry >= RUNAWAY_LOOPS:
                raise ScriptError(st.line, f"this loop ran {RUNAWAY_LOOPS} times without once waiting")

    async def _repeat(self, flow: Flow, st: Repeat, depth: int) -> None:
        count = await self.value(flow, st.count)
        if isinstance(count, bool) or not isinstance(count, (int, float)) or count != int(count) or count < 0:
            raise ScriptError(st.line, f"repeat needs a whole number of times, got {expr.to_string(count)!r}")
        values = await self.value(flow, st.values) if st.values is not None else None
        if st.values is not None and (not isinstance(values, list) or not values):
            raise ScriptError(st.line, "`with` needs a list with something in it")
        outer = flow.n
        spawns = self.generator and contains_new(st.body)
        try:
            for i in range(int(count)):
                flow.n = i
                if st.var:
                    self.vars[st.var] = values[i % len(values)]
                if i:
                    gap = st.interval if st.interval is not None else \
                        (await self._seconds(flow, st.every) if st.every is not None else 0)
                    if gap:
                        await self._sleep(flow, gap, "repeat")
                if spawns:
                    # One order per script: each pass is a script of its own,
                    # started at the pace asked for and not waited on.
                    await self._gate(flow)
                    if not self.runner._start_script(self.run, self.block, st.body, dict(self.vars), i):
                        return
                else:
                    await self._body(flow, st.body, depth + 1)
        finally:
            flow.n = outer

    # -- parking -------------------------------------------------------------------------

    async def _park(self, flow: Flow, future: asyncio.Future, what: str) -> Any:
        flow.parks += 1
        flow.waiting_for = what
        if flow is self.main:
            self.runner._changed(self)
        self.runner.scheduler.finished(flow)
        result = await future                           # whoever resolves it marks the flow busy first
        flow.waiting_for = ""
        return result

    async def _gate(self, flow: Flow) -> None:
        while self.run.paused:
            future = asyncio.get_running_loop().create_future()
            self.run._gates.append((flow, future))
            await self._park(flow, future, "paused")

    async def _sleep(self, flow: Flow, seconds: float, what: str) -> None:
        future = asyncio.get_running_loop().create_future()
        self.runner.clock.call_later(max(seconds, 0.0) / self.run.speed, flow, future)
        await self._park(flow, future, what)

    async def _wait(self, flow: Flow, names: list[str], where: Expr | None, timeout: float | None) -> bool:
        """The next event of ``names`` this flow has not looked at yet —
        arrived already or still to come. False on timeout, with `event` a
        `timeout` event so the script can tell."""
        clock = self.runner.clock
        deadline = None if timeout is None else clock.now() + timeout / self.run.speed
        what = "wait " + " or ".join(names)
        while True:
            while flow.cursor < len(self.log_events):
                event, trade = self.log_events[flow.cursor]
                flow.cursor += 1
                if not set(names) & set(event["kinds"]):
                    continue
                probe = Flow("probe", event=event, trade=trade, since_at=flow.since_at, n=flow.n, line=flow.line)
                if where is None or expr.truthy(await self.value(probe, where)):
                    flow.event, flow.trade, flow.since_at = event, trade, clock.now()
                    return True
            future = asyncio.get_running_loop().create_future()
            handle = None if deadline is None else clock.call_later(deadline - clock.now(), flow, future, TIMEOUT)
            flow.waiter = future
            try:
                result = await self._park(flow, future, what)
            finally:
                flow.waiter = None
                if handle is not None:
                    clock.cancel(handle)
            if result is TIMEOUT:
                flow.event, flow.trade = event_map(("timeout",)), None
                return False

    # -- values ----------------------------------------------------------------------------

    async def value(self, flow: Flow, e: Expr) -> Any:
        compiled = self._compiled.get(id(e))
        if compiled is None:
            if e.template:
                template = expr.compile_template(e.node.value, ENV)
                compiled = (lambda scope, t=template: t(scope), set(template.field_refs))
            else:
                fn = expr.compile_node(e.node, ENV)
                compiled = (lambda scope, f=fn: f(expr.Scope(scope, None, True)), expr.field_refs(e.node))
            self._compiled[id(e)] = compiled
        fn, refs = compiled
        now = self.runner.clock.now()
        scope: dict[str, Any] = {
            "order": self.order, "trade": flow.trade, "event": flow.event,
            "elapsed": (now - self.started_at) * self.run.speed,
            "since": (now - flow.since_at) * self.run.speed, "n": flow.n,
            "trades": None, "history": None, **self.vars, RNG_NAME: self.rng,
        }
        if "trades" in refs:
            scope["trades"] = await self.runner._trades(self)
        if "history" in refs:
            scope["history"] = await self.runner._history(self)
        try:
            return fn(scope)
        except expr.ExprError as err:
            raise ScriptError(e.line, f"{err.message} — in `{e.source}`") from None

    async def _seconds(self, flow: Flow, e: Expr) -> float:
        v = await self.value(flow, e)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ScriptError(e.line, f"`{e.source}` is not a duration: {expr.to_string(v)!r}")
        return float(v)

    # -- actions ---------------------------------------------------------------------------

    async def _act(self, flow: Flow, st: Action) -> None:
        verb = vocab.VERBS[st.verb]
        payload: dict[str, Any] = {}
        if st.template is not None:
            payload.update(await self.runner._template(st, self))
        for term in st.terms:
            value = term.word if term.value is None else await self.value(flow, term.value)
            enum = vocab.enum_of(st.verb, term.name)
            if enum and value is not None:
                value = vocab.enum_code(enum, value)
            payload[term.key] = "" if value is None else value
        if st.verb == "new":
            if self.order is not None:
                raise ScriptError(st.line, "this script has already sent its order: one order per script")
            payload["session_id"], payload["_tag"] = self.run.session_of(self.block), self.tag
        elif self.order is None:
            raise ScriptError(st.line, f"`{st.verb}` before `new`: this script has no order yet")
        else:
            payload["session_id"] = self.order["session_id"]
        if st.verb in ("replace", "cancel"):
            payload = {**self._as_entered(st.verb == "replace"), **payload}

        async with self._action_lock:
            if not self.live:
                raise asyncio.CancelledError()
            self.actions += 1
            if self.actions > self.runner.max_actions:
                raise ScriptError(st.line, f"more than {self.runner.max_actions} actions on one order: "
                                           "is something answering itself?")
            try:
                # The subject is read under the lock: an earlier action of
                # this order may have just renamed it.
                if verb.trade:
                    payload["exec_id"] = (await self._target(flow, st))["exec_id"]
                elif st.verb in ("replace", "cancel"):
                    payload["orig_cl_ord_id"] = self.order["cl_ord_id"]
                elif st.verb != "new":
                    payload["cl_ord_id"] = self.order["cl_ord_id"]
                result = await self.runner.engine.perform(verb.op, payload, source="scenario")
                if st.verb == "new" and self.order is None:     # nobody listened to the announcement
                    self.bind(await self.runner.engine._find_order(payload["session_id"], result["cl_ord_id"]))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                why = e.message if isinstance(e, ScriptError) else f"`{st.verb}` was refused: {e}"
                if self.run.scenario.on_error != "continue":
                    raise ScriptError(st.line, why) from None
                self.runner._log(self, st.line, why)
                self.deliver(event_map(("error",), text=why, op=verb.op))

    def _as_entered(self, replace: bool) -> dict[str, Any]:
        """What the Replace and Cancel dialogs open on: the order's last
        accepted terms, which the script's own terms then override."""
        o = self.order
        base = {"symbol": o["symbol"], "side": o["side_code"], "client": o["client"] or ""}
        if not replace:
            return {**base, "qty": o["order_qty"]}
        return {**base, "qty": o["entered_qty"] or o["order_qty"], "ord_type": o["ord_type_code"] or "2",
                "price": o["entered_price"] if o["entered_price"] is not None else "",
                "handl_inst": o["handl_inst_code"] or "1", "extra_tags": o["extra_tags"] or "",
                "expire_time": o["expire_time"] or o["expire_date"] or ""}

    async def _target(self, flow: Flow, st: Action) -> dict[str, Any]:
        if st.target is None:
            if flow.trade is None:
                raise ScriptError(st.line, f"`{st.verb}` has no trade in hand")
            return flow.trade
        trades = [t for t in await self.runner._trades(self) if t["exec_type"] not in self.runner.BUSTED]
        if st.target.which == "where":
            for trade in trades:
                probe = Flow("probe", event=flow.event, trade=trade, since_at=flow.since_at, n=flow.n)
                if expr.truthy(await self.value(probe, st.target.where)):
                    return trade
            raise ScriptError(st.line, f"no live trade of this order matches `{st.target.where.source}`")
        if not trades:
            raise ScriptError(st.line, f"`{st.verb} {st.target.which} trade`: this order has no live trade")
        return trades[-1] if st.target.which == "last" else trades[0]
