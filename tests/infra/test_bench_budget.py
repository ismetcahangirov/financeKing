"""The budget comparison CI gates on, and the measurement it compares.

Not a test of the benchmark's speed -- a timing assertion inside the unit suite is a flake
generator, and the whole point of `tools/bench` is that the timing assertion lives in a
job of its own on a machine whose spread is known. What is asserted here is the
arithmetic: that 20% over is a pass, that 21% over is a failure, and that both messages
carry the previous and current numbers, because a failure that does not is a failure
somebody has to reproduce locally before they can act on it.
"""

from __future__ import annotations

from datetime import date

from tools.bench._budget import (
    REFERENCE_BUDGET,
    TOLERANCE_FRACTION,
    ReferenceBudget,
    assess_wall_clock,
)
from tools.bench._measure import Measurement, peak_rss_bytes

#: 1000 events over 2 seconds. Named so the assertion is arithmetic, not a literal.
EXPECTED_EVENTS_PER_SECOND = 500.0

_BUDGET = ReferenceBudget(
    machine="test machine",
    wall_clock_seconds=10.0,
    peak_rss_bytes=100_000_000,
    measured_on=date(2026, 1, 1),
)


def test_a_run_at_the_recorded_number_passes() -> None:
    verdict = assess_wall_clock(_BUDGET, wall_clock_seconds=10.0)
    assert verdict.within_budget
    assert "previous 10.00s" in verdict.message
    assert "current 10.00s" in verdict.message


def test_a_run_exactly_at_the_tolerance_passes() -> None:
    """Twenty percent over is within a twenty percent tolerance, not over it."""
    verdict = assess_wall_clock(_BUDGET, wall_clock_seconds=12.0)
    assert verdict.within_budget
    assert "+20.0%" in verdict.message


def test_a_run_past_the_tolerance_fails_with_both_numbers() -> None:
    verdict = assess_wall_clock(_BUDGET, wall_clock_seconds=12.5)
    assert not verdict.within_budget
    assert "previous 10.00s" in verdict.message
    assert "current 12.50s" in verdict.message
    assert "+25.0%" in verdict.message
    assert "ceiling 12.00s" in verdict.message
    # The message must not offer shrinking the workload as a way out; the whole issue is
    # that a defence gets negotiated down to fit the hardware.
    assert "Reducing the workload is not one of the two options" in verdict.message


def test_a_faster_run_reports_the_improvement_rather_than_staying_silent() -> None:
    verdict = assess_wall_clock(_BUDGET, wall_clock_seconds=6.0)
    assert verdict.within_budget
    assert "-40.0%" in verdict.message


def test_the_committed_budget_states_its_machine_and_its_date() -> None:
    """A number with no provenance is a number nobody can tell is stale."""
    assert REFERENCE_BUDGET.machine
    assert REFERENCE_BUDGET.wall_clock_seconds > 0
    assert REFERENCE_BUDGET.peak_rss_bytes > 0
    assert REFERENCE_BUDGET.measured_on <= date.today()  # noqa: DTZ011 - a calendar date on a record, not an instant
    assert REFERENCE_BUDGET.ceiling_seconds == REFERENCE_BUDGET.wall_clock_seconds * (
        1.0 + TOLERANCE_FRACTION
    )


def test_events_per_second_is_derived_from_the_two_numbers_it_comes_from() -> None:
    measurement = Measurement(wall_clock_seconds=2.0, peak_rss_bytes=1, dispatched_event_total=1000)
    assert measurement.events_per_second == EXPECTED_EVENTS_PER_SECOND


def test_events_per_second_of_an_instant_run_is_zero_rather_than_infinite() -> None:
    measurement = Measurement(wall_clock_seconds=0.0, peak_rss_bytes=1, dispatched_event_total=1000)
    assert measurement.events_per_second == 0.0


def test_peak_rss_is_a_positive_number_of_bytes_on_this_platform() -> None:
    """Both platform implementations must return bytes, not kilobytes and not a handle."""
    # A running CPython is always well above a mebibyte, so this catches the unit error
    # that a plausible-looking small integer would otherwise hide.
    assert peak_rss_bytes() > 1024 * 1024
