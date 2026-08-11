"""ccxt construction, inside the kernel because ccxt builds its own transport.

`ccxt` is the only exchange client in this repository (ADR-0008), and left alone it is
a complete bypass of everything in `client.py`: `import-linter` sees
`fking.execution -> ccxt`, not `fking.execution -> aiohttp`, so the allowlist never
runs. Putting the constructor here is what makes the bypass unavailable -- an
`import-linter` contract forbids `fking.execution` from importing `ccxt` at all, so the
only way an adapter gets an exchange object is by asking for one that already carries a
guarded transport.

Three separate mechanisms, in the order they fire:

1. **The injected session.** `guarded_aiohttp_session()` carries an `aiohttp`
   `TraceConfig` whose `on_request_start` validates the resolved URL. That hook runs
   before the connector opens a socket, and it fires on requests `ccxt` issues
   internally -- `load_markets`, `listenKey` keepalives, the time sync that
   `adjustForTimeDifference` performs -- which no call-site check could reach.
2. **The `urls` re-walk after `set_sandbox_mode(True)`.** Those endpoints are supplied
   by the dependency, and "a library changing a default base URL in a minor bump" is
   item 4 of the threat model in ARCHITECTURE.md section 8. If a release ever ships a
   sandbox map containing a production host, the process refuses to construct the
   exchange rather than discovering it on the first order.
3. **The raw-text return.** `call()` hands back the venue's response *text*, never
   ccxt's parsed structure. ccxt decodes JSON with the stdlib decoder and therefore
   hands back `float` for every price and quantity, and a value that has passed
   through a float is not repairable by widening the type afterwards
   (docs/rules/decimal-and-money.md). Returning text is what makes
   `json.loads(body, parse_float=Decimal)` the only way the adapter can read a number.

`set_sandbox_mode(True)` is applied and is *not* the safety mechanism. It is a
convenience supplied by a dependency; the safety mechanism is (1) and (2).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
from typing import Final, Protocol, cast, runtime_checkable

import aiohttp

# ccxt ships no py.typed marker, so mypy cannot see its signatures. The import is
# confined to this module and every value taken from it is immediately narrowed to
# `_CcxtExchange` below, so no Any escapes into the rest of the tree.
import ccxt.async_support as ccxt_async
import structlog

from fking.platform.safety._errors import SafetyViolation
from fking.platform.safety.client import assert_host_permitted

__all__ = [
    "GuardedCcxtExchange",
    "GuardedExchange",
    "UnknownVenueEndpointError",
    "VenueResponseMetadata",
    "VenueResponseRecorder",
    "VenueTransportError",
    "assert_sandbox_urls_permitted",
    "guarded_aiohttp_session",
    "guarded_ccxt",
]

_LOG: Final = structlog.get_logger(__name__)

# ccxt resolves every request URL from `urls["api"][<api group>]`. `set_sandbox_mode`
# moves the production map to `urls["apiBackup"]` and promotes `urls["test"]` into
# `urls["api"]`, so after sandbox mode `apiBackup` *is expected* to hold production
# hosts and validating it would refuse every correct construction. The other excluded
# keys are documentation links -- `www` is https://www.binance.com, `fees` is the fee
# schedule page -- which are never fetched.
#
# Excluding them is not a hole: mechanism (1) validates whatever URL a request actually
# resolves to, whichever key it came from. This walk is the belt, the trace hook is the
# braces, and the walk's job is to make a bad sandbox map a construction-time failure
# rather than a first-order-time one.
_NON_ENDPOINT_URL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "apiBackup",  # the pre-sandbox production map, saved by set_sandbox_mode
        "api_management",
        "demo",  # ccxt's separate demo-trading map; unreachable unless enabled
        "doc",
        "docs",
        "fees",
        "logo",
        "referral",
        "www",
    }
)

# Anything that is not one of the excluded keys above is treated as an endpoint map and
# validated. A ccxt release that adds a new informational key therefore fails
# construction loudly rather than silently widening what goes unchecked -- which is the
# direction to fail in, and the fix is a one-line reviewed addition to the set above.


class _CcxtExchange(Protocol):
    """The slice of `ccxt.Exchange` this module uses.

    Declared rather than imported because ccxt is untyped: narrowing the constructed
    object to this Protocol is what stops `Any` propagating out of the import above.
    """

    id: str
    urls: dict[str, object]
    last_http_response: str | None

    def set_sandbox_mode(self, enabled: bool) -> None: ...

    async def close(self) -> None: ...


# A ccxt implicit-API method: `publicGetExchangeInfo`, `privatePostOrder`, and so on.
_ImplicitMethod = Callable[[Mapping[str, str]], Awaitable[object]]


class UnknownVenueEndpointError(Exception):
    """An endpoint name that the constructed ccxt exchange does not implement.

    A programming error rather than a venue failure -- the name came from a
    compiled-in endpoint map, not from a response -- so it is deliberately not an
    `ExchangeError`: retrying it would never help.
    """


class VenueTransportError(Exception):
    """The request failed before the venue produced a body.

    A DNS failure, a refused connection, a read timeout. Deliberately distinct from a
    venue *rejection*, which arrives as a response and is classified in
    `fking.execution` against the venue's own error codes -- this module holds
    mechanism and gets no trading vocabulary, so it can say "nothing came back" and
    nothing more.

    It carries the ccxt class name rather than the exception object because the object
    is a `ccxt` type, and letting one escape into `fking.execution` would hand that
    package a ccxt dependency through the exception channel.
    """

    def __init__(self, message: str, *, venue_id: str, cause_type: str) -> None:
        super().__init__(message)
        self.venue_id = venue_id
        self.cause_type = cause_type


@dataclass(frozen=True, slots=True)
class VenueResponseMetadata:
    """What the transport saw, as distinct from what the venue said.

    The status code and the response headers are the two facts a rate-limit throttle
    cannot work without and that `call()` structurally cannot carry: it returns the
    response *body*, and Binance sends the identical body -- `{"code":-1003,...}` --
    for a `429` rate-limit refusal and for a `418` IP ban. Those demand opposite
    responses (honour `Retry-After` versus hard stop), so a component that can only see
    the body cannot tell a throttling incident from an outage.

    Header names are lower-cased at construction because HTTP header names are
    case-insensitive and Binance is not consistent about `X-MBX-USED-WEIGHT-1M` versus
    `x-mbx-used-weight-1m` across its spot and futures fleets. A lookup that matched on
    the wrong case would read "no weight header" and the throttle would run blind.

    Mechanism, not policy: this module states what came back and has no opinion about
    what it means. `fking.execution` owns the interpretation.
    """

    http_status: int
    headers: Mapping[str, str]

    @classmethod
    def of(cls, *, http_status: int, headers: Mapping[str, str]) -> VenueResponseMetadata:
        """Build one with header names normalised and the mapping made read-only."""
        return cls(
            http_status=http_status,
            headers=MappingProxyType({name.lower(): value for name, value in headers.items()}),
        )


class VenueResponseRecorder:
    """Holds the transport facts about the most recent response on one session.

    A single slot rather than a history: the throttle reads it immediately after the
    call it issued, and a buffer would invite a consumer to correlate entries with
    requests, which this class has no way to do correctly under concurrency. One slot
    makes the narrow contract obvious -- "the last response this session saw" -- and a
    caller that needs more has to say so.
    """

    __slots__ = ("_latest",)

    def __init__(self) -> None:
        self._latest: VenueResponseMetadata | None = None

    @property
    def latest(self) -> VenueResponseMetadata | None:
        """The most recent response's status and headers, or `None` before the first."""
        return self._latest

    def record(self, metadata: VenueResponseMetadata) -> None:
        self._latest = metadata


