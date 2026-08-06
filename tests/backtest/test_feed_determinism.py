"""Two runs over one window emit the same event sequence, whatever DuckDB does underneath.

`BACKTEST_ENGINE.md` section 5: a result that differs between two runs of one `config_hash`
outranks everything else on the queue. The feed is upstream of that check and can break it
without ever touching the engine -- a scan whose row order depends on how many threads
DuckDB decided to use, a merge across two series whose tie-break falls through to insertion
order, a timestamp carrying a tzinfo object from whichever library produced it. Each of those
leaves the *set* of bars identical and the *sequence* different, and the trace digest is a
function of the sequence.

The thread count is the sharpest of the three, because it is the one nobody chooses: it
defaults to the core count, so the same corpus and the same window produce a different
number of scan tasks on CI than on a laptop. It is varied here deliberately, and it is
deliberately not a field of `RunConfig` -- anything that can change without changing the
result must stay out of the run's identity, or two runs producing identical numbers carry
different hashes and the determinism check compares nothing.

The last test is the one that keeps the rest honest: a digest insensitive to its input would
make every comparison above pass vacuously.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fking.backtest.feed import MarketDataFeed
from fking.data.format_resolver import Market
from tests.backtest import feed_support as fs

pytestmark = pytest.mark.unit

# One thread and four. Four rather than "the default" because the default is the machine's
# core count, so a test written against it asserts nothing on a single-core runner.
THREAD_COUNTS = (1, 4)


def _feed(root: Path, *, duckdb_thread_count: int | None = None) -> MarketDataFeed:
    return MarketDataFeed(
        corpus_root=root, now_utc=fs.NOW_UTC, duckdb_thread_count=duckdb_thread_count
    )


def test_two_loads_of_one_window_produce_the_same_digest(tmp_path: Path) -> None:
    fs.write_corpus(tmp_path)
    request = fs.request_for(exposed_minute=20, until_minute=90, warmup_bar_count=20)

    first = _feed(tmp_path).load(request)
    second = _feed(tmp_path).load(request)

    assert first.event_sequence_digest == second.event_sequence_digest
    assert first.events == second.events


def test_two_loads_agree_event_by_event_and_not_only_on_the_digest(tmp_path: Path) -> None:
    """A digest comparison alone cannot say *where* two reads diverged, and the first
    differing instant is where an investigation into a determinism failure has to start."""
    fs.write_corpus(tmp_path)
    request = fs.request_for(exposed_minute=20, until_minute=90, warmup_bar_count=20)

    first = _feed(tmp_path).load(request)
    second = _feed(tmp_path).load(request)

    divergences = [
        (left, right)
        for left, right in zip(first.events, second.events, strict=True)
        if left != right
    ]
    assert divergences == []


@pytest.mark.parametrize("thread_count", THREAD_COUNTS)
def test_the_digest_does_not_move_with_the_duckdb_thread_count(
    tmp_path: Path, thread_count: int
) -> None:
    """The acceptance criterion, stated per thread count and pinned to one baseline."""
    fs.write_corpus(tmp_path, market=Market.SPOT)
    fs.write_corpus(tmp_path, market=Market.FUTURES_UM)
    request = fs.request_for(
        exposed_minute=20,
        until_minute=200,
        warmup_bar_count=20,
        markets=(Market.SPOT, Market.FUTURES_UM),
    )

    baseline = _feed(tmp_path).load(request)
    threaded = _feed(tmp_path, duckdb_thread_count=thread_count).load(request)

    assert threaded.event_sequence_digest == baseline.event_sequence_digest
    assert threaded.events == baseline.events


def test_two_series_sharing_an_instant_are_ordered_by_series_and_not_by_read_order(
    tmp_path: Path,
) -> None:
    """The tie-break has to be a stated key rather than whichever read finished first.

    Spot and futures 2025-01-02 bars close a microsecond apart -- the archives differ in
    epoch resolution -- so a same-instant tie is arranged here by comparing the order the
    two orderings of the request produce.
    """
    fs.write_corpus(tmp_path, market=Market.SPOT)
    fs.write_corpus(tmp_path, market=Market.FUTURES_UM)

    forwards = _feed(tmp_path).load(
        fs.request_for(
            exposed_minute=20,
            until_minute=40,
            warmup_bar_count=0,
            markets=(Market.SPOT, Market.FUTURES_UM),
        )
    )
    backwards = _feed(tmp_path).load(
        fs.request_for(
            exposed_minute=20,
            until_minute=40,
            warmup_bar_count=0,
            markets=(Market.FUTURES_UM, Market.SPOT),
        )
    )

    assert forwards.event_sequence_digest == backwards.event_sequence_digest


def test_the_digest_changes_when_the_window_changes(tmp_path: Path) -> None:
    """A digest insensitive to its input would make every comparison above vacuous."""
    fs.write_corpus(tmp_path)

    shorter = _feed(tmp_path).load(
        fs.request_for(exposed_minute=20, until_minute=40, warmup_bar_count=20)
    )
    longer = _feed(tmp_path).load(
        fs.request_for(exposed_minute=20, until_minute=41, warmup_bar_count=20)
    )

    assert shorter.event_sequence_digest != longer.event_sequence_digest


def test_the_digest_changes_when_the_warm_up_length_changes(tmp_path: Path) -> None:
    """Two windows exposing the same bars but warmed from different histories are two
    different runs, and a digest that could not tell them apart would let one masquerade
    as a repeat of the other."""
    fs.write_corpus(tmp_path)

    shallow = _feed(tmp_path).load(
        fs.request_for(exposed_minute=20, until_minute=40, warmup_bar_count=5)
    )
    deep = _feed(tmp_path).load(
        fs.request_for(exposed_minute=20, until_minute=40, warmup_bar_count=20)
    )

    assert shallow.exposed_event_count == deep.exposed_event_count
    assert shallow.event_sequence_digest != deep.event_sequence_digest
