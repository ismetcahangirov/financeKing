# Context — Binance Testnet

## What you need to hold in your head

Binance testnet is an **execution-plumbing environment, not a market**. It will tell you whether your order construction, signature scheme, reconnection logic, filter rounding and reconciliation are correct. It will lie to you about every single thing that determines whether a strategy makes money — spread, depth, volume, fill probability, impact — and it lies by roughly a factor of fifty on spread. Two further properties dominate the design of `fking.execution`: **spot and futures use genuinely different user-data mechanisms** because spot `listenKey` is dead everywhere, and **spot testnet wipes itself roughly every 30 days**, which is why reconciliation is a first-class feature rather than a nicety. Everything below is either a verified fact with a date, or a consequence explicitly derived from one. Nothing in this file is an estimate dressed as a measurement.

---

## 1. The verified facts

All facts researched **2026-08-01**. Anything not in this table is inference, and is marked as such where it appears.

| # | Fact (verified 2026-08-01) | Operational consequence |
|---|---|---|
| 1 | Spot testnet API keys require **GitHub OAuth only — no KYC, no identity document, no waiting period** | Onboarding is free and immediate. This is why "testnet remains available and free" is a load-bearing assumption in [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) §13 rather than a risk. It is also why key rotation is cheap: generating a fresh key pair costs a browser round trip |
| 2 | Spot `POST /api/v3/userDataStream` returns **410 Gone on testnet and on production alike**. The replacement is the WebSocket API path: **Ed25519 `session.logon`**, then **`userDataStream.subscribe`** on the same authenticated connection | Spot user data cannot be obtained over REST at all. Futures `listenKey` still works. These are two different mechanisms, modelled as two code paths behind one `UserDataSource` interface — see §3 |
| 3 | **Spot testnet is wiped roughly every 30 days.** API keys survive the wipe; balances and open orders are destroyed | Exchange state is the source of truth and local state converges to it. Any test asserting that a balance persists across a month is broken by design, not flaky. Reconciliation runs on a schedule and on every reconnect, not only at boot |
| 4 | **Testnet is not a subset of production.** Spot testnet is missing **79** symbols that exist on spot production; futures testnet is missing **189** symbols that exist on futures production | The tradable universe is the **intersection**, computed and verified at startup against live `exchangeInfo` from both environments. Never hardcode a symbol list, never assume a production symbol resolves on testnet |
| 5 | **Spot testnet order rate limit is 50 orders / 10s. Production spot is 100 orders / 10s** | Testnet is the *tighter* constraint. A strategy that is rate-limit-clean on testnet is clean on production. The reverse mistake — sizing a burst against the production 100/10s figure — produces `-1015 TOO_MANY_ORDERS` rejections on testnet, in the environment you actually run in. Calibrate the throttle to 50/10s and treat production headroom as unused margin |
| 6 | **Futures testnet showed a 7.5bp spread against production's 0.16bp, with roughly 10x inflated volume** | Hard rule: testnet is **never** used to calibrate a cost model, an impact model, a fill-probability model, or a capacity estimate. `CLAUDE.md` §2 states this as a non-negotiable. See §6 for why the direction of the error is not simply "pessimistic" |
| 7 | **A deliberate Unicode symbol exists in testnet `exchangeInfo`** | Symbol parsers must be Unicode-safe. No `str.isalnum()` gate, no ASCII assumption, no regex anchored to `[A-Z0-9]+`, and every log handler and file sink writes UTF-8 explicitly. A `UnicodeEncodeError` inside a symbol-universe load is a startup crash on a Windows console default codepage |

Two further facts carried from elsewhere in the project, verified and relevant here:

| # | Fact | Consequence |
|---|---|---|
| 8 | Spot data timestamps switched to **microseconds from 2025-01-01**; futures stayed in **milliseconds** ([`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) §6) | Normalisation is keyed on `(market, date)`, never a global constant. A silent unit error shifts bars by three orders of magnitude and produces spectacular fake alpha rather than a crash |
| 9 | `ccxt` >= 4.5.70 is the client ([`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) §7) | It is currently the only library correct on both the endpoint split and the post-`listenKey` user-data model. `python-binance` is broken for spot user data; `binance-connector` is frozen; the official `binance-sdk-*` packages shipped 11 and 16 major versions in roughly twelve months |

