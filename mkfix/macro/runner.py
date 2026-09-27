"""Arming macros and giving each matching order its macro.

The runner listens on the engine's event bus — only while something is
armed, so an idle runner costs the engine nothing — and acts through
`FixEngine.perform`, the same way in the UI's buttons use. A received order
is offered to the armed runs in the order they were armed; the first block
whose `where` is true owns it, and an order has one owner.

Three kinds of block. `on order` takes received orders and `on sent order`
takes orders sent some other way (by hand, by Message Replay): both wait,
armed, for an order to match. `run` sends its own: it starts at once, and
where its `new` sits inside a `repeat`, every pass is a macro of its own
with an order of its own, started at the pace the `repeat` asks for. A run
with nothing armed ends by itself when its last macro does.

A macro is for one side (`Macro.side`), and so is its run. Any number of
runs may be live at once, of one macro or of many: a client macro run
three times sends three sets of orders, on three sessions if asked. Runs
that wait for orders are offered them in `runs` order — their priority,
which `move` changes.
"""

from __future__ import annotations

import itertools
import logging
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from mkio import expr

from . import vocab
from .clock import Clock, Scheduler
from .functions import ENV
from .instance import DETACHED, FAILED, PASSED, STOPPED, Instance, contains_creator, event_map
from .nodes import Action, Macro, Share, walk

if TYPE_CHECKING:
    from mkfix.fix.engine import FixEngine
    from mkfix.fix.events import EngineEvent

log = logging.getLogger(__name__)

# The subject a family event's table names, and the event names a macro
# hears: the engine says `allocation accepted`, the macro `accepted`.
_KIND_OF_TABLE = {table: subject for subject, table in vocab.SUBJECT_TABLES.items()}


def _macro_kinds(kinds: tuple[str, ...], subject: str) -> tuple[str, ...]:
    prefix = subject + " "
    return tuple(k[len(prefix):] if k.startswith(prefix) else k for k in kinds)


class MacroError(Exception):
    """The runner will not do what was asked: nothing to arm, a limit passed."""


@dataclass(eq=False)
class Run:
    id: int
    macro: Macro
    seed: int
    speed: float = 1.0
    # Where `run` blocks send (over the session a block names, when given)
    # and the only session whose orders the `on` blocks are offered.
    session: str | None = None
    status: str = "armed"                 # armed | finished | stopped
    generators: list[Instance] = field(default_factory=list)
    paused: bool = False
    instances: list[Instance] = field(default_factory=list)
    log: list[tuple[float, int | None, int, str]] = field(default_factory=list)   # (time, order id, line, text)
    _gates: list[tuple[Any, Any]] = field(default_factory=list)
    # What the run's macros share (`share NAME = …`, read as shared.NAME):
    # every name the macro sets, NULL until it is.
    shared: dict[str, Any] = field(default_factory=dict)
    signals: int = 0                      # how many were sent: a runaway is stopped
    _started: dict[int, int] = field(default_factory=dict)      # `on signal` block -> how many macros it has started

    @property
    def side(self) -> str:
        return self.macro.side

    @property
    def waits(self) -> bool:
        """It has `on` blocks: it is offered orders, and stays armed until stopped."""
        return any(b.kind != vocab.CLIENT for b in self.macro.blocks)

    def session_of(self, block: Any) -> str | None:
        return self.session or block.session

    @property
    def verdict(self) -> str:
        """failed if any order's macro failed, else passed if any passed, else blank."""
        states = {i.status for i in self.instances}
        return FAILED if FAILED in states else PASSED if PASSED in states else ""

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for i in self.instances:
            out[i.status] = out.get(i.status, 0) + 1
        return out


