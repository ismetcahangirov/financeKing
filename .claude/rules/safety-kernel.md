# Rule — Safety Kernel

## The rule

**This system never trades real money. Not in development, not in testing, not "just once to verify", not behind a flag** (`../../CLAUDE.md` §0).

The mechanism, in full:

1. The set of permitted hosts is a **compiled-in `frozenset` literal** in `fking.platform.safety`. It is not read from configuration, environment, database, file, feature flag, CLI argument or remote service.
2. Every outbound HTTP and WebSocket connection to a **trading venue** is made through `guarded_client()` (or `guarded_ccxt()`, which wraps it). The host is validated **on every request**, not once at construction, because base URLs can be overridden per call.
3. The client is constructed with `trust_env=False` and `follow_redirects=False`.
4. At startup, every configured endpoint is resolved and checked. A single failure aborts the process before the event loop accepts work. The allowlist literal is logged at every boot.
5. `SafetyViolation` inherits `BaseException` and is **never caught**. There is no handler for it anywhere in `src/fking/`.
6. `fking.platform.safety` carries a **100% coverage floor** (`../../CLAUDE.md` §5).
7. Any diff touching the allowlist literal fails CI unless the pull request carries the `safety:critical` label.

There is no override. No flag, no environment variable, no `--force`, no `DRY_RUN=0`, no `if settings.i_know_what_im_doing`.

### The second egress path, and why it is not a widening

A non-venue host — today only `data.binance.vision`, the public archive — gets **its own compiled-in literal and its own client**, never an entry in `PERMITTED_HOSTS`. `ARCHIVE_HOSTS` lives in `fking.platform.safety._archive_allowlist` and is reached only by `guarded_archive_client()` in `fking.platform.safety.archive`, which holds no credential and cannot import one. The two sets are disjoint, each client refuses every host in the other's set, and `import-linter` forbids `fking.execution` from importing either archive module. Both literals are covered by the `safety-kernel-diff` CI job, so widening either still requires `safety:critical`.

The reason this is not clause 1 with a longer list: `PERMITTED_HOSTS` earns its value by being short, entirely venue endpoints, and reviewable in one glance. Once it also contains a data host, reviewing an addition means deciding which category the new entry falls into — and that judgement is made by whoever wants it added. The rule survives; the criterion for applying it moves. Everything below about refusing to widen an allowlist applies to `ARCHIVE_HOSTS` unchanged. ADR 0017 carries the full argument, including what the rejected one-list design got right.

The *checking* is deliberately not duplicated: `fking.platform.safety._hostcheck` holds one implementation of scheme validation, userinfo stripping and trailing-dot normalisation, and takes the permitted set as an argument. Two allowlists, one parser — a hardening fix lands once.

## Why

The threat model is not malice. It is four things that happen routinely in a healthy codebase (`../../ARCHITECTURE.md` §8):

- **A config edit.** Someone changes `BINANCE_BASE_URL` in a `.env` to debug a data question and forgets to change it back.
- **A copied environment variable.** A `.env.example` gets filled in from a personal account's real key material because that is the credential the person had.
- **An agent generating its own HTTP client.** This system will write its own strategies and adapters via LLM agents. An agent that needs to fetch something will `import httpx` and construct a client, because that is what the training data does. It has not read this file and will not.
- **A library changing a default base URL in a minor bump.** `ccxt` ships frequently; a sandbox flag whose semantics shift in a patch release is not a hypothetical.

**A guardrail living in configuration defends against none of those, because configuration is precisely what changes.** The allowlist has to sit somewhere that a config edit, an env var, a generated file and a dependency upgrade all cannot reach. The only such place is a source literal protected by code review.

`trust_env=False` closes the variant of the same hole one layer down: with `trust_env` on, `HTTPS_PROXY` in the environment silently reroutes every request through a host you never validated. The URL still says `testnet.binance.vision`; the bytes go somewhere else.

Per-request validation rather than construction-time validation closes the third variant: `httpx` merges a relative path against `base_url`, but an absolute URL passed to `client.get()` **replaces** it entirely. A client constructed against the testnet base URL will happily issue `GET https://api.binance.com/api/v3/order` if someone passes an absolute URL, and a construction-time check sees nothing.

