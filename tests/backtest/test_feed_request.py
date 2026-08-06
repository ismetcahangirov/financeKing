"""What a feed request refuses to be, and the corpus faults it refuses to serve.

Every refusal here has a permissive alternative that produces a coverage report rather than
an error, and every one of those reports would blame the corpus for something the request or
the file got wrong. A window off the interval lattice reads as a corpus with no bars at all.
A duplicated series reads as a symbol with twice the volume. A misaligned partition reads as
complete while a real hole sits beside it. None of them raises unless something makes it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from fking.backtest.feed import (
    BAR_INTERVALS,
    CorpusIntegrityError,
    FeedRequest,
    FeedRequestError,
    MarketDataFeed,
    SeriesRequest,
    gaps_against,
    interval_duration,
)
from fking.data.format_resolver import Market
from fking.data.loaders import KlineRecord
from tests.backtest import feed_support as fs

pytestmark = pytest.mark.unit

SPOT_SERIES = SeriesRequest(market=Market.SPOT, instrument=fs.BTCUSDT_SPOT)

WARMUP_BAR_COUNT = 20
EXPOSED_BAR_COUNT = 40
WINDOW_BAR_COUNT = WARMUP_BAR_COUNT + EXPOSED_BAR_COUNT


def _request(**overrides: object) -> FeedRequest:
    base = fs.request_for(exposed_minute=20, until_minute=60, warmup_bar_count=20)
    return replace(base, **overrides)  # type: ignore[arg-type]  # dataclasses.replace is typed by field name


def test_an_unaligned_window_is_refused_rather_than_reported_as_an_empty_corpus() -> None:
    """A 1h window opening at 09:30 names no bar Binance ever published. Left unchecked,
    every bar in it reads as missing and the report blames the corpus for the request."""
    with pytest.raises(FeedRequestError, match="is not on the 1h lattice"):
        FeedRequest(
            series=(SPOT_SERIES,),
            bar_interval="1h",
            exposed_from_utc=datetime(2025, 1, 2, 9, 30, tzinfo=UTC),
            until_utc=datetime(2025, 1, 2, 12, 30, tzinfo=UTC),
            warmup_bar_count=2,
        )


def test_a_duplicated_series_is_refused() -> None:
    """Read twice, dispatched twice: every count derived from the run doubles for it alone."""
    with pytest.raises(FeedRequestError, match="the same \\(market, symbol\\) twice"):
        _request(series=(SPOT_SERIES, SPOT_SERIES))


def test_the_same_symbol_on_two_markets_is_not_a_duplicate() -> None:
    """BTCUSDT spot and BTCUSDT futures are two instruments with two histories, and a
    request naming both is the ordinary mixed-market run."""
    request = _request(
        series=(
            SPOT_SERIES,
            SeriesRequest(market=Market.FUTURES_UM, instrument=fs.BTCUSDT_FUTURES),
        )
    )
    assert [entry.label for entry in request.series] == ["spot/BTCUSDT", "futures_um/BTCUSDT"]


@pytest.mark.parametrize("bar_interval", ["1w", "1M", "3d", "", "one-minute"])
def test_an_interval_with_no_constant_duration_is_refused(bar_interval: str) -> None:
    """A calendar month is 28 to 31 days, so a lattice built by adding a fixed timedelta
    drifts against the venue's own boundaries -- reporting gaps that are not there for a
    while, and then reporting real ones as present."""
    with pytest.raises(FeedRequestError):
        _request(bar_interval=bar_interval)


def test_every_declared_interval_has_a_duration_the_lattice_can_use() -> None:
    for bar_interval in BAR_INTERVALS:
        assert interval_duration(bar_interval) > timedelta(0)


def test_a_naive_window_boundary_is_refused_rather_than_localised() -> None:
    """`astimezone` on a naive boundary would launder whatever zone the container runs in
    into a confident value, with no record that anybody guessed."""
    with pytest.raises(FeedRequestError, match="timezone-aware UTC"):
        _request(exposed_from_utc=datetime(2025, 1, 2, 0, 20))  # noqa: DTZ001 - the value under test


def test_an_empty_series_tuple_is_refused() -> None:
    """A run over no series has no result to refuse, and would report a clean gate."""
    with pytest.raises(FeedRequestError, match="at least one series"):
        _request(series=())


def test_a_non_integer_warm_up_length_is_refused() -> None:
    """`True` satisfies `isinstance(x, int)` and would warm the run with exactly one bar."""
    with pytest.raises(FeedRequestError, match="must be an int"):
        _request(warmup_bar_count=True)


def test_a_negative_warm_up_length_is_refused() -> None:
    with pytest.raises(FeedRequestError, match="must not be negative"):
        _request(warmup_bar_count=-1)


def test_an_empty_window_is_refused() -> None:
    with pytest.raises(FeedRequestError, match="must follow exposed_from_utc"):
        _request(until_utc=fs.DAY_START_UTC + fs.MINUTE * 20)


def test_the_lattice_spans_warm_up_and_exposure_together() -> None:
    request = fs.request_for(exposed_minute=20, until_minute=60, warmup_bar_count=20)
    lattice = tuple(request.lattice())

    assert lattice[0] == request.warmup_start_utc == fs.DAY_START_UTC
    assert lattice[-1] == fs.DAY_START_UTC + fs.MINUTE * 59
    assert len(lattice) == request.expected_bar_count == WINDOW_BAR_COUNT


def test_a_trailing_hole_is_reported_as_a_range_and_not_lost() -> None:
    """A gap running to the window's end has no present bar after it to close against, and
    a detector written on pairwise differences between held bars cannot see it at all."""
    request = fs.request_for(exposed_minute=5, until_minute=10, warmup_bar_count=5)
    held = fs.minutes(0, 1, 2, 3, 4, 5, 6)

    gaps = gaps_against(tuple(request.lattice()), held, duration=request.duration)

    assert len(gaps) == 1
    assert (gaps[0].gap_start_utc, gaps[0].missing_bar_count) == (fs.minutes(7)[0], 3)
    assert gaps[0].gap_end_utc == fs.minutes(10)[0]


def test_a_leading_hole_is_reported_too() -> None:
    """Same argument from the other end: a series whose first held bar is an hour into the
    window has an hour-long gap that no pairwise difference can see."""
    request = fs.request_for(exposed_minute=5, until_minute=10, warmup_bar_count=5)
    held = fs.minutes(3, 4, 5, 6, 7, 8, 9)

    gaps = gaps_against(tuple(request.lattice()), held, duration=request.duration)

    assert [(gap.gap_start_utc, gap.missing_bar_count) for gap in gaps] == [(fs.minutes(0)[0], 3)]


# ---------------------------------------------------------------------------
# Corpus faults
# ---------------------------------------------------------------------------


def _record_at(minute: int, *, seconds_late: int = 0) -> KlineRecord:
    opened = fs.DAY_START_UTC + fs.MINUTE * minute + timedelta(seconds=seconds_late)
    return KlineRecord(
        open_time_utc=opened,
        close_time_utc=opened + fs.MINUTE - timedelta(microseconds=1),
        open_quote_price=Decimal("95000.00"),
        high_quote_price=Decimal("95100.00"),
        low_quote_price=Decimal("94900.00"),
        close_quote_price=Decimal("95050.00"),
        base_volume=Decimal("1.5"),
        quote_volume=Decimal("142575.00"),
        trade_count=42,
        taker_buy_base_volume=Decimal("0.75"),
        taker_buy_quote_volume=Decimal("71287.50"),
        ignored_field="0",
    )


def test_a_duplicated_open_time_in_the_corpus_is_refused(tmp_path: Path) -> None:
    """Two rows at one open time are two answers to one question. Dispatching both doubles
    every count derived from that instant and lengthens every rolling window over it."""
    fs.write_corpus(
        tmp_path,
        records=[_record_at(0), _record_at(1), _record_at(1), _record_at(2)],
    )

    with pytest.raises(CorpusIntegrityError, match="two bars opening at"):
        MarketDataFeed(corpus_root=tmp_path, now_utc=fs.NOW_UTC).coverage(
            fs.request_for(exposed_minute=0, until_minute=3, warmup_bar_count=0)
        )


def test_a_bar_off_the_lattice_is_refused_rather_than_counted_as_present(
    tmp_path: Path,
) -> None:
    """The shape a partition written at the wrong interval takes: the count looks right, and
    a bar thirty seconds off contributes to no instant the window asked for -- so the series
    reads as complete with a real hole beside it."""
    fs.write_corpus(
        tmp_path,
        records=[_record_at(0), _record_at(1, seconds_late=30), _record_at(2)],
    )

    with pytest.raises(CorpusIntegrityError, match="not on the 1m lattice"):
        MarketDataFeed(corpus_root=tmp_path, now_utc=fs.NOW_UTC).coverage(
            fs.request_for(exposed_minute=0, until_minute=3, warmup_bar_count=0)
        )


def test_a_non_positive_duckdb_thread_count_is_refused(tmp_path: Path) -> None:
    """A knob on throughput still has to be a number DuckDB accepts, and the refusal must
    name the setting rather than surfacing as a SQL error nobody attributes to a flag."""
    fs.write_corpus(tmp_path)
    feed = MarketDataFeed(corpus_root=tmp_path, now_utc=fs.NOW_UTC, duckdb_thread_count=0)

    with pytest.raises(FeedRequestError, match="must be a positive int"):
        feed.coverage(fs.request_for(exposed_minute=0, until_minute=3, warmup_bar_count=0))
