"""The plan: which archives exist, which Parquet file each fills, and where history starts.

Pure, so these are unit tests with no network and no filesystem. What they are actually
asserting is the one mapping a bulk backfill gets wrong: an archive is not a Parquet file.
Bars are one file per month and are published daily for the recent tail, so a month arrives
as up to thirty-one archives that all belong in one partition -- and a plan that treated
each archive as its own write would leave every recent month holding one day.

The publication search is tested against a stub that answers a predicate rather than a byte
map, because what is being asserted is the number and shape of the probes, not the content
they return.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Final

import pytest

from fking.data.archive import Granularity
from fking.data.backfill import ArchiveSeries, discover_earliest_archive_date, plan_partitions
from fking.data.format_resolver import Dataset, Market
from fking.data.parquet.layout import PartitionGrain
from fking.platform.errors import DataIntegrityError
from fking.platform.safety.archive import ArchiveUnavailableError

pytestmark = pytest.mark.unit

# 2026-08-04. The same reference instant the recorded fixtures use, so a test reading one
# and a test reading the other cannot disagree about which archives are published monthly.
TODAY = date(2026, 8, 4)

SPOT_KLINES = ArchiveSeries(
    market=Market.SPOT, dataset=Dataset.KLINES, symbol="BTCUSDT", interval="1m"
)
SPOT_TRADES = ArchiveSeries(
    market=Market.SPOT, dataset=Dataset.TRADES, symbol="BTCUSDT", interval=None
)

DAYS_IN_JULY: Final[int] = 31
DAYS_IN_LEAP_FEBRUARY: Final[int] = 29
PLANNED_TRADE_DAYS: Final[int] = 3
MAX_BOUNDARY_PROBES: Final[int] = 10


def _probed_date(url: str) -> date:
    """The date a `.CHECKSUM` URL addresses, monthly or daily.

    Read from the filename's trailing numeric tokens rather than from the path, because the
    filename is where the granularity is visible: `BTCUSDT-1m-2021-10.zip` is a month and
    `BTCUSDT-1m-2021-10-05.zip` is a day, and a month is normalised to its first.
    """
    name = url.rsplit("/", 1)[-1].removesuffix(".zip.CHECKSUM")
    numbers = [int(part) for part in name.split("-")[-3:] if part.isdigit()]
    year, month = numbers[0], numbers[1]
    day = numbers[2] if len(numbers) > 2 else 1  # noqa: PLR2004 - year, month, optional day
    return date(year, month, day)


class PredicateEgress:
    """An egress that answers `.CHECKSUM` probes from a predicate over the URL's date."""

    def __init__(self, published: Callable[[date], bool]) -> None:
        self._published = published
        self._request_count = 0

    @property
    def request_count(self) -> int:
        return self._request_count

    async def get_text(self, url: str) -> str:
        self._request_count += 1
        if not self._published(_probed_date(url)):
            raise ArchiveUnavailableError(f"GET {url} returned HTTP 404; expected 200")
        return f"{'0' * 64}  archive.zip\n"

    async def download(self, url: str, destination: Path) -> str:
        """Never called: the publication search probes siblings and downloads nothing."""
        raise AssertionError(f"the search downloaded {url} into {destination}")


class TestKlinePartitions:
    def test_a_settled_month_is_one_monthly_archive(self) -> None:
        plans = plan_partitions(
            SPOT_KLINES,
            earliest_date=date(2025, 3, 1),
            through_date=date(2025, 3, 31),
            today_utc=TODAY,
        )
        assert len(plans) == 1
        assert plans[0].grain is PartitionGrain.MONTHLY
        assert [planned.granularity for planned in plans[0].archives] == [Granularity.MONTHLY]

    def test_the_recent_tail_is_daily_archives_into_one_monthly_partition(self) -> None:
        """The mapping this module exists for: thirty-one archives, one Parquet file."""
        plans = plan_partitions(
            SPOT_KLINES,
            earliest_date=date(2026, 7, 1),
            through_date=date(2026, 7, 31),
            today_utc=TODAY,
        )
        assert len(plans) == 1
        assert plans[0].period_start_date == date(2026, 7, 1)
        assert len(plans[0].archives) == DAYS_IN_JULY
        assert {planned.granularity for planned in plans[0].archives} == {Granularity.DAILY}

    def test_daily_fan_out_is_clipped_to_the_requested_range(self) -> None:
        plans = plan_partitions(
            SPOT_KLINES,
            earliest_date=date(2026, 7, 20),
            through_date=date(2026, 7, 22),
            today_utc=TODAY,
        )
        assert [planned.coordinate.archive_date for planned in plans[0].archives] == [
            date(2026, 7, 20),
            date(2026, 7, 21),
            date(2026, 7, 22),
        ]

    def test_a_monthly_archive_is_not_clipped_and_says_what_it_covers(self) -> None:
        """One file covering the whole month; taking the days outside the request is free."""
        plans = plan_partitions(
            SPOT_KLINES,
            earliest_date=date(2025, 3, 10),
            through_date=date(2025, 3, 12),
            today_utc=TODAY,
        )
        assert plans[0].covered_from_date == date(2025, 3, 1)
        assert plans[0].covered_through_date == date(2025, 3, 31)

    def test_partitions_are_ordered_oldest_first(self) -> None:
        """Seam detection compares against the previous period; reversed order finds none."""
        plans = plan_partitions(
            SPOT_KLINES,
            earliest_date=date(2025, 11, 4),
            through_date=date(2026, 2, 3),
            today_utc=TODAY,
        )
        starts = [plan.period_start_date for plan in plans]
        assert starts == sorted(starts)
        assert starts[0] == date(2025, 11, 1)
        assert starts[-1] == date(2026, 2, 1)

    def test_february_of_a_leap_year_fans_out_to_twenty_nine_days(self) -> None:
        plans = plan_partitions(
            SPOT_KLINES,
            earliest_date=date(2024, 2, 1),
            through_date=date(2024, 2, 29),
            today_utc=date(2024, 3, 5),
        )
        assert len(plans[0].archives) == DAYS_IN_LEAP_FEBRUARY


