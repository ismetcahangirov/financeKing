"""What the corpus actually holds for a window, stated as ranges rather than as a count.

`BACKTEST_ENGINE.md` section 9 states the response to missing bars in one line -- *do not
interpolate; report coverage; narrow the window or refuse* -- and this module is the report
half of it. The refusal is `_feed.MarketDataFeed.load`.

The report is written in **gap ranges**, never in a completeness percentage, because the
two lead to different decisions and only one of them is available from a percentage. "99.6%
covered" invites a shrug; "BTCUSDT is missing 2025-01-02T04:11Z .. 2025-01-02T05:23Z, 72
bars" tells the reader whether the hole overlaps the regime they are testing, whether it is
one outage or forty, and which range to backfill. A count alone cannot answer any of those,
and the number that would let a run proceed is the number nobody should be looking at.

Warm-up is inside the covered span rather than beside it. A hole in warm-up produces
features warmed from fewer observations than the strategy declared it needs -- values no
live run would ever have had -- and those land in the sample looking exactly like real ones.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from fking.data.format_resolver import EpochUnit, Market

__all__ = [
    "CoverageGap",
    "CoverageReport",
    "PartitionFormat",
    "SymbolCoverage",
    "gaps_against",
]


@dataclass(frozen=True, slots=True)
class CoverageGap:
    """A maximal run of consecutive lattice instants the corpus does not hold.

    Bounds are bar **open** times and the range is half-open: `gap_start_utc` is the first
    missing bar's open, `gap_end_utc` is the open of the first bar present after it. Ten
    silent minutes are one gap of ten rather than ten gaps of one, for the same reason the
    ingest cadence detector collapses them -- ten consecutive absences were one outage, and
    reporting them separately turns a readable report into a wall.
    """

    gap_start_utc: datetime
    gap_end_utc: datetime
    missing_bar_count: int

    @property
    def duration(self) -> timedelta:
        """How much wall time this gap spans."""
        return self.gap_end_utc - self.gap_start_utc

    def render(self) -> str:
        """One line naming the range and how many bars it is."""
        return (
            f"{self.gap_start_utc.isoformat()} .. {self.gap_end_utc.isoformat()} "
            f"({self.missing_bar_count} bars)"
        )


@dataclass(frozen=True, slots=True)
class PartitionFormat:
    """The epoch unit resolved for one partition the read touched.

    Recorded rather than discarded because it is the fact a mixed spot/futures run turns
    on: spot archives from 2025-01-01 are microsecond epochs and USDⓈ-M futures are still
    milliseconds, so one run legitimately reads two units and a single unit across both
    legs would misplace one of them by a factor of a thousand. `BACKTEST_ENGINE.md`
    section 8 puts data provenance in the tearsheet footer; this is that provenance, in
    the form the feed can prove.
    """

    year_month: str
    epoch_unit: EpochUnit


@dataclass(frozen=True, slots=True)
class SymbolCoverage:
    """One series' coverage over the requested span, warm-up included."""

    market: Market
    symbol: str
    observed_bar_count: int
    expected_bar_count: int
    first_open_time_utc: datetime | None
    last_open_time_utc: datetime | None
    gaps: tuple[CoverageGap, ...]
    partition_formats: tuple[PartitionFormat, ...]

    @property
    def label(self) -> str:
        """`market/symbol`, matching `SeriesRequest.label`."""
        return f"{self.market.value}/{self.symbol}"

    @property
    def missing_bar_count(self) -> int:
        """How many lattice instants the corpus does not hold."""
        return sum(gap.missing_bar_count for gap in self.gaps)

    @property
    def gapped_duration(self) -> timedelta:
        """Total wall time inside the gaps."""
        return sum((gap.duration for gap in self.gaps), timedelta(0))

    @property
    def is_complete(self) -> bool:
        """Whether every bar the window names is present."""
        return not self.gaps

    def render(self) -> tuple[str, ...]:
        """The lines this series contributes to the report."""
        first = "-" if self.first_open_time_utc is None else self.first_open_time_utc.isoformat()
        last = "-" if self.last_open_time_utc is None else self.last_open_time_utc.isoformat()
        units = ", ".join(
            f"{entry.year_month}={entry.epoch_unit.value}" for entry in self.partition_formats
        )
        lines = [
            f"{self.label:<24} {self.observed_bar_count}/{self.expected_bar_count} bars, "
            f"{first} .. {last}",
            f"{'':<24} epoch units {units or '(no partition read)'}",
        ]
        if self.is_complete:
            lines.append(f"{'':<24} no gaps")
            return tuple(lines)
        lines.append(
            f"{'':<24} {len(self.gaps)} gaps, {self.missing_bar_count} bars missing, "
            f"{self.gapped_duration} gapped"
        )
        lines.extend(f"{'':<24}   {gap.render()}" for gap in self.gaps)
        return tuple(lines)


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Every requested series' coverage, and whether the run can honestly be served.

    There is no threshold field and no `tolerated_missing_bar_count`. A configurable
    tolerance is the flag `docs/rules/safety-kernel.md` refuses for the allowlist, in a
    different costume: the number would be raised by whoever is in a hurry, and the run it
    admits is the one whose result nobody can distinguish from a real one.
    """

    bar_interval: str
    warmup_start_utc: datetime
    exposed_from_utc: datetime
    until_utc: datetime
    series: tuple[SymbolCoverage, ...]

    @property
    def is_servable(self) -> bool:
        """Whether every series holds every bar the window names."""
        return all(entry.is_complete for entry in self.series)

    @property
    def incomplete(self) -> tuple[SymbolCoverage, ...]:
        """The series with at least one gap, in report order."""
        return tuple(entry for entry in self.series if not entry.is_complete)

    def render(self) -> str:
        """The report an operator reads, per symbol, with the gap ranges spelled out."""
        lines = [
            f"coverage {self.bar_interval} "
            f"warmup {self.warmup_start_utc.isoformat()} .. {self.exposed_from_utc.isoformat()}, "
            f"exposed {self.exposed_from_utc.isoformat()} .. {self.until_utc.isoformat()}",
            "",
        ]
        for entry in self.series:
            lines.extend(entry.render())
        lines.append("")
        if self.is_servable:
            lines.append("every requested bar is present; the window is servable")
        else:
            lines.append(
                f"REFUSED: {len(self.incomplete)} of {len(self.series)} series have gaps. "
                f"Narrow the window or backfill the ranges above. Bars are never "
                f"interpolated -- an invented bar is a price that existed nowhere, at a "
                f"timestamp at which nobody could have traded"
            )
        return "\n".join(lines)


def gaps_against(
    lattice: Iterable[datetime], observed_open_times: Sequence[datetime], *, duration: timedelta
) -> tuple[CoverageGap, ...]:
    """Maximal runs of lattice instants absent from `observed_open_times`.

    Computed from the lattice rather than from adjacent differences between the bars that
    *are* present. The difference matters at the edges: a series whose first held bar is an
    hour into the window has an hour-long gap that no pairwise difference can see, because
    there is no earlier bar to difference against -- and that is the shape a truncated
    archive takes.

    Each gap ends `duration` after its last missing bar's open, so the range covers the
    wall time nobody could have traded in rather than stopping at the last absent open.
    That makes a trailing gap -- one running to the window's end, with no present bar after
    it to close against -- reportable in the same terms as an interior one.
    """
    held = frozenset(observed_open_times)
    gaps: list[CoverageGap] = []
    run_start_utc: datetime | None = None
    run_length = 0

    def _close(run_start: datetime, length: int) -> CoverageGap:
        return CoverageGap(
            gap_start_utc=run_start,
            gap_end_utc=run_start + duration * length,
            missing_bar_count=length,
        )

    for candidate in lattice:
        if candidate in held:
            if run_start_utc is not None:
                gaps.append(_close(run_start_utc, run_length))
                run_start_utc, run_length = None, 0
        else:
            if run_start_utc is None:
                run_start_utc = candidate
            run_length += 1

    if run_start_utc is not None:
        gaps.append(_close(run_start_utc, run_length))
    return tuple(gaps)
