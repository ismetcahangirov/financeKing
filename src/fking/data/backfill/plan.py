"""Which archives exist, and which Parquet file each one belongs in.

A plan is computed before anything is fetched and is a pure function of
`(market, dataset, symbol, interval, earliest_date, through_date, today_utc)`. That is
what makes a resumed run enumerate exactly the periods the interrupted run enumerated:
the plan is not derived from what is already on disk, so it cannot drift as the corpus
fills up. Resume is a decision taken per planned partition, against the registry and the
corpus, and it is taken in `runner`.

Two facts decide the shape, and they pull in opposite directions:

**The publication calendar.** Monthly archives lag, so the current and previous month are
only available daily (`resolve_granularity`). One monthly request replaces thirty daily
ones everywhere else.

**The partition grain.** Bars are one Parquet file per month, trades one per day
(`DATASET_PARTITION_GRAIN`). `write_records` writes a partition whole, so an archive may
never span more than one partition -- which makes a monthly *trades* archive unusable
however cheap it is, and is why the plan states a granularity rather than letting the
fetcher pick the cheaper one.

The result is the mapping this module exists to compute: a bar partition is fed by one
monthly archive for settled history and by up to thirty-one daily archives for the recent
tail; a trade partition is fed by exactly one daily archive, always.

**Earliest dates are discovered by probing, never assumed.** BTCUSDT spot 1m klines begin
2017-08-17 and every other symbol begins later (`DATA_PIPELINE.md` section 2). The probe
fetches the `.CHECKSUM` sibling, which is eighty bytes, and binary-searches the publication
boundary in a logarithmic number of requests rather than walking a hundred months.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

import structlog

from fking.data.archive import (
    ArchiveCoordinate,
    Granularity,
    archive_url,
    resolve_granularity,
)
from fking.data.format_resolver import Dataset, Market
from fking.data.parquet.layout import DATASET_PARTITION_GRAIN, PartitionGrain
from fking.platform.errors import DataIntegrityError
from fking.platform.safety.archive import ArchiveEgress, ArchiveUnavailableError

__all__ = [
    "ArchiveSeries",
    "PartitionPlan",
    "PlannedArchive",
    "discover_earliest_archive_date",
    "plan_partitions",
]

_LOG: Final = structlog.get_logger(__name__)

_ONE_DAY: Final[timedelta] = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class ArchiveSeries:
    """What identifies one series: market, dataset, symbol, and interval where there is one.

    The same four fields the registry keys every row on. Kept as one value rather than four
    parameters because they travel together through every function here and in `runner`, and
    four positional strings in a row is how a symbol ends up in the interval slot.
    """

    market: Market
    dataset: Dataset
    symbol: str
    interval: str | None

    def at(self, archive_date: date) -> ArchiveCoordinate:
        """This series' coordinate for one date."""
        return ArchiveCoordinate(
            market=self.market,
            dataset=self.dataset,
            symbol=self.symbol,
            archive_date=archive_date,
            interval=self.interval,
        )


@dataclass(frozen=True, slots=True)
class PlannedArchive:
    """One archive to fetch, and the granularity its partition grain requires."""

    coordinate: ArchiveCoordinate
    granularity: Granularity

    @property
    def covered_from_date(self) -> date:
        if self.granularity is Granularity.DAILY:
            return self.coordinate.archive_date
        return _month_start(self.coordinate.archive_date)

    @property
    def covered_through_date(self) -> date:
        if self.granularity is Granularity.DAILY:
            return self.coordinate.archive_date
        return _month_end(self.coordinate.archive_date)


@dataclass(frozen=True, slots=True)
class PartitionPlan:
    """One Parquet file and the archives that fill it.

    `coordinate` addresses the partition, not any one archive: its `archive_date` is the
    period's first day, which is what `partition_path` reads to place the file and what the
    registry stores as `period_start_date`.
    """

    coordinate: ArchiveCoordinate
    grain: PartitionGrain
    archives: tuple[PlannedArchive, ...]

    @property
    def period_start_date(self) -> date:
        return self.coordinate.archive_date

    @property
    def covered_from_date(self) -> date:
        return min(planned.covered_from_date for planned in self.archives)

    @property
    def covered_through_date(self) -> date:
        return max(planned.covered_through_date for planned in self.archives)

    @property
    def label(self) -> str:
        interval = "" if self.coordinate.interval is None else f"/{self.coordinate.interval}"
        return (
            f"{self.coordinate.market.value}/{self.coordinate.dataset.value}/"
            f"{self.coordinate.symbol}{interval}@{self.period_start_date.isoformat()}"
        )


