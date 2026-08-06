"""No code path in the feed manufactures a bar. Asserted by counting, not by reading.

The property is exact and it is the one the whole coverage gate exists to protect:
**the emitted bar count equals the archive bar count.** Not approximately, not modulo a
tolerance -- equal, on every window, including ones the corpus cannot fully serve.

An interpolated bar is not a rounding error. It is a price that existed nowhere, at a
timestamp at which nobody could have traded, and a breakout or gap strategy trades into it
and is filled at it. Because interpolation is smooth and real markets are not, the phantom
moves are systematically kinder than real ones, so the resulting equity curve is not noisy
-- it is biased upward and looks exactly like a curve from a complete corpus.

Three shapes of the same assertion, because a feed could pass one and fail another: the
count, the *set* of open times, and the values themselves. The third is what catches a
forward fill, which preserves both the count and the set the moment it is paired with a
window narrowed around the hole.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fking.backtest.feed import CoverageRefusedError, MarketDataFeed
from fking.data.format_resolver import Market
from tests.backtest import feed_support as fs

pytestmark = pytest.mark.unit

GAP_MINUTES = tuple(range(30, 41))

# Twenty warm-up bars and forty exposed ones, per series.
WARMUP_BAR_COUNT = 20
EXPOSED_BAR_COUNT = 40
WINDOW_BAR_COUNT = WARMUP_BAR_COUNT + EXPOSED_BAR_COUNT


def _feed(root: Path) -> MarketDataFeed:
    return MarketDataFeed(corpus_root=root, now_utc=fs.NOW_UTC)


def test_emitted_bar_count_equals_archive_bar_count(tmp_path: Path) -> None:
    """The acceptance criterion, on a complete window."""
    fs.write_corpus(tmp_path)
    loaded = _feed(tmp_path).load(
        fs.request_for(exposed_minute=20, until_minute=60, warmup_bar_count=20)
    )

    assert len(loaded.events) == loaded.archive_bar_count
    assert loaded.archive_bar_count == WINDOW_BAR_COUNT


def test_emitted_bar_count_equals_archive_bar_count_across_two_markets(tmp_path: Path) -> None:
    """A mixed run must not net one leg's shortfall against the other's surplus."""
    fs.write_corpus(tmp_path, market=Market.SPOT)
    fs.write_corpus(tmp_path, market=Market.FUTURES_UM)
    loaded = _feed(tmp_path).load(
        fs.request_for(
            exposed_minute=20,
            until_minute=60,
            warmup_bar_count=20,
            markets=(Market.SPOT, Market.FUTURES_UM),
        )
    )

    assert len(loaded.events) == loaded.archive_bar_count == 2 * WINDOW_BAR_COUNT


def test_a_window_around_a_hole_emits_exactly_the_bars_that_survived(tmp_path: Path) -> None:
    """The interesting case: a corpus with a hole, and a window that stops short of it.

    A forward fill would keep both the count and the lattice intact for the *gapped* window
    -- which is why the assertion below is on the open times the corpus actually holds
    rather than on the ones the lattice names.
    """
    written = fs.write_corpus(tmp_path, omit_open_times=fs.minutes(*GAP_MINUTES))
    request = fs.request_for(exposed_minute=10, until_minute=30, warmup_bar_count=10)

    loaded = _feed(tmp_path).load(request)

    emitted = [fs.bar_of(event).open_time_utc for event in loaded.events]
    held = [
        moment
        for moment in written.open_times
        if request.warmup_start_utc <= moment < request.until_utc
    ]
    assert emitted == held
    assert len(emitted) == loaded.archive_bar_count


def test_no_emitted_bar_falls_inside_an_omitted_range(tmp_path: Path) -> None:
    """A window narrowed to end exactly at the hole must not have reached into it."""
    fs.write_corpus(tmp_path, omit_open_times=fs.minutes(*GAP_MINUTES))
    loaded = _feed(tmp_path).load(
        fs.request_for(exposed_minute=20, until_minute=30, warmup_bar_count=20)
    )

    omitted = frozenset(fs.minutes(*GAP_MINUTES))
    assert not omitted & {fs.bar_of(event).open_time_utc for event in loaded.events}


def test_every_emitted_bar_is_a_row_the_corpus_holds_value_for_value(tmp_path: Path) -> None:
    """Counts and instants can both survive a fabricated bar; the OHLCV cannot.

    Comparing the values is what makes "no synthesis" total rather than "no synthesis of a
    shape these assertions happen to look at".
    """
    written = fs.write_corpus(tmp_path)
    loaded = _feed(tmp_path).load(
        fs.request_for(exposed_minute=20, until_minute=25, warmup_bar_count=5)
    )

    by_open = {record.open_time_utc: record for record in written.records}
    for event in loaded.events:
        bar = fs.bar_of(event)
        record = by_open[bar.open_time_utc]
        assert (
            bar.open_quote_price,
            bar.high_quote_price,
            bar.low_quote_price,
            bar.close_quote_price,
            bar.trade_count,
        ) == (
            record.open_quote_price,
            record.high_quote_price,
            record.low_quote_price,
            record.close_quote_price,
            record.trade_count,
        )


def test_a_gapped_window_produces_no_events_at_all_rather_than_partial_ones(
    tmp_path: Path,
) -> None:
    """The refusal is total. There is no degraded stream over the bars that survived.

    Returning the present bars with the absent ones simply missing would be the most
    tempting behaviour here and is the worst: every rolling window computed over that series
    silently spans more wall time than it claims to, and nothing raises.
    """
    fs.write_corpus(tmp_path, omit_open_times=fs.minutes(*GAP_MINUTES))

    with pytest.raises(CoverageRefusedError):
        _feed(tmp_path).load(
            fs.request_for(exposed_minute=20, until_minute=60, warmup_bar_count=20)
        )
