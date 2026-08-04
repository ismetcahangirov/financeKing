"""Repairing a tape `sequence` gap: what is fetched, what is written, what still narrows.

The claims here are about a Parquet file and about a `coverage_gap` row, so both are real:
a temporary corpus on disk and PostgreSQL in a container. A mocked partition would prove
the mock can be rewritten, and a mocked registry would prove the mock refuses a residual
outside its parent -- which is a trigger and a `CHECK`, not a Python branch
(`TESTING.md`, `CLAUDE.md` section 5).

Every print comes from `tests/support/tape_prints`, which parses frames captured off a live
testnet socket and renders `/aggTrades` pages from the same payloads. Only the milliseconds
are re-based.

The shape every test builds: twelve consecutive recorded prints, of which the corpus holds
the first three and the last five. The four in the middle are the loss, and the gap row
records their exact count the way `SequenceGapDetector` would have.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.data.archive import ArchiveCoordinate
from fking.data.backfill.registry import (
    NO_INTERVAL,
    STREAM_TIMESTAMP_RESOLUTION,
    GapKind,
    GapResolution,
    IngestRegistry,
    RecordedGap,
    SeriesKey,
)
from fking.data.backfill.seam import reconcile_trades
from fking.data.backfill.trade_gaps import TradeBackfillOutcome, TradeGapBackfiller
from fking.data.format_resolver import Dataset, Market
from fking.data.loaders.records import TradeRecord
from fking.data.parquet.layout import partition_path
from fking.data.parquet.records import read_partition_trades
from fking.data.parquet.schema import RecordSource
from fking.data.parquet.writer import write_records
from fking.platform.errors import DataIntegrityError, SeamDisagreementError
from tests.support import tape_prints

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SYMBOL = "BTCUSDT"
SERIES = SeriesKey(
    market=Market.SPOT, dataset=Dataset.AGG_TRADES, symbol=SYMBOL, bar_interval=NO_INTERVAL
)
COORDINATE = ArchiveCoordinate(
    market=Market.SPOT, dataset=Dataset.AGG_TRADES, symbol=SYMBOL, archive_date=date(2026, 8, 3)
)

TAPE_START = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
# Fixed rather than clock-derived: `epoch_to_utc`'s plausibility window is a function of
# `now`, so a test reading the real clock would move its own boundary conditions daily.
NOW_UTC = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
DISCOVERED_AT = NOW_UTC - timedelta(days=1)

TAPE_LENGTH = 12
FIRST_MISSING = 3
MISSING_COUNT = 4
FIRST_HELD_AFTER = FIRST_MISSING + MISSING_COUNT

# What a page cap of three leaves: the left bracket plus the first two of the four.
PREFIX_RECOVERED = 2
PREFIX_STILL_MISSING = MISSING_COUNT - PREFIX_RECOVERED

# What a page cap of two leaves when the corpus already holds one print inside the gap:
# one recovered, and the two absent ids split either side of the one on disk.
SPLIT_RESIDUAL_COUNT = 2


def _tape() -> tuple[TradeRecord, ...]:
    return tape_prints.prints(TAPE_LENGTH, first_event_utc=TAPE_START, now_utc=NOW_UTC)


def _identifier(record: TradeRecord) -> int:
    return int(record.venue_trade_id)


class _RecordedAggTradeRest:
    """An `AggTradeRestSource` replaying recorded prints, bounded like the real client.

    `page_cap` is how "the endpoint gave us the first two of the four" is expressed without
    inventing a venue behaviour: the aggregate ids are contiguous, so a short answer from
    Binance is a prefix -- the page cap in `GuardedAggTradeRest`, or its retention horizon --
    and never a hole in the middle.
    """

    def __init__(self, tape: Sequence[TradeRecord], *, page_cap: int | None = None) -> None:
        self._by_id = {_identifier(record): record for record in tape}
        self._page_cap = page_cap
        self.requests: list[tuple[str, int, int]] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def agg_trades(
        self, *, symbol: str, from_id: int, print_count: int, now_utc: datetime
    ) -> tuple[TradeRecord, ...]:
        del now_utc
        self.requests.append((symbol, from_id, print_count))
        wanted = print_count if self._page_cap is None else min(print_count, self._page_cap)
        served: list[TradeRecord] = []
        for step in range(wanted):
            record = self._by_id.get(from_id + step)
            if record is None:
                break
            served.append(record)
        return tuple(served)


def _write_partition(root: Path, held: Sequence[TradeRecord]) -> None:
    """Seed the corpus with the prints a live session would have sealed."""
    write_records(
        reconcile_trades((), held).merged,
        coordinate=COORDINATE,
        source=RecordSource.STREAM,
        ingested_at_utc=DISCOVERED_AT,
        root=root,
    )


async def _record_gap(
    engine: AsyncEngine,
    tape: Sequence[TradeRecord],
    *,
    missing_count: int = MISSING_COUNT,
) -> None:
    """The row `SequenceGapDetector` would have written when it saw the jump.

    The bounds are the two bracketing prints' own event times, widened to one millisecond
    when they share an instant -- exactly what the detector does, because the venue's event
    times have no finer resolution and the table refuses a zero-width row.
    """
    opened_at = tape[FIRST_MISSING - 1].event_time_utc
    closed_at = max(tape[FIRST_HELD_AFTER].event_time_utc, opened_at + STREAM_TIMESTAMP_RESOLUTION)
    await IngestRegistry(engine).record_series_gaps(
        SERIES,
        [
            RecordedGap(
                gap_start_utc=opened_at,
                gap_end_utc=closed_at,
                gap_kind=GapKind.SEQUENCE,
                missing_bar_count=missing_count,
            )
        ],
        discovered_at_utc=DISCOVERED_AT,
    )


def _backfiller(
    engine: AsyncEngine, source: _RecordedAggTradeRest, root: Path
) -> TradeGapBackfiller:
    return TradeGapBackfiller(
        engine=engine,
        market=Market.SPOT,
        source=source,
        corpus_root=root,
        clock=lambda: NOW_UTC,
    )


async def _run(
    engine: AsyncEngine, source: _RecordedAggTradeRest, root: Path
) -> TradeBackfillOutcome:
    return await _backfiller(engine, source, root).run([SYMBOL])


def _partition_sources(root: Path) -> dict[str, RecordSource]:
    return {
        entry.record.venue_trade_id: entry.source
        for entry in read_partition_trades(partition_path(COORDINATE, root=root))
    }


# ---------------------------------------------------------------------------
# Recovering the whole loss
# ---------------------------------------------------------------------------


async def test_a_fully_recovered_sequence_gap_holds_every_print_and_closes(
    ingest_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The gap is marked `backfilled` only once the corpus holds all N of the prints."""
    tape = _tape()
    held = (*tape[:FIRST_MISSING], *tape[FIRST_HELD_AFTER:])
    _write_partition(tmp_path, held)
    await _record_gap(ingest_engine, tape)
    source = _RecordedAggTradeRest(tape)

    outcome = await _run(ingest_engine, source, tmp_path)

    assert outcome.gaps_examined == 1
    assert outcome.gaps_closed == 1
    assert outcome.prints_written == MISSING_COUNT
    assert outcome.prints_still_missing == 0
    assert await IngestRegistry(ingest_engine).open_gaps(SERIES) == ()

    held_ids = [
        entry.record.venue_trade_id
        for entry in read_partition_trades(partition_path(COORDINATE, root=tmp_path))
    ]
    assert held_ids == [record.venue_trade_id for record in tape]