---

## 2. Hosts, and why the list is compiled in

The demo-only guarantee is a `frozenset` of permitted hosts compiled into `fking.platform.safety` — not config, not environment, not database, not file. Every request goes through `guarded_client()`, which validates the host **on every call** rather than at construction, because `ccxt` accepts per-call URL overrides and libraries change default base URLs in minor bumps.

| Purpose | Host | Environment |
|---|---|---|
| Spot REST | `testnet.binance.vision` | Binance spot testnet |
| Spot market-data streams | `stream.testnet.binance.vision` | Binance spot testnet |
| Spot WebSocket API (`session.logon`, `userDataStream.subscribe`) | `ws-api.testnet.binance.vision` | Binance spot testnet |
| Futures REST | `testnet.binancefuture.com` | Binance USD-M futures testnet |
| Futures streams | `stream.binancefuture.com` | Binance USD-M futures testnet |
| Fallback REST | `api-testnet.bybit.com` | Bybit testnet |
| Fallback streams | `stream-testnet.bybit.com` | Bybit testnet |

**There is no production host anywhere in the allowlist, and no code path that adds one.** Not for a read-only price check, not for cost-model calibration, not behind a flag. Production market data for cost calibration arrives through the historical archive in `fking.data`, which is a separate ingestion path with no live credential and no order surface — read paths become write paths during refactors, which is exactly why the separation is physical rather than conventional.

If you find yourself writing code that would widen the allowlist, stop and ask the user. `CLAUDE.md` §0.

---

## 3. Two user-data mechanisms, one interface

This is the single most surprising thing about the current Binance API surface, and the reason a naive port of any tutorial fails.

### Spot: Ed25519 `session.logon` over the WebSocket API

`POST /api/v3/userDataStream` returns **410 Gone**. There is no listen key for spot. Instead you open a WebSocket to the WebSocket API endpoint, authenticate the *connection* with `session.logon`, and then subscribe.

**The key type is not interchangeable with HMAC.** An HMAC-SHA256 key produces a hex-encoded signature and works for REST signing. `session.logon` requires an **Ed25519** key, whose signature is base64-encoded over the same canonical payload. Presenting an HMAC key to `session.logon` fails authentication; presenting an Ed25519 key where the code expects to compute an HMAC produces a signature over the wrong algorithm. You register the Ed25519 *public* key with Binance and keep the private key locally — Binance never sees it, which also means a lost private key is unrecoverable and requires registering a new key.

```python
from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def generate_ed25519_keypair(private_key_path: Path) -> str:
    """Generate an Ed25519 key pair; return the PEM public key to register.

    Binance stores only the public key. There is no recovery path for a lost
    private key — you register a new one, which on testnet costs a browser round
    trip (fact 1) and on any other venue would not.
    """
    private_key = Ed25519PrivateKey.generate()
    private_key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    private_key_path.chmod(0o600)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return public_pem.decode("ascii")


def sign_ed25519(private_key: Ed25519PrivateKey, params: dict[str, str]) -> str:
    """Sign the canonical Binance payload with Ed25519, base64-encoded.

    The payload is the request parameters sorted by key and joined as
    `key=value&key=value`, EXCLUDING the signature itself. Sort order is part of
    the contract: an unsorted payload signs correctly and authenticates
    incorrectly, which surfaces as a -1022 signature error rather than as
    anything that points at ordering.
    """
    payload = "&".join(f"{key}={params[key]}" for key in sorted(params))
    return base64.b64encode(private_key.sign(payload.encode("ascii"))).decode("ascii")
```

The handshake, in shape:

