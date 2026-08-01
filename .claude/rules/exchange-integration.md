# Rule — Exchange Integration

## The rule

1. **`ccxt` >= 4.5.70 is the only exchange client.** No `python-binance`, no `binance-connector`, no `binance-sdk-*`, no hand-rolled REST. The reasoning is in `../../ARCHITECTURE.md` §7 and is not re-litigated per module.
2. **Every request — REST and WebSocket, read and write — goes through `fking.platform.safety.guarded_client()`**, which validates the host on each call rather than at construction, because `ccxt` accepts a per-call base URL override.
3. **The two user-data mechanisms are modelled as two implementations of one interface, not as one implementation with a branch.** Spot: Ed25519 `session.logon` over the WebSocket API followed by `userDataStream.subscribe`. Futures: `listenKey` plus a keepalive. `POST /api/v3/userDataStream` returns **410 Gone** on testnet and production alike; code that calls it is dead.
4. **Exchange responses are hostile input.** Parse into a Pydantic v2 model with `extra="ignore"` and explicit types; take every `Decimal` from the raw *string* field; never index optimistically. `response["orderId"]` is a bug.
5. **Symbol parsing is Unicode-safe and round-trip exact.** Testnet `exchangeInfo` contains a deliberate non-ASCII symbol. Whatever code points the venue sent, you send back unchanged.
6. **`clientOrderId` is the idempotency key and is derived deterministically** from the correlation id — never random, never a counter. See `./idempotency.md`.
7. **Reconcile on connect, on every reconnect, and on a timer.** Exchange state is the source of truth; local state converges to it.
8. **Every venue-varying number lives in a venue profile, never in code.** Order-rate budget, `recvWindow`, keepalive interval, `clientOrderId` charset and length, timestamp unit.
9. **Cost-model parameters are never calibrated from testnet.** Futures testnet showed a 7.5bp spread against production's 0.16bp with roughly 10x inflated volume.
10. **The tradable symbol universe is an intersection, computed at startup, never assumed.** Spot testnet is missing 79 symbols present in production; futures testnet is missing 189.

## Why

Testnet is not a smaller production. It is a different exchange that shares an API shape, and every one of the clauses above exists because that difference has a specific way of reaching the strategy layer disguised as truth.

The universe intersection matters most and is the least obvious. Testnet is *not a subset* of production — each side has symbols the other lacks — so both of these are wrong: assuming a backtest symbol is tradable on testnet, and assuming a testnet symbol has production history. A strategy validated on production archives and deployed against a symbol testnet does not list will silently produce zero fills and be scored as a strategy with no edge, which retires a good strategy for an infrastructure reason. The intersection is computed against the **public data archive manifest**, not a live production API call — the archive is a data host, and the trading allowlist is not widened to reach it (`../../ARCHITECTURE.md` §8).

Reconciliation is a first-class feature because Binance spot testnet is wiped roughly every 30 days without notice: **API keys survive, balances and open orders do not** (`../contexts/binance-testnet.md`). A system that trusts its local order book will, one morning, believe it has seven open orders that no longer exist anywhere, and will keep sizing against a position that is gone. The wipe is also diagnostically useful — an order the exchange has never heard of *plus* a balance reset is a wipe; the same order missing while balances are intact is a rejection you failed to record, which is a much worse problem.

The rate-limit difference is a risk input, not a tuning knob. Testnet allows 50 orders per 10 seconds against production's 100 per 10 seconds. A strategy whose order rate fits production but not testnet cannot be validated here at all, and the correct response is to reject the strategy rather than to sleep in the client until the budget clears — a limiter that sleeps turns a capacity problem into a latency problem, and latency in the order path is how you get fills at prices your decision never saw.

The dual user-data path is not an abstraction for its own sake. Spot's `session.logon` binds the authenticated session to the *socket*: the session dies with the connection, and reconnecting means re-doing the Ed25519 handshake, not resuming. Futures' `listenKey` is a server-side resource that outlives the socket and dies of a missed keepalive instead. Those are opposite lifetimes. Code that models them as "connect, then subscribe" with a flag will leak a futures `listenKey` on every spot reconnect or, worse, silently stop receiving fills on futures because the keepalive lived in the socket's task group and died with it.

## Incorrect

```python
import ccxt.async_support as ccxt


class BinanceVenue:
    def __init__(self, cfg: dict) -> None:
        self.ex = ccxt.binance({"apiKey": cfg["key"], "secret": cfg["secret"]})
        self.ex.set_sandbox_mode(True)

    async def symbols(self) -> list[str]:
        info = await self.ex.fetch_markets()
        return [m["symbol"] for m in info if m["symbol"].isalnum()]

    async def submit(self, symbol: str, qty: float, price: float) -> int:
        r = await self.ex.create_limit_buy_order(symbol, qty, price)
        return int(r["orderId"])

    async def user_stream(self) -> None:
        key = await self.ex.publicPostUserDataStream()      # 410 Gone
        while True:
            async for msg in self.ex.watch_orders():
                self.apply(msg)
```

