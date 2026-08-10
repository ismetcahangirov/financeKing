"""The paper venue: the backtest's fill model, released by wall time instead of a queue.

Every test here is about the seam between the two, because the fill model itself is
already covered by the backtest venue's suites and is the same object. What is new is that
nothing arrives by being popped off a queue -- an ack becomes real when the clock says so,
and the tests that matter are the ones asserting it cannot become real earlier.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from fking.backtest import FillEvent, OrderAckEvent, RejectEvent
from fking.backtest.venue import VenueSimulationError
from tests.backtest.venue_support import (
    EPOCH,
    SteppingClock,
    make_bar,
    make_order,
    make_paper_venue,
)

pytestmark = pytest.mark.unit

# Marketable: 10bp through a close of 64,200 against a modelled 2bp spread.
_CROSSING_QUOTE_PRICE = "64300.00"


def test_an_ack_is_held_until_the_clock_reaches_it() -> None:
    """The latency window is real time in a paper run, not a scheduling convention."""
    bar = make_bar()
    clock = SteppingClock(bar.close_time_utc)
    venue = make_paper_venue(clock=clock)
    venue.observe(bar)
    order = make_order(limit_quote_price=_CROSSING_QUOTE_PRICE)

    answer = venue.submit(order, decided_at_utc=bar.close_time_utc)

    assert isinstance(answer, OrderAckEvent)
    assert venue.due_events() == ()
    assert len(venue.pending_acks) == 1
    assert venue.pending_acks[0] is answer

    clock.advance(answer.occurs_at_utc - bar.close_time_utc)
    released = venue.due_events()

    assert released
    assert all(isinstance(event, FillEvent) for event in released)
    assert not venue.pending_acks


def test_a_decision_stamped_after_the_clock_is_refused() -> None:
    """A decision in the future is a bug, and its fill would be at a price nobody saw."""
    bar = make_bar()
    clock = SteppingClock(bar.close_time_utc)
    venue = make_paper_venue(clock=clock)
    venue.observe(bar)

    with pytest.raises(VenueSimulationError, match="cannot be taken in the future"):
        venue.submit(
            make_order(created_at_utc=bar.close_time_utc),
            decided_at_utc=bar.close_time_utc + timedelta(seconds=1),
        )


def test_resolving_an_ack_early_is_refused() -> None:
    """The Protocol entry point cannot be used to pull a fill forward past its latency."""
    bar = make_bar()
    clock = SteppingClock(bar.close_time_utc)
    venue = make_paper_venue(clock=clock)
    venue.observe(bar)
    answer = venue.submit(
        make_order(limit_quote_price=_CROSSING_QUOTE_PRICE), decided_at_utc=bar.close_time_utc
    )
    assert isinstance(answer, OrderAckEvent)

    with pytest.raises(VenueSimulationError, match="has not seen yet"):
        venue.resolve_ack(answer)


def test_resolving_an_ack_the_clock_has_reached_clears_it_from_pending() -> None:
    """Resolving through the Protocol and resolving through `due_events` agree."""
    bar = make_bar()
    clock = SteppingClock(bar.close_time_utc)
    venue = make_paper_venue(clock=clock)
    venue.observe(bar)
    answer = venue.submit(
        make_order(limit_quote_price=_CROSSING_QUOTE_PRICE), decided_at_utc=bar.close_time_utc
    )
    assert isinstance(answer, OrderAckEvent)
    clock.advance(answer.occurs_at_utc - bar.close_time_utc)

    resolved = venue.resolve_ack(answer)

    assert resolved
    assert venue.pending_acks == ()
    assert venue.due_events() == ()


def test_a_refusal_is_never_held_as_a_pending_ack() -> None:
    """A refused order has nothing to resolve, and holding one would resolve it twice."""
    bar = make_bar()
    clock = SteppingClock(bar.close_time_utc)
    venue = make_paper_venue(clock=clock)
    venue.observe(bar)

    # Below the recorded 5.00 USDT notional floor.
    answer = venue.submit(
        make_order(base_quantity="0.00001", limit_quote_price="64000.00"),
        decided_at_utc=bar.close_time_utc,
    )

    assert isinstance(answer, RejectEvent)
    assert venue.pending_acks == ()
    assert venue.report.rejection_total == 1


def test_an_unmarketable_order_joins_the_resting_queue() -> None:
    """The resting queue is the simulator's, reached through the paper venue unchanged."""
    first = make_bar()
    clock = SteppingClock(first.close_time_utc)
    venue = make_paper_venue(clock=clock)
    venue.observe(first)

    answer = venue.submit(
        make_order(limit_quote_price="63000.00"), decided_at_utc=first.close_time_utc
    )
    assert isinstance(answer, OrderAckEvent)
    clock.advance(answer.occurs_at_utc - first.close_time_utc)

    assert venue.due_events() == ()
    assert [resting.order.base_quantity for resting in venue.resting_orders] == [Decimal("0.01")]


def test_the_stepping_clock_refuses_to_rewind() -> None:
    """A clock that goes backward makes every latency assertion in this file meaningless."""
    clock = SteppingClock(EPOCH)

    with pytest.raises(ValueError, match="cannot go backward"):
        clock.advance(timedelta(seconds=-1))