def plan_partitions(
    series: ArchiveSeries,
    *,
    earliest_date: date,
    through_date: date,
    today_utc: date,
) -> tuple[PartitionPlan, ...]:
    """Every partition covering `[earliest_date, through_date]`, oldest first.

    Ordering is part of the contract rather than a convenience. Seam gaps are found by
    comparing a partition's first event against the previous partition's last, so a plan
    that emitted periods out of order would compute a negative elapsed interval and report
    no gap where the largest one is.

    Raises:
        DataIntegrityError: the range is inverted, or the dataset has no declared
            partition grain.
    """
    if through_date < earliest_date:
        raise DataIntegrityError(
            f"backfill range for {series.symbol} ends {through_date.isoformat()}, before it "
            f"starts {earliest_date.isoformat()}. An inverted range plans nothing and "
            f"would report a complete run having fetched no archive"
        )
    grain = DATASET_PARTITION_GRAIN.get(series.dataset)
    if grain is None:
        raise DataIntegrityError(
            f"dataset {series.dataset.value!r} has no declared partition grain, so there "
            f"is no "
            f"file for its records to land in. A grain arrives with a canonical schema, "
            f"never before one -- fking.data.parquet.layout"
        )

    if grain is PartitionGrain.DAILY:
        return tuple(
            _daily_partition(series, archive_date=day)
            for day in _days_between(earliest_date, through_date)
        )
    return tuple(
        _monthly_partition(
            series,
            month_start=month_start,
            earliest_date=earliest_date,
            through_date=through_date,
            today_utc=today_utc,
        )
        for month_start in _month_starts_between(earliest_date, through_date)
    )


def _daily_partition(series: ArchiveSeries, *, archive_date: date) -> PartitionPlan:
    """One day, one archive, always daily.

    A monthly archive would be cheaper and cannot be used: it spans thirty daily
    partitions, and `write_records` refuses a batch holding a record outside the partition
    its coordinate names.
    """
    coordinate = series.at(archive_date)
    return PartitionPlan(
        coordinate=coordinate,
        grain=PartitionGrain.DAILY,
        archives=(PlannedArchive(coordinate=coordinate, granularity=Granularity.DAILY),),
    )


def _monthly_partition(
    series: ArchiveSeries,
    *,
    month_start: date,
    earliest_date: date,
    through_date: date,
    today_utc: date,
) -> PartitionPlan:
    """One month, fed by its monthly archive where that exists and by days where it does not.

    The daily fan-out is clipped to the requested range at both ends, so a run starting
    mid-month asks for the days it wants rather than for a month of 404s before them. The
    monthly archive is deliberately *not* clipped: it is one file covering the whole month,
    and taking the days outside the request is free and truthful.
    """
    partition = series.at(month_start)
    granularity = resolve_granularity(archive_date=month_start, today_utc=today_utc)
    archives: tuple[PlannedArchive, ...]
    if granularity is Granularity.MONTHLY:
        archives = (PlannedArchive(coordinate=partition, granularity=Granularity.MONTHLY),)
    else:
        first_day = max(month_start, earliest_date)
        last_day = min(_month_end(month_start), through_date)
        archives = tuple(
            PlannedArchive(coordinate=series.at(day), granularity=Granularity.DAILY)
            for day in _days_between(first_day, last_day)
        )
    return PartitionPlan(coordinate=partition, grain=PartitionGrain.MONTHLY, archives=archives)


