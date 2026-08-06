"""The archive egress path: a second guarded transport that can only reach data hosts.

Deliberately **not** re-exported from `fking.platform.safety.__init__`. An importer has
to name this module, which is what makes the `import-linter` contract forbidding
`fking.execution` from reaching it enforceable at all -- re-exporting it would put the
archive client one attribute access away from every module that already imports the
trading kernel.

What this path does not have is as load-bearing as what it does:

- No credential of any kind. It cannot sign a request because it holds no signer, and
  an `import-linter` contract forbids it from importing `fking.platform.config`, which
  is where every `SecretStr` in this system lives.
- No access to `PERMITTED_HOSTS`. It validates against `ARCHIVE_HOSTS`, and the two sets
  are disjoint -- asserted in `tests/platform/safety/test_archive_allowlist.py`.

Issue #22; .claude/rules/safety-kernel.md.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

import httpx
import structlog

from fking.platform.safety._archive_allowlist import ARCHIVE_HOSTS
from fking.platform.safety._archive_errors import ArchiveUnavailableError
from fking.platform.safety._hostcheck import assert_permitted

__all__ = [
    "ARCHIVE_HOSTS",
    "ArchiveEgress",
    "ArchiveUnavailableError",
    "GuardedArchiveEgress",
    "assert_archive_host_permitted",
    "guarded_archive_client",
]

_LOG: Final = structlog.get_logger(__name__)

_ALLOWLIST_NAME: Final[str] = "the archive host set (ARCHIVE_HOSTS)"

# Archives run to hundreds of megabytes on the trades datasets and are served from a
# CDN, so the trading path's 10s is the wrong budget here. It is a connect/read timeout
# per chunk rather than a whole-transfer deadline, which is what httpx's Timeout means.
DEFAULT_ARCHIVE_TIMEOUT_SECONDS: Final[float] = 30.0

# 1 MiB. Large enough that a 200 MB trades archive is ~200 iterations rather than
# ~50,000, small enough that a chunk never dominates resident memory.
_CHUNK_BYTES: Final[int] = 1024 * 1024


def assert_archive_host_permitted(url: str | httpx.URL) -> str:
    """Validate one URL against `ARCHIVE_HOSTS`, returning the normalised host.

    Raises `SafetyViolation` for every trading host, including the testnet hosts in
    `PERMITTED_HOSTS`. That direction is not an accident of the two sets being
    disjoint -- an archive fetcher that can reach an order endpoint has stopped being
    an archive fetcher.
    """
    return assert_permitted(str(url), permitted_hosts=ARCHIVE_HOSTS, allowlist_name=_ALLOWLIST_NAME)


async def _guard_archive_request(request: httpx.Request) -> None:
    """Validate on the built `Request`, after URL merging.

    Same reasoning as the trading client: `httpx` merges a relative path against
    `base_url`, but an absolute URL passed to `.get()` *replaces* it, so a
    construction-time check never sees it.
    """
    assert_archive_host_permitted(request.url)


def guarded_archive_client(
    *, base_url: str = "", timeout_seconds: float = DEFAULT_ARCHIVE_TIMEOUT_SECONDS
) -> httpx.AsyncClient:
    """The only sanctioned way to construct a client for the archive host.

    `auth` is never set and no header is attached. That is not an omission to be fixed
    later: a client that cannot authenticate cannot place an order even if a future
    refactor points it at the wrong host, so the absence of a credential is a second,
    independent barrier behind the allowlist.
    """
    if base_url:
        assert_archive_host_permitted(base_url)
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(timeout_seconds),
        # Proxy environment variables must not be able to reroute a validated host.
        # With trust_env=True the URL still reads data.binance.vision and the bytes
        # come from wherever HTTPS_PROXY points -- and a substituted archive that
        # matches a substituted checksum verifies perfectly.
        trust_env=False,
        # A redirect would be re-validated by the hook, so this is not about bypass. It
        # is provenance: the digest recorded against a file must belong to the URL we
        # asked for, not to whatever a 302 chose.
        follow_redirects=False,
        event_hooks={"request": [_guard_archive_request]},
    )


@runtime_checkable
class ArchiveEgress(Protocol):
    """The archive network surface, as `fking.data` sees it.

    This Protocol exists so that `fking.data` never imports `httpx` -- the
    "Only the safety kernel constructs network clients" contract forbids it, and that
    contract is worth more than the small cost of an interface with one production
    implementation. Everything policy-shaped stays on the far side: this returns bytes
    and digests, and has no opinion about whether a digest is the right one.
    """

    @property
    def request_count(self) -> int:
        """Requests issued over this egress since construction.

        Exposed because "the cache was used" is otherwise unfalsifiable: a fetcher that
        re-downloaded everything and a fetcher that read from disk return identical
        results, and only the counter distinguishes them.
        """

    async def get_text(self, url: str) -> str:
        """Fetch a small text resource -- in practice a `.zip.CHECKSUM` sibling."""

    async def download(self, url: str, destination: Path) -> str:
        """Stream `url` into `destination`, returning the lowercase SHA-256 hex digest.

        The digest is computed from the bytes as they are written, so it describes what
        landed on disk rather than what a later read believes is there.
        """


class GuardedArchiveEgress:
    """`ArchiveEgress` over `guarded_archive_client()`.

    Owns one client for its lifetime and is an async context manager, because a client
    per request loses connection reuse against a CDN that a bulk backfill hits tens of
    thousands of times.
    """

    def __init__(self, *, timeout_seconds: float = DEFAULT_ARCHIVE_TIMEOUT_SECONDS) -> None:
        self._client = guarded_archive_client(timeout_seconds=timeout_seconds)
        self._request_count = 0

    @property
    def request_count(self) -> int:
        return self._request_count

    async def __aenter__(self) -> GuardedArchiveEgress:
        return self

    async def __aexit__(self, *_exception_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_text(self, url: str) -> str:
        self._request_count += 1
        response = await self._client.get(url)
        if response.status_code != httpx.codes.OK:
            raise ArchiveUnavailableError(
                f"GET {url} returned HTTP {response.status_code}; expected 200"
            )
        return response.text

    async def download(self, url: str, destination: Path) -> str:
        self._request_count += 1
        digest = hashlib.sha256()
        byte_count = 0
        async with self._client.stream("GET", url) as response:
            if response.status_code != httpx.codes.OK:
                # The body is not read on a non-200, so nothing is written and the
                # caller's destination stays absent rather than truncated.
                raise ArchiveUnavailableError(
                    f"GET {url} returned HTTP {response.status_code}; expected 200"
                )
            with destination.open("wb") as sink:
                async for chunk in response.aiter_bytes(_CHUNK_BYTES):
                    digest.update(chunk)
                    sink.write(chunk)
                    byte_count += len(chunk)
        _LOG.info(
            "archive.downloaded",
            url=url,
            byte_count=byte_count,
            sha256=digest.hexdigest(),
        )
        return digest.hexdigest()
