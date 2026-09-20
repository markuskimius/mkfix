"""Functions only a scenario may call.

Registered as an opt-in mkio library, so they never pass validation for an
expression the browser must evaluate (app.json's gates and filters).
"""

from __future__ import annotations

import random
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from mkio import expr

LIBRARY = "scenario"
RNG_NAME = "__rng"        # where the runner binds a run's seeded generator; `__*` is unreadable from a script


def _random(ctx: Any) -> float:
    rng = ctx.scope.vars.get(RNG_NAME) if ctx.scope.parent is None else None
    scope = ctx.scope
    while rng is None and scope is not None:
        rng = scope.vars.get(RNG_NAME)
        scope = scope.parent
    return (rng or random).random()


def _tick(price: Any, size: Any) -> Any:
    """Arithmetic on prices leaves binary noise (100.1 + 0.2); a tag must not carry it."""
    if price is None:
        return None
    if isinstance(price, bool) or not isinstance(price, (int, float)) \
            or isinstance(size, bool) or not isinstance(size, (int, float)):
        raise expr.ExprError("TICK requires two numbers")
    if size <= 0:
        raise expr.ExprError("TICK size must be positive")
    step = Decimal(repr(size))
    ticks = (Decimal(repr(price)) / step).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    value = float(ticks * step)
    return int(value) if value.is_integer() else value


expr.register_library(LIBRARY, {
    "RANDOM": (_random, {"lazy": True, "doc": "A number in [0, 1) from the run's seeded generator: the same run, the same numbers."}),
    "TICK": (_tick, {"numeric": True, "params": ("price", "size"), "doc": "`price` rounded to the nearest multiple of `size`, free of floating-point noise: TICK(order.price + 0.05, 0.01)."}),
}, default=False)

ENV = expr.Env(extra=[LIBRARY])
