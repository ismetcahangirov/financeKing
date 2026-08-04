"""Checksum-verified archive fetching from `data.binance.vision`.

The rule this module makes structural is one sentence from `DATA_PIPELINE.md` section 2:

> **No archive is read before its checksum is verified.** Not "verified after a
> successful parse" -- verified before the first byte is handed to a parser.

The failure it prevents is not a corrupt file that fails to open. A file that will not
open raises, and you retry. The failure that matters is a **truncated** archive that
parses cleanly for 80% of its rows and then ends: a plausible row count, a plausible
price range, and eleven missing hours nobody notices until a backtest six weeks later
shows an implausibly clean edge in exactly that window. Every archive has a SHA-256
sibling (VF-014), so there is no excuse for the guess.

Three decisions here are worth stating because each has an obvious wrong answer:

**Monthly is preferred, daily is the fallback, and the boundary is the calendar.**
One monthly request replaces thirty daily ones and one checksum replaces thirty. But
monthly archives lag publication, so the current and previous month are only available
daily. Getting this backwards produces a pipeline thirty times slower and thirty times
likelier to fail partway through a range.

**A checksum that fails twice is a data-integrity event, not a third retry.** An
upstream archive whose bytes changed invalidates every backtest that consumed the
previous copy, so the response is an escalation naming the file, not a quieter log line
(`DATA_PIPELINE.md` section 11).

**The `.CHECKSUM` sibling carries a digest *and* a filename, and both are compared.** A
digest matching a differently-named file means the wrong archive is being checksummed --
a real outcome of an upstream symbol rename, and one that a digest-only comparison
cannot see because the digest is correct for the file it names.

Nothing here imports `httpx`. The network surface is the `ArchiveEgress` protocol from
`fking.platform.safety.archive`, which is a separate egress path from the trading
client: it validates against `ARCHIVE_HOSTS`, holds no credential, and cannot reach a
venue. Issue #22 carries the argument for why the trading allowlist is not widened.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Final

import structlog

from fking.data.format_resolver import Dataset, Market
from fking.platform.errors import DataIntegrityError
from fking.platform.safety.archive import ArchiveEgress

__all__ = [
    "ARCHIVE_BASE_URL",
    "ArchiveCoordinate",
    "ArchiveFetcher",
    "ChecksumSibling",
    "FetchedArchive",
    "Granularity",
    "archive_filename",
    "archive_url",
    "parse_checksum_sibling",
    "resolve_granularity",
]

_LOG: Final = structlog.get_logger(__name__)

ARCHIVE_BASE_URL: Final[str] = "https://data.binance.vision"

# The path segment `data.binance.vision` files each corpus under. USDⓈ-M futures is two
# segments, which is why this is a table rather than `market.value`.
_MARKET_PATH: Final[dict[Market, str]] = {
    Market.SPOT: "spot",
    Market.FUTURES_UM: "futures/um",
}

# Kline archives are the only ones keyed by interval, and the interval takes the slot
# the dataset name occupies in every other filename:
#   BTCUSDT-1m-2025-01-02.zip          klines, interval in the middle
#   BTCUSDT-bookDepth-2025-01-02.zip   everything else, dataset in the middle
# So "klines needs an interval" and "no other dataset may carry one" are the same rule
# stated from two sides, and both are enforced at construction.
_INTERVAL_DATASETS: Final[frozenset[Dataset]] = frozenset({Dataset.KLINES})

# A SHA-256 digest, lowercased. Anchored: a 64-hex prefix of a longer token is not a
# digest, it is a token this parser does not understand.
_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{64}\Z")

# Re-download once on a digest mismatch, then escalate. Two, not three: the second
# attempt distinguishes a truncated transfer from an archive whose bytes changed
# upstream, and no further attempt can distinguish anything the second did not.
MAX_DOWNLOAD_ATTEMPTS: Final[int] = 2

# Read size for re-verifying a cached file. Matches the egress chunk size for no deeper
# reason than that both are streaming the same kind of file.
_HASH_CHUNK_BYTES: Final[int] = 1024 * 1024


class Granularity(StrEnum):
    """Whether an archive covers one day or one month.

    Values match the path segment, so a resolved granularity and a URL cannot disagree
    about which file is meant.
    """

    DAILY = "daily"
    MONTHLY = "monthly"


@dataclass(frozen=True, slots=True)
class ArchiveCoordinate:
    """One archive, identified by everything the URL needs.

    `archive_date` is a UTC calendar day. Under `Granularity.MONTHLY` only its year and
    month are used, and the day is ignored rather than rejected -- a caller iterating a
    date range should not have to normalise to the first of the month to ask for the
    month's file.
    """

    market: Market
    dataset: Dataset
    symbol: str
    archive_date: date
    interval: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip():
            raise DataIntegrityError(
                f"symbol must be a non-empty unpadded string; got {self.symbol!r}"
            )
        needs_interval = self.dataset in _INTERVAL_DATASETS
        if needs_interval and not self.interval:
            raise DataIntegrityError(
                f"dataset {self.dataset.value!r} archives are keyed by interval and the "
                f"interval occupies a slot in the filename; got interval={self.interval!r}"
            )
        if not needs_interval and self.interval is not None:
            raise DataIntegrityError(
                f"dataset {self.dataset.value!r} archives carry no interval, but "
                f"interval={self.interval!r} was supplied; the filename slot it would "
                f"occupy holds the dataset name"
            )


@dataclass(frozen=True, slots=True)
class ChecksumSibling:
    """The parsed contents of a `.zip.CHECKSUM` file: a digest and the file it names."""

    digest_hex: str
    filename: str


@dataclass(frozen=True, slots=True)
class FetchedArchive:
    """A verified archive on disk, and the provenance of the verification.

    `served_from_cache` is recorded rather than inferred because a caller auditing a
    backfill needs to know whether these bytes were checked against a freshly fetched
    sibling or against the sibling stored beside them, and the two are different claims.
    """

    coordinate: ArchiveCoordinate
    granularity: Granularity
    url: str
    path: Path
    sha256_hex: str
    served_from_cache: bool


def resolve_granularity(*, archive_date: date, today_utc: date) -> Granularity:
    """Monthly where it exists, daily for the current and previous month.

    Monthly archives lag: last month's file may not have been published yet, and asking
    for it produces a 404 that a backfill would have to interpret. Two months back the
    file exists, so one request and one checksum replace thirty of each.

    The boundary is deliberately a whole month rather than a day count. A day count
    would make the choice depend on where in the month the run happens, so the same
    date would resolve differently on the 2nd and the 28th and a cache populated in one
    run would miss in the next.
    """
    months_back = (today_utc.year - archive_date.year) * 12 + (today_utc.month - archive_date.month)
    return Granularity.DAILY if months_back <= 1 else Granularity.MONTHLY


def _date_token(archive_date: date, granularity: Granularity) -> str:
    if granularity is Granularity.MONTHLY:
        return f"{archive_date.year:04d}-{archive_date.month:02d}"
    return archive_date.isoformat()


def _middle_token(coordinate: ArchiveCoordinate) -> str:
    """The filename slot between symbol and date: interval for klines, dataset for the rest."""
    return coordinate.interval if coordinate.interval is not None else coordinate.dataset.value


def archive_filename(coordinate: ArchiveCoordinate, granularity: Granularity) -> str:
    """The `.zip` filename, which is also the name the `.CHECKSUM` sibling must carry."""
    return (
        f"{coordinate.symbol}-{_middle_token(coordinate)}-"
        f"{_date_token(coordinate.archive_date, granularity)}.zip"
    )


def _path_segments(coordinate: ArchiveCoordinate, granularity: Granularity) -> tuple[str, ...]:
    segments: list[str] = [
        _MARKET_PATH[coordinate.market],
        granularity.value,
        coordinate.dataset.value,
        coordinate.symbol,
    ]
    if coordinate.interval is not None:
        segments.append(coordinate.interval)
    return tuple(segments)


def archive_url(coordinate: ArchiveCoordinate, granularity: Granularity) -> str:
    """The absolute URL of the `.zip`. Its `.CHECKSUM` sibling is this plus `.CHECKSUM`."""
    joined = "/".join(_path_segments(coordinate, granularity))
    return f"{ARCHIVE_BASE_URL}/data/{joined}/{archive_filename(coordinate, granularity)}"


def parse_checksum_sibling(text: str, *, url: str) -> ChecksumSibling:
    """Parse a `.zip.CHECKSUM` body into its digest and the filename it claims.

    The format is `<64 hex> <two spaces or ' *'> <filename>` -- the standard
    `sha256sum` output, where a leading `*` on the filename marks binary mode. Anything
    else raises: a sibling this parser does not understand is not a reason to skip
    verification, it is a reason to stop, because skipping is exactly the outcome the
    checksum exists to prevent.

    Raises:
        DataIntegrityError: the body is empty, carries more than one record, or does not
            begin with a lowercase 64-character hex digest.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise DataIntegrityError(
            f"checksum sibling at {url} holds {len(lines)} non-empty lines; expected "
            f"exactly one `<sha256>  <filename>` record"
        )

    fields = lines[0].split(maxsplit=1)
    if len(fields) != 2:  # noqa: PLR2004 -- a digest and a filename; naming it adds nothing
        raise DataIntegrityError(
            f"checksum sibling at {url} does not split into a digest and a filename: {lines[0]!r}"
        )

    digest_hex, raw_filename = fields
    if not _DIGEST_PATTERN.match(digest_hex):
        raise DataIntegrityError(
            f"checksum sibling at {url} does not begin with a lowercase 64-character "
            f"SHA-256 digest; got {digest_hex!r}"
        )

    # `*` marks binary mode in sha256sum output and is not part of the name.
    filename = raw_filename.strip().removeprefix("*")
    if not filename:
        raise DataIntegrityError(f"checksum sibling at {url} names no file: {lines[0]!r}")
    return ChecksumSibling(digest_hex=digest_hex, filename=filename)


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