async def discover_earliest_archive_date(
    series: ArchiveSeries,
    *,
    egress: ArchiveEgress,
    floor_date: date,
    today_utc: date,
) -> date | None:
    """The earliest date this symbol's archives are published, or `None` if none are.

    Probed, never assumed. Every symbol's history starts on its own date and the
    consequence is stated in the run summary, because it is the one a researcher forgets:
    **a hypothesis inherits the shortest history among its inputs**, which is usually far
    shorter than the BTC history that made the idea look testable.

    Two searches, because publication has two regimes. Monthly archives cover everything up
    to two months ago and are searched first; if a symbol listed inside that recent window
    there is no monthly archive at all, and the daily archives of the window are searched
    instead. Each search is a binary search over the `.CHECKSUM` siblings -- eighty bytes
    each -- so a hundred months of history costs seven requests rather than a hundred. What
    the search assumes about publication, and why its failure direction is conservative, is
    in `_first_published`.
    """

    async def published(coordinate: ArchiveCoordinate, granularity: Granularity) -> bool:
        return await _archive_is_published(egress, coordinate, granularity)

    # Monthly archives lag, so the newest one that can be relied on is two months back --
    # the boundary `resolve_granularity` draws, read from the other side. Everything from
    # the previous month onward is searched daily.
    recent_window_start = _month_start(_month_start(today_utc) - _ONE_DAY)
    monthly_candidates = [
        series.at(month_start)
        for month_start in _month_starts_between(floor_date, recent_window_start - _ONE_DAY)
    ]
    found = await _first_published(monthly_candidates, Granularity.MONTHLY, published)
    if found is not None:
        _LOG.info(
            "backfill.earliest_discovered",
            symbol=series.symbol,
            granularity=Granularity.MONTHLY.value,
            earliest_date=found.isoformat(),
        )
        return found

    daily_candidates = [
        series.at(day)
        for day in _days_between(max(floor_date, recent_window_start), today_utc - _ONE_DAY)
    ]
    found = await _first_published(daily_candidates, Granularity.DAILY, published)
    if found is not None:
        _LOG.info(
            "backfill.earliest_discovered",
            symbol=series.symbol,
            granularity=Granularity.DAILY.value,
            earliest_date=found.isoformat(),
        )
    return found


async def _first_published(
    candidates: Sequence[ArchiveCoordinate],
    granularity: Granularity,
    published: Callable[[ArchiveCoordinate, Granularity], Awaitable[bool]],
) -> date | None:
    """Binary search for the first published archive.

    **The search assumes publication is contiguous from a symbol's listing onward**, and
    that assumption is stated here rather than checked, because it cannot be checked
    cheaply: a binary search probes a logarithmic number of candidates, so a hole it never
    probes is invisible to it, and probing every month to rule one out is the linear walk
    the search exists to avoid.

    The assumption's failure direction is what makes it acceptable. A hole *after* the
    listing date does not affect this search at all -- the backfill meets it as an absent
    archive and the coverage registry records it as a gap, which is where a hole belongs.
    A published month isolated *before* the true listing run makes the search return a later
    date than it could have, so the run ingests less history than exists and the coverage
    report states exactly where coverage starts. Both are conservative and both are visible;
    neither silently claims data the corpus does not hold.
    """
    if not candidates:
        return None
    low, high = 0, len(candidates)
    while low < high:
        middle = (low + high) // 2
        if await published(candidates[middle], granularity):
            high = middle
        else:
            low = middle + 1
    if low == len(candidates):
        return None
    return candidates[low].archive_date


async def _archive_is_published(
    egress: ArchiveEgress, coordinate: ArchiveCoordinate, granularity: Granularity
) -> bool:
    """Whether the host serves this archive, asked of its `.CHECKSUM` sibling.

    The sibling rather than the archive: it is eighty bytes against up to hundreds of
    megabytes, it is published alongside the archive, and its absence is the same 404. A
    `HEAD` on the archive would be cheaper still and is not used -- `ArchiveEgress` exposes
    no `head`, deliberately, because a surface that can issue an arbitrary method is a
    surface that can issue `POST`.
    """
    url = f"{archive_url(coordinate, granularity)}.CHECKSUM"
    try:
        await egress.get_text(url)
    except ArchiveUnavailableError:
        return False
    return True


def _days_between(first: date, last: date) -> tuple[date, ...]:
    if last < first:
        return ()
    span = (last - first).days + 1
    return tuple(first + timedelta(days=offset) for offset in range(span))


def _month_start(any_day: date) -> date:
    return any_day.replace(day=1)


def _month_end(any_day: date) -> date:
    """The last calendar day of `any_day`'s month.

    Computed by stepping into the next month and back one day rather than from a
    length-of-month table, so February is right in a leap year without anyone maintaining
    the rule.
    """
    if any_day.month == 12:  # noqa: PLR2004 - December, named by the branch it takes
        return any_day.replace(month=12, day=31)
    return any_day.replace(month=any_day.month + 1, day=1) - _ONE_DAY


def _month_starts_between(first: date, last: date) -> tuple[date, ...]:
    if last < first:
        return ()
    months: list[date] = []
    cursor = _month_start(first)
    while cursor <= last:
        months.append(cursor)
        cursor = _month_end(cursor) + _ONE_DAY
    return tuple(months)
