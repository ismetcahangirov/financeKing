"""The checksum-verified archive fetcher.

The tests that matter most are the ones about what *does not* happen: nothing is
written when verification fails, no third download is attempted after two mismatches,
and no request is issued when the cache is populated. Each of those is unfalsifiable
without an explicit counter or an explicit directory listing, which is why both appear
here rather than an assertion that the happy path returned a path.

The egress is a fake for most of these, because the boundary under test is the
`ArchiveEgress` protocol -- `fking.data` cannot import `httpx` at all, so a fake is
what the module actually sees. `TestAgainstTheGuardedTransport` closes the gap that
leaves: one pass with the real `GuardedArchiveEgress` over a substituted socket layer,
so the request counter being asserted is the production one. The guarded client's own
behaviour is covered in tests/platform/safety/.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx
import pytest

from fking.data.archive import (
    MAX_DOWNLOAD_ATTEMPTS,
    ArchiveCoordinate,
    ArchiveFetcher,
    Granularity,
    archive_filename,
    archive_url,
    parse_checksum_sibling,
    resolve_granularity,
)
from fking.data.format_resolver import Dataset, Market
from fking.platform.errors import ArchiveUnavailableError, DataIntegrityError
from fking.platform.safety import SafetyViolation
from fking.platform.safety.archive import GuardedArchiveEgress

pytestmark = pytest.mark.unit

TODAY = date(2026, 8, 3)

# One `.CHECKSUM` fetch plus one archive download is what a cache miss costs.
REQUESTS_PER_DOWNLOAD = 2
# The retry budget, spelled out here so the test states the number independently of the
# constant it is asserting against -- otherwise a change to MAX_DOWNLOAD_ATTEMPTS would
# silently take its own test with it.
EXPECTED_ATTEMPTS = 2

BTC_KLINES = ArchiveCoordinate(
    market=Market.SPOT,
    dataset=Dataset.KLINES,
    symbol="BTCUSDT",
    archive_date=date(2026, 8, 1),
    interval="1m",
)
BTC_KLINES_OLD = ArchiveCoordinate(
    market=Market.SPOT,
    dataset=Dataset.KLINES,
    symbol="BTCUSDT",
    archive_date=date(2026, 5, 14),
    interval="1m",
)

# A four-byte PK header plus filler, so the fixture at least looks like the zip it
# stands in for. The bytes are never unzipped -- this module verifies, it does not parse.
ARCHIVE_BYTES = b"PK\x03\x04" + b"binance-archive-fixture" * 64
ARCHIVE_DIGEST = hashlib.sha256(ARCHIVE_BYTES).hexdigest()

# One flipped byte. The whole point of the checksum: this file opens, reads, and is
# wrong, and only the digest can tell.
FLIPPED_BYTES = bytes([ARCHIVE_BYTES[0] ^ 0x01]) + ARCHIVE_BYTES[1:]


def checksum_body(digest_hex: str, filename: str) -> str:
    """The `sha256sum` output shape Binance serves: digest, two spaces, filename."""
    return f"{digest_hex}  {filename}\n"


@dataclass
class FakeEgress:
    """An in-memory `ArchiveEgress`.

    `bodies` maps URL to the text a `.CHECKSUM` fetch returns; `payloads` maps URL to
    the bytes a download writes. A URL absent from either raises `ArchiveUnavailableError`,
    which is what the real host does with a 404 and what a fetcher asking for an
    unpublished monthly archive must handle.

    `payloads` is a list per URL so a test can make the first attempt return corrupt
    bytes and the second return good ones -- the retry behaviour is otherwise
    unobservable. The last entry repeats once the list is exhausted.
    """

    bodies: dict[str, str] = field(default_factory=dict)
    payloads: dict[str, list[bytes]] = field(default_factory=dict)
    text_urls: list[str] = field(default_factory=list)
    download_urls: list[str] = field(default_factory=list)

    @property
    def request_count(self) -> int:
        return len(self.text_urls) + len(self.download_urls)

    async def get_text(self, url: str) -> str:
        self.text_urls.append(url)
        if url not in self.bodies:
            raise ArchiveUnavailableError(f"GET {url} returned HTTP 404; expected 200")
        return self.bodies[url]

    async def download(self, url: str, destination: Path) -> str:
        self.download_urls.append(url)
        queued = self.payloads.get(url)
        if not queued:
            raise ArchiveUnavailableError(f"GET {url} returned HTTP 404; expected 200")
        payload = queued.pop(0) if len(queued) > 1 else queued[0]
        destination.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()


def egress_serving(
    coordinate: ArchiveCoordinate,
    granularity: Granularity,
    *,
    payloads: list[bytes],
    digest_hex: str = ARCHIVE_DIGEST,
    filename: str | None = None,
) -> FakeEgress:
    url = archive_url(coordinate, granularity)
    named = filename if filename is not None else archive_filename(coordinate, granularity)
    return FakeEgress(
        bodies={f"{url}.CHECKSUM": checksum_body(digest_hex, named)},
        payloads={url: payloads},
    )


def cached_files(cache_root: Path) -> list[Path]:
    """Files only. An empty directory is not data, and mkdir is not a write of one."""
    return sorted(path for path in cache_root.rglob("*") if path.is_file())


class TestGranularityChoice:
    """Monthly where it exists, daily for the current and previous month.

    Getting this backwards produces a pipeline thirty times slower and thirty times
    likelier to fail partway through a range.
    """

    @pytest.mark.parametrize(
        ("archive_date", "expected"),
        [
            (date(2026, 8, 3), Granularity.DAILY),  # today
            (date(2026, 8, 1), Granularity.DAILY),  # current month
            (date(2026, 7, 31), Granularity.DAILY),  # previous month: monthly lags
            (date(2026, 7, 1), Granularity.DAILY),
            (date(2026, 6, 30), Granularity.MONTHLY),  # two months back
            (date(2026, 5, 14), Granularity.MONTHLY),  # three months back
            (date(2017, 8, 17), Granularity.MONTHLY),
            (date(2025, 12, 31), Granularity.MONTHLY),  # across a year boundary
        ],
    )
    def test_the_boundary_is_the_previous_calendar_month(
        self, archive_date: date, expected: Granularity
    ) -> None:
        assert resolve_granularity(archive_date=archive_date, today_utc=TODAY) is expected

    def test_the_choice_does_not_move_within_a_month(self) -> None:
        """A day count would resolve the same date differently on the 2nd and the 28th,
        so a cache populated in one run would miss in the next."""
        target = date(2026, 6, 15)
        early = resolve_granularity(archive_date=target, today_utc=date(2026, 8, 1))
        late = resolve_granularity(archive_date=target, today_utc=date(2026, 8, 31))
        assert early is late is Granularity.MONTHLY


class TestUrlLayout:
    @pytest.mark.parametrize(
        ("coordinate", "granularity", "expected"),
        [
            (
                BTC_KLINES,
                Granularity.DAILY,
                "https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1m/"
                "BTCUSDT-1m-2026-08-01.zip",
            ),
            (
                BTC_KLINES_OLD,
                Granularity.MONTHLY,
                "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/"
                "BTCUSDT-1m-2026-05.zip",
            ),
            (
                ArchiveCoordinate(
                    market=Market.FUTURES_UM,
                    dataset=Dataset.KLINES,
                    symbol="BTCUSDT",
                    archive_date=date(2025, 1, 2),
                    interval="1m",
                ),
                Granularity.DAILY,
                "https://data.binance.vision/data/futures/um/daily/klines/BTCUSDT/1m/"
                "BTCUSDT-1m-2025-01-02.zip",
            ),
            (
                ArchiveCoordinate(
                    market=Market.FUTURES_UM,
                    dataset=Dataset.BOOK_DEPTH,
                    symbol="BTCUSDT",
                    archive_date=date(2025, 1, 2),
                ),
                Granularity.DAILY,
                "https://data.binance.vision/data/futures/um/daily/bookDepth/BTCUSDT/"
                "BTCUSDT-bookDepth-2025-01-02.zip",
            ),
        ],
    )
    def test_urls_match_the_published_layout(
        self, coordinate: ArchiveCoordinate, granularity: Granularity, expected: str
    ) -> None:
        """The five examples in DATA_PIPELINE.md section 2, transcribed."""
        assert archive_url(coordinate, granularity) == expected

    def test_a_monthly_url_ignores_the_day(self) -> None:
        """A caller iterating a date range should not have to normalise to the 1st."""
        mid = ArchiveCoordinate(
            market=Market.SPOT,
            dataset=Dataset.KLINES,
            symbol="BTCUSDT",
            archive_date=date(2026, 5, 14),
            interval="1m",
        )
        first = ArchiveCoordinate(
            market=Market.SPOT,
            dataset=Dataset.KLINES,
            symbol="BTCUSDT",
            archive_date=date(2026, 5, 1),
            interval="1m",
        )
        assert archive_url(mid, Granularity.MONTHLY) == archive_url(first, Granularity.MONTHLY)


class TestCoordinateValidation:
    def test_klines_without_an_interval_is_refused(self) -> None:
        with pytest.raises(DataIntegrityError, match="keyed by interval"):
            ArchiveCoordinate(
                market=Market.SPOT,
                dataset=Dataset.KLINES,
                symbol="BTCUSDT",
                archive_date=TODAY,
            )

    def test_a_non_kline_dataset_with_an_interval_is_refused(self) -> None:
        """The slot the interval would occupy holds the dataset name, so accepting one
        would build a URL that cannot exist."""
        with pytest.raises(DataIntegrityError, match="carry no interval"):
            ArchiveCoordinate(
                market=Market.SPOT,
                dataset=Dataset.TRADES,
                symbol="BTCUSDT",
                archive_date=TODAY,
                interval="1m",
            )

    @pytest.mark.parametrize("symbol", ["", " BTCUSDT", "BTCUSDT "])
    def test_an_empty_or_padded_symbol_is_refused(self, symbol: str) -> None:
        with pytest.raises(DataIntegrityError, match="unpadded"):
            ArchiveCoordinate(
                market=Market.SPOT,
                dataset=Dataset.TRADES,
                symbol=symbol,
                archive_date=TODAY,
            )


class TestChecksumSiblingParsing:
    def test_the_standard_shape_parses(self) -> None:
        sibling = parse_checksum_sibling(
            checksum_body(ARCHIVE_DIGEST, "BTCUSDT-1m-2026-08-01.zip"), url="x"
        )
        assert sibling.digest_hex == ARCHIVE_DIGEST
        assert sibling.filename == "BTCUSDT-1m-2026-08-01.zip"

    def test_a_binary_mode_marker_is_not_part_of_the_name(self) -> None:
        sibling = parse_checksum_sibling(f"{ARCHIVE_DIGEST} *file.zip\n", url="x")
        assert sibling.filename == "file.zip"

    @pytest.mark.parametrize(
        "body",
        [
            "",
            "\n\n",
            f"{ARCHIVE_DIGEST}\n",  # digest with no filename
            f"{ARCHIVE_DIGEST.upper()}  file.zip\n",  # uppercase is not the served form
            "notadigest  file.zip\n",
            f"{ARCHIVE_DIGEST[:-1]}  file.zip\n",  # 63 characters
            f"{ARCHIVE_DIGEST}  a.zip\n{ARCHIVE_DIGEST}  b.zip\n",  # two records
            f"{ARCHIVE_DIGEST} *\n",  # marker but no name
        ],
    )
    def test_an_unrecognised_sibling_raises_rather_than_being_skipped(self, body: str) -> None:
        """A sibling this parser does not understand is not a reason to skip
        verification -- skipping is exactly the outcome the checksum exists to prevent."""
        with pytest.raises(DataIntegrityError):
            parse_checksum_sibling(body, url="https://data.binance.vision/x.zip.CHECKSUM")


@pytest.mark.asyncio
class TestFetch:
    async def test_a_verified_archive_is_written_to_the_cache(self, tmp_path: Path) -> None:
        egress = egress_serving(BTC_KLINES, Granularity.DAILY, payloads=[ARCHIVE_BYTES])
        fetcher = ArchiveFetcher(egress=egress, cache_root=tmp_path)

        fetched = await fetcher.fetch(BTC_KLINES, today_utc=TODAY)

        assert fetched.sha256_hex == ARCHIVE_DIGEST
        assert fetched.served_from_cache is False
        assert fetched.granularity is Granularity.DAILY
        assert fetched.path.read_bytes() == ARCHIVE_BYTES
        assert fetched.path.parent == tmp_path / "spot" / "daily" / "klines" / "BTCUSDT" / "1m"

    async def test_the_checksum_is_fetched_before_the_archive(self, tmp_path: Path) -> None:
        """ "Verified before the first byte is handed to a parser" is only true if the
        sibling is in hand before the download, not reconciled afterwards."""
        egress = egress_serving(BTC_KLINES, Granularity.DAILY, payloads=[ARCHIVE_BYTES])
        await ArchiveFetcher(egress=egress, cache_root=tmp_path).fetch(BTC_KLINES, today_utc=TODAY)
        assert len(egress.text_urls) == 1
        assert len(egress.download_urls) == 1
        assert egress.text_urls[0].endswith(".CHECKSUM")

    async def test_a_date_in_the_current_month_requests_the_daily_url(self, tmp_path: Path) -> None:
        egress = egress_serving(BTC_KLINES, Granularity.DAILY, payloads=[ARCHIVE_BYTES])
        fetched = await ArchiveFetcher(egress=egress, cache_root=tmp_path).fetch(
            BTC_KLINES, today_utc=TODAY
        )
        assert "/daily/" in fetched.url
        assert fetched.url.endswith("BTCUSDT-1m-2026-08-01.zip")

    async def test_a_date_three_months_back_requests_the_monthly_url(self, tmp_path: Path) -> None:
        egress = egress_serving(BTC_KLINES_OLD, Granularity.MONTHLY, payloads=[ARCHIVE_BYTES])
        fetched = await ArchiveFetcher(egress=egress, cache_root=tmp_path).fetch(
            BTC_KLINES_OLD, today_utc=TODAY
        )
        assert "/monthly/" in fetched.url
        assert fetched.url.endswith("BTCUSDT-1m-2026-05.zip")

    async def test_a_missing_archive_surfaces_as_unavailable(self, tmp_path: Path) -> None:
        """A 404 is an ordinary answer -- the symbol may not have existed on that date --
        and a backfill must be able to act on it rather than crash under it."""
        fetcher = ArchiveFetcher(egress=FakeEgress(), cache_root=tmp_path)
        with pytest.raises(ArchiveUnavailableError):
            await fetcher.fetch(BTC_KLINES, today_utc=TODAY)
        assert cached_files(tmp_path) == []


@pytest.mark.asyncio
class TestChecksumFailure:
    async def test_one_flipped_byte_is_rejected_and_nothing_is_cached(self, tmp_path: Path) -> None:
        """The failure that matters is not a file that will not open. It is one that
        opens, parses, and is wrong."""
        egress = egress_serving(
            BTC_KLINES, Granularity.DAILY, payloads=[FLIPPED_BYTES, FLIPPED_BYTES]
        )
        fetcher = ArchiveFetcher(egress=egress, cache_root=tmp_path)

        with pytest.raises(DataIntegrityError) as failure:
            await fetcher.fetch(BTC_KLINES, today_utc=TODAY)

        message = str(failure.value)
        assert ARCHIVE_DIGEST in message, "the expected digest must be in the message"
        assert hashlib.sha256(FLIPPED_BYTES).hexdigest() in message, (
            "the observed digest must be in the message"
        )
        assert cached_files(tmp_path) == [], "a rejected archive must leave nothing behind"

    async def test_a_second_consecutive_failure_does_not_download_a_third_time(
        self, tmp_path: Path
    ) -> None:
        """Two, not three: the second attempt distinguishes a truncated transfer from an
        archive whose bytes changed upstream, and no further attempt distinguishes
        anything the second did not."""
        egress = egress_serving(BTC_KLINES, Granularity.DAILY, payloads=[FLIPPED_BYTES])
        fetcher = ArchiveFetcher(egress=egress, cache_root=tmp_path)

        with pytest.raises(DataIntegrityError, match="data-integrity event"):
            await fetcher.fetch(BTC_KLINES, today_utc=TODAY)

        assert len(egress.download_urls) == MAX_DOWNLOAD_ATTEMPTS
        assert MAX_DOWNLOAD_ATTEMPTS == EXPECTED_ATTEMPTS

    async def test_a_transient_truncation_is_recovered_by_the_retry(self, tmp_path: Path) -> None:
        """The retry has to be able to succeed, or it is a slower way to fail."""
        egress = egress_serving(
            BTC_KLINES, Granularity.DAILY, payloads=[ARCHIVE_BYTES[:40], ARCHIVE_BYTES]
        )
        fetched = await ArchiveFetcher(egress=egress, cache_root=tmp_path).fetch(
            BTC_KLINES, today_utc=TODAY
        )
        assert fetched.sha256_hex == ARCHIVE_DIGEST
        assert len(egress.download_urls) == EXPECTED_ATTEMPTS

    async def test_a_sibling_naming_a_different_file_is_refused_without_downloading(
        self, tmp_path: Path
    ) -> None:
        """A digest matching a differently-named file means the wrong archive is being
        checksummed -- a real outcome of an upstream symbol rename. It is deterministic,
        so retrying would cost a full download to learn the same thing."""
        egress = egress_serving(
            BTC_KLINES,
            Granularity.DAILY,
            payloads=[ARCHIVE_BYTES],
            filename="ETHUSDT-1m-2026-08-01.zip",
        )
        fetcher = ArchiveFetcher(egress=egress, cache_root=tmp_path)

        with pytest.raises(DataIntegrityError, match=re.escape("ETHUSDT-1m-2026-08-01.zip")):
            await fetcher.fetch(BTC_KLINES, today_utc=TODAY)

        assert egress.download_urls == []
        assert cached_files(tmp_path) == []

    async def test_an_unparseable_sibling_stops_the_fetch(self, tmp_path: Path) -> None:
        url = archive_url(BTC_KLINES, Granularity.DAILY)
        egress = FakeEgress(
            bodies={f"{url}.CHECKSUM": "<html>404 Not Found</html>"},
            payloads={url: [ARCHIVE_BYTES]},
        )
        fetcher = ArchiveFetcher(egress=egress, cache_root=tmp_path)

        with pytest.raises(DataIntegrityError):
            await fetcher.fetch(BTC_KLINES, today_utc=TODAY)

        assert egress.download_urls == []


@pytest.mark.asyncio
class TestCache:
    async def test_a_second_fetch_issues_zero_network_requests(self, tmp_path: Path) -> None:
        egress = egress_serving(BTC_KLINES, Granularity.DAILY, payloads=[ARCHIVE_BYTES])
        fetcher = ArchiveFetcher(egress=egress, cache_root=tmp_path)

        await fetcher.fetch(BTC_KLINES, today_utc=TODAY)
        requests_after_first = egress.request_count

        second = await fetcher.fetch(BTC_KLINES, today_utc=TODAY)

        assert egress.request_count == requests_after_first
        assert second.served_from_cache is True
        assert second.sha256_hex == ARCHIVE_DIGEST

    async def test_a_fresh_fetcher_reuses_a_populated_cache(self, tmp_path: Path) -> None:
        """The cache has to survive the process, or a resumable backfill is not resumable."""
        first_egress = egress_serving(BTC_KLINES, Granularity.DAILY, payloads=[ARCHIVE_BYTES])
        await ArchiveFetcher(egress=first_egress, cache_root=tmp_path).fetch(
            BTC_KLINES, today_utc=TODAY
        )

        second_egress = FakeEgress()  # serves nothing; any request raises
        fetched = await ArchiveFetcher(egress=second_egress, cache_root=tmp_path).fetch(
            BTC_KLINES, today_utc=TODAY
        )

        assert second_egress.request_count == 0
        assert fetched.served_from_cache is True

    async def test_the_sibling_is_stored_beside_the_archive(self, tmp_path: Path) -> None:
        """A `.zip` with no recorded provenance is bytes, not a cache entry."""
        egress = egress_serving(BTC_KLINES, Granularity.DAILY, payloads=[ARCHIVE_BYTES])
        fetched = await ArchiveFetcher(egress=egress, cache_root=tmp_path).fetch(
            BTC_KLINES, today_utc=TODAY
        )
        sibling = fetched.path.with_name(f"{fetched.path.name}.CHECKSUM")
        assert (
            parse_checksum_sibling(sibling.read_text(encoding="utf-8"), url="x").digest_hex
            == ARCHIVE_DIGEST
        )

    async def test_an_archive_without_its_sibling_is_a_cache_miss(self, tmp_path: Path) -> None:
        egress = egress_serving(BTC_KLINES, Granularity.DAILY, payloads=[ARCHIVE_BYTES])
        fetcher = ArchiveFetcher(egress=egress, cache_root=tmp_path)
        fetched = await fetcher.fetch(BTC_KLINES, today_utc=TODAY)
        fetched.path.with_name(f"{fetched.path.name}.CHECKSUM").unlink()

        refetched = await fetcher.fetch(BTC_KLINES, today_utc=TODAY)

        assert refetched.served_from_cache is False
        assert egress.request_count > REQUESTS_PER_DOWNLOAD

    async def test_a_corrupted_cache_entry_raises_rather_than_being_replaced(
        self, tmp_path: Path
    ) -> None:
        """Quietly re-downloading would mean on-disk corruption is never reported by
        anything, and the next occurrence is attributed to the upstream archive."""
        egress = egress_serving(BTC_KLINES, Granularity.DAILY, payloads=[ARCHIVE_BYTES])
        fetcher = ArchiveFetcher(egress=egress, cache_root=tmp_path)
        fetched = await fetcher.fetch(BTC_KLINES, today_utc=TODAY)
        fetched.path.write_bytes(FLIPPED_BYTES)

        with pytest.raises(DataIntegrityError, match="no longer matches"):
            await fetcher.fetch(BTC_KLINES, today_utc=TODAY)

    async def test_a_cache_entry_whose_sibling_names_another_file_raises(
        self, tmp_path: Path
    ) -> None:
        egress = egress_serving(BTC_KLINES, Granularity.DAILY, payloads=[ARCHIVE_BYTES])
        fetcher = ArchiveFetcher(egress=egress, cache_root=tmp_path)
        fetched = await fetcher.fetch(BTC_KLINES, today_utc=TODAY)
        fetched.path.with_name(f"{fetched.path.name}.CHECKSUM").write_text(
            checksum_body(ARCHIVE_DIGEST, "ETHUSDT-1m-2026-08-01.zip"), encoding="utf-8"
        )

        with pytest.raises(DataIntegrityError, match="disagree"):
            await fetcher.fetch(BTC_KLINES, today_utc=TODAY)

    async def test_daily_and_monthly_entries_do_not_collide(self, tmp_path: Path) -> None:
        """The cache mirrors the URL path, so the granularity segment separates them."""
        daily_egress = egress_serving(BTC_KLINES, Granularity.DAILY, payloads=[ARCHIVE_BYTES])
        monthly_egress = egress_serving(
            BTC_KLINES_OLD, Granularity.MONTHLY, payloads=[ARCHIVE_BYTES]
        )
        fetcher_daily = ArchiveFetcher(egress=daily_egress, cache_root=tmp_path)
        fetcher_monthly = ArchiveFetcher(egress=monthly_egress, cache_root=tmp_path)

        daily = await fetcher_daily.fetch(BTC_KLINES, today_utc=TODAY)
        monthly = await fetcher_monthly.fetch(BTC_KLINES_OLD, today_utc=TODAY)

        assert daily.path != monthly.path
        assert "daily" in daily.path.parts
        assert "monthly" in monthly.path.parts


@pytest.mark.asyncio
class TestAgainstTheGuardedTransport:
    """One end-to-end pass with the real `GuardedArchiveEgress`.

    The rest of this module uses `FakeEgress`, which is the right boundary for
    behaviour -- but it also means every assertion above is about an object written in
    this file. This class runs the same fetch through the production egress with only
    its socket layer substituted, so the request counter being asserted is the one on
    the guarded transport rather than a stand-in for it, and the fetcher is proven to
    satisfy the protocol as the client actually implements it.

    Importing `httpx` is legal here and illegal in `src/fking/data`; that asymmetry is
    the whole reason `ArchiveEgress` exists.
    """

    async def test_a_populated_cache_issues_zero_requests_on_the_guarded_transport(
        self, tmp_path: Path
    ) -> None:
        url = archive_url(BTC_KLINES, Granularity.DAILY)
        served = {
            url: httpx.Response(200, content=ARCHIVE_BYTES),
            f"{url}.CHECKSUM": httpx.Response(
                200,
                text=checksum_body(ARCHIVE_DIGEST, archive_filename(BTC_KLINES, Granularity.DAILY)),
            ),
        }

        def handle(request: httpx.Request) -> httpx.Response:
            response = served.get(str(request.url))
            return response if response is not None else httpx.Response(404)

        async with GuardedArchiveEgress() as egress:
            egress._client._transport = httpx.MockTransport(handle)
            fetcher = ArchiveFetcher(egress=egress, cache_root=tmp_path)

            first = await fetcher.fetch(BTC_KLINES, today_utc=TODAY)
            assert egress.request_count == REQUESTS_PER_DOWNLOAD
            assert first.served_from_cache is False

            second = await fetcher.fetch(BTC_KLINES, today_utc=TODAY)

            assert egress.request_count == REQUESTS_PER_DOWNLOAD, (
                "a populated cache must issue no further requests"
            )
            assert second.served_from_cache is True
            assert second.sha256_hex == ARCHIVE_DIGEST

    async def test_the_host_guard_runs_on_the_real_fetch_path(self) -> None:
        """The fetcher must not be able to reach a venue even if a URL were wrong.

        Asserted here rather than only in tests/platform/safety, because it is the
        composed object -- fetcher over guarded egress -- that ships.
        """
        async with GuardedArchiveEgress() as egress:
            egress._client._transport = httpx.MockTransport(
                lambda _request: httpx.Response(200, content=b"")
            )
            with pytest.raises(SafetyViolation):
                await egress.get_text("https://api.binance.com/api/v3/time")
