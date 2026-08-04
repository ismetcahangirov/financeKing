"""The reconnect schedule: full jitter, capped, and unbounded in attempts.

The property test is the one that matters. An example-based test of "attempt 3 is under
60 seconds" passes against a schedule that overflows at attempt 40, and a supervisor
reconnecting for a fortnight reaches attempt 40. The property is asserted across the
whole domain, including attempt counts a real session will only reach after weeks.
"""

from __future__ import annotations

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.data.live.backoff import (
    RECONNECT_BASE_SECONDS,
    RECONNECT_CAP_SECONDS,
    reconnect_delay_seconds,
)

pytestmark = pytest.mark.unit

# The ceilings the schedule must produce at attempts 1 and 5, named so the assertion
# reads as "the ceiling doubled" rather than as two numbers a reader has to re-derive.
CEILING_AT_ATTEMPT_1 = 2.0
CEILING_AT_ATTEMPT_5 = 32.0
SATURATED_ATTEMPT = 6


@given(
    attempt=st.integers(min_value=0, max_value=100_000),
    seed=st.integers(min_value=0, max_value=2**32 - 1),
)
def test_no_delay_ever_exceeds_the_cap(attempt: int, seed: int) -> None:
    delay = reconnect_delay_seconds(attempt, rng=random.Random(seed))
    assert 0.0 <= delay <= RECONNECT_CAP_SECONDS


@given(seed=st.integers(min_value=0, max_value=2**32 - 1))
def test_the_first_attempt_never_exceeds_the_base(seed: int) -> None:
    """Full jitter draws from `[0, ceiling]`, and attempt 0's ceiling is the base. A
    schedule whose first retry can sleep 60 seconds turns a 200 ms blip into a minute of
    unrecorded market."""
    assert reconnect_delay_seconds(0, rng=random.Random(seed)) <= RECONNECT_BASE_SECONDS


def test_the_ceiling_doubles_until_it_saturates() -> None:
    """Asserted through the RNG rather than by reading the ceiling directly.

    A `Random` whose `uniform` is fixed at its upper bound reports the ceiling exactly,
    which keeps the test about the schedule rather than about an internal helper.
    """

    class Ceiling(random.Random):
        def uniform(self, _low: float, high: float) -> float:
            return high

    rng = Ceiling()
    assert reconnect_delay_seconds(0, rng=rng) == 1.0
    assert reconnect_delay_seconds(1, rng=rng) == CEILING_AT_ATTEMPT_1
    assert reconnect_delay_seconds(5, rng=rng) == CEILING_AT_ATTEMPT_5
    assert reconnect_delay_seconds(SATURATED_ATTEMPT, rng=rng) == RECONNECT_CAP_SECONDS
    assert reconnect_delay_seconds(10_000, rng=rng) == RECONNECT_CAP_SECONDS


def test_the_schedule_is_jittered_rather_than_deterministic() -> None:
    """Sleeping the full exponential synchronises every client onto one schedule, which
    is how a venue that dropped everyone gets them all back at once."""
    rng = random.Random(20260804)
    delays = {reconnect_delay_seconds(4, rng=rng) for _ in range(20)}
    assert len(delays) > 1  # a fixed schedule would collapse to a single value


def test_a_negative_attempt_is_refused() -> None:
    with pytest.raises(ValueError, match="counts from zero"):
        reconnect_delay_seconds(-1, rng=random.Random(0))