`SafetyViolation` inherits `BaseException` because `../../CLAUDE.md` §4 forbids catching bare `Exception` — and because that rule will be violated eventually, somewhere, in a retry loop written at 2am. A safety violation swallowed by `except Exception: continue` becomes a retry against production. Inheriting `BaseException` means the ordinary defensive handlers in the codebase cannot absorb it.

## Incorrect

```python
# src/fking/execution/binance/rest.py
import httpx

from fking.platform.config import settings


class BinanceRestClient:
    def __init__(self) -> None:
        # "The URL comes from config, and config points at testnet."
        self._base_url = settings.binance_base_url
        if "testnet" not in self._base_url:
            raise ValueError(f"refusing non-testnet base url {self._base_url}")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=10.0)

    async def place_order(self, params: dict[str, str]) -> dict[str, object]:
        response = await self._client.post("/api/v3/order", params=params)
        response.raise_for_status()
        return response.json()

    async def server_time(self, url_override: str | None = None) -> dict[str, object]:
        response = await self._client.get(url_override or "/api/v3/time")
        return response.json()
```

Four failures, any one of which is sufficient:

- The substring check passes for `https://api.binance.com/?note=testnet` and for `https://testnet.binance.vision.attacker.example`. Substring matching on a URL is not host validation.
- `settings.binance_base_url` is read from the environment. A `HTTPS_PROXY` variable is also read from the environment, because `httpx.AsyncClient` defaults to `trust_env=True`. `place_order` then posts a signed order to whatever the proxy decides, and the client reports a 200.
- `server_time(url_override="https://api.binance.com/api/v3/time")` is validated by nothing. The check ran in `__init__`; this request never touches it. Today it is a read. After the next refactor that generalises `url_override` into a shared request helper, it is a write.
- `import httpx` inside `fking.execution` is exactly the import the `import-linter` contract forbids, and the class exists only because someone bypassed it.

At runtime the observable symptom is the worst possible one: nothing. The order fills, the response parses, the audit log records a successful demo trade, and the position is real.

## Correct

```python
# src/fking/platform/safety/_allowlist.py
"""The allowlist. This file is the safety kernel.

Any change here requires a pull request labelled `safety:critical` and is
blocked in CI otherwise. Do not add a host because it would make testing
easier. Do not add a host "read-only". See .claude/rules/safety-kernel.md.
"""

from __future__ import annotations

from typing import Final

PERMITTED_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "testnet.binance.vision",  # spot testnet REST + WS session.logon
        "stream.testnet.binance.vision",  # spot testnet market-data WS
        "testnet.binancefuture.com",  # USD-M futures testnet REST
        "stream.binancefuture.com",  # USD-M futures testnet user-data WS
        "api-testnet.bybit.com",  # fallback venue REST (ARCHITECTURE.md §7)
        "stream-testnet.bybit.com",  # fallback venue WS
    }
)
```

