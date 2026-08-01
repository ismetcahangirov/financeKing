# Rule — Module Boundaries

## The rule

Dependencies point inward toward `fking.domain`, which imports nothing but the standard library. `strategy` cannot import `execution` or `risk`; `execution` cannot import an HTTP or WebSocket library directly. Modules talk through their package `__init__.py` and never reach into another module's private submodules. Every one of these is a machine-checked `import-linter` contract, not a convention.

## Why

Three separate failures, each prevented by a different contract.

**`strategy` → `execution` is the load-bearing one.** A strategy emits a `Signal` — direction, conviction, horizon, invalidation, rationale — and says nothing about size. The risk engine alone constructs orders (`ARCHITECTURE.md` §5). If a strategy can import `execution`, it can construct an `Order`, and a strategy that sizes its own positions can bankrupt the portfolio regardless of how good its signals are. The reason this must be structural rather than reviewed: this system will write its own strategies via LLM agents, and **an LLM-authored strategy will size its own positions if the type system permits it**. It will do so plausibly, with a comment explaining why this particular case is different, and it will pass a human skim. The author will not have read `RISK_PHILOSOPHY.md`. The import graph will have.

**`execution` → `httpx` bypasses the safety kernel.** The demo-only guarantee is implemented as a compiled-in `frozenset` of permitted hosts in `fking.platform.safety`, validated on every request by `guarded_client()` (`ARCHITECTURE.md` §8). A module that constructs its own `httpx.AsyncClient` has left the kernel entirely — no host validation, no boot-time allowlist check, no audit. The threat model is not malice, it is a library changing a default base URL in a minor bump, or an agent generating a client because that is what the training data does. `CLAUDE.md` §0 is explicit that you should not need the linter to know this; the linter exists because "should not need" is not a guarantee.

**`domain` importing anything makes the core untestable and unstable.** `domain` is what every other module agrees on. If it imports `pydantic`, a Pydantic major bump becomes a change to the meaning of a `Fill`. If it imports `platform`, then constructing a `Position` in a test drags in config loading and telemetry. Zero dependencies is what makes the domain types free to instantiate anywhere — which is why the property tests in [`./immutability.md`](./immutability.md) can enumerate them by walking the package.

The general principle underneath all three: the boundaries are the option value of microservices bought for the price of a CI check (`ARCHITECTURE.md` §2). An architecture that is only in a diagram degrades within weeks. This one is executable.

## The module map

```
src/fking/
  domain/     pure types. stdlib only. Knows about instruments, positions, fills, signals, orders.
  data/       ingestion, storage, feature store. Knows about sources, schemas, point-in-time.
  strategy/   the strategy contract and implementations. Knows about features and beliefs.
  risk/       sizing, limits, netting, kill switch. Knows about signals, exposure and capital.
  execution/  venues, OMS, reconciliation. Knows about order types and venue protocols.
  backtest/   engine, cost model, validation. Knows about simulated venues and historical clocks.
  agents/     LLM agents and runtime. Knows about prompts, schemas, budgets.
  evolution/  lifecycle, scoring, mutation. Knows about populations and survival.
  platform/   config, logging, telemetry, bus, persistence, safety. Knows about mechanism, not policy.
  api/        FastAPI application. Knows about HTTP shapes.
```

**Where does this code go? Ask what it knows about.** Code that knows about order types belongs in `execution`. Code that knows about order types *and* feature engineering belongs in neither — it is two pieces of code that have not been separated yet (`CLAUDE.md` §3). The question is not "where would it be convenient", and a module named after a layer (`utils`, `helpers`, `common`) is the answer you get when nobody asked.

**An abstraction requires two concrete callers before it exists.** One caller plus an anticipated future caller is speculation. Write the second implementation first, then extract the interface from what the two actually share — an interface extracted from one implementation is that implementation with a different name, and it will fit the second caller badly. This is the main way a codebase becomes unnavigable, and it is worth more scrutiny here than elsewhere because agent-authored code produces plausible abstractions cheaply.

## Incorrect

```python
# src/fking/strategy/momentum/breakout.py
from decimal import Decimal

from fking.execution.oms import OrderManager          # crosses the load-bearing boundary
from fking.risk._sizing import kelly_fraction         # reaches into a private submodule
from fking.domain import Bar, Signal


class BreakoutStrategy:
    def __init__(self, oms: OrderManager) -> None:
        self._oms = oms

    async def evaluate(self, bars: Sequence[Bar]) -> Signal:
        conviction = Decimal("0.9")
        # "the risk engine is too conservative for a confirmed breakout"
        fraction = kelly_fraction(win_rate=Decimal("0.55"), payoff=Decimal("2.0"))
        await self._oms.submit_market_order("BTC/USDT", base_quantity=fraction * Decimal("10"))
        return Signal(direction="long", conviction=conviction, ...)
```

