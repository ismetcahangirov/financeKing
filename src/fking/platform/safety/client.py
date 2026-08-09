"""Host validation and the guarded transports.

Every outbound HTTP and WebSocket connection in this process is constructed here.
Nothing else in `src/fking/` may import `httpx` or `websockets` directly, and an
import-linter contract enforces that.

The threat model is not malice (ARCHITECTURE.md section 8). It is a config edit that
does not get reverted, a `.env.example` filled in from the credentials somebody had to
hand, an agent generating `import httpx` because that is what the training data does,
and a library changing a default base URL in a minor bump. A guardrail living in
configuration defends against none of those, because configuration is precisely what
changes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Final

import httpx
import structlog
import websockets
from websockets.asyncio.client import ClientConnection

from fking.platform.safety._allowlist import PERMITTED_HOSTS
from fking.platform.safety._errors import SafetyViolation
from fking.platform.safety._hostcheck import assert_permitted, inspect_url

_LOG: Final = structlog.get_logger(__name__)

# Named in every rejection this module produces, so a reader can tell which of the two
# compiled-in allowlists refused the request. See `_archive_allowlist.py` for why there
# are two and why they must never be merged.
_ALLOWLIST_NAME: Final[str] = "the permitted host set (PERMITTED_HOSTS)"

# The failure vocabulary of a transport this kernel constructed, named here because
# this is the only module allowed to know what that transport is. A supervisor that
# reconnects has to say which exceptions mean "the socket died" and which mean "stop",
# and without this it would either import `websockets` -- which an import-linter
# contract forbids outside the kernel -- or fall back to `except Exception`, which is
# the blanket handler `docs/rules/error-handling.md` exists to prevent.
#
# `OSError` covers the connect-time failures websockets does not wrap (DNS, refused,
# reset); `TimeoutError` covers `open_timeout` and any deadline a caller imposes.
# Deliberately no `Exception`: a `DataIntegrityError` from a malformed frame is not a
# transport failure and must not be retried into.
TRANSPORT_ERRORS: Final[tuple[type[Exception], ...]] = (
    websockets.exceptions.WebSocketException,
    OSError,
    TimeoutError,
)


def _inspect(url: str) -> tuple[str | None, str | None]:
    """Return `(normalised_host, rejection_reason)` against the trading allowlist.

    Kept as a non-raising form so that `verify_endpoints_or_abort` can collect every
    rejection without catching `SafetyViolation` -- which
    `tools/checks/no_catch_safety.py` forbids, and rightly: the moment one `except
    SafetyViolation` exists in the tree, the next one has a precedent to point at.
    """
    return inspect_url(url, permitted_hosts=PERMITTED_HOSTS, allowlist_name=_ALLOWLIST_NAME)


def assert_host_permitted(url: str | httpx.URL) -> str:
    """Validate one URL's host against the compiled-in trading allowlist.

    Returns the normalised host so callers can log what was actually contacted rather
    than what they believe they asked for. Raises `SafetyViolation` otherwise.
    """
    return assert_permitted(
        str(url), permitted_hosts=PERMITTED_HOSTS, allowlist_name=_ALLOWLIST_NAME
    )


async def _guard_httpx_request(request: httpx.Request) -> None:
    """Validate on the built `Request`, after URL merging.

    This is why validation is not done in the constructor. `httpx` merges a relative
    path against `base_url`, but an absolute URL passed to `client.get()` *replaces*
    it entirely -- so a client built for testnet will happily issue
    `GET https://api.binance.com/api/v3/order`, and a construction-time check sees
    nothing.
    """
    assert_host_permitted(request.url)


def guarded_client(*, base_url: str = "", timeout_seconds: float = 10.0) -> httpx.AsyncClient:
    """The only sanctioned way to construct an HTTP client in this process."""
    if base_url:
        assert_host_permitted(base_url)
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(timeout_seconds),
        # Proxy environment variables must not be able to reroute a validated host.
        # With trust_env=True the URL still reads testnet.binance.vision and the bytes
        # go wherever HTTPS_PROXY points.
        trust_env=False,
        # A redirect would be re-validated by the hook, so this is not about bypass.
        # It is provenance: an allowlisted redirect still changes which endpoint
        # answered, and reconciliation attributes the response to the host we asked.
        follow_redirects=False,
        event_hooks={"request": [_guard_httpx_request]},
    )


@asynccontextmanager
async def guarded_ws_connect(
    url: str, *, timeout_seconds: float = 10.0
) -> AsyncIterator[ClientConnection]:
    """The only sanctioned way to open a WebSocket in this process.

    A WebSocket URL is fixed at connect time and cannot be overridden per message, so
    one check is sufficient here -- unlike the HTTP case.
    """
    assert_host_permitted(url)
    async with websockets.connect(url, open_timeout=timeout_seconds) as connection:
        yield connection


def verify_endpoints_or_abort(configured_urls: Iterable[str]) -> None:
    """Startup gate. Called before the API server binds and before any scheduler runs.

    Aborts rather than degrading: a process that cannot prove where it will send
    orders must not accept work, and skipping the bad endpoint while keeping the good
    ones means the first evidence of a misconfiguration is a filled order.

    Every rejected endpoint is reported, not just the first. Fixing one and being told
    about the next on the following boot turns a misconfiguration into three deploys.
    """
    # Materialised once: `configured_urls` may be a generator, and iterating it twice
    # would validate an empty sequence the second time.
    urls = list(configured_urls)

    _LOG.info("safety_allowlist", permitted_hosts=sorted(PERMITTED_HOSTS), checked=len(urls))

    rejected: list[str] = []
    for url in urls:
        _, reason = _inspect(url)
        if reason is not None:
            rejected.append(f"{url} ({reason})")

    if rejected:
        _LOG.critical("safety_startup_abort", rejected=rejected)
        raise SafetyViolation("configured endpoints outside the allowlist: " + "; ".join(rejected))

    _LOG.info("safety_startup_ok", checked=len(urls))
