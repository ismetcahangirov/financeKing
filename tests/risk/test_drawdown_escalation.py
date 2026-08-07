"""What a breach does, and which component is allowed to do it.

Issue #52 names this file by path. The distinction it exists to hold is that a
strategy-level breach and a portfolio-level breach are not the same event at different
sizes: one suspends a strategy and records a scored violation, the other *requests* that
the kill switch trip. The second is a request rather than an action because flattening
the whole book is one mechanism with one owner (`FAILSAFE.md`), and a second caller that
closes positions directly is a second halt path that nobody drills.

The restart scenarios here exercise the arithmetic and the restore contract. The
end-to-end version against real PostgreSQL waits on the persistence layer for this state
(see the pull request body); nothing here mocks a database, because there is no database
call in this module to mock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

import pytest

from fking.risk.drawdown import (
    DrawdownBudgets,
    DrawdownState,
    DrawdownStateError,
    EquityMark,
    evaluate,
    open_first_time,
    restore,
    with_equity,
)

pytestmark = pytest.mark.unit

_MIDDAY: Final = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)
_STRATEGY_BUDGETS: Final = DrawdownBudgets(
    scope="strategy",
    drawdown_ratio=Decimal("0.15"),
    daily_loss_ratio=Decimal("0.03"),
)
_PORTFOLIO_BUDGETS: Final = DrawdownBudgets(
    scope="portfolio",
    drawdown_ratio=Decimal("0.10"),
    daily_loss_ratio=Decimal("0.03"),
)


def _opened(scope: Literal["strategy", "portfolio"], equity_usd: str) -> DrawdownState:
    return open_first_time(
        scope=scope,
        subject_id=f"{scope}-1",
        opening_equity_usd=Decimal(equity_usd),
        as_of_utc=_MIDDAY,
    )


def test_a_single_large_fill_trips_the_daily_limit_at_that_fill() -> None:
    """The breach is reported by the transition that caused it, not by a later sweep.

    A limit evaluated on a schedule carries the schedule's period as slack, and one fill
    can consume a whole day's budget inside it.
    """
    state = _opened("strategy", "100000")
    before = evaluate(state, _STRATEGY_BUDGETS)
    assert before.breach is None

    after_the_fill = with_equity(
        state, equity_usd=Decimal("96500"), as_of_utc=_MIDDAY + timedelta(seconds=1)
    )
    verdict = evaluate(after_the_fill, _STRATEGY_BUDGETS)

    assert verdict.breach is not None
    assert verdict.breach.limit_name == "daily_loss"
    assert verdict.breach.breached_at_utc == _MIDDAY + timedelta(seconds=1)
    assert verdict.derisk_multiplier == Decimal("0")


def test_two_sub_limit_days_either_side_of_midnight_are_caught_by_the_rolling_limit() -> None:
    """2.9% before 00:00 UTC and 2.9% after: neither fixed day breached, the pair did.

    This is the seam. Without the rolling limit a strategy losing 2.9% by 23:50 has its
    whole budget back eleven minutes later, and trend strategies cluster losses in
    exactly that shape during a reversal.
    """
    late_evening = datetime(2026, 3, 14, 23, 50, tzinfo=UTC)
    state = open_first_time(
        scope="strategy",
        subject_id="s-1",
        opening_equity_usd=Decimal("100000"),
        as_of_utc=datetime(2026, 3, 14, 8, 0, tzinfo=UTC),
    )
    state = with_equity(state, equity_usd=Decimal("97100"), as_of_utc=late_evening)
    assert evaluate(state, _STRATEGY_BUDGETS).breach is None  # 2.9% < 3%

    just_after_midnight = datetime(2026, 3, 15, 0, 1, tzinfo=UTC)
    state = with_equity(state, equity_usd=Decimal("94284"), as_of_utc=just_after_midnight)
    verdict = evaluate(state, _STRATEGY_BUDGETS)

    # The new day anchors on the equity carried across midnight, so the fixed-window
    # limit reports no loss at all for 3/15 -- the 2.9% that happened either side of the
    # boundary is invisible to it. That is the seam, stated as an assertion rather than
    # as prose, and it is why the rolling limit is the one that binds.
    assert verdict.observed_ratios["daily_loss"] < _STRATEGY_BUDGETS.daily_loss_ratio
    assert verdict.breach is not None
    assert verdict.breach.limit_name == "rolling_loss"
    assert verdict.breach.budget_ratio == Decimal("0.045")


def test_a_strategy_breach_suspends_the_strategy_and_does_not_reach_the_kill_switch() -> None:
    state = _opened("strategy", "100000")
    state = with_equity(state, equity_usd=Decimal("80000"), as_of_utc=_MIDDAY + timedelta(days=2))
    breach = evaluate(state, _STRATEGY_BUDGETS).breach

    assert breach is not None
    assert breach.limit_name == "drawdown"
    assert breach.response == "suspend_strategy"
    assert breach.observed_ratio == Decimal("0.2")
    assert breach.budget_ratio == Decimal("0.15")


def test_a_portfolio_breach_requests_a_kill_switch_trip_rather_than_acting() -> None:
    state = _opened("portfolio", "100000")
    state = with_equity(state, equity_usd=Decimal("88000"), as_of_utc=_MIDDAY + timedelta(days=2))
    breach = evaluate(state, _PORTFOLIO_BUDGETS).breach

    assert breach is not None
    assert breach.response == "request_kill_switch_trip"
    assert breach.scope == "portfolio"


def test_a_recovered_drawdown_does_not_resume_the_strategy() -> None:
    state = _opened("strategy", "100000")
    state = evaluate(
        with_equity(state, equity_usd=Decimal("80000"), as_of_utc=_MIDDAY + timedelta(days=2)),
        _STRATEGY_BUDGETS,
    ).state
    recovered = with_equity(
        state, equity_usd=Decimal("99000"), as_of_utc=_MIDDAY + timedelta(days=3)
    )
    verdict = evaluate(recovered, _STRATEGY_BUDGETS)

    assert verdict.is_halted
    assert verdict.breach is not None
    assert verdict.breach.limit_name == "drawdown"
    assert verdict.derisk_multiplier == Decimal("0")


def test_a_portfolio_budget_applied_to_strategy_state_is_refused() -> None:
    """Crossing the scopes would apply a 10% budget to a 15% subject, or the reverse."""
    with pytest.raises(DrawdownStateError, match="off by its own size"):
        evaluate(_opened("strategy", "100000"), _PORTFOLIO_BUDGETS)


def test_restoring_at_1500_utc_keeps_the_days_loss_and_the_prior_peak() -> None:
    """The restart scenario from issue #52, stated in one test.

    Peak 100000, current 98000 (a 2% day loss) at 15:00 UTC, process dies. Restored
    state reports both unchanged. A restart that recomputed from an empty in-memory
    series would report a peak of 98000 and a day loss of zero -- which is 2% of budget
    handed back, silently, at the moment the evidence says it should not be.
    """
    fifteen_hundred = datetime(2026, 3, 14, 15, 0, tzinfo=UTC)
    day_start = datetime(2026, 3, 14, 0, 0, tzinfo=UTC)
    live = restore(
        scope="strategy",
        subject_id="s-1",
        peak_equity_usd=Decimal("100000"),
        current_equity_usd=Decimal("98000"),
        day_start_utc=day_start,
        day_open_equity_usd=Decimal("100000"),
        rolling_marks=(
            EquityMark(observed_at_utc=day_start, equity_usd=Decimal("100000")),
            EquityMark(observed_at_utc=fifteen_hundred, equity_usd=Decimal("98000")),
        ),
        observed_at_utc=fifteen_hundred,
        latched_breach=None,
    )

    assert live.peak_equity_usd == Decimal("100000")
    assert live.daily_loss_ratio == Decimal("0.02")
    assert live.drawdown_ratio == Decimal("0.02")

    # And the recomputation-from-scratch alternative, for contrast: it reads clean.
    recomputed = open_first_time(
        scope="strategy",
        subject_id="s-1",
        opening_equity_usd=Decimal("98000"),
        as_of_utc=fifteen_hundred,
    )
    assert recomputed.drawdown_ratio == Decimal("0")
    assert recomputed.daily_loss_ratio == Decimal("0")


def test_a_high_water_mark_below_current_equity_is_refused_at_construction() -> None:
    """The corrupted-restore case: a peak that trails current equity cannot be stored."""
    with pytest.raises(DrawdownStateError, match="restart bug"):
        restore(
            scope="strategy",
            subject_id="s-1",
            peak_equity_usd=Decimal("90000"),
            current_equity_usd=Decimal("98000"),
            day_start_utc=datetime(2026, 3, 14, 0, 0, tzinfo=UTC),
            day_open_equity_usd=Decimal("98000"),
            rolling_marks=(EquityMark(observed_at_utc=_MIDDAY, equity_usd=Decimal("98000")),),
            observed_at_utc=_MIDDAY,
            latched_breach=None,
        )


def test_an_empty_rolling_window_is_refused_rather_than_read_as_no_loss() -> None:
    with pytest.raises(DrawdownStateError, match="never bind"):
        restore(
            scope="strategy",
            subject_id="s-1",
            peak_equity_usd=Decimal("100000"),
            current_equity_usd=Decimal("98000"),
            day_start_utc=datetime(2026, 3, 14, 0, 0, tzinfo=UTC),
            day_open_equity_usd=Decimal("100000"),
            rolling_marks=(),
            observed_at_utc=_MIDDAY,
            latched_breach=None,
        )


def test_a_naive_observation_time_is_refused() -> None:
    state = _opened("strategy", "100000")
    with pytest.raises(DrawdownStateError, match="timezone-aware"):
        with_equity(
            state,
            equity_usd=Decimal("99000"),
            as_of_utc=datetime(2026, 3, 14, 13, 0),  # noqa: DTZ001 - the point of the test
        )


def test_an_out_of_order_observation_is_refused() -> None:
    """Reordering would lower the day anchor or re-open a pruned window."""
    state = _opened("strategy", "100000")
    with pytest.raises(DrawdownStateError, match="not reorderable"):
        with_equity(state, equity_usd=Decimal("99000"), as_of_utc=_MIDDAY - timedelta(seconds=1))


def test_a_budget_above_its_compiled_in_ceiling_is_refused() -> None:
    """Configuration may only make this system more conservative (issue #47's machinery)."""
    with pytest.raises(ValueError, match="hard ceiling"):
        DrawdownBudgets(
            scope="portfolio",
            drawdown_ratio=Decimal("0.30"),
            daily_loss_ratio=Decimal("0.03"),
        )


def test_the_rolling_budget_is_derived_and_cannot_be_configured_below_the_daily_one() -> None:
    """The rolling budget is 1.5x the daily one by construction, not a free field.

    A free field could be set *below* the fixed daily budget, which reads as
    conservative and is not: the fixed limit would then never bind on its own, and the
    midnight seam would be back under a different name.
    """
    budgets = DrawdownBudgets(
        scope="strategy",
        drawdown_ratio=Decimal("0.15"),
        daily_loss_ratio=Decimal("0.02"),
    )
    assert budgets.rolling_loss_ratio == Decimal("0.03")
    assert budgets.rolling_loss_ratio > budgets.daily_loss_ratio