This runs. It produces a real order on the demo account, sized by the strategy, that the risk engine never saw and therefore never counted toward portfolio exposure, correlation netting or the drawdown limit. The kill switch's view of the book is now wrong, which means the one mechanism that is supposed to survive every other failure is operating on incomplete state. The comment is exactly the kind an agent writes.

The second import is a quieter version of the same problem: `fking.risk._sizing` is private, so it carries no compatibility promise. When `risk` refactors its internals — which it is free to do, because they are private — the strategy breaks, or worse, keeps working against a function whose meaning changed.

```python
# src/fking/execution/venues/binance.py
import httpx                                          # bypasses the safety kernel

async def fetch_open_orders(symbol: str) -> list[dict]:
    async with httpx.AsyncClient(base_url=SETTINGS.binance_base_url) as client:
        response = await client.get("/fapi/v1/openOrders", params={"symbol": symbol})
    return response.json()
```

`SETTINGS.binance_base_url` comes from configuration. Configuration is precisely what changes. There is no host check anywhere in this path.

## Correct

```python
# src/fking/strategy/momentum/breakout.py
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal

from fking.domain import Bar, Signal
from fking.platform.clock import Clock


class BreakoutStrategy:
    """Emits a belief. Says nothing about size — that is the risk engine's authority."""

    def evaluate(self, bars: Sequence[Bar], clock: Clock) -> Signal | None:
        as_of = clock.now()
        window = tuple(bar for bar in bars if bar.open_time_utc <= as_of)
        if len(window) < self._lookback:
            return None
        high = max(bar.high_quote_price for bar in window[-self._lookback :])
        last = window[-1]
        if last.close_quote_price <= high:
            return None
        return Signal(
            direction="long",
            conviction=Decimal("0.6"),
            horizon=timedelta(hours=8),
            invalidation=high,                    # what would prove this wrong
            rationale=f"close {last.close_quote_price} above {self._lookback}-bar high {high}",
            decided_at_utc=as_of,
        )
```

The strategy imports `fking.domain` and `fking.platform.clock`. It has no import path to order construction, so the failure above is not a bug that review must catch — it is an import error in CI.

```python
# src/fking/execution/venues/binance.py
from fking.platform.safety import guarded_client


async def fetch_open_orders(symbol: str) -> tuple[VenueOrder, ...]:
    client = guarded_client()                     # host validated on every request
    body = await client.get_text("/fapi/v1/openOrders", params={"symbol": symbol})
    return parse_open_orders(body)
```

**Public interface via `__init__.py`.** Each package declares what it promises:

```python
# src/fking/risk/__init__.py
"""Position sizing, exposure limits and the kill switch.

Everything not listed in __all__ is private and may change without notice.
"""

from fking.risk.engine import RiskEngine
from fking.risk.limits import ExposureLimits, LimitBreach
from fking.risk.killswitch import KillSwitch, KillSwitchState

__all__ = ["ExposureLimits", "KillSwitch", "KillSwitchState", "LimitBreach", "RiskEngine"]
```

Callers write `from fking.risk import RiskEngine`. A module whose name starts with `_`, or that is not re-exported here, is internal — importing it from another package is a boundary violation whether or not a linter caught that particular spelling.

## Enforcement

`import-linter`, configured in `pyproject.toml` and run as `lint-imports` inside `make check`. Everything below is the real configuration.

```toml
[tool.importlinter]
root_package = "fking"
include_external_packages = true

[[tool.importlinter.contracts]]
name = "Dependencies point inward toward domain"
type = "layers"
layers = [
    "fking.api",
    "fking.evolution",
    "fking.agents",
    "fking.backtest",
    "fking.execution",
    "fking.risk",
    "fking.strategy",
    "fking.data",
    "fking.domain",
]
exhaustive = true
exhaustive_ignores = ["fking.platform"]

[[tool.importlinter.contracts]]
name = "Strategies never reach the order path"
type = "forbidden"
source_modules = ["fking.strategy"]
forbidden_modules = ["fking.execution", "fking.risk"]

[[tool.importlinter.contracts]]
name = "domain imports nothing but the standard library"
type = "forbidden"
source_modules = ["fking.domain"]
forbidden_modules = [
    "fking.data",
    "fking.strategy",
    "fking.risk",
    "fking.execution",
    "fking.backtest",
    "fking.agents",
    "fking.evolution",
    "fking.platform",
    "fking.api",
    "pydantic",
    "sqlalchemy",
    "httpx",
    "redis",
    "ccxt",
    "numpy",
    "pandas",
    "structlog",
]

[[tool.importlinter.contracts]]
name = "Only the safety kernel constructs network clients"
type = "forbidden"
source_modules = [
    "fking.domain",
    "fking.data",
    "fking.strategy",
    "fking.risk",
    "fking.execution",
    "fking.backtest",
    "fking.agents",
    "fking.evolution",
    "fking.api",
]
forbidden_modules = ["httpx", "aiohttp", "websockets", "requests", "urllib.request", "http.client", "socket"]
allow_indirect_imports = true

[[tool.importlinter.contracts]]
name = "Venues do not know about each other"
type = "independence"
modules = [
    "fking.execution.venues.demo",
    "fking.execution.venues.paper",
    "fking.backtest.venue",
]
```