```python
# src/fking/platform/safety/client.py
from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace
from typing import Final
from urllib.parse import urlsplit

import aiohttp
import httpx
import structlog

from fking.platform.safety._allowlist import PERMITTED_HOSTS

_LOG: Final = structlog.get_logger(__name__)


class SafetyViolation(BaseException):
    """A request targeted a host outside the compiled-in allowlist.

    Inherits BaseException rather than Exception so that a defensive
    `except Exception` anywhere in the codebase cannot absorb it and retry.
    Nothing in src/fking/ catches this. Nothing ever will.
    """


def assert_host_permitted(url: str | httpx.URL) -> str:
    """Validate a single URL's host against the compiled-in allowlist."""
    parts = urlsplit(str(url))
    if parts.scheme not in {"https", "wss"}:
        raise SafetyViolation(f"non-TLS scheme {parts.scheme!r} in {url!s}")
    host = parts.hostname
    if host is None:
        raise SafetyViolation(f"no host in {url!s}")
    normalised = host.rstrip(".").casefold()  # a trailing dot is a distinct FQDN
    if normalised not in PERMITTED_HOSTS:
        raise SafetyViolation(
            f"host {normalised!r} is not in the compiled-in allowlist "
            f"{sorted(PERMITTED_HOSTS)}"
        )
    return normalised


async def _guard_httpx_request(request: httpx.Request) -> None:
    assert_host_permitted(request.url)


def guarded_client(
    *, base_url: str = "", timeout_seconds: float = 10.0
) -> httpx.AsyncClient:
    """The only sanctioned way to construct an HTTP client in this process."""
    if base_url:
        assert_host_permitted(base_url)
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(timeout_seconds),
        # Proxy environment variables must not be able to reroute a validated
        # host. With trust_env=True the URL is still testnet and the bytes are
        # not.
        trust_env=False,
        # A redirect would be re-validated by the hook, but a redirect that IS
        # allowlisted still changes which endpoint answered, which breaks
        # reconciliation provenance. Fail instead of following.
        follow_redirects=False,
        event_hooks={"request": [_guard_httpx_request]},
    )


def guarded_aiohttp_session() -> aiohttp.ClientSession:
    """aiohttp equivalent, used to inject a guarded transport into ccxt."""

    async def _on_request_start(
        session: aiohttp.ClientSession,
        context: SimpleNamespace,
        params: aiohttp.TraceRequestStartParams,
    ) -> None:
        assert_host_permitted(str(params.url))

    trace = aiohttp.TraceConfig()
    trace.on_request_start.append(_on_request_start)
    return aiohttp.ClientSession(trust_env=False, trace_configs=[trace])


def verify_endpoints_or_abort(configured_urls: Iterable[str]) -> None:
    """Startup gate. Called before the API server binds and before any scheduler starts.

    Aborts the process rather than degrading: a process that cannot prove where
    it will send orders must not accept work.
    """
    _LOG.info("safety_allowlist", permitted_hosts=sorted(PERMITTED_HOSTS))
    rejected: list[str] = []
    for url in configured_urls:
        try:
            assert_host_permitted(url)
        except SafetyViolation as violation:
            rejected.append(f"{url} -> {violation}")
    if rejected:
        _LOG.critical("safety_startup_abort", rejected=rejected)
        raise SafetyViolation(
            "configured endpoints outside the allowlist: " + "; ".join(rejected)
        )
    _LOG.info("safety_startup_ok", checked=len(list(configured_urls)))
```

The difference that matters: `_guard_httpx_request` runs on the `httpx.Request` object *after* URL merging, so it sees the final absolute URL regardless of whether it came from `base_url`, a relative path, or an absolute override passed at the call site. `server_time(url_override="https://api.binance.com/...")` now raises `SafetyViolation` before a socket is opened, and no handler in the codebase will catch it.

## ccxt is not exempt

`ccxt` is the exchange client (`../../ARCHITECTURE.md` §7) and it constructs its own transport by default. Left alone, it is a complete bypass of everything above: `import-linter` sees `fking.execution` importing `ccxt`, not `aiohttp`, and the allowlist never runs.

```python
# src/fking/platform/safety/exchange.py
from __future__ import annotations

import ccxt.async_support as ccxt

from fking.platform.safety.client import (
    SafetyViolation,
    assert_host_permitted,
    guarded_aiohttp_session,
)


def guarded_ccxt(exchange_id: str, api_key: str, secret: str) -> ccxt.Exchange:
    """Construct a ccxt exchange whose every request passes the allowlist."""
    factory = getattr(ccxt, exchange_id, None)
    if factory is None:
        raise SafetyViolation(f"unknown ccxt exchange id {exchange_id!r}")
    exchange: ccxt.Exchange = factory(
        {
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            # Injecting the guarded session is what makes the trace hook fire on
            # every ccxt request, including ones ccxt issues internally such as
            # load_markets and listenKey keepalives.
            "session": guarded_aiohttp_session(),
        }
    )
    exchange.set_sandbox_mode(True)
    # set_sandbox_mode swaps exchange.urls to the vendor's test endpoints. Those
    # endpoints are supplied by the dependency, so they are re-validated here
    # rather than trusted: a base URL changing in a minor bump is in the threat
    # model.
    for endpoint in _iter_url_strings(exchange.urls):
        assert_host_permitted(endpoint)
    return exchange
```

