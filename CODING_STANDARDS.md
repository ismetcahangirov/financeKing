# Coding Standards

`CLAUDE.md` §2 and §4 state the rules. This document gives each one a correct/incorrect pair and names the mechanism that enforces it, because a rule with no enforcement is a preference and preferences erode.

Every rule below is followed by **Enforced by**. Where that says "review only", the rule is weaker than it looks and you should be correspondingly more careful.

---

## 1. Money

### 1.1 `Decimal` for every price, quantity and monetary amount

```python
# WRONG
price = 0.1 + 0.2                       # 0.30000000000000004
notional = float(row["price"]) * qty

# WRONG — the subtle one
price = Decimal(0.1)                    # Decimal('0.1000000000000000055511151231257827')

# WRONG — the subtler one, and the most common in practice
price = Decimal(str(some_float))        # the float already lost precision; str() cannot recover it

# RIGHT
price = Decimal("0.1")
notional = Decimal(row["price"]) * base_quantity   # row["price"] is str, straight from the CSV/JSON reader
```

The third case is the one that survives review. `Decimal(str(x))` *looks* like the documented fix, and it is — but only when `x` was never a float. If `x` is a float, the damage happened before `str()` ran. The correct fix is almost never on the line you are looking at; it is at the boundary where the value was parsed. Trace every money value back to where it entered the process and make sure the parse produced a `str` or a `Decimal`, never a `float`.

This matters specifically because **`ccxt` returns floats in its unified structures.** `order["price"]` is a Python float. The raw exchange string is in `order["info"]["price"]`. Every ingestion point parses from `info`, never from the unified field.

Why: float error accumulates across thousands of fills, and the resulting drift between our position notional and the exchange's presents as a reconciliation failure. It looks like an exchange bug for about a day before anyone suspects arithmetic.

**Enforced by**: `mypy --strict` catches the declared-type cases. A custom `ruff` rule flags `float(` and float literals in `domain/`, `risk/`, and `execution/`. The `Decimal(str(...))` pattern and float-typed parses at boundaries are **review only** — this is the single highest-yield thing a reviewer checks.

### 1.2 Quantize at boundaries, never mid-computation

```python
# WRONG — compounds rounding error at every step
avg = (a.quantize(TICK) * qa + b.quantize(TICK) * qb) / (qa + qb)

# RIGHT — full precision through the computation, one quantize at the end
avg = (a * qa + b * qb) / (qa + qb)
# ROUND_DOWN: an overstated average entry understates realized loss.
avg = avg.quantize(price_tick, rounding=ROUND_DOWN)
```

The rounding mode is always explicit and always commented with its consequence. For quantities the direction has a hard consequence: rounding an available quantity **up** produces orders the exchange rejects for insufficient balance, and the rejection arrives as a generic error that costs an hour to attribute.

Never use the builtin `round()` on money. It applies banker's rounding to a `Decimal` via `__round__` and gives no place to state the mode.

**Enforced by**: `ruff` bans `round(` on any name matching the money-name conventions in §7. Explicit-mode-on-`quantize` is review only.

### 1.3 Exchange JSON may contain `NaN` and `Infinity`

Python's `json.loads` accepts bare `NaN`, `Infinity` and `-Infinity` — they are not valid JSON, but the stdlib parses them into floats by default and does so silently. A `NaN` that reaches a `Decimal` gives `Decimal("NaN")`, which compares `False` against everything, including itself. A risk limit check written as `if notional > limit: reject` therefore **passes** a `NaN` notional straight through.

```python
# WRONG
data = json.loads(response_body)

# RIGHT
def _reject_constant(literal: str) -> NoReturn:
    raise ExchangeProtocolError(f"non-finite literal in exchange response: {literal}")

data = json.loads(response_body, parse_float=Decimal, parse_constant=_reject_constant)
```

`parse_float=Decimal` also removes an entire class of the §1.1 problem at the source: numbers arrive as `Decimal` without ever having been a float.

**Enforced by**: one shared `fking.platform.serde.loads()` is the only permitted JSON entry point; `ruff` bans `json.loads` and `json.load` outside that module.

---

## 2. Time

### 2.1 Timezone-aware UTC everywhere; naive datetimes rejected at construction

