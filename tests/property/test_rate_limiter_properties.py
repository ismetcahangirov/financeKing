"""No submission schedule can exceed the venue's order rate. Not "usually" -- none.

Example-based tests confirm the schedules someone thought of, and the schedules that
break a rate limiter are the ones nobody thought of: a burst that lands exactly on a
window boundary, a long idle followed by a burst, timestamps that repeat because the
clock did not tick between two calls. Hypothesis generates those.

The property is stated over an *admitted* schedule rather than over an attempted one:
the governor's job is not to predict what a caller will do, it is to guarantee that what
it lets through never violates the budget. Every attempt is offered; the refusals are
counted and discarded; the survivors must satisfy the invariant in every window.
"""

from __future__ import annotations

import ast
import contextlib
from pathlib import Path
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fking.domain import Venue
from fking.execution import VENUE_PROFILES, RateBudgetExhausted, RequestClass, VenueRateGovernor
from fking.execution.throttle import EndpointCost
from tests.support.frozen_clock import FrozenClock

pytestmark = pytest.mark.property

_ORDER_WINDOW_SECONDS: Final[float] = 10.0
_ORDER_COST: Final[EndpointCost] = EndpointCost(request_weight=1, consumes_order_slot=True)
_THROTTLE_SOURCE: Final[Path] = (
    Path(__file__).resolve().parents[2] / "src" / "fking" / "execution" / "throttle.py"
)

# Every venue profile, so a future profile with a different rate is covered the day it is
# added rather than the day it produces a -1015.
_PROFILE_IDS: Final[tuple[Venue, ...]] = tuple(VENUE_PROFILES)


@given(
    venue=st.sampled_from(_PROFILE_IDS),
    # Gaps in tenths of a second, including 0 so that a whole burst can land on one
    # reading of the clock -- which is the case a naive "one per interval" limiter gets
    # wrong and a windowed one does not.
    gaps_deciseconds=st.lists(st.integers(min_value=0, max_value=60), min_size=1, max_size=300),
)
@settings(max_examples=200, deadline=None)
def test_no_admitted_schedule_exceeds_the_order_rate_in_any_10s_window(
    venue: Venue, gaps_deciseconds: list[int]
) -> None:
    profile = VENUE_PROFILES[venue]
    clock = FrozenClock()
    governor = VenueRateGovernor(
        profile=profile, monotonic_seconds=clock.monotonic_seconds, clock=clock.now_utc
    )

    admitted_at_seconds: list[float] = []
    for gap in gaps_deciseconds:
        clock.advance(gap / 10)
        try:
            governor.admit(
                request_class=RequestClass.ORDER,
                endpoint="privatePostOrder",
                cost=_ORDER_COST,
            )
        except RateBudgetExhausted:
            # A refusal is the design working. It costs nothing and the caller moves on;
            # the schedule continues so that later attempts are still exercised.
            continue
        admitted_at_seconds.append(clock.monotonic_seconds())

    # Every admission opens a window. Checking at each admission rather than on a fixed
    # grid is what makes this exhaustive: a violation must contain an admission at its
    # left edge, so no window that could be over the limit goes unchecked.
    for window_start in admitted_at_seconds:
        window_end = window_start + _ORDER_WINDOW_SECONDS
        in_window = sum(
            1 for at_seconds in admitted_at_seconds if window_start <= at_seconds < window_end
        )
        assert in_window <= profile.order_rate_per_10s


@given(gaps_deciseconds=st.lists(st.integers(min_value=0, max_value=200), min_size=1, max_size=200))
@settings(max_examples=100, deadline=None)
def test_a_refusal_never_advances_the_clock(gaps_deciseconds: list[int]) -> None:
    """Whatever the schedule, the only thing that moves time is the test.

    This is the property that "rejects instead of sleeping" reduces to once the clock is
    injected: if the governor could wait, it would have to move the clock it was given,
    and it never does.
    """
    clock = FrozenClock()
    governor = VenueRateGovernor(
        profile=VENUE_PROFILES[Venue.BINANCE_SPOT_TESTNET],
        monotonic_seconds=clock.monotonic_seconds,
        clock=clock.now_utc,
    )

    expected_seconds = 0.0
    for gap in gaps_deciseconds:
        clock.advance(gap / 10)
        expected_seconds += gap / 10
        with contextlib.suppress(RateBudgetExhausted):
            governor.admit(
                request_class=RequestClass.ORDER,
                endpoint="privatePostOrder",
                cost=_ORDER_COST,
            )
        assert clock.monotonic_seconds() == pytest.approx(expected_seconds)


def test_the_implementation_contains_no_wait_of_any_kind() -> None:
    """A source-level assertion, because the runtime one cannot see a rare branch.

    `test_exhaustion_does_not_sleep` measures the refusal path that a test drives. This
    reads the module's AST for any call that could suspend or block on any path,
    including one no test reaches. Both are needed: the timing test would pass a
    limiter that only sleeps on the tenth refusal, and this one would pass a limiter
    that busy-waits in a loop.
    """
    tree = ast.parse(_THROTTLE_SOURCE.read_text(encoding="utf-8"))
    forbidden = {"sleep", "wait", "wait_for", "delay", "timeout_at", "perf_counter"}

    called_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            called_names.add(node.func.attr)
        elif isinstance(node.func, ast.Name):
            called_names.add(node.func.id)

    assert called_names.isdisjoint(forbidden), (
        f"fking.execution.throttle calls {sorted(called_names & forbidden)}; a limiter "
        f"that waits turns a capacity problem into latency in the order path"
    )