What goes wrong at runtime:

`ccxt.binance(...)` constructs its own `aiohttp` session, so nothing about this object is host-validated — `set_sandbox_mode(True)` is a *configuration* value, and configuration is precisely what changes (`../../ARCHITECTURE.md` §8). One dependency bump that alters a default base URL and this places real orders.

`m["symbol"].isalnum()` returns `True` for many non-ASCII strings and `False` for others; either way the deliberate Unicode symbol in testnet `exchangeInfo` is silently dropped with no log, so the universe is quietly wrong and nobody knows which symbol went missing.

`qty: float` and `price: float` violate the money non-negotiable at the exact boundary where it matters most — the value you send to the venue.

`r["orderId"]` raises `KeyError` on any error envelope, and `int(...)` on a value Binance may serialize as a string is a coin flip across endpoints. The submit call passes no `clientOrderId`, so a retry after a timeout places a **second** order.

`publicPostUserDataStream()` returns 410 Gone on spot, forever.

And `watch_orders()` inside an infinite loop with no reconciliation means that after a testnet wipe this object keeps operating on a fiction, with no event that would tell it otherwise.

## Correct

One interface, two genuinely different implementations:

```python
# src/fking/execution/userdata.py
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from fking.execution.models import UserDataEvent
from fking.execution.venue_profile import VenueProfile
from fking.platform.safety import guarded_client


class UserDataStream(Protocol):
    """Fill and balance events. Implementations differ in session lifetime, which is
    why this is a Protocol and not a base class with a `mode` flag."""

    async def open(self) -> None: ...
    async def events(self) -> AsyncIterator[UserDataEvent]: ...
    async def aclose(self) -> None: ...


class SpotSessionLogonStream:
    """Spot: `POST /api/v3/userDataStream` is 410 Gone. Authentication is an Ed25519
    `session.logon` over the WebSocket API; the session is bound to this socket and
    cannot be resumed, so every reconnect re-authenticates and re-reconciles."""

    def __init__(self, profile: VenueProfile, signer: Ed25519Signer) -> None:
        self._profile = profile
        self._signer = signer
        self._client = guarded_client(profile.ws_api_url)

    async def open(self) -> None:
        params = {
            "apiKey": self._signer.public_key_b64,
            "timestamp": self._profile.now_in_venue_units(),
            "recvWindow": self._profile.recv_window_ms,
        }
        params["signature"] = self._signer.sign(canonical_query(params))
        logon = SessionLogonAck.model_validate(
            await self._client.request("session.logon", params)
        )
        await self._client.request("userDataStream.subscribe", {})
        self._authorized_until = logon.authorized_until


class FuturesListenKeyStream:
    """Futures: `listenKey` is a server-side resource that outlives the socket and
    expires on a missed keepalive. The keepalive task is owned by the *stream*, not by
    the socket's task group, because a socket reconnect must not restart the key."""

    def __init__(self, profile: VenueProfile) -> None:
        self._profile = profile
        self._rest = guarded_client(profile.rest_url)
        self._keepalive: asyncio.Task[None] | None = None

    async def open(self) -> None:
        ack = ListenKeyAck.model_validate(await self._rest.post("/fapi/v1/listenKey"))
        self._listen_key = ack.listen_key
        self._keepalive = asyncio.create_task(
            self._keepalive_loop(self._profile.listen_key_keepalive_seconds)
        )
```

Responses are parsed, not indexed:

```python
# src/fking/execution/models.py
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _decimal_from_venue(value: object) -> Decimal:
    """Binance serializes quantities and prices as strings. A float here has already
    lost precision before Pydantic sees it, so a non-string is a contract change."""
    if not isinstance(value, str):
        raise ValueError(f"expected a string-encoded decimal, got {type(value).__name__}")
    return Decimal(value)


VenueDecimal = Annotated[Decimal, BeforeValidator(_decimal_from_venue)]


class VenueOrderAck(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    order_id: str = Field(alias="orderId")
    client_order_id: str = Field(alias="clientOrderId")
    symbol: str
    status: Literal["NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "EXPIRED"]
    executed_quantity: VenueDecimal = Field(alias="executedQty")
    cummulative_quote_quantity: VenueDecimal = Field(alias="cummulativeQuoteQty")
```

