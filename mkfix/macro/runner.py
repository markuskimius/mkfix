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
from .instance import DETACHED, FAILED, PASSED, STOPPED, Instance, contains_new, event_map
from .nodes import Action, Macro

if TYPE_CHECKING:
    from mkfix.fix.engine import FixEngine
    from mkfix.fix.events import EngineEvent

log = logging.getLogger(__name__)

# The template columns an action's payload takes as they are.
_TEMPLATE_KEYS = ("symbol", "side", "ord_type", "qty", "price", "tif", "dk_reason", "restate_reason",
                  "text", "extra_tags", "client", "handl_inst")
_TEMPLATE_SCOPE = {
    "new": "order", "replace": "order", "cancel": "cancel", "accept": "accept", "reject": "reject",
    "fill": "fill", "unsol cxl": "unsolicited", "restate": "restate", "dk": "dk", "correct": "correct",
    "bust": "bust", "renotify": "renotify",
}


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
                 max_actions: int = 1000, max_instances: int = 10000, max_live: int = 20000) -> None:
        self.engine = engine
        self.scheduler = clock.scheduler if clock is not None else Scheduler()
        self.clock = clock if clock is not None else Clock(self.scheduler)
        self.max_actions, self.max_instances = max_actions, max_instances
        self.max_live = max_live              # live macros, every run together
        self.live = 0
        self.runs: list[Run] = []
        self.owners: dict[int, Instance] = {}
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
            if block.kind != vocab.CLIENT:
                continue
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

    def start(self, run: Run) -> None:
        """Start the run's `run` blocks."""
        for block in run.macro.blocks:
            if block.kind != vocab.CLIENT:
                continue
            if any(isinstance(st, Action) and st.verb == "new" for st in block.body) or not contains_new(block.body):
                self._start_script(run, block, block.body, {}, 0)        # the block is one order's macro
            else:
                generator = Instance(self, run, block, None, -1 - len(run.generators), generator=True)
                run.generators.append(generator)
                generator.start()
        self._maybe_finished(run)

    def _start_script(self, run: Run, block: Any, body: Any, names: dict[str, Any], n: int) -> bool:
        """A sending macro, its order still to come. False when the run is full or over."""
        if run.status != "armed":
            return False
        if len(run.instances) >= self.max_instances:
            self._log(None, block.line, f"no more orders: the run already has {self.max_instances}", run=run)
            return False
        if self.live >= self.max_live:
            self._log(None, block.line, f"no more orders: {self.max_live} macros are live already", run=run)
            return False
        instance = Instance(self, run, block, None, len(run.instances), body=body, vars=names, n=n)
        run.instances.append(instance)
        self.live += 1
        self._claims[instance.tag] = instance
        instance.start()
        return True

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

    def detach(self, order_key: int) -> bool:
        """Give an order back to whoever is at the keyboard."""
        instance = self.owners.get(order_key)
        if instance is None:
            return False
        instance.finish(DETACHED, "detached by hand")
        return True

    async def settle(self) -> None:
        await self.scheduler.settle()

    # -- events ----------------------------------------------------------------------------

    def _on_event(self, ev: EngineEvent) -> None:
        kind = ev.kinds[0]
        if kind in ("session up", "session down"):
            for instance in list(self.owners.values()):
                if instance.order["session_id"] == ev.session_id:
                    instance.deliver(event_map(ev.kinds, "engine", text=ev.detail.get("status")))
            return
        key = ev.order_key
        if key is None:
            return
        owner = self.owners.get(key)
        if owner is None:
            if kind == "sent order":
                claimant = self._claims.pop(ev.detail.get("tag") or "", None) if ev.source == "macro" else None
                if claimant is not None and claimant.live:
                    claimant.bind(ev.order)
                elif ev.source != "macro":
                    self._offer(ev, vocab.ATTACHED)
            elif "order" in ev.kinds and ev.source == "wire":
                self._offer(ev, vocab.MARKET)
            return
        owner.order = ev.order
        if ev.source == "macro":
            return                      # its own action: the order row above is all it needs of it
        if ev.source == "manual":
            owner.deliver(event_map(("manual",), "manual", op=ev.detail.get("op")), ev.trade)
            return
        owner.deliver(event_map(ev.kinds, ev.source, request=ev.request, msg=ev.msg, prev=ev.prev, **ev.detail),
                      ev.trade)

    def _offer(self, ev: EngineEvent, kind: str) -> None:
        order = ev.order
        for run in self.runs:
            if run.status != "armed" or (run.session and run.session != order["session_id"]):
                continue
            for block in run.macro.blocks:
                if block.kind != kind or not self._matches(run, block, order):
                    continue
                if len(run.instances) >= self.max_instances:
                    self._log(None, block.line, f"order {order['cl_ord_id']} not taken: the run already has "
                                                f"{self.max_instances} orders", run=run)
                    return
                if self.live >= self.max_live:
                    self._log(None, block.line, f"order {order['cl_ord_id']} not taken: {self.max_live} macros "
                                                "are live already", run=run)
                    return
                instance = Instance(self, run, block, order, len(run.instances))
                self.live += 1
                instance.main.event = event_map(ev.kinds, ev.source, request=ev.request, msg=ev.msg)
                run.instances.append(instance)
                self.owners[order["id"]] = instance
                instance.start()
                return

    def _matches(self, run: Run, block: Any, order: dict[str, Any]) -> bool:
        if block.where is None:
            return True
        fn = self._matchers.get(id(block.where))
        if fn is None:
            fn = self._matchers[id(block.where)] = expr.compile_node(block.where.node, ENV)
        try:
            return expr.truthy(fn(expr.Scope({**order, "order": order}, None, True)))
        except expr.ExprError as e:
            self._log(None, block.line, f"`where` failed on order {order['cl_ord_id']}: {e.message}", run=run)
            return False

    # -- what instances ask of us ----------------------------------------------------------

    def _finished(self, instance: Instance) -> None:
        if instance.key is not None and self.owners.get(instance.key) is instance:
            del self.owners[instance.key]
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
        sent for an order we received, the ones we received for one we sent.
        A received trade carries the ClOrdID of its moment and the
        counterparty's OrderID, so it is found through every ClOrdID the
        order's row has held."""
        order = instance.order
        if order is None:
            return []
        if order["direction"] == "RX":
            sql = ("SELECT * FROM fix_executions WHERE session_id = ? AND direction = 'TX' AND order_id = ? "
                   "ORDER BY id")
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

    async def _history(self, instance: Instance) -> list[dict[str, Any]]:
        if instance.key is None:
            return []
        cursor = await self.engine.db.read_conn.execute(
            "SELECT * FROM fix_orders__history WHERE id = ? ORDER BY _mkio_version", (instance.key,))
        rows = [dict(r) for r in await cursor.fetchall()]
        await cursor.close()
        return rows

    async def _template(self, st: Action, instance: Instance) -> dict[str, Any]:
        from .instance import ScriptError
        scope = _TEMPLATE_SCOPE[st.verb]
        cursor = await self.engine.db.read_conn.execute(
            "SELECT * FROM fix_templates WHERE scope = ? AND name = ?", (scope, st.template))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise ScriptError(st.line, f"no {scope} template named {st.template!r}")
        row = dict(row)
        return {k: row[k] for k in _TEMPLATE_KEYS if row.get(k) not in (None, "")}
