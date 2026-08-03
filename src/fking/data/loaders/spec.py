"""Everything a parser needs to read one archive, and nothing it could infer wrongly.

`DATA_PIPELINE.md` section 4 sketches a wider `IngestionSpec` carrying a date range and a
write destination. Those belong to the *backfill*, not to a parser: a pure function of one
file's bytes has no use for the range it was drawn from, and a destination it cannot write
to is a field that will eventually be read by something that can. What is here is what a
parse of one file genuinely depends on.

The load-bearing part is `__post_init__`. It refuses a spec whose declared format does not
cover the file's own date -- which is trap 1 in its constructible form. Passing last
year's format to this year's file produces timestamps three orders of magnitude wrong and
no exception, so the mismatch is refused at construction rather than diagnosed later from
a 1970 timestamp.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from fking.data.archive import ArchiveCoordinate
from fking.data.format_resolver import ArchiveFormat
from fking.platform.errors import DataIntegrityError

__all__ = ["DEFAULT_MAX_REJECTION_FRACTION", "IngestionSpec"]

# 0.1%, from DATA_PIPELINE.md section 3, trap 3: "a rejection rate above 0.1% fails the
# run". Deliberately tight. On a 1,440-row day it tolerates exactly one bad row, because
# the failure it guards against -- a boolean encoding or an epoch unit that drifted
# upstream -- is uniform across a file rather than sporadic, so the honest threshold is
# "one anomaly is noise, two is a pattern" rather than a percentage tuned to feel safe.
DEFAULT_MAX_REJECTION_FRACTION: Final[Decimal] = Decimal("0.001")

_SHA256_HEX: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{64}\Z")

_ZERO: Final[Decimal] = Decimal("0")
_ONE: Final[Decimal] = Decimal("1")
_UTC_OFFSET: Final[timedelta] = timedelta(0)


@dataclass(frozen=True, slots=True)
class IngestionSpec:
    """One archive's parse, fully declared.

    `source_checksum_hex` is required and must be a real digest. There is no
    `checksum_verified: bool` flag, because a boolean is satisfiable by writing `True`:
    the only way to hold the digest of a verified archive is to have verified one, so the
    digest is the evidence and the flag would have been the claim.

    `now_utc` is the plausibility reference for every timestamp in the file, fixed once
    for the whole parse. A bound re-read per row drifts mid-file, and then the same raw
    integer can be accepted at the top of an archive and rejected at the bottom.
    """

    coordinate: ArchiveCoordinate
    archive_format: ArchiveFormat
    source_checksum_hex: str
    now_utc: datetime
    max_rejection_fraction: Decimal = DEFAULT_MAX_REJECTION_FRACTION

    def __post_init__(self) -> None:
        declared = (self.archive_format.market, self.archive_format.dataset)
        addressed = (self.coordinate.market, self.coordinate.dataset)
        if declared != addressed:
            raise DataIntegrityError(
                f"the declared format is for {declared[0].value}/{declared[1].value} but the "
                f"file is {addressed[0].value}/{addressed[1].value}; formats do not transfer "
                f"between corpora -- spot and futures had different epoch units for part of "
                f"history, so this is trap 1 with the two halves swapped"
            )
        if not self.archive_format.covers(self.coordinate.archive_date):
            until = self.archive_format.declared_until_date
            raise DataIntegrityError(
                f"the declared format segment covers "
                f"[{self.archive_format.declared_from_date.isoformat()}, "
                f"{'open' if until is None else until.isoformat()}) and the file is dated "
                f"{self.coordinate.archive_date.isoformat()}; resolve the format for the "
                f"file's own date with resolve_archive_format()"
            )
        if not _SHA256_HEX.match(self.source_checksum_hex):
            raise DataIntegrityError(
                f"source_checksum_hex must be a lowercase 64-character SHA-256 digest of the "
                f"verified archive; got {self.source_checksum_hex!r}. No archive is parsed "
                f"before it is verified (DATA_PIPELINE.md section 2)"
            )
        if self.now_utc.tzinfo is None or self.now_utc.utcoffset() != _UTC_OFFSET:
            raise DataIntegrityError(
                f"now_utc must be timezone-aware UTC; got {self.now_utc!r}. A naive or "
                f"offset reference instant moves the plausibility window by its offset"
            )
        _require_fraction(self.max_rejection_fraction, "max_rejection_fraction")


def _require_fraction(candidate: object, field_name: str) -> None:
    """A finite `Decimal` in [0, 1], never a float and never a percent.

    Takes `object` rather than `Decimal` for the same reason `fking.domain._guards` does:
    annotating the parameter as `Decimal` makes the `isinstance` check unreachable to
    `mypy --warn-unreachable`, and the entire point is that the value arriving here has not
    been checked by anything. The annotation is the claim; this is the verification.
    """
    if isinstance(candidate, float):
        raise DataIntegrityError(
            f"{field_name} must be a Decimal constructed from str, not a float; got "
            f"{candidate!r}. A row-count ratio is not money, but a float here would put a "
            f"float on the same expression as the rejection ceiling and the next field to "
            f"be added beside it will be"
        )
    if not isinstance(candidate, Decimal) or not candidate.is_finite():
        raise DataIntegrityError(
            f"{field_name} must be a finite Decimal; got {type(candidate).__name__} {candidate!r}"
        )
    if not _ZERO <= candidate <= _ONE:
        raise DataIntegrityError(
            f"{field_name} is a fraction in [0, 1], not a percent; got {candidate}"
        )