```python
# WRONG
ts = datetime.utcnow()                          # returns a NAIVE datetime. Always wrong here.
ts = datetime.now()                             # local time, and naive
ts = datetime.fromtimestamp(ms / 1000)          # naive, and float division on a timestamp
ts = datetime.utcfromtimestamp(ms / 1000)       # naive; deprecated since 3.12

# RIGHT
ts = datetime.now(tz=UTC)                                    # only outside strategy/ and risk/; see §4
ts = datetime.fromtimestamp(ms / 1000, tz=UTC)
ts = UTC_EPOCH + timedelta(microseconds=micros)              # exact for microsecond sources
```

`datetime.utcnow()` is the trap that keeps working. It returns the right *instant* with no tzinfo, so it prints correctly, logs correctly, and only fails at the moment it is compared against an aware datetime — where it raises `TypeError`, or worse, silently round-trips through a serialisation boundary that assumes local time.

Domain constructors reject naive values:

```python
def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"timestamp must be timezone-aware UTC, got {value!r}")
    return value
```

Note the second half of the condition. `tzinfo is not None` is not enough — an aware datetime in `Europe/Baku` passes that check and then compares fine, sorts fine, and is four hours wrong in every bar alignment. Normalise or reject; do not accept.

Why this bites harder here than elsewhere: crypto trades continuously. There is no market open to make an off-by-hours error visibly absurd. A timezone bug shifts every feature by a constant and the backtest still produces a plausible equity curve.

**Enforced by**: `ruff` bans `datetime.utcnow`, `datetime.utcfromtimestamp`, and `datetime.now()` without a `tz` argument. Domain constructors reject at runtime. Postgres columns are `timestamptz`, never `timestamp` — a migration adding a naive `timestamp` column fails CI.

### 2.2 Monotonic time is for durations only, never for records

```python
# WRONG
audit_row.observed_at = time.monotonic()

# RIGHT
started = time.monotonic()
...
latency_seconds = time.monotonic() - started      # a duration, never persisted as a timestamp
audit_row.observed_at = clock.now()               # injected; see §4
```

`time.monotonic()`'s zero point is arbitrary and changes across process restarts. A monotonic value written to the audit log is uninterpretable the moment the process that wrote it exits — and the audit log's whole purpose is to be readable months later with no access to application memory (`ARCHITECTURE.md` §11).

**Enforced by**: review only. Worth a grep during review of any new instrumentation.

### 2.3 Exchange time beats local time for anything that happened at the exchange

A `Fill`'s timestamp is the exchange's, not ours. Our clock's relationship to the exchange's is an unknown offset plus network delay, and Binance rejects requests whose `timestamp` falls outside `recvWindow` (default 5000ms) with error `-1021` — meaning our clock skew is a real, observed quantity, not a theoretical one.

**Enforced by**: review only.

---

## 3. Immutability

### 3.1 Domain objects are frozen; transitions return new objects

```python
# WRONG
@dataclass
class Position:
    base_quantity: Decimal
    avg_entry: Decimal

    def add_fill(self, fill: Fill) -> None:
        self.base_quantity += fill.quantity
        self.avg_entry = ...

# RIGHT
@dataclass(frozen=True, slots=True)
class Position:
    base_quantity: Decimal
    avg_entry: Decimal

    def with_fill(self, fill: Fill) -> "Position":
        new_quantity = self.base_quantity + fill.quantity
        total_cost = self.base_quantity * self.avg_entry + fill.quantity * fill.price
        return replace(
            self,
            base_quantity=new_quantity,
            # ROUND_DOWN: an overstated average entry understates realized loss.
            avg_entry=(total_cost / new_quantity).quantize(PRICE_TICK, ROUND_DOWN),
        )
```

Naming convention: transitions are `with_*` and return `Self`. A method on a domain object that returns `None` is a mutation and is wrong by construction.

Why: a mutable `Position` is read by the OMS, the risk engine and the reconciler. When it is mutated, the resulting behaviour depends on read order, which depends on scheduling, which is not reproducible. You get a bug that exists only in the running system and never in a test.

### 3.2 Frozen is shallow — collection fields must be immutable types

```python
# WRONG — frozen, and still mutable
@dataclass(frozen=True)
class Portfolio:
    positions: list[Position]          # .append() works fine
    limits: dict[str, Decimal]         # so does __setitem__

# RIGHT
@dataclass(frozen=True, slots=True)
class Portfolio:
    positions: tuple[Position, ...]
    limits: Mapping[str, Decimal]      # constructed from MappingProxyType at the boundary
```

`frozen=True` prevents rebinding the attribute. It does nothing to the object the attribute points at. This is the immutability bug that passes review, because the class declaration has `frozen=True` right there at the top and the reviewer stops reading.

