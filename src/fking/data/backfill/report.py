"""What a run reports, and the two sentences it must not be allowed to omit.

`DATA_PIPELINE.md` section 4: **a run that reports only `rows_out` has hidden its
rejections.** So every rendering here carries rows in, rows out, rows rejected and the
per-reason breakdown, and there is no summary line that reports only the first two. "Ingested
3,214 files" tells you nothing about the eleven that were eighty percent complete.

The second sentence is the one a researcher forgets rather than the one a pipeline hides:
**a hypothesis inherits the shortest history among its inputs.** BTCUSDT spot 1m klines begin
2017-08-17; every other symbol begins later, often by years. An idea that looked testable
over eight years of BTC is testable over the shortest history among the symbols it actually
needs, and that number is printed as its own line because a table of per-symbol start dates
does not make anyone do the minimum in their head.

Rendering is plain text on purpose. The JSON payload the CLI prints is the machine surface;
this is the one an operator reads at the end of a four-hour run, and a JSON blob is not it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from fking.data.format_resolver import Dataset, Market

__all__ = ["BackfillReport", "SymbolReport"]


@dataclass(frozen=True, slots=True)
class SymbolReport:
    """One symbol's run, including everything it declined to do.

    `partitions_resumed` is not decoration. A run reporting only what it wrote cannot tell
    a reader whether a re-run did nothing because the corpus is complete or because it
    failed to enumerate anything, and those look identical from a row count of zero.
    """

    symbol: str
    earliest_archive_date: date | None
    partitions_written: int
    partitions_resumed: int
    archives_ingested: int
    archives_absent: int
    rows_in: int
    rows_out: int
    rows_rejected: int
    rejection_reasons: Mapping[str, int]
    gaps_recorded: int
    gaps_newly_discovered: int
    gapped_duration: timedelta
    first_event_time_utc: datetime | None
    last_event_time_utc: datetime | None

    @property
    def is_published(self) -> bool:
        """Whether any archive for this symbol exists at all in the searched range."""
        return self.earliest_archive_date is not None

    def describe_rejections(self) -> str:
        """Per-reason counts, ordered by count. `none` where the file was clean."""
        if not self.rejection_reasons:
            return "none"
        ordered = sorted(self.rejection_reasons.items(), key=lambda pair: (-pair[1], pair[0]))
        return ", ".join(f"{reason}={tally}" for reason, tally in ordered)


@dataclass(frozen=True, slots=True)
class BackfillReport:
    """Every symbol's run, plus the aggregates a reader is otherwise asked to compute."""

    market: Market
    dataset: Dataset
    interval: str | None
    through_date: date
    symbols: tuple[SymbolReport, ...]

    @property
    def rows_in(self) -> int:
        return sum(symbol.rows_in for symbol in self.symbols)

    @property
    def rows_out(self) -> int:
        return sum(symbol.rows_out for symbol in self.symbols)

    @property
    def rows_rejected(self) -> int:
        return sum(symbol.rows_rejected for symbol in self.symbols)

    @property
    def gaps_recorded(self) -> int:
        return sum(symbol.gaps_recorded for symbol in self.symbols)

    @property
    def gaps_newly_discovered(self) -> int:
        return sum(symbol.gaps_newly_discovered for symbol in self.symbols)

    @property
    def gapped_duration(self) -> timedelta:
        return sum((symbol.gapped_duration for symbol in self.symbols), timedelta(0))

    @property
    def archives_ingested(self) -> int:
        return sum(symbol.archives_ingested for symbol in self.symbols)

    @property
    def archives_absent(self) -> int:
        return sum(symbol.archives_absent for symbol in self.symbols)

    @property
    def rejection_reasons(self) -> Mapping[str, int]:
        merged: dict[str, int] = {}
        for symbol in self.symbols:
            for reason, tally in symbol.rejection_reasons.items():
                merged[reason] = merged.get(reason, 0) + tally
        return merged

    @property
    def shortest_history_start(self) -> datetime | None:
        """The latest first observation across the symbols that produced any rows.

        The *latest* start, not the earliest: a hypothesis using all of these symbols can
        only be tested from the point at which the last of them began, and that is the
        number the run has to state out loud.
        """
        starts = [
            symbol.first_event_time_utc
            for symbol in self.symbols
            if symbol.first_event_time_utc is not None
        ]
        return max(starts) if starts else None

    @property
    def unpublished_symbols(self) -> tuple[str, ...]:
        return tuple(symbol.symbol for symbol in self.symbols if not symbol.is_published)

    def describe_rejections(self) -> str:
        reasons = self.rejection_reasons
        if not reasons:
            return "none"
        ordered = sorted(reasons.items(), key=lambda pair: (-pair[1], pair[0]))
        return ", ".join(f"{reason}={tally}" for reason, tally in ordered)

    def render(self) -> str:
        """The end-of-run summary, in the order a reader needs it."""
        interval = "" if self.interval is None else f" {self.interval}"
        lines = [
            f"backfill {self.market.value}/{self.dataset.value}{interval} "
            f"through {self.through_date.isoformat()}",
            "",
        ]
        for symbol in self.symbols:
            lines.extend(_render_symbol(symbol))
        lines.extend(
            [
                "",
                f"total   rows in {self.rows_in}, out {self.rows_out}, "
                f"rejected {self.rows_rejected} ({self.describe_rejections()})",
                f"        archives ingested {self.archives_ingested}, "
                f"absent {self.archives_absent}",
                f"        gaps {self.gaps_recorded} "
                f"({self.gaps_newly_discovered} newly discovered), "
                f"total gapped duration {self.gapped_duration}",
            ]
        )
        shortest = self.shortest_history_start
        if shortest is not None:
            lines.append(
                f"        usable history for a hypothesis over all of these symbols starts "
                f"{shortest.isoformat()} -- a hypothesis inherits the shortest history among "
                f"its inputs"
            )
        if self.unpublished_symbols:
            lines.append(
                f"        no archive published in the searched range for "
                f"{list(self.unpublished_symbols)}"
            )
        return "\n".join(lines)


def _render_symbol(symbol: SymbolReport) -> list[str]:
    if not symbol.is_published:
        return [f"{symbol.symbol:<12} no published archive in the searched range"]
    first = "-" if symbol.first_event_time_utc is None else symbol.first_event_time_utc.isoformat()
    last = "-" if symbol.last_event_time_utc is None else symbol.last_event_time_utc.isoformat()
    return [
        f"{symbol.symbol:<12} {first} .. {last}",
        f"{'':<12} partitions written {symbol.partitions_written}, "
        f"resumed {symbol.partitions_resumed}; archives ingested "
        f"{symbol.archives_ingested}, absent {symbol.archives_absent}",
        f"{'':<12} rows in {symbol.rows_in}, out {symbol.rows_out}, "
        f"rejected {symbol.rows_rejected} ({symbol.describe_rejections()})",
        f"{'':<12} gaps {symbol.gaps_recorded} "
        f"({symbol.gaps_newly_discovered} newly discovered), "
        f"gapped duration {symbol.gapped_duration}",
    ]
