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
from urllib.parse import urlsplit

import httpx
import structlog
import websockets
from websockets.asyncio.client import ClientConnection

from fking.platform.safety._allowlist import PERMITTED_HOSTS
from fking.platform.safety._errors import SafetyViolation

_LOG: Final = structlog.get_logger(__name__)

# TLS only. A downgrade to plaintext puts the API key on the wire, and the host check
# alone would accept it because the host is right.
_PERMITTED_SCHEMES: Final[frozenset[str]] = frozenset({"https", "wss"})


def _inspect(url: str) -> tuple[str | None, str | None]:
    """Return `(normalised_host, rejection_reason)`; exactly one is None.

    Split out from `assert_host_permitted` so that `verify_endpoints_or_abort` can
    collect every rejection without catching `SafetyViolation` -- which
    `tools/checks/no_catch_safety.py` forbids, and rightly: the moment one `except
    SafetyViolation` exists in the tree, the next one has a precedent to point at.
    """
    parts = urlsplit(url)

    if parts.scheme not in _PERMITTED_SCHEMES:
        return None, (
            f"non-TLS scheme {parts.scheme!r} in {url}; "
            f"permitted schemes are {sorted(_PERMITTED_SCHEMES)}"
        )

    # `hostname` rather than `netloc`: it drops userinfo and the port, so
    # `https://testnet.binance.vision@api.binance.com/` yields api.binance.com --
    # which is the host that actually answers, and the one a netloc comparison would
    # get wrong.
    host = parts.hostname
    if not host:
        return None, f"no host in {url}"

    # A trailing dot makes the DNS root label explicit; `example.com.` and
    # `example.com` are the same name. Stripping it before comparison stops
    # `api.binance.com.` from being treated as an unrecognised -- and therefore
    # separately evaluated -- host. urlsplit already lowercases; casefold is
    # belt-and-braces for non-ASCII.
    normalised = host.rstrip(".").casefold()
    if normalised not in PERMITTED_HOSTS:
        return None, (
            f"host {normalised!r} is not in the permitted host set "
            f"(the compiled-in allowlist is {sorted(PERMITTED_HOSTS)})"
        )
    return normalised, None


def assert_host_permitted(url: str | httpx.URL) -> str:
    """Validate one URL's host against the compiled-in allowlist.

    Returns the normalised host so callers can log what was actually contacted rather
    than what they believe they asked for. Raises `SafetyViolation` otherwise.
    """
    host, reason = _inspect(str(url))
    if host is None:
        # Narrowing on `host` rather than on `reason`, so the return type follows
        # without an assert -- an assert here would vanish under `python -O` and take
        # the guarantee with it, which is what S101 exists to prevent.
        raise SafetyViolation(reason or f"unvalidatable url {url!s}")
    return host


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