class ArchiveFetcher:
    """Fetches archives, verifies them, and caches the verified bytes.

    The cache mirrors the archive's URL path, so a file on disk maps to the URL it came
    from by inspection. That matters during an incident: the question is always "which
    upstream file produced this row", and a hashed or flattened cache key answers it
    only with a lookup table that can itself be wrong.
    """

    def __init__(self, *, egress: ArchiveEgress, cache_root: Path) -> None:
        self._egress = egress
        self._cache_root = cache_root

    def cache_path(self, coordinate: ArchiveCoordinate, granularity: Granularity) -> Path:
        return self._cache_root.joinpath(
            *_path_segments(coordinate, granularity),
            archive_filename(coordinate, granularity),
        )

    async def fetch(
        self,
        coordinate: ArchiveCoordinate,
        *,
        today_utc: date,
        granularity: Granularity | None = None,
    ) -> FetchedArchive:
        """Return a verified archive on disk, downloading it only if the cache misses.

        `today_utc` is a parameter rather than a `date.today()` call, so the daily/
        monthly choice is deterministic and a replayed backfill resolves the same URLs
        it resolved the first time.

        `granularity` overrides that choice. It exists because the Parquet partition
        grain, not the publication calendar, decides which file a caller can use: trades
        are partitioned daily, so a monthly trades archive spans thirty partitions and
        the writer refuses it. A backfill therefore states the granularity its partition
        grain implies rather than accepting the one that costs the fewest requests.
        Callers with no such constraint pass `None` and get the cheaper answer.

        Raises:
            DataIntegrityError: the checksum sibling is unparseable, names a different
                file, or the digest mismatched on two consecutive downloads. Also
                raised when a cached archive no longer matches the sibling stored
                beside it.
            ArchiveUnavailableError: the host answered with something other than 200.
        """
        if granularity is None:
            granularity = resolve_granularity(
                archive_date=coordinate.archive_date, today_utc=today_utc
            )
        url = archive_url(coordinate, granularity)
        destination = self.cache_path(coordinate, granularity)
        sibling_path = destination.with_name(f"{destination.name}.CHECKSUM")
        expected_filename = archive_filename(coordinate, granularity)

        cached = self._read_cache(coordinate, granularity, url=url)
        if cached is not None:
            return cached

        destination.parent.mkdir(parents=True, exist_ok=True)
        # Named beside the destination rather than in the system temp directory: the
        # commit below is an os-level rename, which is only atomic within one
        # filesystem, and a cache root on a different volume from /tmp is ordinary.
        partial = destination.with_name(f"{destination.name}.partial")

        mismatches: list[str] = []
        try:
            for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
                # The sibling is re-fetched with the archive rather than reused. A
                # truncated sibling is rarer than a truncated archive -- it is eighty
                # bytes -- but reusing it would let one bad fetch of it condemn a
                # perfectly good file twice and escalate a transport blip as an
                # integrity event.
                sibling = parse_checksum_sibling(
                    await self._egress.get_text(f"{url}.CHECKSUM"), url=f"{url}.CHECKSUM"
                )
                if sibling.filename != expected_filename:
                    # Not retried: a rename is deterministic, so a second attempt would
                    # return the same wrong name and cost a full download to learn it.
                    raise DataIntegrityError(
                        f"checksum sibling for {url} names {sibling.filename!r}, not "
                        f"{expected_filename!r}; the digest would be verified against "
                        f"the wrong archive. An upstream rename is the usual cause"
                    )

                observed_digest = await self._egress.download(url, partial)
                if observed_digest == sibling.digest_hex:
                    partial.replace(destination)
                    sibling_path.write_text(
                        f"{sibling.digest_hex}  {sibling.filename}\n", encoding="utf-8"
                    )
                    _LOG.info(
                        "archive.verified",
                        url=url,
                        path=str(destination),
                        sha256=observed_digest,
                        attempt=attempt,
                    )
                    return FetchedArchive(
                        coordinate=coordinate,
                        granularity=granularity,
                        url=url,
                        path=destination,
                        sha256_hex=observed_digest,
                        served_from_cache=False,
                    )
                mismatches.append(
                    f"attempt {attempt}: expected {sibling.digest_hex}, observed {observed_digest}"
                )
                _LOG.warning(
                    "archive.checksum_mismatch",
                    url=url,
                    attempt=attempt,
                    expected_sha256=sibling.digest_hex,
                    observed_sha256=observed_digest,
                )
        finally:
            # Nothing partial is ever left in the cache. A `.partial` file that outlived
            # a crash would be indistinguishable from a verified archive to anything
            # matching on the directory rather than the extension.
            partial.unlink(missing_ok=True)

        raise DataIntegrityError(
            f"checksum verification failed {MAX_DOWNLOAD_ATTEMPTS} consecutive times for "
            f"{url} ({'; '.join(mismatches)}). This is a data-integrity event, not a "
            f"retry: if the upstream archive changed, every backtest that consumed the "
            f"previous copy is suspect. DATA_PIPELINE.md section 11"
        )

    def _read_cache(
        self, coordinate: ArchiveCoordinate, granularity: Granularity, *, url: str
    ) -> FetchedArchive | None:
        """Return the cached archive if it verifies locally, `None` if the cache misses.

        Both files must be present. A `.zip` without its sibling is not a cache hit with
        a missing convenience -- it is bytes with no recorded provenance, and serving it
        would mean the verification rule holds only for files fetched in this process.

        The digest is recomputed on every hit rather than trusted. That costs a full
        read of the file, and it buys the property that on-disk corruption is caught
        where it happened instead of surfacing as a parse anomaly weeks later. If a bulk
        backfill measures this as the bottleneck, the fix is a verified manifest,
        not a flag that turns the check off.
        """
        destination = self.cache_path(coordinate, granularity)
        sibling_path = destination.with_name(f"{destination.name}.CHECKSUM")
        expected_filename = archive_filename(coordinate, granularity)

        if not destination.is_file() or not sibling_path.is_file():
            return None

        sibling = parse_checksum_sibling(
            sibling_path.read_text(encoding="utf-8"), url=str(sibling_path)
        )
        if sibling.filename != expected_filename:
            raise DataIntegrityError(
                f"cached checksum sibling {sibling_path} names {sibling.filename!r}, not "
                f"{expected_filename!r}; the cache entry and its provenance disagree"
            )

        observed_digest = _digest_file(destination)
        if observed_digest != sibling.digest_hex:
            # Deliberately not a silent re-download. A file that verified when it was
            # written and no longer does is disk corruption or tampering, and quietly
            # replacing it means that condition is never reported by anything.
            raise DataIntegrityError(
                f"cached archive {destination} no longer matches the checksum recorded "
                f"beside it (expected {sibling.digest_hex}, observed {observed_digest}). "
                f"The cache entry is corrupt; delete it and re-run to refetch"
            )

        _LOG.info("archive.cache_hit", url=url, path=str(destination), sha256=observed_digest)
        return FetchedArchive(
            coordinate=coordinate,
            granularity=granularity,
            url=url,
            path=destination,
            sha256_hex=observed_digest,
            served_from_cache=True,
        )
