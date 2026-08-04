"""Locating the corrupted archives in `tests/fixtures/corrupt/`, and building a spec for one.

Every file there is a declared, deterministic mutation of a real recording, written by
`tools/corrupt_archive_fixture.py`. Nothing is hand-authored, for the same reason nothing
under `tests/fixtures/archives/` is: a hand-typed CSV encodes what its author believes a
truncated archive or a drifted boolean encoding looks like, and the two shapes that
actually break this pipeline -- `True`/`False` booleans and the 2025-01-01 spot microsecond
cutover -- are exactly the two an author would never write down wrongly.

The digest a corrupt fixture's spec carries is the interesting design decision here, and it
is per-fixture rather than uniform:

- The **truncation** fixture takes the *pristine* digest, because that is the situation it
  models: the archive was fetched and verified, and the bytes that reached the parser are
  not those bytes. Gate 1 is what notices.
- **Every other** fixture takes its own digest, because the corruption is upstream. The
  file was served that way, it verified against its own `.CHECKSUM` sibling, and gate 1 has
  nothing to say about it. A corpus that gave them all the pristine digest would trip gate
  1 on every file and prove that gates 2 through 9 were never reached.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from fking.data.archive import ArchiveCoordinate
from fking.data.format_resolver import Dataset, Market, resolve_archive_format
from fking.data.loaders import DEFAULT_MAX_REJECTION_FRACTION, IngestionSpec
from tests.support.archive_fixtures import NOW_UTC

CORRUPT_ROOT: Final[Path] = Path(__file__).resolve().parents[1] / "fixtures" / "corrupt"

# The one fixture whose spec carries the pristine digest. Named here rather than inferred
# from the mutation, so that adding a second truncation-shaped corruption is a deliberate
# edit rather than a string match that silently starts covering it.
VERIFIED_BEFORE_CORRUPTION: Final[frozenset[str]] = frozenset({"spot_klines_truncated_archive"})


@dataclass(frozen=True, slots=True)
class CorruptArchive:
    """One corrupt fixture and the derivation recorded beside it."""

    path: Path
    name: str
    gate: str
    rationale: str
    mutation: str
    source_recording: str
    source_member_sha256: str
    pristine_archive_sha256: str
    corrupt_archive_sha256: str
    member_name: str

    @property
    def market(self) -> Market:
        return Market(self.source_recording.split("/")[0])

    @property
    def dataset(self) -> Dataset:
        return Dataset(self.source_recording.split("/")[1])

    @property
    def symbol(self) -> str:
        return self.member_name.split("-")[0]

    @property
    def interval(self) -> str | None:
        return "1m" if self.dataset is Dataset.KLINES else None

    @property
    def archive_date(self) -> date:
        return date.fromisoformat(self.member_name.removesuffix(".csv").split("-", 2)[2])

    def read(self) -> bytes:
        return self.path.read_bytes()

    def spec(
        self,
        *,
        now_utc: datetime = NOW_UTC,
        max_rejection_fraction: Decimal = DEFAULT_MAX_REJECTION_FRACTION,
    ) -> IngestionSpec:
        """The spec ingestion would hold for this file, digest included.

        See the module docstring for why the digest is not uniform across the corpus.
        """
        digest = (
            self.pristine_archive_sha256
            if self.name in VERIFIED_BEFORE_CORRUPTION
            else self.corrupt_archive_sha256
        )
        return IngestionSpec(
            coordinate=ArchiveCoordinate(
                market=self.market,
                dataset=self.dataset,
                symbol=self.symbol,
                archive_date=self.archive_date,
                interval=self.interval,
            ),
            archive_format=resolve_archive_format(
                market=self.market, dataset=self.dataset, archive_date=self.archive_date
            ),
            source_checksum_hex=digest,
            now_utc=now_utc,
            max_rejection_fraction=max_rejection_fraction,
        )

    def digest_matches_the_file(self) -> bool:
        return hashlib.sha256(self.read()).hexdigest() == self.corrupt_archive_sha256


def corrupt_archives() -> tuple[CorruptArchive, ...]:
    """Every corrupt fixture, sorted so parametrised test ids are stable."""
    return tuple(sorted(_iter_corrupt(), key=lambda corrupt: corrupt.name))


def _iter_corrupt() -> Iterator[CorruptArchive]:
    for sidecar_path in CORRUPT_ROOT.rglob("*.corruption.json"):
        raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
        yield CorruptArchive(
            path=sidecar_path.with_name(sidecar_path.name.removesuffix(".corruption.json")),
            name=str(raw["name"]),
            gate=str(raw["gate"]),
            rationale=str(raw["rationale"]),
            mutation=str(raw["mutation"]),
            source_recording=str(raw["source_recording"]),
            source_member_sha256=str(raw["source_member_sha256"]),
            pristine_archive_sha256=str(raw["pristine_archive_sha256"]),
            corrupt_archive_sha256=str(raw["corrupt_archive_sha256"]),
            member_name=str(raw["member_name"]),
        )


def find(name: str) -> CorruptArchive:
    """The one corrupt fixture with this name, or an assertion failure naming what exists."""
    for corrupt in corrupt_archives():
        if corrupt.name == name:
            return corrupt
    raise AssertionError(
        f"no corrupt fixture named {name!r}; the corpus holds "
        f"{[corrupt.name for corrupt in corrupt_archives()]}. Add a mutation to "
        f"tools/corrupt_archive_fixture.py and regenerate rather than authoring a file"
    )