`extra="ignore"` here and `extra="forbid"` for LLM output (`./llm-output-handling.md`) is a deliberate asymmetry: Binance adds fields without notice and breaking on a new one is a self-inflicted outage, whereas an extra field from a model is a sign the schema was not respected.

Symbol handling keeps the exact code points and quarantines rather than drops:

```python
import re
import unicodedata

TRADABLE_SYMBOL = re.compile(r"\A[A-Z0-9]{2,32}\Z")


def classify_symbol(raw: str) -> SymbolClassification:
    """Never coerce. NFKC-normalising here would change the bytes we must send back,
    and a regex `continue` would delete testnet's deliberate Unicode symbol silently."""
    if TRADABLE_SYMBOL.match(raw):
        return SymbolClassification(symbol=raw, tradable=True, reason=None)
    codepoints = " ".join(f"U+{ord(c):04X}" for c in raw if ord(c) > 0x7F)
    return SymbolClassification(
        symbol=raw,
        tradable=False,
        reason=f"non-ascii codepoints {codepoints} ({unicodedata.name(raw[-1], '?')})",
    )
```

The universe is intersected at startup and a missing symbol is fatal:

```python
async def resolve_universe(venue: Venue, archive: ArchiveManifest, requested: frozenset[str]) -> frozenset[str]:
    venue_symbols = {c.symbol for c in await venue.classify_symbols() if c.tradable}
    tradable = venue_symbols & archive.symbols_with_history()
    missing = requested - tradable
    if missing:
        raise UniverseUnavailable(
            f"{sorted(missing)} requested but not in venue ∩ archive; "
            f"venue-only={sorted(venue_symbols - archive.symbols_with_history())[:5]}, "
            f"archive-only={sorted(archive.symbols_with_history() - venue_symbols)[:5]}"
        )
    return frozenset(tradable)
```

The startup intersection resolves *today's* tradable set. Historical membership is a separate, point-in-time question answered by `universe_as_of` in `./no-lookahead.md` — using today's intersection to select a backtest universe is survivorship bias wearing a safety check as a disguise.

## The venue profile

Every number that differs between spot and futures, or between testnet and production, is data. Putting any of them in code is how a testnet constant ends up governing a production-calibrated model.

```python
# src/fking/execution/venue_profile.py
from decimal import Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict


class VenueProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    venue_id: Literal["binance-spot-testnet", "binance-futures-testnet", "bybit-testnet"]
    rest_url: str
    ws_api_url: str
    ws_stream_url: str
    timestamp_unit: Literal["ms", "us"]
    order_rate_per_10s: int
    recv_window_ms: int
    max_clock_drift_ms: int
    listen_key_keepalive_seconds: int | None      # None => session.logon venue
    user_data_mechanism: Literal["session_logon_ed25519", "listen_key"]
    client_order_id_prefix: str
    client_order_id_max_len: int
    reconcile_interval_seconds: int
    cost_model_calibratable: bool                 # False on every testnet profile


SPOT_TESTNET: Final = VenueProfile(
    venue_id="binance-spot-testnet",
    order_rate_per_10s=50,          # production is 100/10s; measured 2026-08-01
    user_data_mechanism="session_logon_ed25519",
    listen_key_keepalive_seconds=None,
    timestamp_unit="us",            # spot switched to microseconds from 2025-01-01
    cost_model_calibratable=False,
    ...
)
```

`cost_model_calibratable=False` is the mechanism behind the "never calibrate on testnet" rule: `fking.backtest.costs.calibrate()` takes a `VenueProfile` and raises `UncalibratableVenue` when the flag is false. It is a hard failure rather than a warning because the failure mode — a cost model built from a 7.5bp testnet spread — produces a backtest that looks conservative and is fiction.

`recv_window_ms` and `max_clock_drift_ms` pair with a drift check on connect and on the reconcile timer: measure local time against the venue's server-time endpoint, and if drift exceeds `max_clock_drift_ms`, halt and page. **You never widen `recvWindow` to make drift go away** — a wide window does not fix a wrong clock, it just lets requests signed against a wrong clock through, and every timestamp you then write to the audit log is wrong by the same amount.

The `timestamp_unit` split is the other half: spot data timestamps are microseconds from 2025-01-01 while futures stayed milliseconds, so normalization is keyed on `(market, date)` and never on a global constant. Full detail in `../../DATA_PIPELINE.md`; the venue profile carries only the live-path unit.

## Enforcement

**`import-linter`** — in `pyproject.toml`. `allow_indirect_imports` is required because `ccxt` itself imports `aiohttp`; without it the contract fails on `fking.execution -> ccxt -> aiohttp`, which is not the thing being forbidden.

