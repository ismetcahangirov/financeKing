"""The seam: what a REST repair may merge, what it must refuse, and what it may claim.

Split in two on purpose. The reconciler and the page parser are pure and are asserted
without a socket or a database; everything about *narrowing a gap* runs against real
PostgreSQL, because the claims there are claims about a `CHECK`, a trigger and an
`ON CONFLICT` -- and a mocked connection would be the backfiller answering a question
about itself (`TESTING.md`, `CLAUDE.md` section 5).

Every kline in this file comes from `tests/support/rest_klines`, which builds the wire
shape out of checksum-verified bytes recorded from `data.binance.vision`. The prices,
volumes and trade counts are the venue's own; only the minute grid is re-based. A
hand-written page would encode this file's beliefs about the encoding, and the two beliefs
that matter -- string decimals, integer millisecond epochs -- are exactly the two the
parser exists to enforce.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.data.backfill.gaps import BackfillOutcome, KlineGapBackfiller
from fking.data.backfill.registry import (
    GapKind,
    GapResolution,
    IngestRegistry,
    RecordedGap,
    SeriesKey,
)
from fking.data.backfill.rest import KLINE_FIELD_COUNT, parse_kline_page
from fking.data.backfill.seam import reconcile_klines
from fking.data.format_resolver import Dataset, Market
from fking.data.loaders.records import KlineRecord
from fking.data.parquet.schema import RecordSource
from fking.platform.errors import DataIntegrityError, SeamDisagreementError
from tests.support import rest_klines

pytestmark = pytest.mark.asyncio

VENUE_ID = "binance-futures-testnet"
SYMBOL = "BTCUSDT"
BAR_INTERVAL = "1m"
MINUTE = timedelta(minutes=1)

# The window every database test's gap sits in. Fixed rather than clock-derived: the epoch
# plausibility check in `epoch_to_utc` is a function of `now`, so a test reading the real
# clock would move its own boundary conditions every day it ran.
NOW_UTC = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
GAP_START = datetime(2026, 8, 4, 10, 0, tzinfo=UTC)
GAP_MINUTES = 60
GAP_END = GAP_START + MINUTE * GAP_MINUTES
RECOVERED_MINUTES = 40
REMAINING_MINUTES = GAP_MINUTES - RECOVERED_MINUTES

KLINE_SERIES = SeriesKey(
    market=Market.FUTURES_UM, dataset=Dataset.KLINES, symbol=SYMBOL, bar_interval=BAR_INTERVAL
)


# ---------------------------------------------------------------------------
# Building recorded bars
# ---------------------------------------------------------------------------


def _records(first_open_utc: datetime, count: int, *, offset: int = 0) -> tuple[KlineRecord, ...]:
    """`count` recorded bars whose grid starts at `first_open_utc`.

    `offset` picks a different stretch of the recorded day, which is how a test gets two
    bars that share a minute and genuinely differ -- rather than by editing a number,
    which would stop the comparison being against real venue values.
    """
    rows = rest_klines.recorded_rows()[offset : offset + count]
    shifted = rest_klines.shift_to(rows, first_open_utc=first_open_utc)
    return parse_kline_page(rest_klines.page(shifted), now_utc=NOW_UTC)


def _page(first_open_utc: datetime, count: int, *, offset: int = 0) -> str:
    rows = rest_klines.recorded_rows()[offset : offset + count]
    return rest_klines.page(rest_klines.shift_to(rows, first_open_utc=first_open_utc))


class _RecordedRest:
    """A `KlineRestSource` replaying recorded bars, restricted to a chosen minute set.

    The restriction is the interesting part: it is how "the endpoint has forty of the
    sixty minutes you asked for" is expressed without inventing a venue behaviour.
    """

    def __init__(self, available: Sequence[KlineRecord]) -> None:
        self._available = tuple(available)
        self.requests: list[tuple[str, str, datetime, datetime, datetime]] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def klines(
        self,
        *,
        symbol: str,
        interval: str,
        from_utc: datetime,
        until_utc: datetime,
        now_utc: datetime,
    ) -> tuple[KlineRecord, ...]:
        self.requests.append((symbol, interval, from_utc, until_utc, now_utc))
        return tuple(
            record for record in self._available if from_utc <= record.open_time_utc < until_utc
        )


# ---------------------------------------------------------------------------
# The reconciler, pure
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_an_agreeing_overlap_leaves_exactly_one_record_per_minute() -> None:
    """The dedupe case. Two views of one closed bar are one bar, not two rows."""
    held = _records(GAP_START, 5)
    fetched = _records(GAP_START, 8)

    seam = reconcile_klines(held, fetched)

    assert len(seam.merged) == len(fetched)
    assert seam.agreed == len(held)
    assert seam.recovered == len(fetched) - len(held)
    assert [record.open_time_utc for record in seam.merged] == sorted(
        {record.open_time_utc for record in fetched}
    )


@pytest.mark.unit
async def test_a_close_time_differing_by_epoch_resolution_is_not_a_disagreement() -> None:
    """The trap the seam exists to survive, stated as a test.

    Binance files `close_time` as the last representable instant inside the interval, so
    the same bar reads `.999` from a millisecond source and `.999999` from a microsecond
    one (VF-015). Comparing it would escalate every seam spanning the spot cutover while
    detecting nothing about the market.
    """
    held = _records(GAP_START, 3)
    fetched = tuple(
        replace(record, close_time_utc=record.close_time_utc + timedelta(microseconds=999))
        for record in held
    )

    seam = reconcile_klines(held, fetched)

    assert seam.agreed == len(held)


@pytest.mark.unit
async def test_a_volume_disagreement_on_an_overlapping_bar_escalates() -> None:
    """A closed bar is immutable, so two views of it cannot both be right."""
    held = _records(GAP_START, 3)
    # A different stretch of the same recorded day, re-based onto the same minutes: real
    # venue numbers for a different period, which is exactly what a disagreement is.
    fetched = _records(GAP_START, 3, offset=500)

    with pytest.raises(SeamDisagreementError, match="disagree about the closed bar"):
        reconcile_klines(held, fetched)


@pytest.mark.unit
async def test_one_source_filing_a_minute_twice_escalates() -> None:
    """The same failure one step earlier, and it must not be silently deduplicated."""
    first = _records(GAP_START, 1)
    second = _records(GAP_START, 1, offset=500)

    with pytest.raises(SeamDisagreementError, match="two different bars opening at"):
        reconcile_klines((), (*first, *second))


# ---------------------------------------------------------------------------
# The page parser, pure
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_a_recorded_page_parses_to_exact_decimals_and_utc_minutes() -> None:
    parsed = parse_kline_page(_page(GAP_START, 3), now_utc=NOW_UTC)

    assert [record.open_time_utc for record in parsed] == [
        GAP_START,
        GAP_START + MINUTE,
        GAP_START + MINUTE * 2,
    ]
    # The recorded futures archive's first bar for 2025-01-02, to the digit.
    assert parsed[0].open_quote_price == Decimal("94580.90")
    assert parsed[0].base_volume == Decimal("51.415")
    assert all(isinstance(record.close_quote_price, Decimal) for record in parsed)


@pytest.mark.unit
async def test_a_json_number_in_a_price_field_is_refused() -> None:
    """Binance sends decimals as strings so they survive a parser with no Decimal
    support. One arriving as a number has already lost precision before we see it, so the
    honest response is to refuse rather than to build a Decimal over the damage."""
    rows = json.loads(_page(GAP_START, 1))
    rows[0][1] = 94580.90

    with pytest.raises(DataIntegrityError, match="string-encoded decimal"):
        parse_kline_page(json.dumps(rows), now_utc=NOW_UTC)


@pytest.mark.unit
async def test_a_row_with_a_missing_field_is_refused() -> None:
    """The array is positional, so a dropped column shifts every value after it and no
    individual field looks wrong."""
    rows = json.loads(_page(GAP_START, 1))
    rows[0].pop()

    with pytest.raises(DataIntegrityError, match=f"expected {KLINE_FIELD_COUNT}"):
        parse_kline_page(json.dumps(rows), now_utc=NOW_UTC)


@pytest.mark.unit
async def test_an_error_envelope_is_not_read_as_an_empty_page() -> None:
    """`{"code": -1121, "msg": "Invalid symbol."}` is an object. Treating it as zero bars
    would close a gap by deciding the venue has nothing for it."""
    with pytest.raises(DataIntegrityError, match="not a JSON array"):
        parse_kline_page('{"code":-1121,"msg":"Invalid symbol."}', now_utc=NOW_UTC)


# ---------------------------------------------------------------------------
# Narrowing a gap, against real PostgreSQL
# ---------------------------------------------------------------------------


async def _seed_instrument(admin_engine: AsyncEngine) -> uuid.UUID:
    """`venue` and `instrument` are REFERENCE: `fking_ingest` holds only SELECT on them,
    so the reference row is seeded through the migrator's engine."""
    instrument_id = uuid.uuid4()
    async with admin_engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO venue (venue_id, display_name) "
                "VALUES (:venue_id, 'Binance Futures Testnet') ON CONFLICT DO NOTHING"
            ),
            {"venue_id": VENUE_ID},
        )
        await connection.execute(
            sa.text(
                """
                INSERT INTO instrument (instrument_id, venue_id, symbol, market,
                                        base_asset, quote_asset, tick_size, lot_step,
                                        min_notional_quote, listed_at_utc)
                VALUES (:instrument_id, :venue_id, :symbol, 'futures_um', 'BTC', 'USDT',
                        0.10, 0.001, 5, '2019-09-08T00:00:00Z')
                """
            ),
            {"instrument_id": instrument_id, "venue_id": VENUE_ID, "symbol": SYMBOL},
        )
    return instrument_id