`_iter_url_strings` walks the nested `urls` mapping and yields every string that parses as an absolute URL. If `ccxt` ever ships a sandbox map containing a production host, the process refuses to construct the exchange rather than discovering it on the first order.

## Enforcement

**`import-linter` contracts** (`pyproject.toml`). Note `allow_indirect_imports = true`: `fking.execution` *does* reach `httpx` indirectly through `fking.platform.safety`, which is the entire point. Leaving it at the default `false` would fail the contract on the sanctioned path and force someone to weaken it.

```toml
[[tool.importlinter.contracts]]
name = "network clients are reachable only through the safety kernel"
type = "forbidden"
source_modules = [
    "fking.agents",
    "fking.api",
    "fking.backtest",
    "fking.data",
    "fking.domain",
    "fking.evolution",
    "fking.execution",
    "fking.risk",
    "fking.strategy",
]
forbidden_modules = [
    "httpx",
    "aiohttp",
    "websockets",
    "requests",
    "urllib.request",
    "http.client",
]
# Only DIRECT imports are forbidden. The sanctioned path is
# fking.execution -> fking.platform.safety -> httpx, which is an indirect
# import and must remain legal.
allow_indirect_imports = true

[[tool.importlinter.contracts]]
name = "inside platform, only the safety kernel touches a network client"
type = "forbidden"
source_modules = [
    "fking.platform.bus",
    "fking.platform.config",
    "fking.platform.logging",
    "fking.platform.persistence",
    "fking.platform.telemetry",
]
forbidden_modules = ["httpx", "aiohttp", "websockets", "requests", "urllib.request"]
allow_indirect_imports = true
```

**`ruff` banned API** catches the same construction one call earlier, with a message that says what to do instead. `TID251` must be in `select`.

```toml
[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "A", "C4", "TID", "TID251", "RUF"]

[tool.ruff.lint.flake8-tidy-imports.banned-api]
"httpx.AsyncClient".msg = "Use fking.platform.safety.guarded_client()."
"httpx.Client".msg = "Use fking.platform.safety.guarded_client()."
"aiohttp.ClientSession".msg = "Use fking.platform.safety.guarded_aiohttp_session()."
"websockets.connect".msg = "Use fking.platform.safety.guarded_ws_connect()."
"requests.get".msg = "requests is not used in this codebase."

[tool.ruff.lint.per-file-ignores]
# The kernel is the one place allowed to construct the real clients.
"src/fking/platform/safety/*.py" = ["TID251"]
```

**Tests** (`tests/platform/safety/`, all of which must pass for `make check` to be green):

```python
import pytest

from fking.platform.safety import (
    PERMITTED_HOSTS,
    SafetyViolation,
    assert_host_permitted,
    guarded_client,
    verify_endpoints_or_abort,
)

PRODUCTION_HOSTS = [
    "api.binance.com",
    "api1.binance.com",
    "api-gcp.binance.com",
    "fapi.binance.com",
    "dapi.binance.com",
    "stream.binance.com",
    "fstream.binance.com",
    "api.bybit.com",
    "stream.bybit.com",
]


@pytest.mark.parametrize("host", PRODUCTION_HOSTS)
async def test_production_hosts_are_rejected(host: str) -> None:
    async with guarded_client() as client:
        with pytest.raises(SafetyViolation):
            await client.get(f"https://{host}/api/v3/time")


@pytest.mark.parametrize(
    "url",
    [
        "https://testnet.binance.vision.attacker.example/api/v3/time",
        "https://api.binance.com/?note=testnet.binance.vision",
        "https://TESTNET.BINANCE.VISION@api.binance.com/api/v3/time",
        "https://api.binance.com./api/v3/time",
        "http://testnet.binance.vision/api/v3/time",
    ],
)
def test_lookalike_and_downgrade_urls_are_rejected(url: str) -> None:
    with pytest.raises(SafetyViolation):
        assert_host_permitted(url)


async def test_per_request_absolute_url_overrides_the_base_url_and_is_still_checked():
    """The failure mode a construction-time check cannot see."""
    async with guarded_client(base_url="https://testnet.binance.vision") as client:
        with pytest.raises(SafetyViolation):
            await client.get("https://api.binance.com/api/v3/time")


async def test_proxy_environment_cannot_reroute_a_validated_host(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    async with guarded_client() as client:
        assert client.trust_env is False


def test_startup_aborts_on_a_single_bad_endpoint() -> None:
    with pytest.raises(SafetyViolation, match="outside the allowlist"):
        verify_endpoints_or_abort(
            ["https://testnet.binance.vision", "https://api.binance.com"]
        )


def test_allowlist_is_exactly_the_expected_literal() -> None:
    """A golden test. Widening the allowlist must break a test, not just pass review."""
    assert PERMITTED_HOSTS == frozenset(
        {
            "testnet.binance.vision",
            "stream.testnet.binance.vision",
            "testnet.binancefuture.com",
            "stream.binancefuture.com",
            "api-testnet.bybit.com",
            "stream-testnet.bybit.com",
        }
    )


def test_safety_violation_survives_a_bare_except_exception() -> None:
    with pytest.raises(SafetyViolation):
        try:
            assert_host_permitted("https://api.binance.com")
        except Exception:  # noqa: BLE001 - the point of the test
            pytest.fail("SafetyViolation was absorbed by an Exception handler")
```

