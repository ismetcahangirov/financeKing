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

from fking.data.format_resolver import Dataset, Market

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


def funding_rate_archive() -> RecordedAltArchive:
    """The whole verified `.zip` of BTCUSDT's first month of funding history.

    A whole archive rather than a fragment, because 825 bytes is small enough to commit
    and a fragment cannot prove that ninety-three settlements arrived -- which is the
    assertion that catches a truncated month.
    """
    whole = [
        recorded
        for recorded in recorded_alt_archives()
        if recorded.dataset is Dataset.FUNDING_RATE and recorded.is_whole_archive
    ]
    if len(whole) != 1:  # pragma: no cover - a guard on the fixture corpus, not on code
        raise AssertionError(f"expected exactly one whole fundingRate archive, found {len(whole)}")
    return whole[0]