async def test_the_fetch_is_asked_for_the_missing_ids_and_both_brackets(
    ingest_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Paged on `fromId`, and wider than the loss on both sides.

    The gap's own bounds span a fraction of a second, so there is no time window that
    could address these prints. The overlap on the two bracketing ids is the seam: without
    it the venue's view of a print the corpus already holds is never compared to ours.
    """
    tape = _tape()
    _write_partition(tmp_path, (*tape[:FIRST_MISSING], *tape[FIRST_HELD_AFTER:]))
    await _record_gap(ingest_engine, tape)
    source = _RecordedAggTradeRest(tape)

    await _run(ingest_engine, source, tmp_path)

    assert source.calls == 1
    symbol, from_id, requested = source.requests[0]
    assert symbol == SYMBOL
    assert from_id == _identifier(tape[FIRST_MISSING - 1])
    assert requested == MISSING_COUNT + 2


async def test_recovered_prints_carry_the_rest_backfill_source_and_the_rest_keep_theirs(
    ingest_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A partition rewritten by a repair holds two provenances, and both are the truth.

    A print the socket delivered on time and one fetched hours later to repair an outage
    have different latency and different reconcilers behind them. Stamping the rewritten
    file with one value would answer gate 11's question -- which rows came from a live
    stream -- with a guess.
    """
    tape = _tape()
    held = (*tape[:FIRST_MISSING], *tape[FIRST_HELD_AFTER:])
    _write_partition(tmp_path, held)
    await _record_gap(ingest_engine, tape)

    await _run(ingest_engine, _RecordedAggTradeRest(tape), tmp_path)

    sources = _partition_sources(tmp_path)
    recovered_ids = {record.venue_trade_id for record in tape[FIRST_MISSING:FIRST_HELD_AFTER]}
    assert {
        venue_trade_id
        for venue_trade_id, source in sources.items()
        if source is RecordSource.REST_BACKFILL
    } == recovered_ids
    assert {
        venue_trade_id
        for venue_trade_id, source in sources.items()
        if source is RecordSource.STREAM
    } == {record.venue_trade_id for record in held}


async def test_repairing_an_already_repaired_gap_rewrites_nothing(
    ingest_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The partition is read back, merged and written again, so the digest has to survive
    the round trip -- a `decimal128(38, 18)` column returns every value at eighteen places,
    and a scale-sensitive digest would rewrite an identical file on every pass."""
    tape = _tape()
    _write_partition(tmp_path, (*tape[:FIRST_MISSING], *tape[FIRST_HELD_AFTER:]))
    await _record_gap(ingest_engine, tape)
    await _run(ingest_engine, _RecordedAggTradeRest(tape), tmp_path)

    path = partition_path(COORDINATE, root=tmp_path)
    repaired = path.read_bytes()
    # The gap is resolved, so a second pass examines nothing; the file is re-written by
    # hand from what it now holds, which is the same merge the repair performed.
    write_records(
        tuple(entry.record for entry in read_partition_trades(path)),
        coordinate=COORDINATE,
        source=RecordSource.STREAM,
        ingested_at_utc=NOW_UTC + timedelta(days=1),
        root=tmp_path,
    )

    assert path.read_bytes() != repaired  # sources differ; the digest must notice
    outcome = await _run(ingest_engine, _RecordedAggTradeRest(tape), tmp_path)
    assert outcome.gaps_examined == 0


# ---------------------------------------------------------------------------
# Recovering part of it
# ---------------------------------------------------------------------------


async def test_a_partial_recovery_narrows_the_gap_to_what_is_still_absent(
    ingest_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Two of four recovered leaves two missing, and the row is `superseded` not closed.

    A partially backfilled gap recorded as closed is worse than an open one, because the
    coverage report then tells `backtest` it may run over a range that is still holed.
    """
    tape = _tape()
    _write_partition(tmp_path, (*tape[:FIRST_MISSING], *tape[FIRST_HELD_AFTER:]))
    await _record_gap(ingest_engine, tape)
    # Two prints past the left bracket: the bracket itself and the first two missing.
    source = _RecordedAggTradeRest(tape, page_cap=3)

    outcome = await _run(ingest_engine, source, tmp_path)

    assert outcome.gaps_closed == 0
    assert outcome.gaps_narrowed == 1
    assert outcome.prints_written == PREFIX_RECOVERED
    assert outcome.prints_still_missing == PREFIX_STILL_MISSING

    open_gaps = await IngestRegistry(ingest_engine).open_gaps(SERIES)
    assert len(open_gaps) == 1
    residual = open_gaps[0].gap
    assert residual.gap_kind is GapKind.SEQUENCE
    assert residual.missing_bar_count == PREFIX_STILL_MISSING
    # Bounded by the prints either side of the run, because a print we still do not hold
    # has an id and no instant.
    assert residual.gap_start_utc == tape[FIRST_MISSING + 1].event_time_utc
    assert residual.gap_end_utc == tape[FIRST_HELD_AFTER].event_time_utc
    # The residual inherits the original discovery instant: those prints were found
    # missing then and are missing still.
    assert open_gaps[0].discovered_at_utc == DISCOVERED_AT


async def test_the_residual_bounds_agree_with_the_prints_the_corpus_actually_holds(
    ingest_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The claim a coverage report makes, checked against the file it describes."""
    tape = _tape()
    _write_partition(tmp_path, (*tape[:FIRST_MISSING], *tape[FIRST_HELD_AFTER:]))
    await _record_gap(ingest_engine, tape)

    await _run(ingest_engine, _RecordedAggTradeRest(tape, page_cap=3), tmp_path)

    residual = (await IngestRegistry(ingest_engine).open_gaps(SERIES))[0].gap
    inside = [
        entry.record
        for entry in read_partition_trades(partition_path(COORDINATE, root=tmp_path))
        if residual.gap_start_utc < entry.record.event_time_utc < residual.gap_end_utc
    ]
    assert inside == []


async def test_a_print_already_recovered_inside_the_gap_splits_the_residual_in_two(
    ingest_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The corpus can hold a print inside an open gap, and the residual has to see it.

    A gap row is frozen the moment the detector writes it, but the tape keeps arriving: a
    reconnect that replays a skipped print puts it back in the spool, and the next seal
    puts it in the partition. Computing the residual from arithmetic on the gap's own
    bounds -- rather than from the ids the corpus holds -- would then re-declare a print
    missing that is on disk, and would report one run where there are two.
    """
    tape = _tape()
    late_arrival = tape[FIRST_MISSING + 2]
    _write_partition(tmp_path, (*tape[:FIRST_MISSING], late_arrival, *tape[FIRST_HELD_AFTER:]))
    await _record_gap(ingest_engine, tape)
    # The bracket plus one missing print: the venue's answer stops there.
    source = _RecordedAggTradeRest(tape, page_cap=2)

    outcome = await _run(ingest_engine, source, tmp_path)

    assert outcome.gaps_narrowed == 1
    assert outcome.prints_written == 1
    assert outcome.prints_still_missing == SPLIT_RESIDUAL_COUNT

    residuals = [entry.gap for entry in await IngestRegistry(ingest_engine).open_gaps(SERIES)]
    assert len(residuals) == SPLIT_RESIDUAL_COUNT
    assert [residual.missing_bar_count for residual in residuals] == [1, 1]
    assert residuals[0].gap_start_utc == tape[FIRST_MISSING].event_time_utc
    assert residuals[0].gap_end_utc == late_arrival.event_time_utc
    assert residuals[1].gap_start_utc == late_arrival.event_time_utc
    assert residuals[1].gap_end_utc == tape[FIRST_HELD_AFTER].event_time_utc


async def test_an_endpoint_with_nothing_to_offer_leaves_the_gap_exactly_as_it_was(
    ingest_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Resolving on a residual that reproduces the gap's own bounds would delete it: the
    insert deduplicates to the same row and the mark then closes it."""
    tape = _tape()
    held = (*tape[:FIRST_MISSING], *tape[FIRST_HELD_AFTER:])
    _write_partition(tmp_path, held)
    await _record_gap(ingest_engine, tape)
    # Only the left bracket comes back, which is a print the corpus already holds.
    source = _RecordedAggTradeRest(tape, page_cap=1)

    outcome = await _run(ingest_engine, source, tmp_path)

    assert outcome.gaps_unrepaired == 1
    assert outcome.gaps_closed == 0
    assert outcome.gaps_narrowed == 0
    assert outcome.prints_written == 0
    open_gaps = await IngestRegistry(ingest_engine).open_gaps(SERIES)
    assert len(open_gaps) == 1
    assert open_gaps[0].gap.missing_bar_count == MISSING_COUNT


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


async def test_a_day_with_no_partition_is_deferred_and_the_venue_is_not_asked(
    ingest_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The corpus for a day still in progress is a spool, not a partition. Reconciling
    against a file that is still being appended to is reconciling against a moving target,
    and the rewrite would race the seal for the same path."""
    tape = _tape()
    await _record_gap(ingest_engine, tape)
    source = _RecordedAggTradeRest(tape)

    outcome = await _run(ingest_engine, source, tmp_path)

    assert outcome.gaps_examined == 1
    assert outcome.gaps_deferred == 1
    assert source.calls == 0
    assert len(await IngestRegistry(ingest_engine).open_gaps(SERIES)) == 1


async def test_a_gap_whose_left_bracket_is_gone_is_refused_rather_than_approximated(
    ingest_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Every id in the repair is derived from that one print. Falling back to a nearby
    one would fetch a plausible range that is not the missing one, write it, and close the
    gap over prints that are still absent."""
    tape = _tape()
    # The corpus holds the day, but not the print the gap was measured from.
    _write_partition(tmp_path, (*tape[: FIRST_MISSING - 1], *tape[FIRST_HELD_AFTER:]))
    await _record_gap(ingest_engine, tape)
    source = _RecordedAggTradeRest(tape)

    outcome = await _run(ingest_engine, source, tmp_path)

    assert outcome.gaps_unrepaired == 1
    assert source.calls == 0
    assert len(await IngestRegistry(ingest_engine).open_gaps(SERIES)) == 1


async def test_a_disagreeing_overlap_writes_nothing_and_leaves_the_gap_open(
    ingest_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The escalation, end to end. The venue and the corpus describe one execution -- one
    aggregate trade id -- with different prices, so nothing from the seam is written."""
    tape = _tape()
    held = (*tape[:FIRST_MISSING], *tape[FIRST_HELD_AFTER:])
    _write_partition(tmp_path, held)
    await _record_gap(ingest_engine, tape)
    # A different stretch of the same recording, re-labelled with the bracket's id: real
    # venue numbers for a different trade, which is exactly what a disagreement is.
    elsewhere = tape_prints.prints(1, first_event_utc=TAPE_START, now_utc=NOW_UTC, offset=40)
    contradicted = tuple(
        replace(elsewhere[0], venue_trade_id=record.venue_trade_id)
        if index == FIRST_MISSING - 1
        else record
        for index, record in enumerate(tape)
    )

    with pytest.raises(SeamDisagreementError, match="disagree about trade"):
        await _run(ingest_engine, _RecordedAggTradeRest(contradicted), tmp_path)

    assert _partition_sources(tmp_path) == {
        record.venue_trade_id: RecordSource.STREAM for record in held
    }
    assert len(await IngestRegistry(ingest_engine).open_gaps(SERIES)) == 1


async def test_a_partition_carrying_an_unrecognised_source_stops_the_repair(
    ingest_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Defaulting it to one of ours on the rewrite is the one thing gate 11 -- the standing
    proof that no synthesised rows exist -- reads the column to rule out."""
    tape = _tape()
    _write_partition(tmp_path, (*tape[:FIRST_MISSING], *tape[FIRST_HELD_AFTER:]))
    await _record_gap(ingest_engine, tape)

    path = partition_path(COORDINATE, root=tmp_path)
    table = pq.read_table(path)
    relabelled = table.set_column(
        table.schema.get_field_index("source"),
        "source",
        pa.array(["invented"] * table.num_rows, type=pa.string()),
    )
    pq.write_table(relabelled, path)

    with pytest.raises(DataIntegrityError, match="which is not one of"):
        await _run(ingest_engine, _RecordedAggTradeRest(tape), tmp_path)


async def test_a_disconnect_gap_on_the_tape_is_not_repaired_by_fetching_prints(
    ingest_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A disconnect gap is a claim about observation, not about records. Recovering the
    prints does not make "nothing was being watched" false."""
    tape = _tape()
    _write_partition(tmp_path, (*tape[:FIRST_MISSING], *tape[FIRST_HELD_AFTER:]))
    await IngestRegistry(ingest_engine).record_series_gaps(
        SERIES,
        [
            RecordedGap(
                gap_start_utc=tape[FIRST_MISSING - 1].event_time_utc,
                gap_end_utc=tape[FIRST_HELD_AFTER].event_time_utc,
                gap_kind=GapKind.DISCONNECT,
                missing_bar_count=None,
            )
        ],
        discovered_at_utc=DISCOVERED_AT,
    )
    source = _RecordedAggTradeRest(tape)

    outcome = await _run(ingest_engine, source, tmp_path)

    assert outcome.gaps_examined == 0
    assert source.calls == 0
    assert len(await IngestRegistry(ingest_engine).open_gaps(SERIES)) == 1


async def test_a_closed_gap_records_its_resolution_and_stops_being_reported(
    ingest_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The row stays -- it is still the answer to which backtests ran over the hole -- and
    it stops refusing windows."""
    tape = _tape()
    _write_partition(tmp_path, (*tape[:FIRST_MISSING], *tape[FIRST_HELD_AFTER:]))
    await _record_gap(ingest_engine, tape)

    await _run(ingest_engine, _RecordedAggTradeRest(tape), tmp_path)

    assert await IngestRegistry(ingest_engine).recorded_gaps() == ()
    async with ingest_engine.connect() as connection:
        row = (
            await connection.execute(
                sa.text(
                    "SELECT resolution, resolved_at_utc, discovered_at_utc "
                    "FROM coverage_gap WHERE symbol = :symbol AND dataset = :dataset"
                ),
                {"symbol": SYMBOL, "dataset": Dataset.AGG_TRADES.value},
            )
        ).one()
    assert row.resolution == GapResolution.BACKFILLED.value
    assert row.resolved_at_utc == NOW_UTC
    assert row.discovered_at_utc == DISCOVERED_AT
