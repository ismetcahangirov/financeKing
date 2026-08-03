"""Locating the recorded archive fixtures, and building a spec for one.

Every file under `tests/fixtures/archives/` is a genuine, checksum-verified recording from
`data.binance.vision`, written by `tools/record_archive_fragment.py`. Nothing there is
hand-authored, which is the whole point: a hand-written CSV encodes what its author
believes Binance emits, and the two things that actually break a loader -- `True`/`False`
booleans and the 2025-01-01 spot microsecond cutover -- are the two things an author would
never think to write down wrongly (`.claude/rules/testing-rules.md`).

Mutations for the error paths are applied **in the test**, to bytes read from a recording,
rather than committed as separate files. A mutation performed in the test is visible in the
diff that asserts on it; a committed mutated file is a fixture that looks recorded, and six
months later nobody remembers which of the two it is.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from fking.data.archive import ArchiveCoordinate
from fking.data.format_resolver import Dataset, Market, resolve_archive_format
from fking.data.loaders import DEFAULT_MAX_REJECTION_FRACTION, IngestionSpec

FIXTURE_ROOT: Final[Path] = Path(__file__).resolve().parents[1] / "fixtures" / "archives"

# 2026-08-04T00:00:00Z. Fixed rather than read from the clock: the timestamp plausibility
# window is a function of `now`, so a test reading the real clock would move its own
# boundary conditions every day it ran.
NOW_UTC: Final[datetime] = datetime(2026, 8, 4, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class RecordedArchive:
    """One recorded fixture and the provenance recorded beside it."""

    path: Path
    market: Market
    dataset: Dataset
    symbol: str
    interval: str | None
    archive_date: date
    source_url: str
    archive_sha256: str
    member_sha256: str
    member_line_count: int
    fragment_sha256: str | None
    fragment_line_count: int | None

    @property
    def is_whole_archive(self) -> bool:
        """Whether this fixture is a complete `.zip` rather than a CSV prefix of one."""
        return self.path.suffix == ".zip"

    @property
    def has_header_row(self) -> bool:
        """Whether the archive's declared format says the file opens with column names."""
        return resolve_archive_format(
            market=self.market, dataset=self.dataset, archive_date=self.archive_date
        ).has_header_row

    @property
    def data_row_count(self) -> int:
        """Lines that are data rather than a header. Zero for a whole `.zip` fixture."""
        if self.fragment_line_count is None:
            return 0
        return self.fragment_line_count - (1 if self.has_header_row else 0)

    @property
    def label(self) -> str:
        """A short id for pytest parametrisation output."""
        return f"{self.market.value}/{self.dataset.value}/{self.path.name}"

    def read(self) -> bytes:
        return self.path.read_bytes()

    def coordinate(self) -> ArchiveCoordinate:
        return ArchiveCoordinate(
            market=self.market,
            dataset=self.dataset,
            symbol=self.symbol,
            archive_date=self.archive_date,
            interval=self.interval,
        )

    def spec(
        self,
        *,
        now_utc: datetime = NOW_UTC,
        max_rejection_fraction: Decimal = DEFAULT_MAX_REJECTION_FRACTION,
        market: Market | None = None,
        dataset: Dataset | None = None,
        archive_date: date | None = None,
    ) -> IngestionSpec:
        """The spec the archive's own provenance implies, or a deliberately wrong one.

        The overrides exist so a test can declare the *other* market's format for this
        file, which is trap 1 stated as a call: same bytes, wrong epoch unit, wrong header
        expectation, no exception anywhere unless the parser refuses.
        """
        resolved_market = market if market is not None else self.market
        resolved_dataset = dataset if dataset is not None else self.dataset
        resolved_date = archive_date if archive_date is not None else self.archive_date
        return IngestionSpec(
            coordinate=ArchiveCoordinate(
                market=resolved_market,
                dataset=resolved_dataset,
                symbol=self.symbol,
                archive_date=resolved_date,
                interval=self.interval if resolved_dataset is Dataset.KLINES else None,
            ),
            archive_format=resolve_archive_format(
                market=resolved_market, dataset=resolved_dataset, archive_date=resolved_date
            ),
            source_checksum_hex=self.archive_sha256,
            now_utc=now_utc,
            max_rejection_fraction=max_rejection_fraction,
        )


def _load(provenance_path: Path) -> RecordedArchive:
    raw = json.loads(provenance_path.read_text(encoding="utf-8"))
    interval = raw["interval"]
    fragment_sha256 = raw["fragment_sha256"]
    fragment_lines = raw["fragment_line_count"]
    return RecordedArchive(
        path=provenance_path.with_name(provenance_path.name.removesuffix(".provenance.json")),
        market=Market(str(raw["market"])),
        dataset=Dataset(str(raw["dataset"])),
        symbol=str(raw["symbol"]),
        interval=None if interval is None else str(interval),
        archive_date=date.fromisoformat(str(raw["archive_date"])),
        source_url=str(raw["source_url"]),
        archive_sha256=str(raw["archive_sha256"]),
        member_sha256=str(raw["member_sha256"]),
        member_line_count=int(raw["member_line_count"]),
        fragment_sha256=None if fragment_sha256 is None else str(fragment_sha256),
        fragment_line_count=None if fragment_lines is None else int(fragment_lines),
    )


def recorded_archives() -> tuple[RecordedArchive, ...]:
    """Every recorded fixture, sorted so parametrised test ids are stable."""
    return tuple(sorted(_iter_recorded(), key=lambda recorded: recorded.label))


def _iter_recorded() -> Iterator[RecordedArchive]:
    for provenance_path in FIXTURE_ROOT.rglob("*.provenance.json"):
        yield _load(provenance_path)


def csv_fragments() -> tuple[RecordedArchive, ...]:
    """The CSV-prefix fixtures: parseable directly, with no unzip step."""
    return tuple(recorded for recorded in recorded_archives() if not recorded.is_whole_archive)


def whole_archives() -> tuple[RecordedArchive, ...]:
    """The complete `.zip` fixtures, which are the only ones that can prove a full day."""
    return tuple(recorded for recorded in recorded_archives() if recorded.is_whole_archive)


def find(*, market: Market, dataset: Dataset, archive_date: date, whole: bool) -> RecordedArchive:
    """The one fixture matching these coordinates, or an assertion failure."""
    matches = [
        recorded
        for recorded in recorded_archives()
        if recorded.market is market
        and recorded.dataset is dataset
        and recorded.archive_date == archive_date
        and recorded.is_whole_archive is whole
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one recorded fixture for {market.value}/{dataset.value} on "
            f"{archive_date.isoformat()} (whole={whole}), found {[m.label for m in matches]}"
        )
    return matches[0]