def _record_response_metadata(
    recorder: VenueResponseRecorder,
) -> Callable[
    [aiohttp.ClientSession, SimpleNamespace, aiohttp.TraceRequestEndParams], Awaitable[None]
]:
    """An `on_request_end` hook that files the status and headers with `recorder`.

    Bound to the session rather than read off the ccxt exchange afterwards, because
    ccxt only records headers when `enableLastResponseHeaders` is left on and records no
    status code at all -- so the fact a ban detector depends on would be a library
    default somebody could flip in a config bump.
    """

    async def hook(
        _session: aiohttp.ClientSession,
        _context: SimpleNamespace,
        params: aiohttp.TraceRequestEndParams,
    ) -> None:
        recorder.record(
            VenueResponseMetadata.of(
                http_status=params.response.status, headers=params.response.headers
            )
        )

    return hook


async def _reject_redirect(
    _session: aiohttp.ClientSession,
    _context: SimpleNamespace,
    params: aiohttp.TraceRequestRedirectParams,
) -> None:
    """Refuse redirects outright, matching `guarded_client`'s `follow_redirects=False`.

    A redirect target would be re-validated by `on_request_start` anyway, so this is
    not about bypass. It is provenance: an allowlisted redirect still changes which
    endpoint answered, and reconciliation attributes a response to the host it asked.
    """
    raise SafetyViolation(
        f"{params.url} redirected; the guarded transport does not follow redirects, "
        f"because the endpoint that answers must be the endpoint that was asked"
    )


