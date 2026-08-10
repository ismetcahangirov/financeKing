"""The process-wide decimal context, set once at boot and nowhere else.

`docs/rules/decimal-and-money.md` names this module and this function. It is called
first thing in `fking.platform.supervisor.start`, before any other startup step, because
`Decimal` arithmetic -- including addition -- is rounded to the *context's* precision and
the traps only fire while they are installed. A money value parsed before the context is
configured is a value rounded under whatever the interpreter's default happened to be,
and nothing downstream can tell that it was.

`FloatOperation` is the load-bearing trap. Without it, `Decimal(0.1)` succeeds silently
and stores 0.1000000000000000055511151231257827021181583404541015625; with it, the
constructor raises at the line that made the mistake, which is the only place the mistake
is cheap to fix.

One boundary of that trap is worth stating, because `docs/rules/decimal-and-money.md`
overstates it and a reader who trusts the rule will not test for this: **equality
comparisons between `Decimal` and `float` stay silent even when the signal is trapped.**
CPython's documented behaviour is that with `FloatOperation` trapped, "only equality
comparisons and explicit conversions are silent" -- so `Decimal("0.1") < 0.1` raises and
`Decimal("0.1") == 0.1` quietly answers False, comparing a decimal against a binary
approximation. Nothing in the context can close that hole; `tools/checks/money_types.py`
is what stops a float reaching the comparison in the first place.

The context is process-wide state and this module is the only writer. A caller that needs
different behaviour for one calculation uses `decimal.localcontext()`, which copies rather
than mutates -- see `fking.risk.metrics` and `fking.backtest.tearsheet._chart` for the
established shape.
"""

from __future__ import annotations

from decimal import (
    ROUND_HALF_EVEN,
    Clamped,
    DecimalException,
    DivisionByZero,
    FloatOperation,
    InvalidOperation,
    Overflow,
    Underflow,
    getcontext,
)
from typing import Final

__all__ = ["DECIMAL_PRECISION", "TRAPPED_SIGNALS", "configure_decimal_context"]

# 38 significant digits, matching `NUMERIC(38, 18)` in Postgres and `decimal128(38, 18)`
# in the Parquet corpus (src/fking/data/parquet/schema.py). Equality of the three is the
# property that matters: a value representable in the database is representable in memory,
# so the round trip cannot lose digits and a reconciliation difference is never an
# artefact of where the number was standing.
DECIMAL_PRECISION: Final[int] = 38

# Every signal that would otherwise be absorbed into a quiet default. `Underflow` and
# `Clamped` look harmless and are not: both mean a value has been altered to fit, and a
# quantity altered to fit is a quantity nobody chose. `docs/rules/decimal-and-money.md`.
#
# `Inexact`, `Rounded` and `Subnormal` are deliberately absent, and so is their common
# base `DecimalException`: `Decimal("1") / Decimal("3")` signals both Inexact and Rounded,
# so trapping either -- or the base class that covers them -- makes ordinary division
# raise and leaves the process unable to compute a fee.
TRAPPED_SIGNALS: Final[tuple[type[DecimalException], ...]] = (
    Clamped,
    DivisionByZero,
    FloatOperation,
    InvalidOperation,
    Overflow,
    Underflow,
)


def configure_decimal_context() -> None:
    """Install the money contract into the current decimal context. Call once, at boot.

    Idempotent: calling it twice installs the same context. That matters because the
    supervisor calls it before it has proved anything else about the process, and a
    second call must not be a reason to abort a start that is otherwise fine.
    """
    context = getcontext()
    context.prec = DECIMAL_PRECISION
    # ROUND_HALF_EVEN for the ambient context because reported money -- PnL, fees,
    # equity -- is the arithmetic that runs through it unqualified. ROUND_HALF_UP adds
    # half a tick of expected value per rounded half, which across a year of per-fill PnL
    # is a systematic overstatement of the number the evolution engine optimises. Order
    # quantities and prices round the other way and do so explicitly, at quantization.
    context.rounding = ROUND_HALF_EVEN
    for signal in TRAPPED_SIGNALS:
        context.traps[signal] = True