```python
import json
import time
import uuid


def session_logon_frame(api_key: str, private_key: Ed25519PrivateKey) -> str:
    params = {
        "apiKey": api_key,
        "timestamp": str(int(time.time() * 1000)),  # request timestamps are ms
    }
    params["signature"] = sign_ed25519(private_key, params)
    return json.dumps(
        {"id": str(uuid.uuid4()), "method": "session.logon", "params": params}
    )


def user_data_subscribe_frame() -> str:
    """No credentials here: the CONNECTION is authenticated, not the request.

    This is the structural difference from the listenKey model. A dropped socket
    drops the authentication with it, so reconnect must re-run session.logon
    before re-subscribing, and the gap between drop and resubscribe is a hole in
    the event stream that reconciliation has to close.
    """
    return json.dumps({"id": str(uuid.uuid4()), "method": "userDataStream.subscribe"})
```

Note the timestamp above is **milliseconds** — that is the request-signing convention and it is unrelated to fact 8, which is about the units in historical *data* archives. Conflating the two is easy and the failure mode is a `-1021` timestamp-outside-recvWindow error that looks like clock drift.

### Futures: `listenKey` still works

`POST /fapi/v1/listenKey` returns a listen key; you connect to the stream host with the key in the path and `PUT` every 30 minutes to keep it alive. Keys expire after 60 minutes without a keepalive.

### The interface

```python
from collections.abc import AsyncIterator
from typing import Protocol

from fking.domain.events import UserDataEvent


class UserDataSource(Protocol):
    """One interface, two genuinely different mechanisms behind it.

    Spot authenticates the connection (Ed25519 session.logon) and has no key to
    refresh. Futures authenticates with a listenKey that must be renewed on a
    timer. `refresh()` is a no-op on spot and a PUT on futures; `reconnect()`
    re-runs session.logon on spot and reuses the key on futures.

    ARCHITECTURE.md section 7: these are modelled as two code paths precisely
    because collapsing them into one produced a "generic" adapter that was wrong
    about both.
    """

    async def connect(self) -> None: ...
    async def refresh(self) -> None: ...
    async def reconnect(self) -> None: ...
    def events(self) -> AsyncIterator[UserDataEvent]: ...
```

Every consumer downstream of this is idempotent, because delivery is at-least-once and because a reconnect replays. `CLAUDE.md` §2.

---

## 4. `ccxt` configuration

Both snippets construct through `guarded_client()`. `ccxt` is never instantiated directly in `fking.execution` — an `import-linter` contract forbids `execution` from importing `httpx`, `aiohttp`, `websockets` or `requests`, and the same discipline applies to handing `ccxt` a session it did not get from the safety kernel.

### Spot testnet

```python
import ccxt.async_support as ccxt

from fking.platform.safety import guarded_client


def build_spot_testnet_client(api_key: str, ed25519_private_key_pem: str) -> ccxt.binance:
    """Binance spot testnet over ccxt >= 4.5.70.

    `set_sandbox_mode(True)` rewrites base URLs to testnet.binance.vision. It is
    NOT the safety mechanism — it is a convenience that a library upgrade could
    change. The safety mechanism is guarded_client() validating the resolved host
    on every request against the compiled-in allowlist.
    """
    client = ccxt.binance(
        {
            "apiKey": api_key,
            "secret": ed25519_private_key_pem,   # Ed25519 PEM, not an HMAC secret
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
                # Ed25519 is required for session.logon (fact 2) and is accepted
                # for REST signing, so one key type covers both paths.
                "sign": "ed25519",
                "adjustForTimeDifference": True,  # avoids -1021 on a drifting clock
            },
            "session": guarded_client(),
        }
    )
    client.set_sandbox_mode(True)
    return client
```

### Futures testnet

```python
def build_futures_testnet_client(api_key: str, api_secret: str) -> ccxt.binance:
    """Binance USD-M futures testnet.

    HMAC is fine here: futures user data still uses listenKey (fact 2), so there
    is no session.logon and no Ed25519 requirement. Using two different key types
    across the two venues is not an inconsistency to clean up — it is the
    minimum-privilege consequence of the two mechanisms.
    """
    client = ccxt.binance(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "future",
                "adjustForTimeDifference": True,
            },
            "session": guarded_client(),
        }
    )
    client.set_sandbox_mode(True)
    return client
```

