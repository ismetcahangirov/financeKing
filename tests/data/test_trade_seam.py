"""The trade seam: what an id joins, what a timestamp must not, and what escalates.

`DATA_PIPELINE.md` section 5 states the rule these tests exist for:

> **Reconcile the seam on exchange trade id, never on timestamp.**

The two assertions that matter are opposites of each other, and a timestamp-keyed dedupe
gets both wrong in different directions: it drops one of two prints that share a
millisecond, and it keeps two copies of one print whose two sources stamped it
differently. Neither failure raises, and neither is visible in the corpus afterwards --
one shows up as a volume that is too low and the other as a volume that is too high, both
by an amount nothing records.

Every print here comes from `tests/support/tape_prints`, which parses frames captured from
a live testnet socket. Only the milliseconds are re-based; prices, quantities, sides and
aggregate trade ids are the venue's own.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from fking.data.backfill.seam import TRADE_SEAM_COMPARED_FIELDS, reconcile_trades
from fking.platform.errors import DataIntegrityError, SeamDisagreementError
from tests.support import tape_prints

pytestmark = pytest.mark.unit

# Fixed rather than clock-derived: `epoch_to_utc`'s plausibility window is a function of
# `now`, so a test reading the real clock would move its own boundary conditions daily.
NOW_UTC = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
TAPE_START = datetime(2026, 8, 4, 10, 0, tzinfo=UTC)

# Far enough apart that a millisecond-resolution comparison would notice, far enough
# inside one second that nothing crosses a partition boundary. This is the disagreement
# the id is chosen to survive.
CLOCK_SKEW = timedelta(milliseconds=7)


def test_the_same_print_stamped_differently_survives_exactly_once() -> None:
    """One aggregate trade id, two event times, one row.

    The stream stamps `T` from the frame it received; a REST view of the same aggregate
    can re-derive it. A `(symbol, timestamp)` dedupe sees two prints and doubles the
    volume for that trade, which nothing downstream can detect.
    """
    held = tape_prints.prints(5, first_event_utc=TAPE_START, now_utc=NOW_UTC)
    fetched = tuple(
        replace(record, event_time_utc=record.event_time_utc + CLOCK_SKEW) for record in held
    )

    seam = reconcile_trades(held, fetched)

    assert len(seam.merged) == len(held)
    assert seam.agreed == len(held)
    assert seam.recovered == 0
    assert [record.venue_trade_id for record in seam.merged] == [
        record.venue_trade_id for record in held
    ]
    # The held stamp is the one kept, so a repair over a range already reconciled writes
    # the same bytes and the partition's content digest does not move.
    assert [record.event_time_utc for record in seam.merged] == [
        record.event_time_utc for record in held
    ]


def test_two_distinct_prints_sharing_a_timestamp_both_survive() -> None:
    """The case a timestamp-keyed dedupe loses, and it is not rare.

    One aggressive order filling against several resting ones produces several aggregate
    prints inside one millisecond. Keying on the instant keeps one of them and silently
    deletes the rest of the volume.
    """
    recorded = tape_prints.prints(2, first_event_utc=TAPE_START, now_utc=NOW_UTC)
    simultaneous = tuple(replace(record, event_time_utc=TAPE_START) for record in recorded)
    assert len({record.venue_trade_id for record in simultaneous}) == len(simultaneous)

    seam = reconcile_trades((), simultaneous)

    assert len(seam.merged) == len(simultaneous)
    assert {record.venue_trade_id for record in seam.merged} == {
        record.venue_trade_id for record in simultaneous
    }


def test_a_repeated_identical_print_is_deduplicated_rather_than_refused() -> None:
    """What a reconnect produces, and the reason the seal path goes through here."""
    recorded = tape_prints.prints(3, first_event_utc=TAPE_START, now_utc=NOW_UTC)

    seam = reconcile_trades((), (*recorded, *recorded))

    assert len(seam.merged) == len(recorded)


def test_a_price_disagreement_under_one_id_escalates() -> None:
    """One id is one execution, so two prices for it cannot both be right."""
    held = tape_prints.prints(3, first_event_utc=TAPE_START, now_utc=NOW_UTC)
    # A different stretch of the same recording, re-based onto the same instants and
    # re-labelled with the held ids: real venue numbers for a different set of trades,
    # which is exactly what a disagreement is.
    elsewhere = tape_prints.prints(3, first_event_utc=TAPE_START, now_utc=NOW_UTC, offset=40)
    fetched = tuple(
        replace(other, venue_trade_id=original.venue_trade_id)
        for original, other in zip(held, elsewhere, strict=True)
    )
    assert any(
        getattr(original, field_name) != getattr(other, field_name)
        for field_name in TRADE_SEAM_COMPARED_FIELDS
        for original, other in zip(held, fetched, strict=True)
    )

    with pytest.raises(SeamDisagreementError, match="disagree about trade"):
        reconcile_trades(held, fetched)


def test_one_source_filing_an_id_two_different_ways_escalates() -> None:
    """The same contradiction one step earlier, and not something a merge can resolve."""
    recorded = tape_prints.prints(1, first_event_utc=TAPE_START, now_utc=NOW_UTC)
    contradicted = replace(recorded[0], quote_price=recorded[0].quote_price + Decimal("1"))

    with pytest.raises(SeamDisagreementError, match="two different trades under id"):
        reconcile_trades((), (*recorded, contradicted))


def test_an_inverted_side_under_one_id_escalates() -> None:
    """`is_buyer_maker` is the aggressor side inverted -- the one field whose wrongness
    leaves every other column of the print looking correct."""
    held = tape_prints.prints(1, first_event_utc=TAPE_START, now_utc=NOW_UTC)
    fetched = (replace(held[0], is_buyer_maker=not held[0].is_buyer_maker),)

    with pytest.raises(SeamDisagreementError, match="is_buyer_maker"):
        reconcile_trades(held, fetched)


def test_a_fetch_supplies_the_prints_the_corpus_lacks_and_says_how_many() -> None:
    """`recovered` is what decides whether a sequence gap closes, so it counts prints the
    corpus did not already hold rather than prints the fetch returned."""
    recorded = tape_prints.prints(10, first_event_utc=TAPE_START, now_utc=NOW_UTC)
    held = recorded[:4]

    seam = reconcile_trades(held, recorded)

    assert seam.agreed == len(held)
    assert seam.recovered == len(recorded) - len(held)
    assert len(seam.merged) == len(recorded)


def test_the_merge_is_ordered_by_instant_then_by_aggregate_id() -> None:
    """Event time is not a total order over a tape, and an order that depended on which
    source a print came from would make the partition's content digest a function of the
    merge rather than of the content."""
    recorded = tape_prints.prints(8, first_event_utc=TAPE_START, now_utc=NOW_UTC)
    reversed_input = tuple(reversed(recorded))

    seam = reconcile_trades(reversed_input[:4], reversed_input)

    assert [record.venue_trade_id for record in seam.merged] == [
        record.venue_trade_id for record in recorded
    ]


def test_a_non_integer_trade_id_is_refused_rather_than_ordered_lexically() -> None:
    """The whole seam rests on the venue's id being a monotone integer; ordering the tape
    by its characters would file print 9 after print 10."""
    recorded = tape_prints.prints(1, first_event_utc=TAPE_START, now_utc=NOW_UTC)

    with pytest.raises(DataIntegrityError, match="not a decimal integer"):
        reconcile_trades((), (replace(recorded[0], venue_trade_id="a12"),))
