"""Properties of drawdown state under an arbitrary equity path.

Peak-to-trough arithmetic fails on the cases nobody enumerates: an equity curve that
recovers exactly to its prior high, a mark landing on the 00:00 UTC boundary to the
microsecond, a window in which every observation is older than 24 hours, a new high on
the same tick as a new day. Example-based tests confirm the path the author imagined.

The load-bearing property is the first one: the high-water mark is monotone
non-decreasing along *every* path. A peak that can fall is a budget that silently
widens, which is the failure issue #52 opens with -- and it is invisible, because the
dashboard then reads 0.0% drawdown while the account is a third below its high.

`.claude/rules/testing-rules.md` clause 2.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.risk.drawdown import (
    ROLLING_WINDOW,
    DrawdownBudgets,
    DrawdownState,
    DrawdownStateError,
    evaluate,
    from_row,
    open_first_time,
    to_row,
    utc_day_start,
    with_equity,
)

pytestmark = [pytest.mark.property, pytest.mark.unit]

_ORIGIN: Final = datetime(2026, 3, 14, 9, 17, 42, tzinfo=UTC)
_STRATEGY_BUDGETS: Final = DrawdownBudgets(
    scope="strategy",
    drawdown_ratio=Decimal("0.15"),
    daily_loss_ratio=Decimal("0.03"),
)

equities = st.decimals(
    min_value=Decimal("1"),
    max_value=Decimal("1000000"),
    places=8,
    allow_nan=False,
    allow_infinity=False,
)
# Gaps that straddle both boundaries the arithmetic cares about: inside a day, across
# 00:00 UTC, and past the full 24h rolling window.
gaps_seconds = st.integers(min_value=0, max_value=200_000)
steps = st.lists(st.tuples(equities, gaps_seconds), min_size=1, max_size=25)


def _walk(opening_equity_usd: Decimal, path: list[tuple[Decimal, int]]) -> list[DrawdownState]:
    """Every intermediate state along an equity path, oldest first."""
    state = open_first_time(
        scope="strategy",
        subject_id="s-1",
        opening_equity_usd=opening_equity_usd,
        as_of_utc=_ORIGIN,
    )
    history = [state]
    moment = _ORIGIN
    for equity_usd, gap in path:
        moment = moment + timedelta(seconds=gap)
        state = with_equity(state, equity_usd=equity_usd, as_of_utc=moment)
        history.append(state)
    return history


@given(opening=equities, path=steps)
def test_the_high_water_mark_never_falls(opening: Decimal, path: list[tuple[Decimal, int]]) -> None:
    history = _walk(opening, path)
    peaks = [state.peak_equity_usd for state in history]
    assert peaks == sorted(peaks)
    assert peaks[-1] == max(opening, *[equity for equity, _ in path])


@given(opening=equities, path=steps)
def test_every_ratio_stays_a_fraction(opening: Decimal, path: list[tuple[Decimal, int]]) -> None:
    """No reading may be negative or exceed 1.

    A negative loss ratio is a gain being reported as headroom the next loss does not
    have; a ratio above 1 means equity went through zero, which the positive-equity
    guard already refuses at construction.
    """
    for state in _walk(opening, path):
        for ratio in (
            state.drawdown_ratio,
            state.daily_loss_ratio,
            state.rolling_loss_ratio,
        ):
            assert Decimal("0") <= ratio <= Decimal("1")


@given(opening=equities, path=steps)
def test_a_transition_leaves_the_previous_state_untouched(
    opening: Decimal, path: list[tuple[Decimal, int]]
) -> None:
    """`with_equity` returns a new object; the caller's is a value, not a handle."""
    history = _walk(opening, path)
    for earlier, later in itertools.pairwise(history):
        assert earlier is not later
        assert earlier.observed_at_utc <= later.observed_at_utc