Four details in that configuration are load-bearing and easy to get wrong:

**`exhaustive = true` with `exhaustive_ignores = ["fking.platform"]`.** Without `exhaustive`, a new top-level module — `fking.reporting`, say — is simply absent from the layer order and therefore unconstrained: it can import anything and be imported by anything, and nobody notices for a year. With it, CI fails until the new module is placed in the hierarchy, which forces the "what does this code know about?" question at the moment it is cheapest to answer. `platform` is the sole ignore, because it deliberately sits outside the layering.

**`allow_indirect_imports = true` on the network contract.** By default a `forbidden` contract also fails on indirect chains, and `fking.execution` → `fking.platform.safety` → `httpx` is exactly such a chain — the intended one. Without this flag the contract forbids the correct architecture and the pressure to delete it becomes irresistible. The contract's job is to catch a *direct* import, which is what bypassing the kernel looks like.

**`include_external_packages = true`** is required for `httpx`, `ccxt` and friends to be visible to the graph at all; without it the network contract silently passes because the forbidden modules do not exist in the analysed graph. A silently-passing safety contract is worse than no contract.

**The `strategy` → `execution` forbidden contract is redundant with the layers contract, and stays.** Layers already forbids it. But when the layers contract breaks, the failure names a layer ordering; when the named contract breaks, the failure names the invariant. The reader of a CI log at 02:00 needs the second one.

Failure output looks like this:

```
=============
Import Linter
=============

---------
Contracts
---------

Analyzed 214 files, 1281 dependencies.
-------------------------------------

Dependencies point inward toward domain KEPT
Strategies never reach the order path BROKEN
domain imports nothing but the standard library KEPT
Only the safety kernel constructs network clients KEPT
Venues do not know about each other KEPT

Contracts: 4 kept, 1 broken.

----------------
Broken contracts
----------------

Strategies never reach the order path
-------------------------------------

fking.strategy is not allowed to import fking.execution:

-   fking.strategy.momentum.breakout -> fking.execution.oms (l.7)
```

Two supporting mechanisms, because the import graph cannot see everything:

- **The private-submodule rule** is not expressible as an `import-linter` contract at useful granularity; it is a review item in `CODE_REVIEW.md` and a naming convention — a leading underscore on a module means "the package does not promise this exists". A cross-package import of a `_`-prefixed module blocks a merge.
- **`fking.platform.safety` coverage floor is 100%** (`CLAUDE.md` §5). A contract that says the kernel is the only path is worth what the kernel's own tests are worth.

## The one exception

`fking.platform` is importable from anywhere, including from `strategy` and `risk`, and is exempt from the layers contract.

This is safe because `platform` contains **mechanism, not policy**. Config loading, structured logging, OpenTelemetry wiring, the Redis Streams client, the persistence session, the `Clock` protocol, the decimal context, the host allowlist — none of these decide anything about trading. `platform` knows how to send a request; it has no opinion about whether a request should be sent. Importing it therefore cannot smuggle a decision across a boundary, which is the only thing the layering exists to prevent.

The one decision `platform` does encode — the permitted-host `frozenset` — is a prohibition rather than a policy, and it has to be reachable from every module for the prohibition to be total. A safety kernel that only some layers can see is not a kernel.

Two constraints keep this from becoming a loophole:

1. **`platform` never imports another `fking` module**, including `domain`. It sits below everything, so nothing can be routed through it. If a helper in `platform` needs a `Position`, it is not a platform helper — it is domain policy in the wrong package, and moving it there is how a boundary quietly disappears.
2. **`platform` gets no trading vocabulary.** A function in `platform` named `size_position`, `should_trade` or `apply_limit` is a boundary violation regardless of what the import graph says, and [`./naming.md`](./naming.md) is what makes that visible in review.

There is no exception for `strategy` → `execution`, none for `execution` → HTTP libraries, and none for widening the allowlist "to test more easily". Wanting one means stopping and asking the user (`CLAUDE.md` §0).