The same applies to Pydantic: `model_config = ConfigDict(frozen=True)` blocks assignment and does not deep-freeze a nested list.

**Enforced by**: `ruff` requires `frozen=True` on every dataclass in `domain/`. Mutable *field types* inside frozen classes are caught by a project `mypy` plugin that rejects `list`, `dict` and `set` annotations on `domain/` dataclass fields.

---

## 4. Purity in `strategy` and `risk`

### 4.1 No I/O, no clock, no unseeded randomness

```python
# WRONG
class MomentumStrategy:
    def evaluate(self, bars: BarWindow) -> Signal:
        if datetime.now(tz=UTC).hour < 8:          # clock read
            return Signal.flat()
        cfg = json.load(open("params.json"))       # I/O
        jitter = random.random()                   # unseeded
        ...

# RIGHT
class MomentumStrategy:
    def __init__(self, params: MomentumParams, rng: Generator) -> None:
        self._params = params                       # injected at construction
        self._rng = rng                             # np.random.default_rng(seed), injected

    def evaluate(self, bars: BarWindow, as_of: datetime) -> Signal:
        if as_of.hour < 8:                          # clock is a parameter
            return Signal.flat()
        ...
```

Two consequences that are not obvious until you need them:

**Replay.** An evolved strategy that read the clock cannot be re-scored against its own history, so its survival score cannot be recomputed when the scoring engine changes — and the scoring engine will change (`RELEASE_PROCESS.md` §3). Impure strategies are unscorable retroactively, which means they are unevolvable.

**Seeding.** Never call `random.seed()` or `np.random.seed()`. Those set global state, which any imported library may also set, and which is not isolated across concurrently running backtest folds. Inject a `numpy.random.Generator` built from an explicit seed. The seed is recorded in the run manifest so the run is reproducible from the manifest alone.

### 4.2 `strategy` cannot import `execution`

Structural, not advisory. A strategy has no import path to order construction. `ARCHITECTURE.md` §5 has the reasoning; the short version is that this system will write its own strategies via LLM agents, and an agent-authored strategy will size its own positions if the type system permits it.

**Enforced by**: `import-linter` contracts, run in `make check`. `ruff` bans `open(`, `requests`, `httpx`, `datetime.now`, `random.`, and `os.environ` inside `strategy/` and `risk/`. If a contract fails, the design is wrong — do not move the import into `ignore_imports`.

---

## 5. Typing

### 5.1 `mypy --strict`, no exceptions

Every `# type: ignore` is narrowed to a specific error code and carries an inline reason:

```python
# WRONG
import ccxt  # type: ignore

# RIGHT
import ccxt  # type: ignore[import-untyped]  # ccxt ships no stubs as of 4.5.70; wrapped in venues/_ccxt.py
```

Why the reason is mandatory: the next session has no memory of why the ignore was added, and an unexplained ignore is either removed (breaking the build) or copied (spreading the hole).

### 5.2 Untyped third-party surfaces are wrapped once, at the boundary

`ccxt` is untyped and returns `Any`. `Any` propagates silently through `--strict` — mypy will not complain about anything downstream of it, which means one untyped import can disable type checking across an entire call path without a single error being reported.

```python
# WRONG — Any leaks into the whole execution module
raw = self._exchange.fetch_order(order_id)
return Order(price=raw["price"], quantity=raw["origQty"])

# RIGHT — one typed adapter; Any stops here
def parse_order(raw: object) -> ExchangeOrder:
    payload = _require_mapping(raw, "order")
    info = _require_mapping(payload.get("info"), "order.info")
    return ExchangeOrder(
        order_id=_require_str(payload, "id"),
        price=Decimal(_require_str(info, "price")),          # from info, not the unified float field
        base_quantity=Decimal(_require_str(info, "origQty")),
    )
```

**Enforced by**: `mypy --strict` with `disallow_any_explicit` and `warn_return_any`; `--strict` alone does not catch `Any` flowing from an untyped import, so those two flags are set additionally in `pyproject.toml`.

### 5.3 Domain types over primitives where confusion is possible

`Symbol`, `OrderId`, `CorrelationId` are `NewType`s over `str`, not bare `str`. It costs one line and makes `place_order(symbol, order_id)` with the arguments swapped a type error rather than a production incident.

---

## 6. Error handling

### 6.1 Catch what you can handle; never catch to continue

