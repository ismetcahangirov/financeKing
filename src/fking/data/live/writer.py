"""Persisting what a live session produced: closed bars, and gaps.

Two stores, because the two facts have different owners. Bars go to the `bar`
hypertable with `source = 'stream'`, which is the operational series the live loop reads
back (`DATA_PIPELINE.md` section 6). Gaps go to `coverage_gap` through the same
`IngestRegistry` the archive backfill writes to, which is what makes a live outage and a
missing archive indistinguishable to every downstream reader.

**`ON CONFLICT DO NOTHING`, not `DO UPDATE`.** A reconnect replays the minutes that
overlap the previous session, and a bar is immutable once closed -- so a second arrival
of the same `(instrument_id, timeframe, open_time_utc)` is a duplicate delivery, not a
correction. Updating would let a later frame silently rewrite a bar a backtest has
already consumed, which is the mutation of a time series that section 5 forbids in its
first paragraph. If the venue ever does send a *different* bar for a minute we already
hold, that is an escalation for `fking.data.backfill.seam`, not something a write path
resolves by preference.

**An unknown symbol is refused, never dropped.** Bars are keyed by `instrument_id`, and
a symbol with no `instrument` row cannot be written. Skipping it would mean a session
that looks healthy while one symbol's data goes nowhere; refusing means the mistake is
found at the first frame.

The engine is the ingest role's. `bar` is `INGEST_OWNED` and `coverage_gap` is
`INGEST_RESOLVABLE` (`fking.platform.persistence.privileges`), so `fking_app` can read
both and write neither -- an application role that could write a coverage row could
declare a gap closed that the corpus does not hold.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from fking.data.backfill.registry import IngestRegistry, SeriesKey
from fking.data.bars import INSERT_BAR, bar_parameters, resolve_instrument_ids
from fking.data.live.router import LiveBar, LiveGap
from fking.data.parquet.schema import RecordSource
from fking.platform.errors import DataUnavailableError

__all__ = ["BAR_SOURCE_STREAM", "LiveMarketDataWriter"]

# The `bar.source` value the table's own CHECK admits for live data. The archive path
# writes 'archive' and the REST gap backfill writes 'rest_backfill'; keeping the three
# distinct is what lets the standing no-synthesised-rows gate
# (`fking.data.quality.standing`) ask which writer produced a row.
BAR_SOURCE_STREAM: Final[str] = RecordSource.STREAM.value


class LiveMarketDataWriter:
    """Writes a live session's output. Holds an engine, never a caller's connection.

    One short transaction per batch rather than one per session: a live session runs
    until the venue closes it, which is up to 24 hours, and a session-length transaction
    would hold its snapshot for that long and lose every bar in it to one interruption.
    """

    __slots__ = ("_engine", "_instrument_ids", "_registry", "_venue_id")

    def __init__(self, engine: AsyncEngine, *, venue_id: str) -> None:
        self._engine = engine
        self._venue_id = venue_id
        self._registry = IngestRegistry(engine)
        self._instrument_ids: dict[str, UUID] = {}

    async def resolve_instruments(self, symbols: Sequence[str]) -> None:
        """Look up every symbol's instrument id up front, or refuse the session.

        Resolved once at startup rather than lazily on the first bar, so a missing
        `instrument` row stops the session before it has opened a socket -- rather than
        fifty-nine seconds later, when the first minute closes and the write fails with
        a partially consumed stream behind it.
        """
        self._instrument_ids.update(
            await resolve_instrument_ids(self._engine, venue_id=self._venue_id, symbols=symbols)
        )

    async def write_bars(self, bars: Sequence[LiveBar]) -> int:
        """Insert closed bars, returning how many were new.

        The count excludes rows the primary key already held, which is what makes
        "this reconnect replayed four minutes we already had" a number a log line can
        state rather than a guess.
        """
        if not bars:
            return 0
        inserted = 0
        async with self._engine.begin() as connection:
            for bar in bars:
                row = (await connection.execute(INSERT_BAR, self._bar_parameters(bar))).first()
                if row is not None:
                    inserted += 1
        return inserted

    async def write_gaps(self, gaps: Sequence[LiveGap], *, discovered_at_utc: datetime) -> int:
        """Record gaps, returning how many were not already known.

        Grouped by series so one transaction covers a series' gaps, matching the
        registry's own grain. A gap already in the table is not counted, so a poll that
        rediscovers nothing reports zero rather than repeating itself.
        """
        if not gaps:
            return 0
        by_series: dict[SeriesKey, list[LiveGap]] = {}
        for gap in gaps:
            by_series.setdefault(gap.series, []).append(gap)
        newly_discovered = 0
        for series, series_gaps in by_series.items():
            newly_discovered += await self._registry.record_series_gaps(
                series,
                [entry.gap for entry in series_gaps],
                discovered_at_utc=discovered_at_utc,
            )
        return newly_discovered

    def _bar_parameters(self, bar: LiveBar) -> dict[str, object]:
        instrument_id = self._instrument_ids.get(bar.series.symbol)
        if instrument_id is None:
            raise DataUnavailableError(
                f"{bar.series.symbol} was never resolved to an instrument id; call "
                f"resolve_instruments() before writing"
            )
        return bar_parameters(
            bar.record,
            instrument_id=instrument_id,
            timeframe=bar.series.bar_interval,
            source=RecordSource.STREAM,
        )
