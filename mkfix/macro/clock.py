"""Time for macros, real or virtual, and knowing when they have all gone quiet.

A macro spends its life parked — in an `after`, or waiting for an event —
and busy only briefly in between, though "briefly" includes real database
writes on another thread. The `Scheduler` counts the flows that are busy, so
`settle()` can wait for exactly that: every flow parked again. A virtual
clock needs it to step time without racing those writes, and a test needs it
to know a delivered event has been fully answered.

The accounting is by flow, not by count: a flow is busy from the moment
something decides to wake it (`wake` marks it before setting its future)
until it parks again or ends, and marking or clearing twice is harmless —
which is what makes cancelling a flow at any point safe.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from typing import Any


class Scheduler:
    def __init__(self) -> None:
        self.busy: set[Any] = set()
        self._quiet = asyncio.Event()
        self._quiet.set()

    def started(self, flow: Any) -> None:
        """``flow`` was created or woken: busy until it parks or ends."""
        self.busy.add(flow)
        self._quiet.clear()

    def finished(self, flow: Any) -> None:
        """``flow`` parked, or ended."""
        self.busy.discard(flow)
        if not self.busy:
            self._quiet.set()

    def wake(self, flow: Any, future: asyncio.Future, value: Any = None) -> bool:
        """Resolve a parked flow's future; False if something else already had."""
        if future.done():
            return False
        self.started(flow)
        future.set_result(value)
        return True

    async def settle(self) -> None:
        """Return once no flow is busy."""
        while self.busy:
            await self._quiet.wait()
            await asyncio.sleep(0)      # let a flow that was just woken be counted


class Clock:
    """Wall-clock time through the event loop."""

    def __init__(self, scheduler: Scheduler) -> None:
        self.scheduler = scheduler

    def now(self) -> float:
        return time.monotonic()

    def call_later(self, delay: float, flow: Any, future: asyncio.Future, value: Any = None) -> Any:
        return asyncio.get_running_loop().call_later(max(delay, 0.0), self.scheduler.wake, flow, future, value)

    def cancel(self, handle: Any) -> None:
        handle.cancel()


class VirtualClock(Clock):
    """Time that moves only when told to: `await clock.advance(seconds)` runs
    every timer due in that span, in order, letting the macros settle after
    each — so a test of ten minutes of fills takes as long as its writes."""

    def __init__(self, scheduler: Scheduler) -> None:
        super().__init__(scheduler)
        self._now = 0.0
        self._timers: list[tuple[float, int, Any, asyncio.Future, Any]] = []
        self._ids = itertools.count()
        self._cancelled: set[int] = set()

    def now(self) -> float:
        return self._now

    def call_later(self, delay: float, flow: Any, future: asyncio.Future, value: Any = None) -> Any:
        handle = next(self._ids)
        heapq.heappush(self._timers, (self._now + max(delay, 0.0), handle, flow, future, value))
        return handle

    def cancel(self, handle: Any) -> None:
        self._cancelled.add(handle)

    async def advance(self, seconds: float) -> None:
        target = self._now + seconds
        await self.scheduler.settle()
        while self._timers and self._timers[0][0] <= target:
            when, handle, flow, future, value = heapq.heappop(self._timers)
            if handle in self._cancelled:
                self._cancelled.discard(handle)
                continue
            self._now = when
            if self.scheduler.wake(flow, future, value):
                await self.scheduler.settle()
        self._now = target

    @property
    def pending(self) -> int:
        return sum(1 for _, handle, _, future, _ in self._timers
                   if handle not in self._cancelled and not future.done())