```python
# WRONG
for msg in stream:
    try:
        apply(msg)
    except Exception as e:
        log.error("failed to apply: %s", e)
        continue

# RIGHT
for msg in stream:
    try:
        apply(msg)
    except DuplicateEventError:
        # At-least-once delivery: redelivery of an already-applied event is expected.
        metrics.duplicate_events.inc()
    except ExchangeProtocolError:
        # The payload is not what the venue contract says. Stop; do not guess.
        kill_switch.trip(reason="exchange protocol violation")
        raise
```

The wrong version converts a visible failure into silent wrong behaviour with real positions open. The loop keeps running, the position state diverges from the exchange, and the log line scrolls past.

Note that the right version also demonstrates the useful distinction: `DuplicateEventError` is *expected* under the bus's at-least-once contract and is handled. `ExchangeProtocolError` is not, and stops the system.

### 6.2 Retryable vs terminal is a type, never a parsed message

```python
# WRONG
if "insufficient" in str(err).lower():
    ...

# RIGHT
class FkingError(Exception): ...
class TransientError(FkingError): ...          # retry with backoff
class TerminalError(FkingError): ...           # do not retry; escalate
class InsufficientBalanceError(TerminalError): ...
class RecvWindowExceededError(TransientError): ...   # Binance -1021; clock skew, resync and retry
```

Exchange error strings are not a stable interface. They change in minor API revisions and vary by endpoint for the same underlying condition. Map the numeric error code to a type once, at the boundary, and branch on the type everywhere else.

### 6.3 Validate at boundaries, then trust internally

The boundaries are: the HTTP/WS API, exchange responses, config files, and agent output. Inside those, types are trusted and no defensive re-checking happens — defensive checks in the interior hide the fact that the boundary is not doing its job.

Exchange responses are hostile input:

```python
# WRONG
price = response["result"][0]["price"]

# RIGHT
price = Decimal(_require_str(_require_index(_require_list(response, "result"), 0), "price"))
```

**Enforced by**: `ruff` bans bare `except Exception` and `except:` outside a small allowlist of top-level supervisor loops that re-raise after tripping the kill switch. Boundary validation is review only.

---

## 7. Naming

Names state units and intent. In a trading system, an ambiguous name is a correctness problem, not a readability one.

| Banned alone | Use |
|---|---|
| `size` | `base_quantity`, `notional_usd`, `contract_count` |
| `price` | `quote_price`, `mark_price`, `limit_price`, `decision_price` |
| `amount` | `notional_usd`, `fee_usd`, `base_quantity` |
| `qty` | `base_quantity`, `filled_base_quantity`, `remaining_base_quantity` |
| `timeout` | `timeout_seconds` |
| `interval`, `period` | `interval_seconds`, `lookback_bars` |
| `value` | whatever it actually is |
| `data` | whatever it actually is |

```python
# WRONG
def size_position(price: float, size: float, timeout: float) -> float: ...

# RIGHT
def size_position(
    decision_price: Decimal,
    account_equity_usd: Decimal,
    timeout_seconds: float,
) -> Decimal:  # returns base_quantity
```

`size` is singled out because in a trading system it means base quantity, notional, contract count, or leverage depending on who wrote the line, and every one of those is a different number by orders of magnitude.

Unit suffix conventions, used consistently: `_usd`, `_seconds`, `_ms`, `_bars`, `_bps`, `_pct` (where `_pct` is 0–100 and a plain fraction is 0–1 and is named `_frac`).

**Enforced by**: a `ruff` naming rule rejects the banned bare names as parameters or attributes in `domain/`, `risk/` and `execution/`. Review elsewhere.

---

## 8. Comments

Comment *why*, never *what*. `CLAUDE.md` §4. The expansion:

### 8.1 Every non-obvious constant carries a source

```python
# WRONG
MAX_LEVERAGE = Decimal("3")
SPREAD_BPS = Decimal("0.16")

# RIGHT
# 3x: at 3x, a 33% adverse move is liquidation. BTC has printed 30%+ daily
# drawdowns in 2020-03 and 2021-05; 3x is the point at which a repeat of
# either does not end the account. See docs/adr/0009.
MAX_LEVERAGE = Decimal("3")

# 0.16bp: measured median top-of-book spread on Binance USD-M BTCUSDT
# production data, 2025-01 to 2025-06. NOT testnet — testnet measures 7.5bp
# with ~10x inflated volume and calibrating on it produces fiction.
# Recalibrate quarterly; script: scripts/calibrate_spread.py
PRODUCTION_SPREAD_BPS = Decimal("0.16")
```

