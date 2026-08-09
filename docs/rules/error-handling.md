# Rule — Error Handling

## The rule

Fail loudly and early. Every error this system raises is a member of a declared taxonomy, every `raise` inside an `except` block carries `from err`, and no code catches `Exception` to keep a loop alive. Retry only what has been *classified* as transient, with bounded attempts and jittered backoff. An unexpected state trips the kill switch; it does not continue with open positions.

## Why

A trading system that continues after an unexpected state is more dangerous than one that stops. That sentence is in `CLAUDE.md` §4 and it is the whole rule, but the mechanism deserves spelling out.

Consider the loop that catches `Exception` and continues. The exception was a `ConnectionError` on the fill-polling call. The loop logs it and sleeps. Meanwhile the position is open, the trailing stop is computed from a fill the system never observed, the reconciler's next pass sees a divergence it attributes to latency, and the kill switch — which triggers on *drawdown*, computed from a PnL derived from fills — has no idea. The visible failure (a stack trace, a crashed process, a restart) has been converted into invisible wrong behaviour with real exposure. The crash was the safe outcome. `CLAUDE.md` §11 names this as an anti-pattern for precisely that reason.

The second mechanism is swallowing into a log line. `log.error("failed to parse fill", error=str(exc))` followed by `return None` produces a system where every caller must treat every return as possibly-meaningless, and where the audit trail contains a log entry but no state transition. `ARCHITECTURE.md` §11 requires that any trade be fully reconstructable from the audit log alone, months later. A swallowed error is a hole in that reconstruction exactly where the interesting thing happened.

The third is misclassified retries. Retrying a `PermanentExchangeError` — a bad signature, an unknown symbol, a quantity below the minimum — burns the rate limit budget and delays the real failure by the full backoff schedule. Retrying a *submission* that may have succeeded creates a duplicate order. Retry is a decision that requires knowing which kind of error you have, which is why classification comes first and retry is a consequence.

## The taxonomy

```python
# src/fking/platform/errors.py
"""The complete error taxonomy. Nothing in fking raises an exception outside this tree."""


class FkingError(Exception):
    """Base for every error this system raises deliberately."""


class ConfigError(FkingError):
    """Invalid or missing configuration. Raised at startup; never recoverable at runtime."""


class DomainError(FkingError):
    """A domain invariant was violated — naive datetime, negative quantity, impossible state."""


class RiskViolation(FkingError):
    """A risk limit would be breached. Not an error condition; a refusal, and it is audited."""


class ExchangeError(FkingError):
    """The venue rejected or failed a request."""

    def __init__(self, message: str, *, venue: str, http_status: int | None, venue_code: int | None) -> None:
        super().__init__(message)
        self.venue = venue
        self.http_status = http_status
        self.venue_code = venue_code


class TransientExchangeError(ExchangeError):
    """Retryable: timeout, 5xx, rate limit, connection reset. The same request may succeed."""


class PermanentExchangeError(ExchangeError):
    """Not retryable: bad signature, unknown symbol, filter rejection, insufficient balance."""


class DataIntegrityError(FkingError):
    """Ingested data failed a validation invariant — checksum, epoch range, schema."""


class SafetyViolation(BaseException):
    """A request was addressed to a host outside the compiled-in allowlist.

    Inherits BaseException, not Exception, so that no `except Exception` anywhere in
    this process — including inside third-party libraries we do not control — can
    catch it. There is no handler for this exception. The process dies.
    """
```

`SafetyViolation(BaseException)` is the single most important line in this file. `ccxt`, `httpx` and the async machinery all contain `except Exception` blocks written by people with no knowledge of our prime directive; a `SafetyViolation` raised inside a request hook and derived from `Exception` could be caught, logged as a network problem, and retried against the non-allowlisted host. Deriving from `BaseException` makes that impossible without someone writing `except BaseException` on purpose — which the AST check below rejects outright.