async def _insert_bars(
    engine: AsyncEngine, instrument_id: uuid.UUID, records: Sequence[KlineRecord], *, source: str
) -> None:
    """Seed bars the corpus is supposed to already hold, as the ingest role would have."""
    async with engine.begin() as connection:
        for record in records:
            await connection.execute(
                sa.text(
                    """
                    INSERT INTO bar (
                        instrument_id, timeframe, open_time_utc, close_time_utc,
                        open_quote_price, high_quote_price, low_quote_price,
                        close_quote_price, base_volume, quote_volume,
                        taker_buy_base_volume, taker_buy_quote_volume, trade_count, source
                    )
                    VALUES (
                        :instrument_id, :timeframe, :open_time_utc, :close_time_utc,
                        :open_quote_price, :high_quote_price, :low_quote_price,
                        :close_quote_price, :base_volume, :quote_volume,
                        :taker_buy_base_volume, :taker_buy_quote_volume, :trade_count,
                        :source
                    )
                    """
                ),
                {
                    "instrument_id": instrument_id,
                    "timeframe": BAR_INTERVAL,
                    "open_time_utc": record.open_time_utc,
                    "close_time_utc": record.close_time_utc,
                    "open_quote_price": record.open_quote_price,
                    "high_quote_price": record.high_quote_price,
                    "low_quote_price": record.low_quote_price,
                    "close_quote_price": record.close_quote_price,
                    "base_volume": record.base_volume,
                    "quote_volume": record.quote_volume,
                    "taker_buy_base_volume": record.taker_buy_base_volume,
                    "taker_buy_quote_volume": record.taker_buy_quote_volume,
                    "trade_count": record.trade_count,
                    "source": source,
                },
            )