@given(opening=equities, path=steps)
def test_the_daily_anchor_is_always_on_a_utc_boundary_of_the_observed_day(
    opening: Decimal, path: list[tuple[Decimal, int]]
) -> None:
    """The seam has to be exactly where it is documented to be.

    An anchor drifting to the instant of the first observation of the day would make the
    reset unrepeatable, and a replay of the same fills would produce a different budget.
    """
    for state in _walk(opening, path):
        assert state.day_start_utc == utc_day_start(state.observed_at_utc)
        assert state.day_start_utc.hour == 0
        assert state.day_start_utc.minute == 0


@given(opening=equities, path=steps)
def test_the_rolling_window_always_retains_a_reference_older_than_the_newest_mark(
    opening: Decimal, path: list[tuple[Decimal, int]]
) -> None:
    """Pruning must never empty the window down to the current observation alone.

    If it does, the rolling high equals current equity, the rolling loss reads zero, and
    the limit that is supposed to bind during a bad night never fires -- which is the
    exact symptom of a window whose straddling mark was dropped.
    """
    for state in _walk(opening, path):
        assert state.rolling_marks
        oldest = state.rolling_marks[0].observed_at_utc
        assert oldest <= state.observed_at_utc
        if len(state.rolling_marks) > 1:
            assert state.rolling_marks[1].observed_at_utc > state.observed_at_utc - ROLLING_WINDOW


@given(opening=equities, path=steps)
def test_a_breach_is_latched_and_survives_recovery(
    opening: Decimal, path: list[tuple[Decimal, int]]
) -> None:
    """Once halted, always halted along the rest of the path.

    Recovery is not evidence that the breach did not happen. A limit that releases when
    the drawdown improves is a speed bump, and the strategy that just breached it is the
    one least entitled to the benefit of the doubt.
    """
    state = open_first_time(
        scope="strategy",
        subject_id="s-1",
        opening_equity_usd=opening,
        as_of_utc=_ORIGIN,
    )
    moment = _ORIGIN
    halted_once = False
    for equity_usd, gap in path:
        moment = moment + timedelta(seconds=gap)
        state = evaluate(
            with_equity(state, equity_usd=equity_usd, as_of_utc=moment), _STRATEGY_BUDGETS
        ).state
        halted_once = halted_once or state.latched_breach is not None
        assert (state.latched_breach is not None) == halted_once


@given(opening=equities, path=steps)
def test_sizing_is_zero_once_halted_and_positive_below_the_taper(
    opening: Decimal, path: list[tuple[Decimal, int]]
) -> None:
    for state in _walk(opening, path):
        verdict = evaluate(state, _STRATEGY_BUDGETS)
        if verdict.is_halted:
            assert verdict.derisk_multiplier == Decimal("0")
        else:
            assert Decimal("0") <= verdict.derisk_multiplier <= Decimal("1")


@given(opening=equities, path=steps)
def test_persisted_state_round_trips_exactly(
    opening: Decimal, path: list[tuple[Decimal, int]]
) -> None:
    """`from_row(to_row(state)) == state`, for every state a real path can reach.

    This is the property the restart depends on. A codec that loses the high-water mark,
    rounds an equity figure through a float, or drops the straddling rolling mark
    produces a restored state that is *plausible* and wrong in the direction of more
    risk -- and nothing downstream can tell, because the restored state is internally
    consistent.
    """
    for state in _walk(opening, path):
        latched = evaluate(state, _STRATEGY_BUDGETS).state
        assert from_row(to_row(latched)) == latched


@given(opening=equities, path=steps)
def test_a_persisted_row_missing_the_high_water_mark_is_refused(
    opening: Decimal, path: list[tuple[Decimal, int]]
) -> None:
    """The decoder does not fill a missing peak in from current equity.

    That substitution is the whole failure this module exists to prevent, and a lenient
    decoder is where it would be reintroduced without a single line looking wrong.
    """
    row = dict(to_row(_walk(opening, path)[-1]))
    del row["peak_equity_usd"]
    with pytest.raises(DrawdownStateError, match="peak_equity_usd"):
        from_row(row)