`RiskViolation` being an exception rather than a return value is deliberate: a refused order must be impossible to ignore by forgetting to check a result. It is caught in exactly one place, the order path, where it is turned into an audited rejection record.

## Incorrect

```python
import asyncio

import structlog

log = structlog.get_logger()


async def poll_fills(venue: Venue, order_id: str) -> None:
    while True:
        try:
            payload = await venue.fetch_order(order_id)
            fill = Fill(
                fill_id=payload["fillId"],
                quote_price=Decimal(str(payload["price"])),
                base_quantity=Decimal(str(payload["qty"])),
            )
            await apply(fill)
        except Exception as exc:                       # keeps the loop alive
            log.error("poll failed", error=str(exc))   # swallowed into a log line
        await asyncio.sleep(1)


def parse_balance(payload: dict) -> Decimal:
    try:
        return Decimal(payload["balances"][0]["free"])
    except (KeyError, IndexError, TypeError):
        return Decimal("0")                            # a default that means "I do not know"
```

The loop survives everything: a `KeyError` because the venue renamed `fillId`, a `SafetyViolation` if it were derived from `Exception`, a `DomainError` from a malformed price, a `MemoryError`. It logs one line per second and never applies another fill, while the position stays open and the risk engine's PnL freezes at its last known value — so the drawdown kill switch will never fire no matter how far the market moves. The process looks healthy. Metrics look healthy. Nothing alerts, because "error rate above zero" was tuned out months ago by exactly this loop.

`parse_balance` is the same failure in miniature. A zero balance is indistinguishable from an empty account, so the sizing logic downstream computes a position of zero, or — worse, on the reverse case — a drawdown of 100% and trips the kill switch for no reason. `CLAUDE.md` §4: exchange responses are hostile input; never index into them optimistically.

## Correct

```python
import asyncio
import random
from collections.abc import Callable, Coroutine
from typing import TypeVar

import structlog

from fking.platform.errors import PermanentExchangeError, TransientExchangeError

log = structlog.get_logger()

T = TypeVar("T")


async def poll_fills(venue: Venue, order_id: str, clock: Clock, kill_switch: KillSwitch) -> None:
    """Poll until the order reaches a terminal state.

    Transient venue failures are retried inside `with_retry`. Anything else is a
    condition this loop cannot reason about, so it flattens the book and propagates.
    """
    while True:
        try:
            payload = await with_retry(lambda: venue.fetch_order(order_id), attempts=5)
        except PermanentExchangeError as err:
            await kill_switch.trip(reason=f"order {order_id} unresolvable: {err}", clock=clock)
            raise
        except DomainError as err:
            await kill_switch.trip(reason=f"order {order_id} produced invalid state: {err}", clock=clock)
            raise

        order_update = parse_order_update(payload)     # raises PermanentExchangeError on a schema mismatch
        for fill in order_update.fills:
            await apply(fill)                          # idempotent by fill_id
        if order_update.is_terminal:
            return
        await asyncio.sleep(1)


async def with_retry(
    operation: Callable[[], Coroutine[None, None, T]],
    *,
    attempts: int,
    base_delay_seconds: float = 0.5,
    max_delay_seconds: float = 30.0,
    rng: random.Random = random.SystemRandom(),
) -> T:
    """Retry `operation` on classified transient failures only.

    Full jitter: sleeping the full capped exponential deterministically synchronises
    every retrying client onto the same schedule, which is how a rate limit becomes a
    thundering herd against the venue that is already struggling.
    """
    last_error: TransientExchangeError | None = None
    for attempt in range(attempts):
        try:
            return await operation()
        except TransientExchangeError as err:
            last_error = err
            if attempt == attempts - 1:
                break
            ceiling = min(max_delay_seconds, base_delay_seconds * 2**attempt)
            await asyncio.sleep(rng.uniform(0.0, ceiling))
            log.warning("retrying transient venue error", attempt=attempt + 1, venue_code=err.venue_code)

    raise PermanentExchangeError(
        f"exhausted {attempts} attempts",
        venue=last_error.venue,
        http_status=last_error.http_status,
        venue_code=last_error.venue_code,
    ) from last_error
```

