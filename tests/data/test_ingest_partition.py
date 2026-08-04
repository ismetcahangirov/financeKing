"""One Parquet file from several archives, and the gap that only exists between them.

The behaviour under test is the one a single-archive ingest cannot have: a bar partition is
a month, the recent tail is published daily, and a month therefore arrives as up to
thirty-one files. Writing each as it arrives leaves the month holding the last one, and
detecting cadence per file reports no gap for a day that is missing entirely -- each
surviving file is individually complete.

Every archive here is a real recording or that recording shifted onto another date by whole
days. Nothing is hand-authored, for the reason `tests/support/archive_fixtures.py` states.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Final

import pytest

from fking.data.archive import ArchiveCoordinate
from fking.data.format_resolver import Dataset, Market, resolve_archive_format
from fking.data.loaders import IngestionSpec
from fking.data.parquet import partition_path
from fking.data.quality import ArchiveMember, ingest_partition
from fking.platform.errors import DataIntegrityError
from tests.support import archive_fixtures
from tests.support.archive_stub import member_of, shift_member, zip_member

pytestmark = pytest.mark.unit

RECORDED: Final = archive_fixtures.find(
    market=Market.SPOT,
    dataset=Dataset.KLINES,
    archive_date=date(2025, 1, 2),
    whole=True,
)
MINUTES_PER_DAY: Final[int] = 1440
JANUARY: Final[date] = date(2025, 1, 1)


def _partition() -> ArchiveCoordinate:
    """The month the recorded day belongs to. Bars are one Parquet file per month."""
    return ArchiveCoordinate(
        market=Market.SPOT,
        dataset=Dataset.KLINES,
        symbol=RECORDED.symbol,
        archive_date=JANUARY,
        interval=RECORDED.interval,
    )


def _member(day_offset: int) -> ArchiveMember:
    """The recording shifted `day_offset` days forward, with a spec for its own date."""
    coordinate = RECORDED.coordinate()
    shifted_date = coordinate.archive_date + timedelta(days=day_offset)
    payload = zip_member(
        shift_member(member_of(RECORDED.read()), coordinate=coordinate, days=day_offset),
        member_name=f"{coordinate.symbol}-1m-{shifted_date.isoformat()}.csv",
    )
    shifted = ArchiveCoordinate(
        market=coordinate.market,
        dataset=coordinate.dataset,
        symbol=coordinate.symbol,
        archive_date=shifted_date,
        interval=coordinate.interval,
    )
    return ArchiveMember(
        archive_bytes=payload,
        spec=IngestionSpec(
            coordinate=shifted,
            archive_format=resolve_archive_format(
                market=shifted.market, dataset=shifted.dataset, archive_date=shifted_date
            ),
            source_checksum_hex=sha256(payload).hexdigest(),
            now_utc=archive_fixtures.NOW_UTC,
        ),
        source=f"stub://{shifted.symbol}-{shifted_date.isoformat()}",
    )


def test_three_daily_archives_produce_one_parquet_file(tmp_path: Path) -> None:
    ingested = ingest_partition(
        [_member(0), _member(1), _member(2)],
        coordinate=_partition(),
        write_root=tmp_path,
        ingested_at_utc=datetime(2026, 8, 4, tzinfo=UTC),
    )

    assert ingested.write.path == partition_path(_partition(), root=tmp_path)
    assert ingested.write.rows_written == 3 * MINUTES_PER_DAY
    assert len(ingested.members) == 3  # noqa: PLR2004 - the three archives supplied
    assert ingested.first_event_time_utc == datetime(2025, 1, 2, tzinfo=UTC)
    assert ingested.last_event_time_utc == datetime(2025, 1, 4, 23, 59, tzinfo=UTC)
    assert ingested.cadence_gaps == ()


def test_a_missing_day_between_two_archives_is_a_gap_with_exact_bounds(tmp_path: Path) -> None:
    """The gap no per-file detector can see: both surviving files are complete."""
    ingested = ingest_partition(
        [_member(0), _member(2)],
        coordinate=_partition(),
        write_root=tmp_path,
        ingested_at_utc=datetime(2026, 8, 4, tzinfo=UTC),
    )

    assert [outcome.cadence_gaps for outcome in ingested.members] == [(), ()]
    assert len(ingested.cadence_gaps) == 1
    gap = ingested.cadence_gaps[0]
    assert gap.after_open_time_utc == datetime(2025, 1, 2, 23, 59, tzinfo=UTC)
    assert gap.before_open_time_utc == datetime(2025, 1, 4, tzinfo=UTC)
    assert gap.missing_bar_count == MINUTES_PER_DAY


def test_the_missing_day_is_not_synthesised_into_the_file(tmp_path: Path) -> None:
    """No interpolation, no forward fill, ever. The file holds two days, not three."""
    ingested = ingest_partition(
        [_member(0), _member(2)],
        coordinate=_partition(),
        write_root=tmp_path,
        ingested_at_utc=datetime(2026, 8, 4, tzinfo=UTC),
    )
    assert ingested.write.rows_written == 2 * MINUTES_PER_DAY


def test_archives_supplied_out_of_order_are_refused_rather_than_sorted(tmp_path: Path) -> None:
    with pytest.raises(DataIntegrityError, match="order"):
        ingest_partition(
            [_member(2), _member(0)],
            coordinate=_partition(),
            write_root=tmp_path,
            ingested_at_utc=datetime(2026, 8, 4, tzinfo=UTC),
        )


def test_an_archive_from_another_series_is_refused(tmp_path: Path) -> None:
    """A foreign row is filed under a series it is not in, and every query agrees."""
    foreign = _member(0)
    other_symbol = ArchiveCoordinate(
        market=Market.SPOT,
        dataset=Dataset.KLINES,
        symbol="ETHUSDT",
        archive_date=foreign.spec.coordinate.archive_date,
        interval="1m",
    )
    with pytest.raises(DataIntegrityError, match="One partition is one series"):
        ingest_partition(
            [foreign],
            coordinate=ArchiveCoordinate(
                market=Market.SPOT,
                dataset=Dataset.KLINES,
                symbol=other_symbol.symbol,
                archive_date=JANUARY,
                interval="1m",
            ),
            write_root=tmp_path,
            ingested_at_utc=datetime(2026, 8, 4, tzinfo=UTC),
        )


def test_a_partition_with_no_archives_is_refused(tmp_path: Path) -> None:
    """A period whose archives are all absent is a gap, not a zero-row file."""
    with pytest.raises(DataIntegrityError, match="no archives"):
        ingest_partition(
            [],
            coordinate=_partition(),
            write_root=tmp_path,
            ingested_at_utc=datetime(2026, 8, 4, tzinfo=UTC),
        )
