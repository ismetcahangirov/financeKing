"""A stub archive host, and day-shifted derivations of the recorded fixtures.

Nothing here is hand-authored data. The bytes served always originate in a recorded,
checksum-verified archive under `tests/fixtures/archives/`; `shift_member` moves every epoch
in one by a whole number of days, which is a mutation applied in the test to bytes read from
a recording -- the shape `tests/support/archive_fixtures.py` sanctions, and for the reason it
gives: a hand-written CSV encodes what its author believes Binance emits, and the two things
that actually break a loader are the two things an author would never write down wrongly.

Shifting by whole days is what makes a multi-day range testable at all. One recorded day
cannot demonstrate a gap between two days, a partition fed by several archives, or a seam
between two months, and those are the three behaviours a backfill exists to get right. Only
the epoch columns move: prices, volumes and the boolean columns stay the recording's own, so
a shifted day exercises the same decimal parse and the same boolean encoding as the day that
was really downloaded.

`StubArchiveEgress` answers exactly like the real one: 200 with bytes, or
`ArchiveUnavailableError`. A URL it was not given is a 404 rather than an error, because
"this archive is not published" is the ordinary answer a backfill must handle -- and it is
the whole point of a fabricated missing day.
"""

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Iterator, Mapping
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from typing import Final

from fking.data.archive import (
    ArchiveCoordinate,
    Granularity,
    archive_filename,
    archive_url,
)
from fking.data.format_resolver import Dataset, EpochUnit, resolve_archive_format
from fking.platform.safety.archive import ArchiveUnavailableError

_SECONDS_PER_DAY: Final[int] = 86_400

_EPOCH_MULTIPLIER: Final[Mapping[EpochUnit, int]] = {
    EpochUnit.MILLISECONDS: 1_000,
    EpochUnit.MICROSECONDS: 1_000_000,
}

# Which columns hold an epoch, by index into the dataset's declared column tuple. A kline
# row opens with `open_time` and carries `close_time` seventh; a trade row opens with its
# `trade_id` and carries the epoch fifth. Reading column 0 on a trades file would shift a
# nine-digit trade id instead of a timestamp, and the fixture would be quietly wrong in a
# way every assertion about it inherits.
_EPOCH_COLUMNS: Final[Mapping[Dataset, tuple[int, ...]]] = {
    Dataset.KLINES: (0, 6),
    Dataset.TRADES: (4,),
}


class InterruptedRunError(RuntimeError):
    """Raised by the stub after a declared number of downloads, to simulate a kill.

    A `RuntimeError` rather than a member of the error taxonomy on purpose: it stands in
    for SIGKILL, which is not an exception the code under test is allowed to have a handler
    for. Nothing in `fking` catches it, which is what makes the resumed run prove that
    recovery comes from the registry and the corpus rather than from cleanup code.
    """


def member_of(archive_bytes: bytes) -> bytes:
    """The single CSV inside a recorded `.zip`."""
    with zipfile.ZipFile(BytesIO(archive_bytes)) as bundle:
        return bundle.read(bundle.namelist()[0])


def shift_member(member: bytes, *, coordinate: ArchiveCoordinate, days: int) -> bytes:
    """The CSV with every epoch column moved forward by `days`."""
    archive_format = resolve_archive_format(
        market=coordinate.market,
        dataset=coordinate.dataset,
        archive_date=coordinate.archive_date,
    )
    offset = _SECONDS_PER_DAY * days * _EPOCH_MULTIPLIER[archive_format.epoch_unit]
    columns = _EPOCH_COLUMNS[coordinate.dataset]
    shifted = _shift_rows(
        member.decode("utf-8"),
        offset=offset,
        columns=columns,
        has_header=archive_format.has_header_row,
    )
    return "\n".join(shifted).encode("utf-8")


def _shift_rows(
    member: str, *, offset: int, columns: tuple[int, ...], has_header: bool
) -> Iterator[str]:
    for index, line in enumerate(member.splitlines()):
        if not line.strip() or (index == 0 and has_header):
            yield line
            continue
        fields = line.split(",")
        for column in columns:
            fields[column] = str(int(fields[column]) + offset)
        yield ",".join(fields)


def zip_member(member: bytes, *, member_name: str) -> bytes:
    """One CSV packed the way Binance packs one: a zip holding exactly one member."""
    staged = BytesIO()
    with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(member_name, member)
    return staged.getvalue()


def daily_archives(
    *, member: bytes, coordinate: ArchiveCoordinate, days: tuple[date, ...]
) -> dict[str, bytes]:
    """A URL-to-bytes map holding one daily archive per requested day.

    Each day is the recording shifted onto that date, so the whole set is contiguous and any
    day left out of `days` is a genuine hole in the series rather than a hole in the fixture.
    """
    archives: dict[str, bytes] = {}
    for day in days:
        shifted_coordinate = ArchiveCoordinate(
            market=coordinate.market,
            dataset=coordinate.dataset,
            symbol=coordinate.symbol,
            archive_date=day,
            interval=coordinate.interval,
        )
        payload = shift_member(
            member, coordinate=coordinate, days=(day - coordinate.archive_date).days
        )
        name = archive_filename(shifted_coordinate, Granularity.DAILY).removesuffix(".zip")
        archives[archive_url(shifted_coordinate, Granularity.DAILY)] = zip_member(
            payload, member_name=f"{name}.csv"
        )
    return archives


class StubArchiveEgress:
    """An `ArchiveEgress` serving a fixed URL-to-bytes map, with a `.CHECKSUM` for each.

    The checksum sibling is derived from the bytes rather than declared, so a test cannot
    accidentally assert that verification works by handing the fetcher a digest of
    something else.
    """

    def __init__(self, archives: Mapping[str, bytes]) -> None:
        self._archives = dict(archives)
        self._request_count = 0
        self._downloads = 0
        self._interrupt_after: int | None = None

    @property
    def request_count(self) -> int:
        return self._request_count

    @property
    def download_count(self) -> int:
        """Downloads issued, as opposed to `.CHECKSUM` probes. What the cache elides."""
        return self._downloads

    def interrupt_after(self, downloads: int) -> None:
        """Raise `InterruptedRunError` once `downloads` archives have been served."""
        self._interrupt_after = downloads

    async def get_text(self, url: str) -> str:
        self._request_count += 1
        if not url.endswith(".CHECKSUM"):
            raise ArchiveUnavailableError(f"GET {url} returned HTTP 404; expected 200")
        archive = url.removesuffix(".CHECKSUM")
        payload = self._archives.get(archive)
        if payload is None:
            raise ArchiveUnavailableError(f"GET {url} returned HTTP 404; expected 200")
        return f"{hashlib.sha256(payload).hexdigest()}  {archive.rsplit('/', 1)[-1]}\n"

    async def download(self, url: str, destination: Path) -> str:
        self._request_count += 1
        payload = self._archives.get(url)
        if payload is None:
            raise ArchiveUnavailableError(f"GET {url} returned HTTP 404; expected 200")
        if self._interrupt_after is not None and self._downloads >= self._interrupt_after:
            raise InterruptedRunError(f"simulated kill before downloading {url}")
        self._downloads += 1
        destination.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()


def days_in(first: date, last: date) -> tuple[date, ...]:
    span = (last - first).days + 1
    return tuple(first + timedelta(days=offset) for offset in range(span))