Classification happens where the venue's vocabulary is understood — in the adapter, not at the call site:

```python
def classify_venue_failure(status: int, body: str, venue: str) -> ExchangeError:
    """Map a venue response onto the taxonomy. Unrecognised codes are permanent by default."""
    payload = json.loads(body, parse_float=Decimal)
    code = payload.get("code")
    message = str(payload.get("msg", body[:200]))

    if status in _RETRYABLE_HTTP_STATUSES or code in _RETRYABLE_VENUE_CODES:
        return TransientExchangeError(message, venue=venue, http_status=status, venue_code=code)
    return PermanentExchangeError(message, venue=venue, http_status=status, venue_code=code)
```

Unknown codes classify as **permanent**. The alternative — defaulting to transient — means a novel failure is retried five times with backoff on every request forever, which converts an unknown condition into a self-inflicted outage. Defaulting to permanent surfaces the unknown condition immediately, and adding the code to `_RETRYABLE_VENUE_CODES` is a one-line change with a comment citing where the code came from.

Retry is only safe because order submission carries a client-supplied idempotency key, so a retried submission that already succeeded is recognised by the venue rather than duplicated. Retrying a non-idempotent write is not covered by this rule; it is a bug.

And parsing that refuses to guess:

```python
def parse_free_balance(payload: Mapping[str, Any], asset: str, venue: str) -> Decimal:
    try:
        balances = payload["balances"]
        entry = next(item for item in balances if item["asset"] == asset)
        return Decimal(entry["free"])
    except (KeyError, StopIteration, TypeError, InvalidOperation) as err:
        raise PermanentExchangeError(
            f"balance payload missing a usable entry for {asset}",
            venue=venue,
            http_status=None,
            venue_code=None,
        ) from err
```

`from err` is not decoration. It sets `__cause__`, which makes the traceback read `PermanentExchangeError ... caused by KeyError: 'balances'` — the venue-level message *and* the exact structural surprise that produced it. Without it, the original is attached as `__context__` and rendered under "During handling of the above exception, another exception occurred", which reads like an error in the error handler and sends the reader to the wrong file.

## Enforcement

**ruff:**

```toml
[tool.ruff.lint]
select = ["E", "F", "B", "N", "UP", "RUF", "TRY", "DTZ", "BLE", "FURB", "PL", "SIM", "ANN"]
ignore = [
    "FURB157",
    # raise-vanilla-args: this project deliberately puts context in the message at the
    # raise site — venue codes, ids, the value that failed. Moving it into the class
    # would produce one exception type per message.
    "TRY003",
]
```

The rules that carry this file: `BLE001` (blind `except Exception`), `E722` (bare `except:`), `B904` (`raise` inside `except` without `from`), `TRY002` (raising a vanilla `Exception` instead of a taxonomy member), `TRY004` (raise `TypeError` for type checks), `TRY201` (`raise err` where bare `raise` is meant), `TRY300` (a `return` in `try` that belongs in `else`), `TRY301` (raise-then-catch within the same `try`), `TRY400` (`log.error` in an exception handler where `log.exception` preserves the traceback), `TRY401` (redundant exception object in a logging call).

**AST check** at `tools/checks/no_catch_safety.py`, wired into the `checks` target from [`./decimal-and-money.md`](./decimal-and-money.md). `BLE001` does not cover `except BaseException`, and nothing in ruff knows that `SafetyViolation` is special:

```python
"""SafetyViolation is never caught, and BaseException is never caught."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

FORBIDDEN_HANDLERS: frozenset[str] = frozenset({"SafetyViolation", "BaseException"})


def handler_names(handler: ast.ExceptHandler) -> list[str]:
    node = handler.type
    if node is None:
        return ["<bare>"]
    parts = node.elts if isinstance(node, ast.Tuple) else [node]
    names: list[str] = []
    for part in parts:
        if isinstance(part, ast.Name):
            names.append(part.id)
        elif isinstance(part, ast.Attribute):
            names.append(part.attr)
    return names


def main(root: Path) -> int:
    failures: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            caught = set(handler_names(node)) & FORBIDDEN_HANDLERS
            if caught:
                failures.append(
                    f"{path}:{node.lineno} catches {sorted(caught)}; the safety kernel has no handler"
                )
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
```

The check runs over `src/fking` **and** `tests/`. A test that catches `SafetyViolation` to assert it was raised is the first step toward code that does — use `pytest.raises(SafetyViolation)`, which does not appear as an `except` clause in the AST.

**Tests that malformed input raises rather than defaults.** Against recorded real venue responses, mutated — not hand-written fixtures (`CLAUDE.md` §5):

```python
import pytest

from fking.platform.errors import PermanentExchangeError


def test_balance_payload_missing_the_asset_raises(recorded_response: Mapping[str, Any]) -> None:
    payload = {"balances": [entry for entry in recorded_response["balances"] if entry["asset"] != "USDT"]}
    with pytest.raises(PermanentExchangeError, match="missing a usable entry for USDT"):
        parse_free_balance(payload, asset="USDT", venue="binance-testnet")


def test_unknown_venue_code_classifies_as_permanent() -> None:
    error = classify_venue_failure(status=400, body='{"code": -9999, "msg": "new thing"}', venue="binance-testnet")
    assert isinstance(error, PermanentExchangeError)


@pytest.mark.parametrize("attempts", [1, 3, 5])
def test_retry_never_retries_a_permanent_error(attempts: int) -> None:
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise PermanentExchangeError("nope", venue="v", http_status=400, venue_code=-1121)

    with pytest.raises(PermanentExchangeError):
        asyncio.run(with_retry(operation, attempts=attempts))
    assert calls == 1
```

The third test is the one that decays if unwritten: `with_retry` catching `ExchangeError` instead of `TransientExchangeError` is a one-character-class mistake that no type checker sees and that only shows up as an unexplained five-fold rate-limit spike in production.

## The one exception

The top-level supervisor loop — one function, `fking.platform.supervisor.run`, and nowhere else — may catch `Exception`. It may do exactly three things with it, in this order, and then it must exit non-zero:

```python
async def run(runtime: Runtime) -> int:
    try:
        await runtime.serve()
    except Exception as err:                                        # noqa: BLE001
        # The only sanctioned blind except in the codebase. We do not know what this is,
        # which is precisely why we must not continue: unknown state plus open positions
        # is the condition FAILSAFE.md exists to prevent.
        await runtime.kill_switch.trip(reason=f"unhandled: {type(err).__name__}: {err}")
        await runtime.execution.flatten_all()                       # close the book first
        await runtime.audit.record_fatal(error=err, correlation_id=runtime.correlation_id)
        log.exception("supervisor caught an unhandled exception; book flattened, exiting")
        return 1
    return 0
```

It flattens the book, writes the audit record, and exits. It does not log-and-continue, it does not restart the loop, it does not sleep and retry, and it does not swallow the exit code. Restart is the supervisor above this one's job — Docker Compose — and a restart from a clean process start reconciles against the exchange, which `ARCHITECTURE.md` §7 makes the source of truth. A restart from inside a corrupted process does not.

`SafetyViolation` is not caught here either, because it is a `BaseException`. It propagates through this handler untouched, the process dies without flattening, and that is correct: a request addressed to a host outside the allowlist means the system's model of what it is talking to is wrong, and issuing more orders — even closing ones — through that path is the worst available option. See `FAILSAFE.md` for the recovery procedure and [`./module-boundaries.md`](./module-boundaries.md) for the contracts that make this path the only one.