A magic number in risk code with no provenance will eventually be "cleaned up" or "rounded to something sensible" by a session that does not know what it protects against. The comment is the only thing standing between the constant and that edit.

### 8.2 Comment the workaround with its expiry condition

```python
# RIGHT
# ccxt 4.5.70 returns futures fees in the settlement asset but spot fees in
# the fee asset, with no flag distinguishing them. Normalise here.
# Remove when ccxt unifies fee reporting across market types; link the
# upstream issue here so the expiry condition is checkable.
```

A workaround with no expiry condition becomes permanent, and then becomes load-bearing.

### 8.3 Do not comment what the code says

```python
# WRONG
i += 1  # increment i
# Loop over the positions
for position in positions:
```

**Enforced by**: review only, and this is one of the genuinely important review checks. See `CODE_REVIEW.md` §3.

---

## 9. Imports

### 9.1 Absolute imports, `from __future__ import annotations`, `TYPE_CHECKING` for type-only

```python
# WRONG
from ..domain.position import Position
from .helpers import compute

# RIGHT
from __future__ import annotations

from typing import TYPE_CHECKING

from fking.domain.position import Position

if TYPE_CHECKING:
    from fking.execution.venue import ExecutionVenue     # type-only; no runtime import
```

Relative imports break when a module moves and make `import-linter` contracts harder to read. `from __future__ import annotations` makes all annotations strings, so type-only imports cost nothing at runtime.

### 9.2 Function-level imports are banned

This one is not about style.

**`import-linter` performs static analysis of module-level imports. An import inside a function body is invisible to it.** A deferred import is therefore a mechanism — accidental or otherwise — for `strategy` to reach `execution`, or for `execution` to construct a raw `httpx` client, with every architecture contract still reporting green.

```python
# WRONG — passes lint-imports, breaks the architecture
def evaluate(self, bars: BarWindow) -> Signal:
    from fking.execution.oms import OrderManager     # invisible to import-linter
    ...

# RIGHT
# If you need it at module level and cannot have it, the dependency direction
# is wrong. Restructure, or pass the dependency in (§11).
```

There is exactly one permitted exception, and it is documented in `pyproject.toml` with the module named: breaking a genuine circular import at application-composition time in `fking.platform.container`. Anywhere else, a function-level import is a blocking review finding.

### 9.3 Import order

stdlib, third-party, first-party (`fking.*`), local — separated by blank lines, alphabetised within group.

**Enforced by**: `ruff` (isort rules, `TID252` for relative imports, `required-imports` for the future annotations line) and a `ruff` rule flagging imports inside function bodies. `import-linter` enforces the layering.

---

## 10. Async

### 10.1 Structured concurrency; no orphan tasks

```python
# WRONG
asyncio.create_task(self._poll_orders())         # nobody holds a reference; GC may collect it mid-flight,
                                                 # and an exception inside it is never seen

# RIGHT
async with asyncio.TaskGroup() as tg:
    tg.create_task(self._poll_orders())
    tg.create_task(self._consume_bus())
# TaskGroup cancels siblings and re-raises the first exception on exit.
```

A bare `create_task` whose result is never awaited swallows exceptions into a "Task exception was never retrieved" warning at interpreter shutdown — which is to say, hours after the failure, in a log nobody reads, having already left positions in an unknown state.

### 10.2 Never block the event loop — and know exactly why here

```python
# WRONG
async def on_bar(self, bar: Bar) -> None:
    features = compute_all_features(self._history)   # 400ms of pandas, synchronous
    ...

# RIGHT
async def on_bar(self, bar: Bar) -> None:
    features = await asyncio.to_thread(compute_all_features, self._history)
```

The specific consequence in this system: the Binance user-data WebSocket sends a ping and expects a pong. Miss the response window and the server closes the connection. A synchronous 400ms feature computation on the event loop does not just add latency — repeated across a bar boundary where every strategy computes at once, it starves the WS keepalive and **drops the user-data stream**. The stream drop then causes missed fill events, and missed fill events cause a position divergence that surfaces at the next reconciliation as an apparent exchange problem.

Anything CPU-bound over ~10ms goes to `asyncio.to_thread` or a process pool. `Decimal` arithmetic in tight loops and pandas operations both qualify.

### 10.3 Rate limiting is state, not `sleep`

```python
# WRONG
await asyncio.sleep(0.1)     # "stay under the rate limit"

# RIGHT — a shared token bucket, keyed on the venue's weight accounting
await self._rate_limiter.acquire(weight=REQUEST_WEIGHTS["fetch_open_orders"])
```