async def _record_gap(
    engine: AsyncEngine,
    *,
    gap_start_utc: datetime = GAP_START,
    gap_end_utc: datetime = GAP_END,
    gap_kind: GapKind = GapKind.CADENCE,
    missing_bar_count: int | None = GAP_MINUTES,
) -> None:
    await IngestRegistry(engine).record_series_gaps(
        KLINE_SERIES,
        [
            RecordedGap(
                gap_start_utc=gap_start_utc,
                gap_end_utc=gap_end_utc,
                gap_kind=gap_kind,
                missing_bar_count=missing_bar_count,
            )
        ],
        discovered_at_utc=NOW_UTC - timedelta(days=1),
    )


async def _bar_count_between(
    engine: AsyncEngine, instrument_id: uuid.UUID, *, from_utc: datetime, until_utc: datetime
) -> int:
    async with engine.connect() as connection:
        return int(
            (
                await connection.execute(
                    sa.text(
                        "SELECT count(*) FROM bar WHERE instrument_id = :instrument_id "
                        "AND open_time_utc >= :from_utc AND open_time_utc < :until_utc"
                    ),
                    {
                        "instrument_id": instrument_id,
                        "from_utc": from_utc,
                        "until_utc": until_utc,
                    },
                )
            ).scalar_one()
        )