Money never leaves these clients as `float`. `ccxt` parses JSON with the stdlib decoder and hands back `float` for prices and quantities; the venue adapter re-parses from the raw response text with `json.loads(body, parse_float=Decimal)` before anything else in the system sees a number. See [`../../docs/rules/decimal-and-money.md`](../../docs/rules/decimal-and-money.md) — a value that has passed through a `float` is not repairable by widening the type afterwards.

---

## 5. Rate limits

| Limit | Spot testnet | Spot production | Rule here |
|---|---|---|---|
| Orders per 10s | **50** | **100** | Throttle to 50. Testnet is the binding constraint |
| Orders per 24h | Documented per account tier | Documented per account tier | Track it; a wipe (fact 3) does not reset your own counter, only the exchange's balances |
| Request weight per minute | Weight-based, per IP | Weight-based, per IP | `enableRateLimit: True` handles the common case; a `429` still needs backoff and a `418` (IP ban) needs a hard stop, not a retry loop |

The asymmetry worth internalising: **a throughput test run on testnet measures the tighter limit**, so passing it is meaningful. A throughput budget derived from production documentation and deployed against testnet gets rejected. Both errors are one-directional, and only one of them is safe.

Rate-limit rejections are not exceptions to swallow. A `-1015` in the order path means the risk engine's intended position was not established, and continuing as if it were is the "catch an exception to keep the loop alive" anti-pattern with real positions open (`CLAUDE.md` §11).

---

## 6. Why testnet must never calibrate a cost model

Fact 6 again, because it is the one people try to argue around: **futures testnet showed a 7.5bp spread against production's 0.16bp, with roughly 10x inflated volume.** That is a factor of 47 on spread and an order of magnitude on volume, in opposite directions.

The naive reading is "testnet is pessimistic, so a strategy that survives there is safe". That reading is wrong in both halves:

- **Pessimistic on spread, wildly optimistic on fill.** Testnet's order book is thin and populated largely by other bots and by the exchange's own liquidity simulation. Passive orders fill at rates that bear no relationship to a real queue, because there is no real queue and no adverse selection. A maker strategy that fills 80% of the time on testnet may fill 15% of the time in production, and the 65% that did not fill is precisely the adversely-selected portion.
- **Optimistic on impact and capacity.** 10x inflated volume makes any size look absorbable. A capacity estimate derived from testnet volume is off by an order of magnitude in the direction that lets you deploy too much.

So the rule is absolute, and it covers four distinct models, not one:

> Cost model, impact model, fill-probability model, and capacity estimate are calibrated from **production historical market data** in `fking.data`, never from testnet, and the cost-parameter set carries a provenance id that names the calibration source. A backtest whose cost parameters have testnet provenance is void.

What testnet *is* good for, and this is genuinely valuable: order construction, filter and step-size rounding, signature schemes, reconnection and resubscription, listen-key renewal, idempotency under replay, reconciliation after a wipe, error-code handling, and the shape of every response you will parse. That is most of `fking.execution`, and getting it right on testnet is why the demo runtime works at all.

---

## 7. Unicode in `exchangeInfo`

Fact 7: testnet's `exchangeInfo` deliberately contains a symbol with a non-ASCII character. It is there to break parsers, and it does.

```python
# WRONG — three separate failures
def parse_symbols(exchange_info: dict) -> list[str]:
    return [
        s["symbol"]
        for s in exchange_info["symbols"]
        if s["symbol"].isalnum()          # 1. drops the Unicode symbol silently
        and s["status"] == "TRADING"      # 2. optimistic indexing into hostile input
    ]
    # 3. and logging the result with a default-codepage handler raises
    #    UnicodeEncodeError on a Windows console, crashing startup
```