```toml
[[tool.importlinter.contracts]]
name = "execution never constructs its own network client"
type = "forbidden"
source_modules = ["fking.execution"]
forbidden_modules = [
  "httpx", "aiohttp", "websockets", "requests", "urllib.request", "http.client", "socket",
]
allow_indirect_imports = "true"

[[tool.importlinter.contracts]]
name = "only the safety kernel constructs transports"
type = "forbidden"
source_modules = ["fking.data", "fking.agents", "fking.api", "fking.backtest"]
forbidden_modules = ["httpx", "aiohttp", "websockets", "requests"]
allow_indirect_imports = "true"
ignore_imports = ["fking.platform.safety -> httpx", "fking.platform.safety -> websockets"]
```

**Safety-kernel test** — `tests/unit/test_guarded_client.py`, part of the 100% floor on `platform/safety`:

```python
import pytest

from fking.platform.safety import SafetyViolation, guarded_client

PRODUCTION_HOSTS = [
    "https://api.binance.com", "https://fapi.binance.com", "https://api1.binance.com",
    "wss://stream.binance.com:9443", "https://api.bybit.com",
]


@pytest.mark.parametrize("url", PRODUCTION_HOSTS)
def test_production_hosts_are_refused_at_construction(url: str) -> None:
    with pytest.raises(SafetyViolation, match="not in the permitted host set"):
        guarded_client(url)


@pytest.mark.parametrize("url", PRODUCTION_HOSTS)
async def test_production_hosts_are_refused_per_request(url: str) -> None:
    client = guarded_client("https://testnet.binance.vision")
    with pytest.raises(SafetyViolation):
        await client.get("/api/v3/time", base_url=url)   # per-call override
```

The second test is the one that matters. Validating only at construction is the failure mode `ccxt`'s per-call URL override creates.

**Recorded-response tests for both user-data paths** — `tests/integration/test_userdata_streams.py` replays captured payloads from `tests/fixtures/recorded/binance-spot-testnet/session_logon/` and `tests/fixtures/recorded/binance-futures-testnet/listen_key/`, asserting: the spot path never issues `POST /api/v3/userDataStream`; a socket drop on the spot path triggers a fresh `session.logon` and a reconciliation pass; a socket drop on the futures path reuses the existing `listenKey` and does **not** restart the keepalive task; and a missed keepalive surfaces as `ERROR` plus a reconnect rather than a silent stall. Recording rules are in `./testing-rules.md`.

**Unicode fixture** — `tests/fixtures/recorded/binance-spot-testnet/exchange_info/` retains testnet's deliberate non-ASCII symbol, and `test_fixture_integrity.py` asserts at least one such symbol is present so nobody "cleans it up". `tests/unit/test_symbol_classification.py` asserts it is classified `tradable=False` with the code points named in `reason`, and that `classify_symbol(s).symbol == s` byte-for-byte.

**Reconciliation test** — after a simulated wipe (open orders and balances cleared, keys valid), the system rebuilds entirely from the exchange and emits `reconciliation.diverged` at `CRITICAL` before it emits `reconciliation.converged`. It must distinguish the wipe (orders gone **and** balances reset) from an unrecorded rejection (order gone, balances intact) and take different actions.

**Rate limiting** — the limiter is constructed from `profile.order_rate_per_10s` and rejects with `RateBudgetExhausted` rather than sleeping. `tests/property/test_rate_limiter_properties.py` asserts with Hypothesis that no submission schedule can exceed the budget in any 10-second window, and `tests/unit/test_venue_profiles.py` asserts every `*-testnet` profile has `cost_model_calibratable=False`.

## The one exception

**None for the host allowlist.** There is no exception, and there is no mechanism by which one could be added at runtime.

Not for a read-only balance check. Not for `exchangeInfo`, which is public and unauthenticated. Not for "just to compare the spread against testnet's 7.5bp". Not behind an environment variable, a `--force` flag, a `pytest` fixture, or a `monkeypatch` in a test. The permitted hosts are a `frozenset` compiled into `fking.platform.safety`; widening it requires editing source and merging a pull request labelled `safety:critical` (`../../CLAUDE.md` §0).

The reason read-only is refused specifically: read paths become write paths during refactors. A client constructed for `fetch_balance` is a client, and the next person to need `create_order` will reach for the one that already exists and already points at production. The allowlist does not distinguish intent because intent is not a property of a socket.

For the rest of this file the exceptions are narrow and named where they occur — `extra="ignore"` on venue models, `allow_indirect_imports` on the two contracts, and two explicitly enumerated `ignore_imports` edges for the safety kernel's own transports. Everything else in this file admits none.