async def _guard_aiohttp_request(
    _session: aiohttp.ClientSession,
    _context: SimpleNamespace,
    params: aiohttp.TraceRequestStartParams,
) -> None:
    """Validate the resolved URL before the connector is asked for a socket.

    `on_request_start` fires after aiohttp has built the final URL and before
    `TCPConnector.connect`, which is what makes "no socket is opened" true rather than
    merely likely -- and it is the only place a check can see a URL that ccxt built
    from a base it was handed by the library rather than by us.
    """
    assert_host_permitted(str(params.url))


def guarded_aiohttp_session(
    *, timeout_seconds: float = 10.0, recorder: VenueResponseRecorder | None = None
) -> aiohttp.ClientSession:
    """The only sanctioned way to construct an `aiohttp` session in this process.

    Must be called from inside a running event loop: `aiohttp` binds the session to the
    loop at construction, and a session built on a loop that later closes fails at the
    first request with an error that names neither the loop nor the session.

    `recorder`, when supplied, receives the status and headers of every response the
    session sees -- including responses to requests ccxt issues internally, which is the
    same reason the host check lives on the trace config rather than at the call site.
    """
    trace = aiohttp.TraceConfig()
    trace.on_request_start.append(_guard_aiohttp_request)
    trace.on_request_redirect.append(_reject_redirect)
    if recorder is not None:
        trace.on_request_end.append(_record_response_metadata(recorder))
    return aiohttp.ClientSession(
        # Proxy environment variables must not be able to reroute a validated host.
        # With trust_env=True the URL still reads testnet.binance.vision and the bytes
        # go wherever HTTPS_PROXY points.
        trust_env=False,
        timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        trace_configs=[trace],
    )