```python
# RIGHT
import logging
from typing import Any


def parse_symbols(exchange_info: dict[str, Any]) -> frozenset[str]:
    """Extract tradable symbols without assuming ASCII or a well-formed payload.

    Testnet exchangeInfo deliberately contains a non-ASCII symbol (verified
    2026-08-01). No isalnum() gate, no [A-Z0-9]+ regex, no assumption that a
    symbol round-trips through a non-UTF-8 encoder. Exchange responses are
    hostile input (CLAUDE.md section 4) — every field access is checked.
    """
    symbols: set[str] = set()
    for entry in exchange_info.get("symbols", []):
        symbol = entry.get("symbol")
        status = entry.get("status")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError(f"exchangeInfo entry has no usable symbol: {entry!r}")
        if status == "TRADING":
            symbols.add(symbol)
    return frozenset(symbols)
```

And the log sink is configured for UTF-8 explicitly, at bootstrap, so that a symbol containing a non-ASCII character is logged rather than raising inside the logging call — an exception thrown by a log statement is uniquely hard to diagnose because the diagnostic channel is the thing that failed.

---

## 8. Startup pre-flight checklist

Run this against testnet before any session that will place an order. Every item produces evidence, not a claim — `CLAUDE.md` §7.

1. **Allowlist logged.** The resolved base URLs for every configured client appear in the boot log and every one is a member of the compiled-in allowlist. Abort on any miss.
2. **Clock skew.** `GET /api/v3/time` against local UTC. Skew above the configured `recvWindow` minus a margin is a hard stop, not a warning — it manifests later as `-1021` in the order path.
3. **Credentials resolve, and to the right key type.** Spot: Ed25519 private key loads and its public key matches the registered one. Futures: HMAC secret signs a `GET /fapi/v2/account` successfully. A key-type mismatch found here costs a minute; found in the order path it costs a session.
4. **Symbol universe intersected.** Load testnet `exchangeInfo` and the production symbol reference from the archive. Compute the intersection. Log the count of production symbols absent from testnet and assert it is consistent with fact 4 (79 spot / 189 futures as of 2026-08-01) — a large deviation means the venue changed and the universe assumption needs re-verifying.
5. **Symbol parser is Unicode-safe.** The parse in step 4 completed without dropping entries and the resulting set round-trips through the log sink.
6. **Per-symbol filters loaded.** `tickSize`, `stepSize`, `minNotional` for every symbol in the universe, as `Decimal` from the response text. An order rounded against a stale filter is rejected in a hot path.
7. **Balances reconciled.** Fetch balances and open orders from the exchange and overwrite local state. If balances are zero and open orders are empty where local state expected otherwise, **assume a wipe occurred** (fact 3), log it as a wipe rather than as a discrepancy, and rebuild rather than alerting.
8. **Rate-limit budget set to 50 orders / 10s** for spot (fact 5), not the production figure.
9. **User-data path live.** Spot: `session.logon` succeeds and `userDataStream.subscribe` acknowledges. Futures: `listenKey` obtained and the keepalive timer is scheduled at 30 minutes. Confirm at least one heartbeat or event arrives before declaring ready.
10. **Cost-model provenance asserted.** The loaded cost parameter set's provenance id names a production calibration. Refuse to start the demo runtime with testnet-provenance cost parameters (§6).

A failed item is a hard stop. A trading system that continues past an unexpected state is more dangerous than one that stops (`CLAUDE.md` §4).

---

## 9. Wipes and reconciliation

Every ~30 days, spot testnet balances and open orders vanish. Keys survive. There is no notification.

The design consequence, stated once in [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) §7 and elaborated here: **the system must be able to rebuild its entire view of the world from the exchange at any moment.** Concretely:

- Reconciliation runs at boot, on every user-data reconnect, and on a timer — not only after an error.
- A discrepancy between local and exchange state resolves **toward the exchange**, always. Local state is a cache.
- A total wipe (zero balances, zero open orders, where local state had positions) is a **recognised state with its own handler**, not an anomaly that trips the kill switch. It logs as `TESTNET_WIPE_DETECTED`, closes out local position records with a synthetic reconciliation event so the audit trail stays continuous, and requests a fresh faucet balance.
- Any test asserting balance persistence across a month is wrong. Tests that need a known balance establish it in setup.
- The append-only audit trail spans the wipe. The record of what the system believed before the wipe is exactly what makes the wipe diagnosable, so nothing is deleted to "clean up" after one.

