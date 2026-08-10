"""Properties of the twelve trigger evaluators under arbitrary equity paths and samples.

The threshold arithmetic is where a kill switch quietly stops working, and it fails on
the inputs nobody enumerates: an equity path that recovers exactly to its five-minute
high, a rejection sample of exactly twenty with exactly four rejections, a skew that is
negative, a gap series whose 99th percentile lands between two observations.

Two properties carry the weight. **Monotonicity**: a larger loss never fires fewer
triggers, so a threshold cannot be escaped by losing more. And **velocity's
independence**: there exist paths on which trigger 4 fires with the daily budget
untouched, which is the whole reason the trigger exists.

`docs/rules/testing-rules.md` clause 2.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from fking.risk.drawdown import DrawdownState, EquityMark
from fking.risk.triggers import (
    LOSS_VELOCITY_WINDOW,
    MINIMUM_GAP_OBSERVATIONS,
    REJECTION_SAMPLE_ORDERS,
    OrderOutcome,
    TriggerId,
    TriggerObservationError,
    TriggerObservations,
    TriggerThresholds,
    derive_p99_inter_tick_gap_seconds,
    evaluate_triggers,
    loss_velocity_ratio,
)

pytestmark = [pytest.mark.property, pytest.mark.unit]

_NOW: Final = datetime(2026, 8, 1, 14, 30, tzinfo=UTC)
_DAY_START: Final = datetime(2026, 8, 1, tzinfo=UTC)
_CAUSE: Final = UUID("11111111-1111-4111-8111-111111111111")
_OPENING: Final = Decimal("100000")

_equities = st.integers(min_value=1, max_value=100_000).map(lambda cents: Decimal(cents) * 10)
_within_window = st.integers(min_value=0, max_value=4)
_outside_window = st.integers(min_value=6, max_value=1400)


def _state(
    *,
    current_equity_usd: Decimal,
    day_open_equity_usd: Decimal = _OPENING,
    peak_equity_usd: Decimal = _OPENING,
    marks: tuple[EquityMark, ...] = (),
) -> DrawdownState:
    fallback = (EquityMark(observed_at_utc=_NOW - timedelta(hours=12), equity_usd=_OPENING),)
    return DrawdownState(
        scope="portfolio",
        subject_id="portfolio",
        peak_equity_usd=peak_equity_usd,
        current_equity_usd=current_equity_usd,
        day_start_utc=_DAY_START,
        day_open_equity_usd=day_open_equity_usd,
        rolling_marks=marks or fallback,
        observed_at_utc=_NOW,
    )


def _observations(state: DrawdownState) -> TriggerObservations:
    return TriggerObservations(correlation_id=_CAUSE, observed_at_utc=_NOW, drawdown_state=state)


@given(current=_equities)
def test_a_larger_loss_never_fires_fewer_triggers(current: Decimal) -> None:
    """Monotone in the loss. A threshold that can be escaped by losing more is not one."""
    assume(current <= _OPENING)
    worse = max(Decimal("10"), current / 2)

    fired = {
        trigger.trigger_id
        for trigger in evaluate_triggers(_observations(_state(current_equity_usd=current))).firing
    }
    fired_worse = {
        trigger.trigger_id
        for trigger in evaluate_triggers(_observations(_state(current_equity_usd=worse))).firing
    }
    assert fired <= fired_worse


@given(current=_equities, minutes_ago=_within_window)
def test_velocity_measures_from_the_window_high_and_never_reports_a_gain_as_a_loss(
    current: Decimal, minutes_ago: int
) -> None:
    mark = EquityMark(observed_at_utc=_NOW - timedelta(minutes=minutes_ago), equity_usd=_OPENING)
    assume(current <= _OPENING)
    observed = loss_velocity_ratio(_state(current_equity_usd=current, marks=(mark,)))

    assert observed >= Decimal("0")
    assert observed == (_OPENING - current) / _OPENING


@given(current=_equities, minutes_ago=_outside_window)
def test_a_mark_outside_the_window_cannot_contribute_to_velocity(
    current: Decimal, minutes_ago: int
) -> None:
    """The same loss, moved further into the past, is not a velocity event."""
    assume(current <= _OPENING)
    mark = EquityMark(observed_at_utc=_NOW - timedelta(minutes=minutes_ago), equity_usd=_OPENING)
    assert loss_velocity_ratio(_state(current_equity_usd=current, marks=(mark,))) == Decimal("0")
    assert timedelta(minutes=5) == LOSS_VELOCITY_WINDOW


@given(lost_ratio=st.integers(min_value=15, max_value=29))
def test_velocity_fires_inside_an_untouched_daily_budget(lost_ratio: int) -> None:
    """Trigger 4's independence, over the whole band between the two thresholds.

    Anything from 1.5% to 2.9% lost inside five minutes fires velocity, and none of it
    touches the daily budget when the day opened where equity now stands.
    """
    fraction = Decimal(lost_ratio) / Decimal("1000")
    current = _OPENING * (Decimal("1") - fraction)
    state = _state(
        current_equity_usd=current,
        day_open_equity_usd=current,
        marks=(EquityMark(observed_at_utc=_NOW - timedelta(minutes=2), equity_usd=_OPENING),),
    )
    fired = {trigger.trigger_id for trigger in evaluate_triggers(_observations(state)).firing}

    assert TriggerId.LOSS_VELOCITY in fired
    assert TriggerId.DAILY_LOSS not in fired
    assert state.daily_loss_ratio == Decimal("0")


@given(rejected=st.integers(min_value=0, max_value=REJECTION_SAMPLE_ORDERS))
def test_the_rejection_trigger_fires_exactly_above_a_fifth_of_the_sample(rejected: int) -> None:
    outcomes: tuple[OrderOutcome, ...] = tuple(
        "rejected" if index < rejected else "accepted" for index in range(REJECTION_SAMPLE_ORDERS)
    )
    evaluation = evaluate_triggers(
        TriggerObservations(
            correlation_id=_CAUSE,
            observed_at_utc=_NOW,
            drawdown_state=_state(current_equity_usd=_OPENING),
            recent_order_outcomes=outcomes,
        )
    )
    fired = {trigger.trigger_id for trigger in evaluation.firing}
    assert (TriggerId.ORDER_REJECTION_RATE in fired) is (
        Decimal(rejected) / Decimal(REJECTION_SAMPLE_ORDERS) > Decimal("0.20")
    )


@given(sample=st.integers(min_value=0, max_value=REJECTION_SAMPLE_ORDERS - 1))
def test_no_sample_shorter_than_twenty_can_fire_the_rejection_trigger(sample: int) -> None:
    """However bad a short sample looks, it is a sample of that size and nothing more."""
    outcomes: tuple[OrderOutcome, ...] = tuple("rejected" for _ in range(sample))
    evaluation = evaluate_triggers(
        TriggerObservations(
            correlation_id=_CAUSE,
            observed_at_utc=_NOW,
            drawdown_state=_state(current_equity_usd=_OPENING),
            recent_order_outcomes=outcomes,
        )
    )
    assert evaluation.firing == ()


@given(skew_milliseconds=st.integers(min_value=-10_000, max_value=10_000))
def test_clock_skew_fires_on_magnitude_in_either_direction(skew_milliseconds: int) -> None:
    skew = Decimal(skew_milliseconds) / Decimal("1000")
    evaluation = evaluate_triggers(
        TriggerObservations(
            correlation_id=_CAUSE,
            observed_at_utc=_NOW,
            drawdown_state=_state(current_equity_usd=_OPENING),
            clock_skew_seconds=skew,
        )
    )
    fired = {trigger.trigger_id for trigger in evaluation.firing}
    assert (TriggerId.CLOCK_SKEW in fired) is (abs(skew) > Decimal("1"))


@given(
    gap_milliseconds=st.lists(
        st.integers(min_value=1, max_value=3_600_000),
        min_size=MINIMUM_GAP_OBSERVATIONS,
        max_size=MINIMUM_GAP_OBSERVATIONS + 40,
    )
)
def test_the_derived_percentile_is_an_observed_gap_and_never_an_invented_one(
    gap_milliseconds: list[int],
) -> None:
    """Nearest rank: the answer is a gap that happened, not an interpolation between two."""
    moment = _NOW - timedelta(days=20)
    ticks = [moment]
    for gap in gap_milliseconds:
        moment += timedelta(milliseconds=gap)
        ticks.append(moment)
    assume(ticks[-1] <= _NOW)

    observed = derive_p99_inter_tick_gap_seconds(ticks, as_of_utc=_NOW)
    gaps = {Decimal(gap) / Decimal("1000") for gap in gap_milliseconds}

    assert observed in gaps
    assert observed <= max(gaps)
    below = sum(1 for gap in gaps if gap < observed)
    assert below <= len(gap_milliseconds)


@given(ratio=st.integers(min_value=1, max_value=25))
def test_a_threshold_at_or_below_its_ceiling_is_accepted_and_above_it_is_refused(
    ratio: int,
) -> None:
    """Config tightens, never loosens -- checked across the whole admissible band."""
    accepted = TriggerThresholds(loss_velocity_ratio=Decimal(ratio) / Decimal("1000"))
    assert accepted.loss_velocity_ratio <= Decimal("0.025")

    with pytest.raises(ValueError, match="hard ceiling"):
        TriggerThresholds(loss_velocity_ratio=Decimal(ratio + 25) / Decimal("1000"))


@given(seconds=st.integers(min_value=1, max_value=86_400))
def test_an_observation_can_never_be_evaluated_against_a_state_from_its_future(
    seconds: int,
) -> None:
    with pytest.raises(TriggerObservationError, match="not replayable"):
        TriggerObservations(
            correlation_id=_CAUSE,
            observed_at_utc=_NOW - timedelta(seconds=seconds),
            drawdown_state=_state(current_equity_usd=_OPENING),
        )
