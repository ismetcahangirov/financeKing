"""Shared plan fixtures for the CPCV tests, plus the toy series the leakage tests use.

The declaration is the worked example from `BACKTEST_ENGINE.md` section 6.2 -- a four-hour
feature lookback and a six-hour maximum hold, whose stated embargo floor is ten hours --
paired with a 24-hour label horizon. Reused from one place so that a change to the gap
derivation moves every test at once rather than the ones somebody remembered.

`pseudo_returns` is a linear congruential generator written out longhand rather than a
`random.Random`. Two reasons, and neither is stylistic: the sequence must be identical on
every machine and under every `pytest-randomly` shuffle, and it must be identical between
the leaking run and the embargoed run of the same leakage test -- the whole assertion is a
delta between two Sharpes computed over the *same* series, and a generator reseeded by the
test runner between them would turn that delta into a coin flip.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from fking.backtest.cpcv import CpcvPlan
from fking.backtest.walkforward import WalkForwardDeclaration

DECLARATION: Final = WalkForwardDeclaration(
    label_horizon=timedelta(hours=24),
    availability_lag=timedelta(0),
    max_feature_lookback=timedelta(hours=4),
    max_holding_horizon=timedelta(hours=6),
)

WINDOW_START: Final = datetime(2024, 1, 1, tzinfo=UTC)
WINDOW_END: Final = datetime(2024, 7, 1, tzinfo=UTC)

#: The issue's own worked example: `C(8, 2) = 28` paths.
GROUP_TOTAL: Final = 8
TEST_GROUP_SIZE: Final = 2

#: Written out rather than derived, so that a test asserting "twenty-eight paths charge
#: twenty-eight trials" cannot silently become a test about a different number when the
#: shape constants move. `test_cpcv_partition` asserts the two agree.
PATH_TOTAL: Final = 28


def plan_for(
    *,
    group_total: int = GROUP_TOTAL,
    test_group_size: int = TEST_GROUP_SIZE,
    declaration: WalkForwardDeclaration = DECLARATION,
    embargo: timedelta | None = None,
    start_utc: datetime = WINDOW_START,
    end_utc: datetime = WINDOW_END,
) -> CpcvPlan:
    """A plan over six months, defaulting to the declaration's own embargo.

    `embargo=None` means "the sanctioned value", which is `declaration.embargo` and never
    below the floor. A test that wants a shorter one passes it explicitly, which is the
    only way to reach the refusal path.
    """
    return CpcvPlan(
        start_utc=start_utc,
        end_utc=end_utc,
        group_total=group_total,
        test_group_size=test_group_size,
        declaration=declaration,
        embargo=declaration.embargo if embargo is None else embargo,
    )


def pseudo_returns(bar_total: int) -> tuple[Decimal, ...]:
    """A deterministic, zero-mean-ish return series with no edge in it.

    A 32-bit LCG (the `glibc` constants) scaled into [-0.01, 0.01). The series carries no
    predictable structure, which is the point: any Sharpe a leakage test measures above
    the baseline came from the leak and not from the data.
    """
    state = 20260810
    values: list[Decimal] = []
    for _ in range(bar_total):
        state = (1103515245 * state + 12345) % (2**31)
        values.append((Decimal(state % 2001) - Decimal(1000)) / Decimal(100000))
    return tuple(values)


def bar_times(bar_total: int, *, start_utc: datetime = WINDOW_START) -> tuple[datetime, ...]:
    """Hourly, timezone-aware UTC bar timestamps."""
    return tuple(start_utc + index * timedelta(hours=1) for index in range(bar_total))