def _iter_url_strings(node: object) -> Iterator[str]:
    """Yield every string in a nested `urls` structure."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, Mapping):
        for child in node.values():
            yield from _iter_url_strings(child)
    elif isinstance(node, list | tuple):
        for child in node:
            yield from _iter_url_strings(child)


def assert_sandbox_urls_permitted(urls: Mapping[str, object]) -> tuple[str, ...]:
    """Validate every endpoint URL a constructed exchange could resolve a request from.

    Returns the validated URLs so a caller can log what the venue actually resolved to,
    rather than what its configuration claims. Raises `SafetyViolation` on the first
    endpoint outside the allowlist.
    """
    validated: list[str] = []
    for key, node in urls.items():
        if key in _NON_ENDPOINT_URL_KEYS:
            continue
        for url in _iter_url_strings(node):
            assert_host_permitted(url)
            validated.append(url)
    return tuple(validated)


@runtime_checkable
class GuardedExchange(Protocol):
    """The venue network surface, as `fking.execution` sees it.

    This Protocol exists so that `fking.execution` never imports `ccxt` -- if it could,
    it could call `ccxt.binance(...)` and get an exchange with its own unguarded
    transport, which is the whole failure this module prevents.

    Everything policy-shaped stays on the far side. `call` returns response *text* and
    has no opinion about what the text means, which is also what keeps every `Decimal`
    in the system constructed from the venue's own characters.
    """

    @property
    def venue_id(self) -> str:
        """The profile id this exchange was constructed for."""

    @property
    def request_count(self) -> int:
        """Requests issued over this exchange since construction.

        Exposed because "the adapter went to the venue" is otherwise unfalsifiable: a
        replayed fixture and a live call return identical text.
        """

    @property
    def last_response_metadata(self) -> VenueResponseMetadata | None:
        """Status and headers of the most recent response, or `None` before the first.

        Part of the interface rather than an implementation detail of the ccxt variant,
        because the rate-limit throttle reads it after every call and a transport that
        could not answer would leave the throttle running on its own estimate with no
        correction from the venue.
        """

    async def call(self, endpoint: str, params: Mapping[str, str]) -> str:
        """Invoke a venue endpoint and return the raw response body.

        `endpoint` is a ccxt implicit-API method name such as `publicGetExchangeInfo`.
        The body is returned verbatim -- not re-serialised, not parsed -- so that a
        `Decimal` built from it is a claim about the venue's formatting.
        """

    async def aclose(self) -> None:
        """Release the transport. Safe to call more than once."""


class GuardedCcxtExchange:
    """`GuardedExchange` over a `ccxt` exchange whose transport this module injected."""

    def __init__(
        self,
        exchange: _CcxtExchange,
        session: aiohttp.ClientSession,
        venue_id: str,
        recorder: VenueResponseRecorder,
    ) -> None:
        self._exchange = exchange
        self._session = session
        self._venue_id = venue_id
        self._recorder = recorder
        self._request_count = 0

    @property
    def venue_id(self) -> str:
        return self._venue_id

    @property
    def request_count(self) -> int:
        return self._request_count

    @property
    def last_response_metadata(self) -> VenueResponseMetadata | None:
        return self._recorder.latest

    async def call(self, endpoint: str, params: Mapping[str, str]) -> str:
        method = getattr(self._exchange, endpoint, None)
        if method is None or not callable(method):
            raise UnknownVenueEndpointError(
                f"{self._venue_id}: ccxt exchange {self._exchange.id!r} implements no "
                f"endpoint named {endpoint!r}"
            )
        # Cleared first: ccxt leaves the previous call's body in place, so a transport
        # failure would otherwise return the *last successful* response and the adapter
        # would parse a stale success as this call's answer.
        self._exchange.last_http_response = None
        self._request_count += 1
        failure: BaseException | None = None
        try:
            await cast(_ImplicitMethod, method)(dict(params))
        except ccxt_async.BaseError as raised:
            # ccxt turns a venue rejection into one of its own exception types, but it
            # records the body first. A rejection is a *response*, and classifying it
            # belongs to `fking.execution` against the venue's own error codes -- so the
            # body is returned and the ccxt type is discarded rather than leaked.
            failure = raised

        # ccxt records the response body before it raises. Reading the text rather than
        # the return value is the whole point: the return value has already been through
        # `json.loads` with the stdlib float parser, so every price in it is a float.
        # Annotated rather than inferred: mypy narrows the attribute to None from the
        # assignment above and would call the success branch unreachable, which is the
        # opposite of true -- ccxt sets it inside the request.
        body: str | None = self._exchange.last_http_response
        if isinstance(body, str):
            return body
        if failure is None:
            raise UnknownVenueEndpointError(
                f"{self._venue_id}: {endpoint} recorded no response body; ccxt's "
                f"enableLastHttpResponse must stay on for the Decimal path to work"
            )
        raise VenueTransportError(
            f"{self._venue_id}: {endpoint} produced no response body",
            venue_id=self._venue_id,
            cause_type=type(failure).__name__,
        ) from failure

    async def aclose(self) -> None:
        # ccxt sets own_session=False when a session is injected, so `close()` releases
        # its own resources and leaves ours alone -- which means we must close ours.
        await self._exchange.close()
        await self._session.close()


# PLR0913: every parameter is a distinct axis of the construction -- which library, which
# profile it is being built for, two credentials, venue options, and a deadline. A config
# object would hide exactly the arguments a reviewer of a safety-critical constructor
# needs to see at the call site.
async def guarded_ccxt(  # noqa: PLR0913
    *,
    ccxt_exchange_id: str,
    venue_id: str,
    api_key: str | None = None,
    secret: str | None = None,
    options: Mapping[str, str | bool] | None = None,
    timeout_seconds: float = 10.0,
) -> GuardedCcxtExchange:
    """Construct a ccxt exchange whose every request passes the host allowlist.

    A coroutine rather than a plain function because `aiohttp` binds a session to the
    running loop at construction; building one outside a loop produces a session that
    fails later, somewhere else, with an error naming neither.
    """
    factory = getattr(ccxt_async, ccxt_exchange_id, None)
    if factory is None or not callable(factory):
        raise SafetyViolation(
            f"unknown ccxt exchange id {ccxt_exchange_id!r}; refusing to construct a "
            f"client whose endpoints cannot be resolved and therefore cannot be checked"
        )

    recorder = VenueResponseRecorder()
    session = guarded_aiohttp_session(timeout_seconds=timeout_seconds, recorder=recorder)
    config: dict[str, object] = {
        "apiKey": api_key,
        "secret": secret,
        "enableRateLimit": True,
        "timeout": int(timeout_seconds * 1000),
        "options": dict(options or {}),
        # Injecting the session is what makes the trace hook fire on requests ccxt
        # issues internally. It also sets ccxt's own_session=False, so ccxt neither
        # creates nor closes a transport of its own.
        "session": session,
    }
    exchange = cast(_CcxtExchange, factory(config))
    exchange.set_sandbox_mode(True)

    endpoints = assert_sandbox_urls_permitted(exchange.urls)
    _LOG.info(
        "safety.venue_endpoints_validated",
        venue_id=venue_id,
        ccxt_exchange_id=ccxt_exchange_id,
        endpoint_count=len(endpoints),
        hosts=sorted({assert_host_permitted(url) for url in endpoints}),
    )
    return GuardedCcxtExchange(exchange, session, venue_id, recorder)