**Coverage gate at 100%**, run as its own step so the kernel's floor cannot be subsidised by well-tested utilities elsewhere:

```makefile
safety-coverage:
	uv run pytest tests/platform/safety --cov=fking.platform.safety --cov-report=term-missing
	uv run coverage report --include='src/fking/platform/safety/*' --fail-under=100
```

**CI check on the allowlist literal** (`.github/workflows/check.yml`):

```yaml
- name: Allowlist changes require the safety:critical label
  env:
    GH_TOKEN: ${{ github.token }}
  run: |
    if git diff --quiet origin/${{ github.base_ref }}...HEAD \
        -- src/fking/platform/safety/_allowlist.py; then
      echo "allowlist unchanged"
      exit 0
    fi
    labels="$(gh pr view ${{ github.event.pull_request.number }} \
              --json labels --jq '.labels[].name')"
    if ! printf '%s\n' "$labels" | grep -qx 'safety:critical'; then
      echo "::error file=src/fking/platform/safety/_allowlist.py::allowlist changed \
without the safety:critical label"
      exit 1
    fi
```

The golden test and the CI label check are deliberately redundant. The test makes the change visible to the author; the label check makes it visible to the reviewer. Removing either one is itself a change to the safety kernel.

## The one exception

**None.**

Not for read-only endpoints. Not for a market-data feed that "does not have credentials attached". Not for a one-off script outside `src/`. Not temporarily, behind a branch that will never merge.

The argument that arrives most often is: *let me hit mainnet read-only to calibrate the cost model, since `../../CLAUDE.md` §2 requires production-calibrated costs anyway and testnet showed a 7.5bp spread against production's 0.16bp.* The requirement is real; the conclusion is wrong. Production market data is obtained from the public historical archives — fetched and checksum-verified over the separate, credential-free archive egress path described above, then loaded from Parquet — not by opening a live client against a production *exchange* host from a process that also holds order-placement code paths. Note what the archive path is not: it is not `PERMITTED_HOSTS` with an extra entry, and it cannot sign a request, so it is not a route to an authenticated endpoint even by accident. **Read paths become write paths during refactors** (`../../CLAUDE.md` §11): a `_request()` helper written for klines gets a `method` parameter six weeks later, then a signing step, and nobody re-derives the safety property because the allowlist already had the host in it.

The second argument is that the friction is slowing work down. It is. That is the feature. Widening the allowlist costs a source edit, a `safety:critical` pull request, a broken golden test, and a human reviewer — four separate moments where somebody has to state out loud that they intend this system to be able to reach a production exchange. A guardrail with an exception is a guardrail with a documented procedure for turning it off, and the procedure is always used by someone in a hurry.

If you find yourself writing code that would widen the allowlist, or adding an override so it can be tested more easily: **stop and ask the user** (`../../CLAUDE.md` §0).
