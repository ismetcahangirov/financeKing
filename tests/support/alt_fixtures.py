"""Locating the recorded alternative-series fixtures.

Deliberately separate from `tests/support/archive_fixtures.py`, which resolves a declared
corpus format for every file it finds. `fundingRate` and `metrics` have no declared corpus
format -- they never become bars or prints -- so a recording of either placed under
`tests/fixtures/archives/` would make the whole corpus suite raise on collection. Hence a
second root, `tests/fixtures/alt/`, written by the same recorder from the same
checksum-verified path.

Nothing here is hand-authored, and the recording is worth reading before writing an
assertion against it. `BTCUSDT-fundingRate-2020-01` contains three things nobody would
have thought to invent: settlements one or two milliseconds off the eight-hour boundary,
a rate serialised as `8.4E-7` in scientific notation, and negative rates in the very first
rows. All three are exactly the shapes a hand-written fixture would have smoothed away.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

from fking.data.format_resolver import ArchiveFormat, Dataset, Market, resolve_archive_format

FIXTURE_ROOT: Final[Path] = Path(__file__).resolve().parents[1] / "fixtures" / "alt"

# 2026-08-05T00:00:00Z. Fixed rather than read from the clock: the timestamp plausibility
# window is a function of `now`, so a test reading the real clock would move its own
# boundary conditions every day it ran.
NOW_UTC: Final[datetime] = datetime(2026, 8, 5, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class RecordedAltArchive:
    """One recorded alternative-series fixture and the provenance recorded beside it."""

    path: Path
    market: Market
    dataset: Dataset
    symbol: str
    archive_date: date
    source_url: str
    archive_sha256: str
    member_sha256: str
    member_line_count: int
    fragment_sha256: str | None
    fragment_line_count: int | None

    @property
    def is_whole_archive(self) -> bool:
        return self.path.suffix == ".zip"

    @property
    def label(self) -> str:
        return f"{self.market.value}/{self.dataset.value}/{self.path.name}"

    def read(self) -> bytes:
        return self.path.read_bytes()

    def archive_format(self) -> ArchiveFormat:
        """The declaration `ingest_alt_period` would resolve for this recording.

        Resolved from the recording's own `(market, dataset, archive_date)` rather than
        constructed, so a test exercises the same declaration production does. A test that
        built its own `ArchiveFormat` would keep passing after the table was changed under
        it -- which is the one thing these tests exist to notice.
        """
        return resolve_archive_format(
            market=self.market, dataset=self.dataset, archive_date=self.archive_date
        )


def _load(provenance_path: Path) -> RecordedAltArchive:
    recorded = json.loads(provenance_path.read_text(encoding="utf-8"))
    return RecordedAltArchive(
        # The recorder writes `<fixture name>.provenance.json` beside the fixture, so the
        # fixture's name is this one with that suffix removed. Derived rather than stored,
        # so a renamed pair fails to load instead of loading the wrong file.
        path=provenance_path.parent / provenance_path.name.removesuffix(".provenance.json"),
        market=Market(recorded["market"]),
        dataset=Dataset(recorded["dataset"]),
        symbol=recorded["symbol"],
        archive_date=date.fromisoformat(recorded["archive_date"]),
        source_url=recorded["source_url"],
        archive_sha256=recorded["archive_sha256"],
        member_sha256=recorded["member_sha256"],
        member_line_count=recorded["member_line_count"],
        fragment_sha256=recorded["fragment_sha256"],
        fragment_line_count=recorded["fragment_line_count"],
    )


def recorded_alt_archives() -> tuple[RecordedAltArchive, ...]:
    """Every recorded alternative-series fixture, in a stable order."""
    return tuple(
        _load(provenance) for provenance in sorted(FIXTURE_ROOT.rglob("*.provenance.json"))
    )


def _only_whole_archive(dataset: Dataset) -> RecordedAltArchive:
    whole = [
        recorded
        for recorded in recorded_alt_archives()
        if recorded.dataset is dataset and recorded.is_whole_archive
    ]
    if len(whole) != 1:  # pragma: no cover - a guard on the fixture corpus, not on code
        raise AssertionError(
            f"expected exactly one whole {dataset.value} archive, found {len(whole)}"
        )
    return whole[0]


def funding_rate_archive() -> RecordedAltArchive:
    """The whole verified `.zip` of BTCUSDT's first month of funding history.

    A whole archive rather than a fragment, because 825 bytes is small enough to commit
    and a fragment cannot prove that ninety-three settlements arrived -- which is the
    assertion that catches a truncated month.
    """
    return _only_whole_archive(Dataset.FUNDING_RATE)


def metrics_archive() -> RecordedAltArchive:
    """The whole verified `.zip` of one day of BTCUSDT open interest.

    Whole for the same reason as funding and one stronger: the parser's five-minute
    spacing check only means something across a complete day, and 288 samples ending at
    23:55 is what proves the day is complete. 11.6 KB, which is inside the "a few tens of
    KB" bound `tools/record_archive_fragment.py --keep-archive` states.
    """
    return _only_whole_archive(Dataset.METRICS)