Binance rate limits are weight-based and shared across the whole API key, not per-endpoint and not per-process. A `sleep` in one coroutine knows nothing about the other five, and knows nothing at all after a process restart — the exchange's window keeps counting across our restart, so the first burst after a crash-loop is what gets the key banned.

### 10.4 Cancellation is not an error

```python
# WRONG
try:
    await self._run()
except Exception:
    ...
except asyncio.CancelledError:
    log.error("cancelled")      # unreachable in 3.12: CancelledError derives from BaseException

# RIGHT
try:
    await self._run()
except asyncio.CancelledError:
    await self._flush_pending_audit_rows()
    raise                        # always re-raise; swallowing it breaks TaskGroup and shutdown
```

**Enforced by**: `ruff` bans bare `asyncio.create_task` outside a `TaskGroup` context and flags `except asyncio.CancelledError` without a `raise`. §10.2 and §10.3 are review only.

---

## 11. Dependency injection over globals

### 11.1 No module-level singletons

```python
# WRONG
engine = create_engine(os.environ["DATABASE_URL"])        # runs at import time
settings = Settings()                                     # ditto

class RiskEngine:
    def size(self, signal: Signal) -> Order:
        with Session(engine) as s: ...                    # reaches out and grabs it

# RIGHT
class RiskEngine:
    def __init__(self, limits: RiskLimits, clock: Clock) -> None:
        self._limits = limits
        self._clock = clock

# Wired once, in the composition root:
# fking/platform/container.py
def build_app(settings: Settings) -> Application: ...
```

Two reasons, one obvious and one not.

The obvious one: a global engine cannot be swapped for the test container, so tests either mock the database (forbidden — `TESTING.md` §4) or share state across tests.

The non-obvious one: **module-level construction executes at import time, which means it runs during pytest collection and during `lint-imports` static analysis.** A config validation error therefore surfaces as a collection error with a traceback pointing at the import machinery rather than at the bad config value, and `lint-imports` — which only wants to read the import graph — fails because it cannot reach the database. Both are twenty-minute debugging detours triggered by a one-character typo in an environment variable.

### 11.2 Inject the clock, the RNG, and the venue

These three are the dependencies that make or break reproducibility:

```python
class Clock(Protocol):
    def now(self) -> datetime: ...      # always tz-aware UTC

# Production: SystemClock. Backtest: BarClock, advanced by the engine.
# Tests: FixedClock.
```

`BarClock` is why backtest/live parity works at all. The strategy calls `clock.now()`; in a backtest that returns the bar's timestamp, so the strategy cannot see past the bar it is being shown. The same code in demo returns the real time. One code path, and no way for the strategy to cheat, because the only clock it can reach is the one it was handed.

### 11.3 Constructor injection, not a service locator

Pass dependencies to `__init__`. Do not pass a container and pull from it — that reintroduces the global with extra steps and makes the dependency invisible in the signature.

**Enforced by**: `ruff` flags module-level calls to `create_engine`, `Settings()`, `redis.Redis()` and `ccxt.*()`. The clock rule is enforced by the `datetime.now` ban in `strategy/` and `risk/` (§4). The rest is review.

---

## 12. What `make check` actually runs

```bash
make check
```

1. `ruff check` — including this project's custom rules referenced above
2. `ruff format --check`
3. `mypy --strict`
4. `lint-imports` — the `import-linter` architecture contracts
5. `tools/checks/` — the AST checks for rules no linter can express
6. `pytest`
7. `tools/coverage_floors.py` — the per-module coverage floors

Step 5 exists because four rules in this document cannot be written as a ruff rule or a
type: `money_types` rejects a float annotation on a money-shaped name, `clock_isolation`
rejects a wall-clock read inside `strategy/` or `risk/`, `no_catch_safety` rejects
`except SafetyViolation` and `except BaseException`, and `naming` rejects the ambiguous
trading nouns in §7. Each has a test asserting it catches a known violation as well as
one asserting it passes clean code — a check that cannot fail proves nothing.

Step 7 is separate from step 6 because `coverage.py` has one `fail_under`, and one
global number lets a well-tested utility subsidise untested risk logic. The floors are
enforced as separate report passes over the same data.

Every rule in this document is either in that list or is marked **review only**. If you add a rule to this document, add its check or mark it honestly — a rule that is neither enforced nor labelled unenforced is a rule everyone assumes someone else is checking.