class MacroRunner:
    BUSTED: tuple[str, ...] = ("Cancel", "TradeCancel")

    def __init__(self, engine: FixEngine, clock: Clock | None = None, *,
                 max_actions: int = 1000, max_instances: int = 10000, max_live: int = 20000,
                 max_signals: int = 100000) -> None:
        self.engine = engine
        self.scheduler = clock.scheduler if clock is not None else Scheduler()
        self.clock = clock if clock is not None else Clock(self.scheduler)
        self.max_actions, self.max_instances = max_actions, max_instances
        self.max_live = max_live              # live macros, every run together
        self.max_signals = max_signals        # signals in one run
        self.live = 0
        self.runs: list[Run] = []
        self.owners: dict[tuple[str, int], Instance] = {}     # (kind, row id) -> the macro that owns it
        self.on_change: Callable[[Instance], None] | None = None     # status, line, waiting_for
        self.on_log: Callable[[Run, Instance | None, int, str], None] | None = None
        self.on_run_finished: Callable[[Run], None] | None = None
        self._ids = itertools.count(1)
        self._unsubscribe: Callable[[], None] | None = None
        self._matchers: dict[int, Any] = {}
        self._claims: dict[str, Instance] = {}        # tag -> the macro whose `new` is going out

    # -- runs ------------------------------------------------------------------------------

    def arm(self, macro: Macro, *, session: str | None = None, seed: int | None = None,
            speed: float = 1.0, start: bool = True) -> Run:
        """Start a run: offer orders to ``macro``'s `on order` and `on sent
        order` blocks, and start its `run` blocks — on ``session`` when one is
        given, else on the session each names. The caller has checked the
        macro: a macro with errors is not armed.

        ``start=False`` arms without starting the `run` blocks, for a
        caller that must record the run before anything can happen in it —
        a macro may fail, and the run end, in its first instant — and then
        calls `start(run)`."""
        self.validate(macro, session=session, speed=speed)
        if seed is None:
            seed = macro.seed if macro.seed is not None else random.SystemRandom().randrange(2**31)
        run = Run(next(self._ids), macro, seed, speed, session)
        self._share(run)
        self.runs.append(run)
        if self._unsubscribe is None:
            self._unsubscribe = self.engine.events.subscribe(self._on_event)
        if start:
            self.start(run)
        return run

    def validate(self, macro: Macro, *, session: str | None = None, speed: float = 1.0) -> None:
        """Raise what `arm` would, and start nothing: several macros played
        together are all checked before the first of them sends an order."""
        if not macro.blocks:
            raise MacroError(f"{macro.name!r} has no block to run")
        for block in macro.blocks:
            if block.kind != vocab.CLIENT or block.signal is not None:
                continue                  # an `on signal` block sends where the macro that signalled is
            name = session or block.session
            if not name:
                raise MacroError(f"line {block.line}: `run` names no session, so choose the one to send on")
            target = self.engine.sessions.get(name)
            if target is None:
                raise MacroError(f"`run` on {name}: no such session")
            if not target.is_active:
                raise MacroError(f"`run` on {name}: the session is not active — start it, and wait "
                                    "for it to log on, before running a macro that sends on it")
        if speed <= 0:
            raise MacroError("speed must be more than zero")

    def _share(self, run: Run) -> None:
        """What the run starts with: every shared name the macro sets, NULL,
        then the `share` lines before its blocks, in order."""
        macro = run.macro
        every = [st for block in macro.blocks for st in walk(block.body) if isinstance(st, Share)]
        run.shared = dict.fromkeys(st.name for st in [*macro.shares, *every])
        for st in macro.shares:
            try:
                run.shared[st.name] = expr.compile_node(st.value.node, ENV)(
                    expr.Scope({"shared": run.shared}, None, True))
            except expr.ExprError as e:
                raise MacroError(f"line {st.line}: {e.message} — in `{st.value.source}`") from None

    def start(self, run: Run) -> None:
        """Start the run's `run` blocks."""
        for block in run.macro.blocks:
            if block.kind == vocab.CLIENT and block.signal is None:
                self._start_block(run, block)
        self._maybe_finished(run)

    def _start_block(self, run: Run, block: Any, n: int = 0, *, event: dict[str, Any] | None = None,
                     session: str | None = None) -> bool:
        """Start a sending block — a `run` at Run…, an `on signal` at its
        signal: one subject's macro when its sending verb stands among its
        own lines, else a generator whose `repeat` starts one each pass."""
        creator = vocab.CREATORS[block.subject]
        if any(isinstance(st, Action) and st.verb == creator for st in block.body) \
                or not contains_creator(block.body, block.subject):
            return self._start_script(run, block, block.body, {}, n, event=event, session=session)
        if run.status != "armed":
            return False
        generator = Instance(self, run, block, None, -1 - len(run.generators), n=n, generator=True, session=session)
        generator.main.event = event
        run.generators.append(generator)
        generator.start()
        return True

    def _start_script(self, run: Run, block: Any, body: Any, names: dict[str, Any], n: int, *,
                      event: dict[str, Any] | None = None, session: str | None = None) -> bool:
        """A sending macro, its order still to come. False when the run is
        full or over. ``event`` is what started it — the signal an `on
        signal` block was waiting for — and ``session`` where it sends."""
        if run.status != "armed":
            return False
        if len(run.instances) >= self.max_instances:
            self._log(None, block.line, f"no more orders: the run already has {self.max_instances}", run=run)
            return False
        if self.live >= self.max_live:
            self._log(None, block.line, f"no more orders: {self.max_live} macros are live already", run=run)
            return False
        instance = Instance(self, run, block, None, len(run.instances), body=body, vars=names, n=n, session=session)
        instance.main.event = event
        run.instances.append(instance)
        self.live += 1
        self._claims[instance.tag] = instance
        instance.start()
        return True

    # -- between the macros of a run ---------------------------------------------------------

    async def _signal(self, sender: Instance, name: str, value: Any, line: int, n: int = 0) -> None:
        """``sender`` said `signal 'name'`: every other live macro of its run
        hears it, and every `on signal 'name'` block whose `where` it meets
        starts a macro. It stays within the run, and the sender does not
        hear itself."""
        run = sender.run
        run.signals += 1
        if run.signals > self.max_signals:
            from .instance import ScriptError
            raise ScriptError(line, f"more than {self.max_signals} signals in one run: is something answering itself?")
        event = event_map((vocab.signal_event(name), vocab.SIGNAL), "macro")
        # A signal is a `signal` by kind, whatever its name: the name is event.name.
        event.update(kind=vocab.SIGNAL, name=name, value=value, sender=dict(sender.row) if sender.row else None,
                     subject=sender.kind, n=n)
        self._log(sender, line, f"signal {name!r}" + ("" if value is None else f" with {expr.to_string(value)}"))
        for instance in list(run.instances):
            if instance is not sender and instance.live:
                instance.deliver(event)
        for block in run.macro.blocks:
            if block.signal != name or not await self._started_by(run, block, event):
                continue
            session = run.session or (sender.row["session_id"] if sender.row else
                                      sender.session or run.session_of(sender.block))
            if not session:
                self._log(None, block.line, f"`on signal {name!r}` not started: the macro that signalled has no "
                                            "session yet, and the run names none", run=run)
                continue
            count = run._started.get(id(block), 0)
            if self._start_block(run, block, count, event=event, session=session):
                run._started[id(block)] = count + 1

    async def _started_by(self, run: Run, block: Any, event: dict[str, Any]) -> bool:
        """Whether the signal meets an `on signal` block's `where`, which
        sees the signal, what is shared and the run's rows."""
        if block.where is None:
            return True
        fn = self._matchers.get(id(block.where))
        if fn is None:
            fn = self._matchers[id(block.where)] = expr.compile_node(block.where.node, ENV)
        scope: dict[str, Any] = {"event": event, "shared": run.shared, **dict.fromkeys(vocab.PEERS.values())}
        refs = expr.field_refs(block.where.node)
        for kind, name in vocab.PEERS.items():
            if name in refs:
                scope[name] = await self._peers(run, kind)
        try:
            return expr.truthy(fn(expr.Scope(scope, None, True)))
        except expr.ExprError as e:
            self._log(None, block.line, f"`where` failed on signal {event['name']!r}: {e.message}", run=run)
            return False

    async def _peers(self, run: Run, kind: str) -> list[dict[str, Any]]:
        """The rows of ``kind`` the run's macros hold — taken or sent, live
        or done — as they stand now, oldest first."""
        ids = sorted({i.key for i in run.instances if i.kind == kind and i.key is not None})
        rows: list[dict[str, Any]] = []
        for at in range(0, len(ids), 500):
            some = ids[at:at + 500]
            cursor = await self.engine.db.read_conn.execute(
                f"SELECT * FROM {vocab.SUBJECT_TABLES[kind]} WHERE id IN ({','.join('?' * len(some))}) ORDER BY id",
                tuple(some))
            rows += [dict(r) for r in await cursor.fetchall()]
            await cursor.close()
        return rows

    def stop(self, run: Run) -> None:
        """Disarm: no new orders, and every live macro of the run stops where it is."""
        if run.status == "stopped":
            return
        run.status = "stopped"
        for instance in [*run.generators, *run.instances]:
            instance.finish(STOPPED, "run stopped")
        self._release_gates(run)
        if not any(r.status == "armed" for r in self.runs) and self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def stop_all(self) -> None:
        for run in list(self.runs):
            self.stop(run)

    def offered(self, side: str | None = None) -> list[Run]:
        """The live runs that wait for orders, first offered first."""
        return [r for r in self.runs if r.status == "armed" and r.waits and side in (None, r.side)]

    def move(self, run: Run, up: bool) -> bool:
        """One place earlier or later among its side's waiting runs. False at the end of the line."""
        peers = self.offered(run.side)
        if run not in peers:
            return False
        at = peers.index(run) + (-1 if up else 1)
        if not 0 <= at < len(peers):
            return False
        a, b = self.runs.index(run), self.runs.index(peers[at])
        self.runs[a], self.runs[b] = self.runs[b], self.runs[a]
        return True

    def pause(self, run: Run) -> None:
        """Macros park before their next line; a wait already under way runs out."""
        run.paused = True

    def resume(self, run: Run) -> None:
        run.paused = False
        self._release_gates(run)

    def _release_gates(self, run: Run) -> None:
        gates, run._gates = run._gates, []
        for flow, future in gates:
            self.scheduler.wake(flow, future)

    def detach(self, order_key: int, kind: str = vocab.ORDER) -> bool:
        """Give an order (or IOI, advert, allocation) back to whoever is at the keyboard."""
        instance = self.owners.get((kind, order_key))
        if instance is None:
            return False
        instance.finish(DETACHED, "detached by hand")
        return True

    async def settle(self) -> None:
        await self.scheduler.settle()

    # -- events ----------------------------------------------------------------------------

    def _on_event(self, ev: EngineEvent) -> None:
        first = ev.kinds[0]
        if first in ("session up", "session down"):
            for instance in list(self.owners.values()):
                if instance.row["session_id"] == ev.session_id:
                    instance.deliver(event_map(ev.kinds, "engine", text=ev.detail.get("status")))
            return
        # An event is about an order (`ev.order`) or a row of one of the
        # three families (`ev.row` in `ev.table`); the same rules apply to
        # each, under its own kind and with the family's name dropped from
        # the event's kinds.
        if ev.table:
            subject, row = _KIND_OF_TABLE.get(ev.table, ""), ev.row
            kinds = _macro_kinds(ev.kinds, subject)
        else:
            subject, row, kinds = vocab.ORDER, ev.order, ev.kinds
        if row is None or not subject:
            return
        key = (subject, row["id"])
        owner = self.owners.get(key)
        if owner is None:
            if kinds[0] == f"sent {subject}":
                tag = ev.detail.get("tag") or ""
                claimant = self._claims.pop(tag, None) if ev.source == "macro" and tag else None
                if claimant is not None and claimant.live:
                    claimant.bind(row)
                elif ev.source != "macro" or not tag:
                    # Sent by hand, by Message Replay, or by a macro as an
                    # answer that is nobody's (`new` in an `on ioi` block).
                    self._offer(ev, vocab.ATTACHED, subject, row, kinds)
            elif kinds[0] == subject and ev.source == "wire":
                self._offer(ev, vocab.MARKET, subject, row, kinds)
            return
        owner.row = row
        if ev.source == "macro":
            return                      # its own action: the row above is all it needs of it
        if ev.source == "manual":
            owner.deliver(event_map(("manual",), "manual", op=ev.detail.get("op")), ev.trade)
            return
        owner.deliver(event_map(kinds, ev.source, request=ev.request, msg=ev.msg, prev=ev.prev, **ev.detail),
                      ev.trade)

    def _offer(self, ev: EngineEvent, kind: str, subject: str, row: dict[str, Any], kinds: tuple[str, ...]) -> None:
        name = f"{subject} {row[vocab.SUBJECT_IDS[subject]]}"
        for run in self.runs:
            if run.status != "armed" or (run.session and run.session != row["session_id"]):
                continue
            for block in run.macro.blocks:
                if block.kind != kind or block.subject != subject or not self._matches(run, block, row):
                    continue
                if len(run.instances) >= self.max_instances:
                    self._log(None, block.line, f"{name} not taken: the run already has "
                                                f"{self.max_instances} orders", run=run)
                    return
                if self.live >= self.max_live:
                    self._log(None, block.line, f"{name} not taken: {self.max_live} macros are live already", run=run)
                    return
                instance = Instance(self, run, block, row, len(run.instances))
                self.live += 1
                instance.main.event = event_map(kinds, ev.source, request=ev.request, msg=ev.msg)
                run.instances.append(instance)
                self.owners[(subject, row["id"])] = instance
                instance.start()
                return

    def _matches(self, run: Run, block: Any, row: dict[str, Any]) -> bool:
        if block.where is None:
            return True
        fn = self._matchers.get(id(block.where))
        if fn is None:
            fn = self._matchers[id(block.where)] = expr.compile_node(block.where.node, ENV)
        try:
            return expr.truthy(fn(expr.Scope({**row, block.subject: row}, None, True)))
        except expr.ExprError as e:
            self._log(None, block.line, f"`where` failed on {block.subject} {row[vocab.SUBJECT_IDS[block.subject]]}: "
                                        f"{e.message}", run=run)
            return False

    # -- what instances ask of us ----------------------------------------------------------

    def _finished(self, instance: Instance) -> None:
        if instance.key is not None and self.owners.get(instance.owner_key) is instance:
            del self.owners[instance.owner_key]
        self._claims.pop(instance.tag, None)
        if not instance.generator:
            self.live -= 1
        if instance.message:
            self._log(instance, instance.line, f"{instance.status}: {instance.message}")
        self._maybe_finished(instance.run)

    def _maybe_finished(self, run: Run) -> None:
        """A run that only sends is over when its last macro is: nothing of
        it waits for orders. One with `on` blocks stays armed until stopped."""
        if run.status != "armed" or run.waits:
            return
        if any(i.live for i in [*run.generators, *run.instances]):
            return
        run.status = "finished"
        if self.on_run_finished is not None:
            self.on_run_finished(run)
        if not any(r.status == "armed" for r in self.runs) and self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _changed(self, instance: Instance) -> None:
        if self.on_change is not None:
            try:
                self.on_change(instance)
            except Exception:
                log.exception("macro on_change failed")

    def _log(self, instance: Instance | None, line: int, text: str, run: Run | None = None) -> None:
        run = run or (instance.run if instance is not None else None)
        if run is None:
            return
        run.log.append((self.clock.now(), instance.key if instance is not None else None, line, text))
        if self.on_log is not None:
            try:
                self.on_log(run, instance, line, text)
            except Exception:
                log.exception("macro on_log failed")

    async def _trades(self, instance: Instance) -> list[dict[str, Any]]:
        """The order's trades, oldest first, busted ones included: the ones we
        sent for an order we received, the ones we received for one we sent,
        both by the order's immutable order_id (a received trade's is the
        order's own; the counterparty's OrderID sits in market_order_id)."""
        order = instance.order
        if order is None:
            return []                   # an IOI, advert or allocation has no trades
        if order["direction"] == "RX":
            sql = ("SELECT * FROM fix_executions WHERE session_id = ? AND direction = 'TX' AND order_id = ? "
                   "ORDER BY id")
            params: tuple[Any, ...] = (order["session_id"], order["order_id"])
        else:
            sql = ("SELECT * FROM fix_executions WHERE session_id = ? AND direction = 'RX' AND order_id = ? "
                   "ORDER BY id")
            params = (order["session_id"], order["order_id"])
        cursor = await self.engine.db.read_conn.execute(sql, params)
        rows = [dict(r) for r in await cursor.fetchall()]
        await cursor.close()
        return rows

    async def _history(self, instance: Instance) -> list[dict[str, Any]]:
        if instance.key is None:
            return []
        table = vocab.SUBJECT_TABLES[instance.kind]
        cursor = await self.engine.db.read_conn.execute(
            f"SELECT * FROM {table}__history WHERE id = ? ORDER BY _mkio_version", (instance.key,))
        rows = [dict(r) for r in await cursor.fetchall()]
        await cursor.close()
        return rows

    async def _sent_row(self, kind: str, session_id: str, subject_id: str) -> dict[str, Any] | None:
        """The row a sending verb just created, by the ID it returned."""
        if kind == vocab.ORDER:
            return await self.engine._find_order(session_id, subject_id)
        return await self.engine._find_family_row(vocab.SUBJECT_TABLES[kind], vocab.SUBJECT_IDS[kind],
                                                  session_id, subject_id, "TX")

    async def _template(self, st: Action, instance: Instance) -> dict[str, Any]:
        """A saved template's terms as the payload takes them: every term
        column is named as the payload key (`TEMPLATE_TERM_COLS`, less the
        session, which the block or Run… gives)."""
        from mkfix.fix.engine import TEMPLATE_TERM_COLS
        from .instance import ScriptError
        scope = vocab.VERBS[st.verb].scope
        cursor = await self.engine.db.read_conn.execute(
            "SELECT * FROM fix_templates WHERE scope = ? AND name = ?", (scope, st.template))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise ScriptError(st.line, f"no {scope} template named {st.template!r}")
        row = dict(row)
        return {k: row[k] for k in TEMPLATE_TERM_COLS if k != "session_id" and row.get(k) not in (None, "")}
