"""Each of the twelve `FAILSAFE.md` section 2.1 triggers, fired from a synthetic stream.

One test per trigger, and each one carries the assertion issue #54 asks for: the row a
trip would be written from names the trigger, the measured value, the threshold it was
measured against, the correlation id of the causing event, and the book as it stood.

The states are built as a *stream* -- equity marks stamped at instants -- rather than as
ratios handed to the evaluator, because the arithmetic between a mark and a ratio is
where the two limits that look alike (rolling loss and loss velocity) turn out to differ.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from fking.domain import Portfolio
from fking.risk import (
    HARD_CEILINGS,
    REJECTION_SAMPLE_ORDERS,
    BookSnapshot,
    DrawdownBudgets,
    DrawdownState,
    EquityMark,
    OrderOutcome,
    SymbolStaleness,
    TriggerEvaluation,
    TriggerId,
    TriggerObservationError,
    TriggerObservations,
    TriggerThresholds,
    TripEvent,
    TripTrigger,
    derive_p99_inter_tick_gap_seconds,
    evaluate_triggers,
    loss_velocity_ratio,
    trip,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 1, 14, 30, tzinfo=UTC)
DAY_START = datetime(2026, 8, 1, tzinfo=UTC)
CAUSE = UUID("11111111-1111-4111-8111-111111111111")
INCIDENT = UUID("22222222-2222-4222-8222-222222222222")
EVENT = UUID("33333333-3333-4333-8333-333333333333")

# One dead-flat portfolio state: peak equals current, the day opened where it stands, and
# the only rolling mark is ten hours old. Every trigger test moves exactly one of those
# away from flat, so a firing trigger is attributable to the thing the test changed.
FLAT_EQUITY = Decimal("100000")

# Five rejections in twenty is 25%, over the 20% threshold; four is exactly on it and the
# trigger is stated as "> 20%", so the pair brackets the boundary from both sides.
REJECTIONS_OVER_THE_THRESHOLD = 5
REJECTIONS_ON_THE_THRESHOLD = 4

# The synthetic tick series below pause on every hundredth tick, which is what puts the
# 99th percentile on the pause rather than on the ordinary gap.
TICKS_BETWEEN_PAUSES = 100
LAST_TICK_BEFORE_A_PAUSE = TICKS_BETWEEN_PAUSES - 1

# "More than an order of magnitude" from the acceptance criterion, written once.
ORDER_OF_MAGNITUDE = 10


def _state(
    *,
    peak_equity_usd: Decimal = FLAT_EQUITY,
    current_equity_usd: Decimal = FLAT_EQUITY,
    day_open_equity_usd: Decimal = FLAT_EQUITY,
    rolling_marks: tuple[EquityMark, ...] | None = None,
) -> DrawdownState:
    marks = rolling_marks or (
        EquityMark(observed_at_utc=NOW - timedelta(hours=10), equity_usd=current_equity_usd),
    )
    return DrawdownState(
        scope="portfolio",
        subject_id="portfolio",
        peak_equity_usd=peak_equity_usd,
        current_equity_usd=current_equity_usd,
        day_start_utc=DAY_START,
        day_open_equity_usd=day_open_equity_usd,
        rolling_marks=marks,
        observed_at_utc=NOW,
    )


def _observations(**overrides: object) -> TriggerObservations:
    defaults: dict[str, object] = {
        "correlation_id": CAUSE,
        "observed_at_utc": NOW,
        "drawdown_state": _state(),
    }
    return TriggerObservations(**{**defaults, **overrides})  # type: ignore[arg-type]
    # The kwargs are heterogeneous by construction -- this helper exists to vary one
    # field of a fourteen-field record per test -- and `TriggerObservations` validates
    # every one of them at construction, so the ignore hides no unchecked path.


def _snapshot() -> BookSnapshot:
    return BookSnapshot(
        portfolio=Portfolio(as_of_utc=NOW, positions=(), cash_balances={}),
        open_client_order_ids=("fk-1",),
        protective_client_order_ids=("fk-1",),
        reconciled_at_utc=NOW - timedelta(minutes=2),
        reconciliation_is_clean=True,
    )


def _only(evaluation: TriggerEvaluation, trigger_id: TriggerId) -> TripTrigger:
    """The single firing trigger with this id, asserting nothing else fired."""
    assert [fired.trigger_id for fired in evaluation.firing] == [trigger_id]
    return evaluation.firing[0]


def _trip_row(evaluation: TriggerEvaluation, trigger: TripTrigger) -> TripEvent:
    return trip(
        event_id=EVENT,
        incident_id=INCIDENT,
        correlation_id=evaluation.correlation_id,
        actor="risk.triggers",
        trigger=trigger,
        snapshot=_snapshot(),
        now_utc=evaluation.observed_at_utc,
    )


def _assert_trip_row_is_complete(evaluation: TriggerEvaluation, trigger: TripTrigger) -> None:
    """Every trip row carries the five things a post-mortem cannot be run without."""
    row = _trip_row(evaluation, trigger)
    assert row.trigger.trigger_id == trigger.trigger_id
    assert row.trigger.observed_value == trigger.observed_value
    assert row.trigger.threshold_value == trigger.threshold_value
    assert row.correlation_id == CAUSE
    assert row.snapshot.portfolio.as_of_utc == NOW
    assert row.occurred_at_utc == NOW


def test_trigger_1_portfolio_drawdown_from_the_persisted_high_water_mark() -> None:
    observations = _observations(
        drawdown_state=_state(
            peak_equity_usd=FLAT_EQUITY,
            current_equity_usd=Decimal("90000"),
            day_open_equity_usd=Decimal("90000"),
        )
    )
    evaluation = evaluate_triggers(observations)

    fired = _only(evaluation, TriggerId.PORTFOLIO_DRAWDOWN)
    assert fired.observed_value == Decimal("0.10")
    assert fired.threshold_value == Decimal("0.10")
    _assert_trip_row_is_complete(evaluation, fired)


def test_trigger_2_daily_loss_against_midnight_utc_equity() -> None:
    observations = _observations(
        drawdown_state=_state(current_equity_usd=Decimal("97000"), day_open_equity_usd=FLAT_EQUITY)
    )
    evaluation = evaluate_triggers(observations)

    fired = _only(evaluation, TriggerId.DAILY_LOSS)
    assert fired.observed_value == Decimal("0.03")
    _assert_trip_row_is_complete(evaluation, fired)


def test_trigger_3_rolling_twenty_four_hour_loss_measured_from_the_window_high() -> None:
    observations = _observations(
        drawdown_state=_state(
            current_equity_usd=Decimal("95500"),
            day_open_equity_usd=Decimal("95500"),
            rolling_marks=(
                EquityMark(observed_at_utc=NOW - timedelta(hours=10), equity_usd=FLAT_EQUITY),
            ),
        )
    )
    evaluation = evaluate_triggers(observations)

    fired = _only(evaluation, TriggerId.ROLLING_LOSS)
    assert fired.observed_value == Decimal("0.045")
    assert fired.threshold_value == Decimal("0.045")
    _assert_trip_row_is_complete(evaluation, fired)


def test_trigger_4_loss_velocity_fires_while_the_daily_limit_has_not_bound() -> None:
    """The independence issue #54 asks to be proved, not asserted.

    1.5% lost inside five minutes with the daily budget 50% unused. One threshold cannot
    distinguish this from the same loss spread over twenty hours; two can.
    """
    observations = _observations(
        drawdown_state=_state(
            current_equity_usd=Decimal("98500"),
            day_open_equity_usd=Decimal("98500"),
            rolling_marks=(
                EquityMark(observed_at_utc=NOW - timedelta(hours=10), equity_usd=Decimal("98500")),
                EquityMark(observed_at_utc=NOW - timedelta(minutes=2), equity_usd=FLAT_EQUITY),
            ),
        )
    )
    evaluation = evaluate_triggers(observations)

    fired = _only(evaluation, TriggerId.LOSS_VELOCITY)
    assert fired.observed_value == Decimal("0.015")
    # The daily limit is nowhere near binding: nothing was lost against 00:00 UTC equity.
    assert observations.drawdown_state.daily_loss_ratio == Decimal("0")
    _assert_trip_row_is_complete(evaluation, fired)


def test_the_same_loss_spread_over_twenty_hours_does_not_fire_the_velocity_trigger() -> None:
    """The other half of the independence: velocity is about speed, not size."""
    observations = _observations(
        drawdown_state=_state(
            current_equity_usd=Decimal("98500"),
            day_open_equity_usd=Decimal("98500"),
            rolling_marks=(
                EquityMark(observed_at_utc=NOW - timedelta(hours=20), equity_usd=FLAT_EQUITY),
            ),
        )
    )
    assert evaluate_triggers(observations).firing == ()


def test_trigger_5_reconciliation_divergence_that_survived_two_attempts() -> None:
    observations = _observations(
        reconciliation_divergence_is_beyond_dust=True,
        reconciliation_divergence_attempts=2,
    )
    evaluation = evaluate_triggers(observations)

    fired = _only(evaluation, TriggerId.RECONCILIATION_DIVERGENCE)
    assert fired.observed_value == Decimal("2")
    _assert_trip_row_is_complete(evaluation, fired)


def test_a_divergence_on_the_first_attempt_is_a_race_and_does_not_trip() -> None:
    observations = _observations(
        reconciliation_divergence_is_beyond_dust=True,
        reconciliation_divergence_attempts=1,
    )
    assert evaluate_triggers(observations).firing == ()


def test_trigger_6_rejection_rate_over_the_last_twenty_orders() -> None:
    outcomes: tuple[OrderOutcome, ...] = tuple(
        "rejected" if index < REJECTIONS_OVER_THE_THRESHOLD else "accepted"
        for index in range(REJECTION_SAMPLE_ORDERS)
    )
    evaluation = evaluate_triggers(_observations(recent_order_outcomes=outcomes))

    fired = _only(evaluation, TriggerId.ORDER_REJECTION_RATE)
    assert fired.observed_value == Decimal("0.25")
    assert fired.threshold_value == Decimal("0.20")
    _assert_trip_row_is_complete(evaluation, fired)


def test_a_short_sample_never_fires_the_rejection_trigger() -> None:
    """One rejection out of one is a 100% rejection rate and evidence of nothing."""
    assert evaluate_triggers(_observations(recent_order_outcomes=("rejected",))).firing == ()


def test_exactly_four_rejections_in_twenty_sits_on_the_threshold_and_does_not_fire() -> None:
    outcomes: tuple[OrderOutcome, ...] = tuple(
        "rejected" if index < REJECTIONS_ON_THE_THRESHOLD else "accepted"
        for index in range(REJECTION_SAMPLE_ORDERS)
    )
    assert evaluate_triggers(_observations(recent_order_outcomes=outcomes)).firing == ()


def test_trigger_7_market_data_staleness_only_where_a_position_is_open() -> None:
    held = SymbolStaleness(
        symbol="BTCUSDT",
        last_tick_at_utc=NOW - timedelta(seconds=6),
        p99_inter_tick_gap_seconds=Decimal("0.5"),
        has_open_position=True,
    )
    unheld = SymbolStaleness(
        symbol="THINALT",
        last_tick_at_utc=NOW - timedelta(hours=3),
        p99_inter_tick_gap_seconds=Decimal("0.5"),
        has_open_position=False,
    )
    evaluation = evaluate_triggers(_observations(symbol_staleness=(held, unheld)))

    fired = _only(evaluation, TriggerId.MARKET_DATA_STALENESS)
    assert fired.observed_value == Decimal("6")
    assert fired.threshold_value == Decimal("5.0")
    assert "BTCUSDT" in fired.detail
    _assert_trip_row_is_complete(evaluation, fired)


def test_trigger_8_clock_skew_past_one_second_in_either_direction() -> None:
    for skew in (Decimal("1.001"), Decimal("-1.001")):
        evaluation = evaluate_triggers(_observations(clock_skew_seconds=skew))
        fired = _only(evaluation, TriggerId.CLOCK_SKEW)
        assert fired.observed_value == Decimal("1.001")
        _assert_trip_row_is_complete(evaluation, fired)

    assert evaluate_triggers(_observations(clock_skew_seconds=Decimal("1"))).firing == ()


def test_trigger_9_an_unhandled_exception_inside_risk_or_execution() -> None:
    evaluation = evaluate_triggers(_observations(unhandled_exception_module="execution"))

    fired = _only(evaluation, TriggerId.UNHANDLED_EXCEPTION)
    assert "execution" in fired.detail
    _assert_trip_row_is_complete(evaluation, fired)


def test_trigger_10_a_hard_ceiling_that_order_construction_should_have_made_unreachable() -> None:
    ceiling = HARD_CEILINGS["max_portfolio_notional_usd"].bound
    evaluation = evaluate_triggers(_observations(gross_exposure_usd=ceiling + Decimal("1")))

    fired = _only(evaluation, TriggerId.HARD_CEILING_BREACH)
    assert fired.unit == "usd"
    assert fired.threshold_value == ceiling
    _assert_trip_row_is_complete(evaluation, fired)


def test_both_hard_ceiling_breaches_are_reported_rather_than_the_first() -> None:
    evaluation = evaluate_triggers(
        _observations(
            gross_exposure_usd=HARD_CEILINGS["max_portfolio_notional_usd"].bound + Decimal("1"),
            open_position_count=int(HARD_CEILINGS["max_open_positions"].bound) + 1,
        )
    )
    units = [fired.unit for fired in evaluation.firing]
    assert units == ["usd", "count"]


def test_trigger_11_an_audit_write_failure() -> None:
    evaluation = evaluate_triggers(_observations(audit_write_failed=True))

    fired = _only(evaluation, TriggerId.AUDIT_WRITE_FAILURE)
    assert "unreconstructable" in fired.detail
    _assert_trip_row_is_complete(evaluation, fired)


def test_trigger_12_a_manual_trip_records_the_reason_it_was_asked_for() -> None:
    evaluation = evaluate_triggers(_observations(manual_trip_reason="venue epoch advanced"))

    fired = _only(evaluation, TriggerId.MANUAL)
    assert "venue epoch advanced" in fired.detail
    _assert_trip_row_is_complete(evaluation, fired)


def test_a_manual_trip_with_a_blank_reason_is_refused_at_construction() -> None:
    with pytest.raises(TriggerObservationError, match="carries a reason"):
        _observations(manual_trip_reason="   ")


def test_every_trigger_that_fires_is_reported_in_failsafe_table_order() -> None:
    """A trip caused by three triggers is not the same incident as one caused by one."""
    evaluation = evaluate_triggers(
        _observations(
            drawdown_state=_state(
                current_equity_usd=Decimal("85000"),
                day_open_equity_usd=FLAT_EQUITY,
                rolling_marks=(
                    EquityMark(observed_at_utc=NOW - timedelta(hours=10), equity_usd=FLAT_EQUITY),
                    EquityMark(observed_at_utc=NOW - timedelta(minutes=2), equity_usd=FLAT_EQUITY),
                ),
            ),
            audit_write_failed=True,
            manual_trip_reason="operator halted while investigating",
        )
    )
    assert [fired.trigger_id for fired in evaluation.firing] == [
        TriggerId.PORTFOLIO_DRAWDOWN,
        TriggerId.DAILY_LOSS,
        TriggerId.ROLLING_LOSS,
        TriggerId.LOSS_VELOCITY,
        TriggerId.AUDIT_WRITE_FAILURE,
        TriggerId.MANUAL,
    ]
    assert evaluation.should_trip
    assert evaluation.primary is not None
    assert evaluation.primary.trigger_id == TriggerId.PORTFOLIO_DRAWDOWN


def test_a_flat_book_fires_nothing_and_names_no_primary() -> None:
    evaluation = evaluate_triggers(_observations())
    assert not evaluation.should_trip
    assert evaluation.primary is None


def test_a_tightened_threshold_is_accepted_and_a_loosened_one_is_refused() -> None:
    tightened = TriggerThresholds(loss_velocity_ratio=Decimal("0.005"))
    assert tightened.loss_velocity_ratio == Decimal("0.005")

    with pytest.raises(ValueError, match="hard ceiling"):
        TriggerThresholds(loss_velocity_ratio=Decimal("0.03"))


def test_a_strategy_scope_budget_cannot_be_used_as_a_kill_switch_threshold() -> None:
    with pytest.raises(TriggerObservationError, match="portfolio-scope"):
        TriggerThresholds(
            portfolio_budgets=DrawdownBudgets(
                scope="strategy",
                drawdown_ratio=Decimal("0.15"),
                daily_loss_ratio=Decimal("0.03"),
            )
        )


def test_a_state_stamped_after_the_observation_instant_is_refused() -> None:
    with pytest.raises(TriggerObservationError, match="not replayable"):
        _observations(observed_at_utc=NOW - timedelta(minutes=1))


def test_a_naive_observation_instant_is_refused() -> None:
    with pytest.raises(TriggerObservationError, match="timezone-aware UTC"):
        TriggerObservations(
            correlation_id=CAUSE,
            observed_at_utc=datetime(2026, 8, 1, 14, 30),  # noqa: DTZ001 - the point
            drawdown_state=_state(),
        )


# --------------------------------------------------------------------------------------
# Trigger 7's per-symbol threshold, derived rather than configured.
# --------------------------------------------------------------------------------------
#
# Two recorded-shaped tick series: a liquid symbol that ticks several times a second with
# occasional half-second pauses, and a thin alt that ticks every few minutes with
# occasional hour-long silences. Both are generated deterministically rather than sampled,
# so the assertion is about the derivation and not about a particular random draw.


def _liquid_ticks(count: int) -> list[datetime]:
    start = NOW - timedelta(days=30)
    ticks: list[datetime] = []
    moment = start
    for index in range(count):
        # Every hundredth tick pauses for 400 ms; the rest arrive 100 ms apart.
        pausing = index % TICKS_BETWEEN_PAUSES == LAST_TICK_BEFORE_A_PAUSE
        moment += timedelta(milliseconds=400 if pausing else 100)
        ticks.append(moment)
    return ticks


def _thin_ticks(count: int) -> list[datetime]:
    start = NOW - timedelta(days=30)
    ticks: list[datetime] = []
    moment = start
    for index in range(count):
        pausing = index % TICKS_BETWEEN_PAUSES == LAST_TICK_BEFORE_A_PAUSE
        moment += timedelta(seconds=3600 if pausing else 180)
        ticks.append(moment)
    return ticks


def test_derived_staleness_thresholds_differ_by_more_than_an_order_of_magnitude() -> None:
    liquid = derive_p99_inter_tick_gap_seconds(_liquid_ticks(500), as_of_utc=NOW)
    thin = derive_p99_inter_tick_gap_seconds(_thin_ticks(500), as_of_utc=NOW)

    assert liquid == Decimal("0.4")
    assert thin == Decimal("3600")
    assert thin / liquid > ORDER_OF_MAGNITUDE


def test_a_constant_threshold_fails_the_same_test_in_both_directions() -> None:
    """Why trigger 7 is not a constant, stated as the two failures a constant produces."""
    liquid = derive_p99_inter_tick_gap_seconds(_liquid_ticks(500), as_of_utc=NOW)
    thin = derive_p99_inter_tick_gap_seconds(_thin_ticks(500), as_of_utc=NOW)
    thresholds = TriggerThresholds()
    multiple = thresholds.market_data_staleness_multiple

    # A constant sized for the liquid symbol fires on the thin alt's ordinary silence.
    constant_from_liquid = liquid * multiple
    assert thin > constant_from_liquid

    # A constant sized for the thin alt leaves the liquid symbol unobserved for ten
    # hours before anything fires -- a position held against a price nobody has seen.
    constant_from_thin = thin * multiple
    stale_liquid = SymbolStaleness(
        symbol="BTCUSDT",
        last_tick_at_utc=NOW - timedelta(hours=9),
        p99_inter_tick_gap_seconds=liquid,
        has_open_position=True,
    )
    assert stale_liquid.age_seconds(NOW) < constant_from_thin
    # The derived per-symbol threshold catches it, which is the whole argument.
    assert evaluate_triggers(_observations(symbol_staleness=(stale_liquid,))).should_trip


def test_a_percentile_from_too_short_a_sample_is_refused() -> None:
    with pytest.raises(TriggerObservationError, match="wearing a percentile's name"):
        derive_p99_inter_tick_gap_seconds(_liquid_ticks(50), as_of_utc=NOW)


def test_ticks_outside_the_trailing_window_are_not_counted() -> None:
    ticks = _liquid_ticks(500)
    with pytest.raises(TriggerObservationError, match="at least"):
        derive_p99_inter_tick_gap_seconds(ticks, as_of_utc=NOW, window=timedelta(days=1))


def test_a_tick_series_that_goes_backwards_is_refused() -> None:
    ticks = _liquid_ticks(200)
    scrambled = [*ticks[:100], ticks[50], *ticks[100:]]
    with pytest.raises(TriggerObservationError, match="ascend strictly"):
        derive_p99_inter_tick_gap_seconds(scrambled, as_of_utc=NOW)


# --------------------------------------------------------------------------------------
# The refusals. Every one of these is a measurement this module cannot read, and an
# unreadable measurement is not a measurement of "nothing happening" -- so each is raised
# rather than defaulted, and the supervisor's own error path (trigger 9) handles it.
# --------------------------------------------------------------------------------------


def test_a_negative_count_is_refused() -> None:
    with pytest.raises(TriggerObservationError, match="must not be negative"):
        _observations(reconciliation_divergence_attempts=-1)


def test_a_float_where_a_decimal_belongs_is_refused() -> None:
    """The check exists for the callers mypy does not see: `ccxt` hands back floats."""
    with pytest.raises(TriggerObservationError, match="must be a Decimal"):
        _observations(clock_skew_seconds=1.5)


def test_a_negative_gross_exposure_is_refused() -> None:
    with pytest.raises(TriggerObservationError, match="absolute notional"):
        _observations(gross_exposure_usd=Decimal("-1"))


def test_a_tick_stamped_after_the_observation_instant_is_refused() -> None:
    fresh = SymbolStaleness(
        symbol="BTCUSDT",
        last_tick_at_utc=NOW + timedelta(seconds=1),
        p99_inter_tick_gap_seconds=Decimal("0.5"),
        has_open_position=True,
    )
    with pytest.raises(TriggerObservationError, match="perfectly fresh"):
        _observations(symbol_staleness=(fresh,))


def test_a_blank_symbol_and_a_non_positive_gap_are_both_refused() -> None:
    with pytest.raises(TriggerObservationError, match="must not be blank"):
        SymbolStaleness(
            symbol=" ",
            last_tick_at_utc=NOW,
            p99_inter_tick_gap_seconds=Decimal("0.5"),
            has_open_position=False,
        )
    with pytest.raises(TriggerObservationError, match="non-positive gap"):
        SymbolStaleness(
            symbol="BTCUSDT",
            last_tick_at_utc=NOW,
            p99_inter_tick_gap_seconds=Decimal("0"),
            has_open_position=False,
        )


def test_a_fresh_symbol_with_an_open_position_fires_nothing() -> None:
    fresh = SymbolStaleness(
        symbol="BTCUSDT",
        last_tick_at_utc=NOW - timedelta(seconds=1),
        p99_inter_tick_gap_seconds=Decimal("0.5"),
        has_open_position=True,
    )
    assert evaluate_triggers(_observations(symbol_staleness=(fresh,))).firing == ()


def test_a_strategy_scope_state_cannot_be_evaluated_by_the_kill_switch() -> None:
    strategy_state = DrawdownState(
        scope="strategy",
        subject_id="alpha",
        peak_equity_usd=FLAT_EQUITY,
        current_equity_usd=FLAT_EQUITY,
        day_start_utc=DAY_START,
        day_open_equity_usd=FLAT_EQUITY,
        rolling_marks=(EquityMark(observed_at_utc=NOW, equity_usd=FLAT_EQUITY),),
        observed_at_utc=NOW,
    )
    with pytest.raises(TriggerObservationError, match="portfolio-scope state"):
        _observations(drawdown_state=strategy_state)


def test_an_unhandled_exception_with_no_module_named_is_refused() -> None:
    with pytest.raises(TriggerObservationError, match="must name a module"):
        _observations(unhandled_exception_module="")


def test_a_zero_length_window_is_refused_by_both_window_functions() -> None:
    """A window of zero admits no marks at all, which reads as a perfectly flat book."""
    with pytest.raises(TriggerObservationError, match="window must be positive"):
        loss_velocity_ratio(_state(), window=timedelta(0))
    with pytest.raises(TriggerObservationError, match="window must be positive"):
        derive_p99_inter_tick_gap_seconds(_liquid_ticks(200), as_of_utc=NOW, window=timedelta(0))


def test_a_threshold_of_zero_is_refused() -> None:
    with pytest.raises(TriggerObservationError, match="fires continuously"):
        TriggerThresholds(clock_skew_seconds=Decimal("0"))
