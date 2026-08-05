"""Total event ordering: the priority ladder, and the tiebreaker that makes it total.

Ordering here is a correctness property, not a scheduling preference. The two things
asserted are that market data precedes venue events at a shared instant -- because the
reverse lets a strategy see its own fill before the price that caused it -- and that
two events never tie, because a tie hands the decision to `heapq`'s sift order, which
nobody wrote down and which is free to change between CPython releases.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from fking.backtest import (
    BacktestError,
    EventLoop,
    EventPriority,
    EventQueue,
    FundingEvent,
    MarketDataEvent,
    OrderAckEvent,
    QueuedEvent,
    ReconciliationEvent,
    RejectEvent,
    TimerEvent,
)
from fking.domain import Venue
from tests.support.backtest_events import (
    BAR_INTERVAL,
    RecordingHandler,
    SchedulingHandler,
    bar_at,
    bar_events,
    fill_event_at,
)
from tests.support.domain_factory import BTCUSDT, make_order
from tests.support.run_config import config_for

pytestmark = pytest.mark.unit

START = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
INSTANT = START + BAR_INTERVAL


def test_a_bar_is_knowable_at_its_close_and_not_at_its_open() -> None:
    """The derived instant is the whole reason `MarketDataEvent` stores no timestamp.

    A bar scheduled at its open time is a candle that has not finished forming, and
    every feature computed from it reads the future without raising.
    """
    bar = bar_at(START)
    assert MarketDataEvent(observation=bar).occurs_at_utc == bar.close_time_utc
    assert bar.close_time_utc != bar.open_time_utc


def test_market_data_precedes_a_fill_sharing_its_instant() -> None:
    """Swap the two priorities and this test is what fails."""
    market_data = MarketDataEvent(observation=bar_at(START))
    fill = fill_event_at(INSTANT)
    assert market_data.occurs_at_utc == fill.occurs_at_utc

    queue = EventQueue()
    queue.schedule(fill)  # inserted first, dispatched second
    queue.schedule(market_data)

    dispatched = [type(queue.pop().event).__name__ for _ in range(2)]
    assert dispatched == ["MarketDataEvent", "FillEvent"]


def test_the_priority_ladder_orders_a_shared_instant_end_to_end() -> None:
    """One event of every band at one instant, inserted backwards."""
    order = make_order()
    inserted = (
        ReconciliationEvent(venue=Venue.BINANCE_SPOT_TESTNET, occurs_at_utc=INSTANT),
        TimerEvent(strategy_id="s", occurs_at_utc=INSTANT, label="wake"),
        RejectEvent(order=order, occurs_at_utc=INSTANT, reason="LOT_SIZE"),
        fill_event_at(INSTANT),
        OrderAckEvent(order=order, occurs_at_utc=INSTANT),
        FundingEvent(instrument=BTCUSDT, occurs_at_utc=INSTANT, funding_rate=Decimal("0.0001")),
        MarketDataEvent(observation=bar_at(START)),
    )

    queue = EventQueue()
    for event in inserted:
        queue.schedule(event)
    popped = [queue.pop() for _ in range(len(inserted))]

    assert [queued.priority for queued in popped] == sorted(queued.priority for queued in popped)
    assert [type(queued.event).__name__ for queued in popped] == [
        "MarketDataEvent",
        "FundingEvent",
        # The three venue events share priority 2, so among themselves they keep the
        # order they were inserted in -- which is the tiebreaker doing its job.
        "RejectEvent",
        "FillEvent",
        "OrderAckEvent",
        "TimerEvent",
        "ReconciliationEvent",
    ]
    assert popped[0].priority is EventPriority.MARKET_DATA
    assert popped[-1].priority is EventPriority.RECONCILIATION


def test_ties_break_on_the_insertion_sequence() -> None:
    """Five events with one instant and one priority come back in insertion order."""
    queue = EventQueue()
    inserted = [fill_event_at(INSTANT, ordinal=ordinal) for ordinal in range(1, 6)]
    for event in inserted:
        queue.schedule(event)

    popped = [queue.pop().event for _ in range(len(inserted))]
    assert popped == inserted


def test_the_sequence_is_what_makes_the_ordering_key_a_total_order() -> None:
    """Drop `sequence` and the key stops distinguishing these five events at all.

    This is the assertion that would survive someone "simplifying" the key: without the
    counter every one of these collapses to the same `(instant, priority)`, and whatever
    order the heap then produces was decided by the heap rather than by this engine.
    """
    queue = EventQueue()
    queued = [queue.schedule(fill_event_at(INSTANT, ordinal=ordinal)) for ordinal in range(1, 6)]

    without_sequence = {(entry.occurs_at_utc, entry.priority) for entry in queued}
    assert len(without_sequence) == 1, "the five events are meant to be indistinguishable"
    assert len({entry.ordering_key for entry in queued}) == len(queued)
    assert [entry.sequence for entry in queued] == list(range(len(queued)))


def test_a_queued_event_compares_completely_and_not_only_with_less_than() -> None:
    """`heapq` needs `__lt__` alone; a type that *is* a total order should offer all four.

    A partial one raises `TypeError` on `>=` and invites a caller to re-derive the key by
    hand with `sorted(..., key=...)`, which is a second copy of the ordering rule.
    """
    queue = EventQueue()
    earlier = queue.schedule(fill_event_at(INSTANT, ordinal=1))
    later = queue.schedule(fill_event_at(INSTANT, ordinal=2))

    twin = QueuedEvent(
        occurs_at_utc=earlier.occurs_at_utc,
        priority=earlier.priority,
        sequence=earlier.sequence,
        event=earlier.event,
    )

    assert (earlier < later, earlier <= later) == (True, True)
    assert (earlier > later, earlier >= later) == (False, False)
    # Reflexivity through an equal-but-distinct object rather than `x <= x`: the
    # comparison has to route through __eq__ to be worth asserting.
    assert (earlier <= twin, earlier >= twin) == (True, True)


def test_the_comparison_never_falls_through_to_the_payload() -> None:
    """A fall-through would raise here rather than reorder -- and it must never happen.

    Domain records are frozen dataclasses without `order=True`, so `<` on two of them is
    a `TypeError`. That makes the fall-through loud in this direction and silent in the
    other, where a comparable payload would order by content instead; the queue must
    reach neither.
    """
    first = fill_event_at(INSTANT, ordinal=1)
    second = fill_event_at(INSTANT, ordinal=2)
    with pytest.raises(TypeError):
        _ = first.fill < second.fill  # type: ignore[operator]  # the point of the assertion

    queue = EventQueue()
    queue.schedule(first)
    queue.schedule(second)
    dispatched = [queue.pop().event for _ in range(2)]
    assert dispatched == [first, second]


def test_an_event_scheduled_mid_drain_lands_in_the_total_order() -> None:
    """Insertion during a drain must not jump the queue at its own instant."""
    initial = bar_events(START, how_many=2)
    follow_ups = (
        fill_event_at(START + BAR_INTERVAL, ordinal=1),
        fill_event_at(START + BAR_INTERVAL * 2, ordinal=2),
    )
    handler = SchedulingHandler(follow_ups=follow_ups)

    EventLoop(config_for(start_utc=START), handler).run(initial)

    # Each fill is scheduled while its own bar is being dispatched, at that same instant,
    # and is dispatched immediately after it -- never before, and never deferred past the
    # next bar.
    assert handler.type_names == (
        "MarketDataEvent",
        "FillEvent",
        "MarketDataEvent",
        "FillEvent",
    )


def test_peek_does_not_consume_and_an_empty_pop_refuses() -> None:
    queue = EventQueue()
    assert queue.peek() is None
    assert len(queue) == 0

    event = fill_event_at(INSTANT)
    first = queue.schedule(event)
    peeked = queue.peek()
    assert first.sequence == 0
    assert peeked is not None
    assert peeked.event is event
    assert len(queue) == 1

    queue.pop()
    # The counter is the run's event ordinal: it does not fall back when one is popped,
    # so a sequence number identifies an event for the whole run rather than a slot that
    # a later event can reuse.
    second = queue.schedule(fill_event_at(INSTANT, ordinal=2))
    assert second.sequence == 1

    queue.pop()
    with pytest.raises(BacktestError, match="empty event queue"):
        queue.pop()


def test_the_recording_handler_sees_every_event_the_loop_dispatched() -> None:
    handler = RecordingHandler()
    initial = bar_events(START, how_many=3)
    trace = EventLoop(config_for(start_utc=START), handler).run(initial)

    assert handler.dispatched == list(initial)
    assert trace.event_count == len(initial)
    assert [entry.sequence for entry in trace.entries] == [0, 1, 2]