def _backfiller(engine: AsyncEngine, source: _RecordedRest) -> KlineGapBackfiller:
    return KlineGapBackfiller(
        engine=engine,
        venue_id=VENUE_ID,
        market=Market.FUTURES_UM,
        source=source,
        clock=lambda: NOW_UTC,
    )


async def _run(engine: AsyncEngine, source: _RecordedRest) -> BackfillOutcome:
    return await _backfiller(engine, source).run([SYMBOL], bar_interval=BAR_INTERVAL)


@pytest.mark.integration
async def test_a_partial_backfill_narrows_the_gap_and_does_not_close_it(
    engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """Forty of sixty missing minutes recovered leaves twenty missing.

    A partially backfilled gap recorded as closed is worse than an open one, because the
    coverage report then tells `backtest` it may run over a range that is still holed.
    """
    instrument_id = await _seed_instrument(engine)
    await _record_gap(ingest_engine)
    # The seam: the bar before the gap, which the corpus holds and the fetch overlaps.
    await _insert_bars(
        ingest_engine,
        instrument_id,
        _records(GAP_START - MINUTE, 1),
        source=RecordSource.STREAM.value,
    )
    available = _records(GAP_START - MINUTE, 1 + RECOVERED_MINUTES)

    outcome = await _run(ingest_engine, _RecordedRest(available))

    assert outcome.gaps_examined == 1
    assert outcome.gaps_closed == 0
    assert outcome.gaps_narrowed == 1
    assert outcome.bars_written == RECOVERED_MINUTES
    assert outcome.minutes_still_missing == REMAINING_MINUTES

    open_gaps = await IngestRegistry(ingest_engine).open_gaps(KLINE_SERIES)
    assert len(open_gaps) == 1
    residual = open_gaps[0].gap
    assert residual.gap_start_utc == GAP_START + MINUTE * RECOVERED_MINUTES
    assert residual.gap_end_utc == GAP_END
    assert residual.missing_bar_count == REMAINING_MINUTES
    assert residual.gap_kind is GapKind.CADENCE

    # The residual inherits the original discovery instant. Those minutes were found
    # missing then and are missing still; stamping them now would launder a long-known
    # hole into a freshly discovered one.
    assert open_gaps[0].discovered_at_utc == NOW_UTC - timedelta(days=1)


@pytest.mark.integration
async def test_the_registry_bounds_agree_with_the_rows_that_are_actually_missing(
    engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """The claim a coverage report makes, checked against the table it describes."""
    instrument_id = await _seed_instrument(engine)
    await _record_gap(ingest_engine)
    await _insert_bars(
        ingest_engine,
        instrument_id,
        _records(GAP_START - MINUTE, 1),
        source=RecordSource.STREAM.value,
    )

    await _run(ingest_engine, _RecordedRest(_records(GAP_START - MINUTE, 1 + RECOVERED_MINUTES)))

    registry = IngestRegistry(ingest_engine)
    residual = (await registry.open_gaps(KLINE_SERIES))[0].gap
    assert (
        await _bar_count_between(
            ingest_engine,
            instrument_id,
            from_utc=residual.gap_start_utc,
            until_utc=residual.gap_end_utc,
        )
        == 0
    )
    assert (
        await _bar_count_between(
            ingest_engine,
            instrument_id,
            from_utc=GAP_START,
            until_utc=residual.gap_start_utc,
        )
        == RECOVERED_MINUTES
    )


@pytest.mark.integration
async def test_a_fully_recovered_gap_is_marked_backfilled_and_stops_being_reported(
    engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """The row stays -- it is still the answer to which backtests ran over the hole --
    and it stops refusing windows."""
    instrument_id = await _seed_instrument(engine)
    await _record_gap(ingest_engine)
    await _insert_bars(
        ingest_engine,
        instrument_id,
        _records(GAP_START - MINUTE, 1),
        source=RecordSource.STREAM.value,
    )

    outcome = await _run(
        ingest_engine, _RecordedRest(_records(GAP_START - MINUTE, 1 + GAP_MINUTES))
    )

    assert outcome.gaps_closed == 1
    assert outcome.bars_written == GAP_MINUTES
    assert await IngestRegistry(ingest_engine).open_gaps(KLINE_SERIES) == ()
    assert await IngestRegistry(ingest_engine).recorded_gaps() == ()

    async with ingest_engine.connect() as connection:
        row = (
            await connection.execute(
                sa.text(
                    "SELECT resolution, resolved_at_utc, discovered_at_utc "
                    "FROM coverage_gap WHERE symbol = :symbol"
                ),
                {"symbol": SYMBOL},
            )
        ).one()
    assert row.resolution == GapResolution.BACKFILLED.value
    assert row.resolved_at_utc == NOW_UTC
    assert row.discovered_at_utc == NOW_UTC - timedelta(days=1)


@pytest.mark.integration
async def test_recovered_bars_carry_the_rest_backfill_source(
    engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """A bar recovered hours later is not a bar the socket delivered on time, and the
    provenance column is the only place that difference survives."""
    instrument_id = await _seed_instrument(engine)
    await _record_gap(ingest_engine)
    await _insert_bars(
        ingest_engine,
        instrument_id,
        _records(GAP_START - MINUTE, 1),
        source=RecordSource.STREAM.value,
    )

    await _run(ingest_engine, _RecordedRest(_records(GAP_START - MINUTE, 1 + GAP_MINUTES)))

    async with ingest_engine.connect() as connection:
        sources = (
            await connection.execute(
                sa.text(
                    "SELECT DISTINCT source FROM bar WHERE instrument_id = :instrument_id "
                    "AND open_time_utc >= :from_utc ORDER BY source"
                ),
                {"instrument_id": instrument_id, "from_utc": GAP_START},
            )
        ).scalars()
    assert list(sources) == [RecordSource.REST_BACKFILL.value]


@pytest.mark.integration
async def test_a_disagreeing_overlap_writes_nothing_and_leaves_the_gap_open(
    engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """The escalation, end to end. Neither version is written -- including the bars from
    the same fetch that agreed."""
    instrument_id = await _seed_instrument(engine)
    await _record_gap(ingest_engine)
    await _insert_bars(
        ingest_engine,
        instrument_id,
        _records(GAP_START - MINUTE, 1),
        source=RecordSource.STREAM.value,
    )
    # The overlapping bar the venue returns is a different recorded minute re-based onto
    # the same open time, so it is a genuine disagreement about real numbers.
    disagreeing = (
        *_records(GAP_START - MINUTE, 1, offset=500),
        *_records(GAP_START, GAP_MINUTES),
    )

    with pytest.raises(SeamDisagreementError, match="disagree about the closed bar"):
        await _run(ingest_engine, _RecordedRest(disagreeing))

    assert (
        await _bar_count_between(
            ingest_engine, instrument_id, from_utc=GAP_START, until_utc=GAP_END
        )
        == 0
    )
    assert len(await IngestRegistry(ingest_engine).open_gaps(KLINE_SERIES)) == 1


@pytest.mark.integration
async def test_an_endpoint_with_nothing_to_offer_leaves_the_gap_exactly_as_it_was(
    engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """Resolving on a residual that reproduces the gap's own bounds would delete it: the
    insert deduplicates to the same row and the mark then closes it."""
    await _seed_instrument(engine)
    await _record_gap(ingest_engine)

    outcome = await _run(ingest_engine, _RecordedRest(()))

    assert outcome.gaps_unrepaired == 1
    assert outcome.gaps_closed == 0
    assert outcome.gaps_narrowed == 0
    open_gaps = await IngestRegistry(ingest_engine).open_gaps(KLINE_SERIES)
    assert len(open_gaps) == 1
    assert (open_gaps[0].gap.gap_start_utc, open_gaps[0].gap.gap_end_utc) == (GAP_START, GAP_END)


@pytest.mark.integration
async def test_a_disconnect_gap_is_not_repaired_by_fetching_its_bars(
    engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """A disconnect gap is a claim about observation, not about bars. Recovering the
    klines does not make "nothing was being watched" false, and it does not recover the
    trade prints from the same window."""
    await _seed_instrument(engine)
    await _record_gap(ingest_engine, gap_kind=GapKind.DISCONNECT, missing_bar_count=None)

    source = _RecordedRest(_records(GAP_START, GAP_MINUTES))
    outcome = await _run(ingest_engine, source)

    assert outcome.gaps_examined == 0
    assert source.calls == 0
    assert len(await IngestRegistry(ingest_engine).open_gaps(KLINE_SERIES)) == 1


@pytest.mark.integration
async def test_the_ingest_role_cannot_delete_a_gap_at_all(ingest_engine: AsyncEngine) -> None:
    """The grant is the primary control, so the refusal is `permission denied` and the
    trigger never has to fire. That ordering matters: TRUNCATE fires no row trigger, so
    only the revoke reaches it."""
    await _record_gap(ingest_engine)

    async with ingest_engine.begin() as connection:
        with pytest.raises(DBAPIError, match="permission denied for table coverage_gap"):
            await connection.execute(
                sa.text("DELETE FROM coverage_gap WHERE symbol = :symbol"), {"symbol": SYMBOL}
            )


@pytest.mark.integration
async def test_the_delete_trigger_refuses_even_the_table_owner(
    engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """The backstop, asserted as the one role the grant does not stop.

    A later migration handing a broad role to a new service restores the `DELETE` grant
    and leaves only this. Run as the migrator, which owns the table, so the grant is out
    of the way and the trigger is the thing under test.
    """
    await _record_gap(ingest_engine)

    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="never deleted"):
            await connection.execute(
                sa.text("DELETE FROM coverage_gap WHERE symbol = :symbol"), {"symbol": SYMBOL}
            )


@pytest.mark.integration
async def test_a_gap_row_cannot_be_rewritten_except_in_its_resolution(
    ingest_engine: AsyncEngine,
) -> None:
    """The bounds, the kind and `discovered_at_utc` are what a superseded backtest is
    traced through, and no CHECK can express "everything but two columns is frozen"."""
    await _record_gap(ingest_engine)

    async with ingest_engine.begin() as connection:
        with pytest.raises(DBAPIError, match="rewritable only in its resolution"):
            await connection.execute(
                sa.text("UPDATE coverage_gap SET gap_end_utc = :moved WHERE symbol = :symbol"),
                {"moved": GAP_END + MINUTE, "symbol": SYMBOL},
            )


@pytest.mark.integration
async def test_a_resolution_is_written_once(
    engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """Re-opening a resolved gap would hide that the range was believed complete, which is
    the state every backtest run in between was scored against."""
    instrument_id = await _seed_instrument(engine)
    await _record_gap(ingest_engine)
    await _insert_bars(
        ingest_engine,
        instrument_id,
        _records(GAP_START - MINUTE, 1),
        source=RecordSource.STREAM.value,
    )
    await _run(ingest_engine, _RecordedRest(_records(GAP_START - MINUTE, 1 + GAP_MINUTES)))

    async with ingest_engine.begin() as connection:
        with pytest.raises(DBAPIError, match="already resolved"):
            await connection.execute(
                sa.text(
                    "UPDATE coverage_gap SET resolution = NULL, resolved_at_utc = NULL "
                    "WHERE symbol = :symbol"
                ),
                {"symbol": SYMBOL},
            )