A useful reframing: the wipe is free chaos engineering. A system that survives a monthly unannounced state reset has a reconciliation path that has been exercised twelve times a year, which is twelve more times than most.

---

## 10. Bybit fallback

Bybit testnet sits behind the same `ExecutionVenue` abstraction and reaches the exchange through the same `guarded_client()` and the same allowlist. It exists because [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) §13 names "testnet remains available and free" as an assumption, and an assumption without a fallback is a single point of failure for the whole project.

What differs and must not be assumed away: symbol naming, filter semantics, the user-data mechanism, error codes, and rate limits are all Bybit's, not Binance's. The abstraction hides the *call shape*, not the venue's behaviour. Every fact in §1 is a Binance fact and none of them transfers.

---

## 11. Where this shows up in the codebase

| Concern | Location |
|---|---|
| Host allowlist as a compiled-in `frozenset`, `guarded_client()` | `fking.platform.safety` — 100% coverage floor, `CLAUDE.md` §5 |
| Venue adapters, `ccxt` construction, filter rounding | `fking.execution` |
| `UserDataSource` protocol and its two implementations | `fking.execution` |
| Reconciliation, wipe detection, exchange-as-source-of-truth | `fking.execution` |
| Symbol universe intersection and the Unicode-safe parser | `fking.execution` startup, feeding `fking.data` availability |
| Cost model parameters and their provenance id | `fking.backtest` cost model, calibrated from `fking.data` production archive |
| Timestamp unit normalisation keyed on `(market, date)` | `fking.data` ingestion |
| `import-linter` contract forbidding direct HTTP clients in `execution` | `pyproject.toml` contracts, run by `make check` |

Related: [`../../docs/rules/exchange-integration.md`](../../docs/rules/exchange-integration.md) for the enforced integration rules, [`../knowledge/verified-facts.md`](../knowledge/verified-facts.md) for the fact ledger these entries belong to, [`./market-microstructure.md`](./market-microstructure.md) for what a real book looks like and why testnet's does not, [`./crypto-perpetuals.md`](./crypto-perpetuals.md) for funding and liquidation mechanics, and [`./backtest-pitfalls.md`](./backtest-pitfalls.md) §4 for the testnet-calibration trap in its backtesting form.

---

## 12. Traps

1. **Assuming a production symbol exists on testnet.** 79 spot and 189 futures symbols do not (fact 4). Intersect at startup.
2. **Reaching for a spot `listenKey`.** It returns 410 Gone everywhere (fact 2). If a library or tutorial offers you one, that code is stale.
3. **Signing `session.logon` with an HMAC key.** It requires Ed25519 and the key types are not interchangeable (fact 2).
4. **Forgetting that spot authentication is per-connection.** A dropped socket drops the session. Reconnect re-runs `session.logon` before re-subscribing, and the gap is a hole in the event stream.
5. **Sizing throughput against the production 100/10s figure.** Testnet is 50/10s (fact 5) and it is the environment you run in.
6. **Calibrating anything economic from testnet.** 7.5bp against 0.16bp (fact 6). Cost, impact, fill probability, capacity — all four, all void.
7. **Reading "testnet is pessimistic" as "testnet is safe".** It is pessimistic on spread and optimistic on fill and capacity simultaneously.
8. **`str.isalnum()` in a symbol parser**, or any ASCII assumption, or a log sink that is not explicitly UTF-8 (fact 7).
9. **Treating a wipe as an anomaly.** It is a scheduled event with a handler (fact 3).
10. **Confusing request-signing timestamps (milliseconds) with archive data timestamps (microseconds for spot from 2025-01-01).** Different domains, and the failure modes look nothing alike.
11. **Trusting `set_sandbox_mode(True)` as the safety mechanism.** It is a convenience. The safety mechanism is the allowlist check on every request.

## If you remember nothing else

**Testnet proves your plumbing and lies about your economics. Spot and futures user data are two different mechanisms, not one with a flag. The exchange is the source of truth and it will erase itself every month. And there is no host in the allowlist that can lose real money.**