class TestTradePartitions:
    def test_trades_are_one_daily_archive_per_daily_partition(self) -> None:
        """A monthly trades archive spans thirty daily partitions, so it is never planned."""
        plans = plan_partitions(
            SPOT_TRADES,
            earliest_date=date(2025, 3, 1),
            through_date=date(2025, 3, 3),
            today_utc=TODAY,
        )
        assert len(plans) == PLANNED_TRADE_DAYS
        assert all(plan.grain is PartitionGrain.DAILY for plan in plans)
        assert all(len(plan.archives) == 1 for plan in plans)
        assert {planned.granularity for plan in plans for planned in plan.archives} == {
            Granularity.DAILY
        }


class TestRefusals:
    def test_an_inverted_range_is_refused_rather_than_planning_nothing(self) -> None:
        with pytest.raises(DataIntegrityError, match="before it starts"):
            plan_partitions(
                SPOT_KLINES,
                earliest_date=date(2025, 3, 5),
                through_date=date(2025, 3, 1),
                today_utc=TODAY,
            )

    def test_a_dataset_with_no_declared_grain_is_refused(self) -> None:
        undeclared = ArchiveSeries(
            market=Market.SPOT, dataset=Dataset.AGG_TRADES, symbol="BTCUSDT", interval=None
        )
        with pytest.raises(DataIntegrityError, match="no declared partition grain"):
            plan_partitions(
                undeclared,
                earliest_date=date(2025, 3, 1),
                through_date=date(2025, 3, 2),
                today_utc=TODAY,
            )


class TestEarliestDateDiscovery:
    @pytest.mark.asyncio
    async def test_the_first_published_month_is_found_by_binary_search(self) -> None:
        egress = PredicateEgress(lambda probed: probed >= date(2019, 9, 1))
        found = await discover_earliest_archive_date(
            SPOT_KLINES,
            egress=egress,
            floor_date=date(2017, 1, 1),
            today_utc=TODAY,
        )
        assert found == date(2019, 9, 1)
        # 112 candidate months. A linear walk would probe at least 33; log2(112) + 2 is 9.
        assert egress.request_count <= MAX_BOUNDARY_PROBES

    @pytest.mark.asyncio
    async def test_a_symbol_listed_inside_the_recent_window_falls_back_to_daily(self) -> None:
        """No monthly archive exists yet, so the boundary is found among the daily files."""
        egress = PredicateEgress(lambda probed: probed >= date(2026, 7, 18))
        found = await discover_earliest_archive_date(
            SPOT_KLINES,
            egress=egress,
            floor_date=date(2017, 1, 1),
            today_utc=TODAY,
        )
        assert found == date(2026, 7, 18)

    @pytest.mark.asyncio
    async def test_an_unpublished_symbol_returns_none_rather_than_a_guess(self) -> None:
        egress = PredicateEgress(lambda _probed: False)
        found = await discover_earliest_archive_date(
            SPOT_KLINES,
            egress=egress,
            floor_date=date(2017, 1, 1),
            today_utc=TODAY,
        )
        assert found is None

    @pytest.mark.asyncio
    async def test_a_hole_after_the_listing_date_does_not_move_the_answer(self) -> None:
        """A hole is the backfill's problem, not the search's: it becomes a recorded gap.

        The search's contiguity assumption is documented in `_first_published` rather than
        checked, and this is what "the failure direction is conservative" means in practice:
        a missing month inside the published run changes nothing about where history starts.
        """
        listed_from = date(2019, 9, 1)
        hole = date(2022, 4, 1)
        egress = PredicateEgress(lambda probed: probed >= listed_from and probed != hole)
        found = await discover_earliest_archive_date(
            SPOT_KLINES,
            egress=egress,
            floor_date=date(2017, 1, 1),
            today_utc=TODAY,
        )
        assert found == listed_from
