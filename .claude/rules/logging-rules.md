# Rule — Logging

## The rule

`structlog` only, JSON renderer in every environment including local, and:

1. **Every event carries `correlation_id`.** It is minted once at the top of a data flow — the bar close, the schedule tick, the HTTP request — and is bound into the context, never passed as a parameter through twelve frames. A log call without it raises in tests.
2. **Every event carries `trace_id` and `span_id`** when a span is active, injected by a processor from the OpenTelemetry context. You never write them by hand.
3. **Structured key/value only. No f-strings, no `%s`, no `.format()`, no concatenation in a log call.** `logger.info("order_submitted", symbol=..., quantity=...)` — the first positional argument is a stable event name, everything else is a queryable field.
4. **Levels have defined meanings in this system** (table below) and are not chosen by feel.
5. **Never log a secret.** API keys, Ed25519 private keys, request signatures, session tokens, database URLs with passwords, `Authorization` headers. A redaction processor enforces this, and you write code as if it did not exist.
6. **Never log full account balances at INFO.** Equity, per-asset free/locked, and position notionals go to WARNING and above, or to the audit table.
7. **Never log an LLM prompt or response into the log stream.** They go to the append-only audit table (`./append-only-audit.md`); the log line carries `audit_ref` — the audit row id — and nothing else from the payload. See `./llm-output-handling.md`.
8. **Log once, at the boundary that owns the failure.** Not at every frame on the way up.
9. **Logging is not error handling.** A `try/except` whose body is a `logger.error` and a `return None` has converted a visible failure into a silent wrong answer with positions open (`../../CLAUDE.md` §4).

## Why

The governing requirement in `../../ARCHITECTURE.md` §11 is that **any trade must be fully reconstructable from the audit log alone, months later, with no access to application memory.** Every clause above is derived from that sentence rather than from taste.

`correlation_id` is what makes reconstruction possible at all. Without it you have a pile of true statements about a system and no way to know which ones describe the same trade — and in a system where the bar close, three strategies, one risk decision and two venue round-trips interleave with a concurrent evolution cycle, timestamp proximity is not a join key.

F-strings are worse than ugly here. `logger.info(f"submitted {qty} {symbol} at {price}")` produces a Loki stream you can grep but cannot aggregate: you cannot ask "p99 submitted quantity for BTCUSDT last week" without writing a regex against your own log format, and the regex breaks the first time someone reorders the sentence. Structured fields make the log a queryable dataset. That is the difference between an investigation that takes four minutes and one that takes a day.

The LLM payload rule is about two separate failures. Prompts and responses are large — a single research prompt with fenced market data runs tens of kilobytes — and Loki retention on a self-hosted single node is finite, so payloads in the log stream evict the operational history you need during an incident. Worse, log retention *expires*, and `../../ARCHITECTURE.md` §11 requires the exact prompt and response months later. A payload in Loki is a payload you will not have.

The log-once rule is about signal. A failure logged at the venue client, again at the OMS, again at the scheduler, and again at the task boundary produces four ERROR events for one problem, and your alert threshold is now calibrated against a multiplier that changes whenever someone adds a layer. The frame that can *do something* about the failure owns the log line; the frames above it re-raise.

## Incorrect

```python
import logging

log = logging.getLogger(__name__)


async def submit(self, order: Order, creds: Credentials) -> str | None:
    log.info(f"submitting {order.quantity} {order.symbol} key={creds.api_key}")
    try:
        result = await self._client.create_order(**order.to_params())
    except Exception as exc:
        log.error(f"order failed: {exc}")
        log.error("account state: %s", await self._client.fetch_balance())
        return None
    log.info(f"llm rationale that produced this: {order.signal.rationale}")
    return result["orderId"]
```

What goes wrong at runtime, in order of severity:

`creds.api_key` is now in Loki, in the container's stdout, and in any log shipper's buffer — and Loki has no delete-by-line, so the only remediation is rotating the key and hoping. `except Exception` plus `return None` means a `-2010 insufficient balance` and a `TimeoutError` after the order was accepted produce the identical outcome: the caller sees `None`, records no order, and the position is now real and unknown to the system — the exact reconciliation divergence that `./exchange-integration.md` exists to prevent. `fetch_balance()` in an except block is an unbounded network call on the failure path and dumps full equity into the log. The f-strings make `quantity`, `symbol` and the error class unqueryable. There is no `correlation_id`, so this line cannot be joined to the signal that caused it. And `result["orderId"]` indexes an exchange response optimistically, so a `-1021` error envelope raises `KeyError` — which the bare `except` above has already taught you to ignore.

## Correct

```python
import structlog

from fking.platform.errors import VenueRejected

log = structlog.get_logger(__name__)


async def submit(self, order: Order) -> VenueOrderId:
    """Submit an order. Raises; never returns a sentinel.

    correlation_id is already bound into the context by the caller that minted it;
    it is not a parameter here because a parameter can be forgotten.
    """
    log.info(
        "order.submitting",
        symbol=order.symbol,
        side=order.side.value,
        base_quantity=str(order.base_quantity),
        limit_price=str(order.limit_price),
        client_order_id=order.client_order_id,
        venue=self.venue_id,
    )
    try:
        response = await self._client.create_order(**order.to_params())
    except VenueRejected as exc:
        # Owned here: this frame knows the venue code and can classify it.
        # Callers re-raise without logging.
        log.error(
            "order.rejected",
            client_order_id=order.client_order_id,
            venue_code=exc.code,
            venue_message=exc.message,
            retryable=exc.is_retryable,
        )
        raise

    accepted = VenueOrderAck.model_validate(response)   # hostile input, parsed not indexed
    log.info(
        "order.accepted",
        client_order_id=order.client_order_id,
        venue_order_id=accepted.order_id,
        status=accepted.status.value,
        agent_audit_ref=order.signal.audit_ref,   # the row id, never the rationale text
    )
    return accepted.order_id
```

The `correlation_id` arrives via context, bound once where the flow begins:

```python
from fking.platform.logging import correlation_scope

async def on_bar_close(bar: Bar) -> None:
    with correlation_scope(f"bar-{bar.symbol}-{bar.close_time.isoformat()}"):
        signals = await run_strategies(bar)
        orders = await risk_engine.decide(signals)
        for order in orders:
            await venue.submit(order)
```

```python
# src/fking/platform/logging.py
from collections.abc import Iterator
from contextlib import contextmanager

import structlog


@contextmanager
def correlation_scope(correlation_id: str) -> Iterator[None]:
    token = structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**token)
```

`contextvars` rather than a parameter because the binding survives `await` boundaries and `asyncio.TaskGroup` children, and because a parameter that thirty call sites must remember to pass will be forgotten by exactly the call site you need during an incident.

## Level semantics in this system

| Level | Means here | Alerts | Example event |
|---|---|---|---|
| `DEBUG` | Replayable detail — enough to reconstruct an intermediate computation. Off in the demo runtime, on when reproducing a specific `correlation_id`. | never | `feature.computed` with input digest and `as_of` |
| `INFO` | A state transition worth reconstructing later. If the system's state machine did not move, it is not INFO. | never | `order.accepted`, `strategy.promoted`, `reconciliation.converged` |
| `WARNING` | Degraded but still **correct**. The system did the right thing on a worse path. | dashboard, no page | `llm.failover` to Groq, `venue.reconnected`, `ratelimit.deferred` |
| `ERROR` | **A decision was not made.** A signal was dropped, an order was not submitted, a feature could not be computed, an agent response failed validation twice. | page if rate exceeds threshold | `order.rejected`, `agent.output_invalid` |
| `CRITICAL` | The kill switch fired or is about to. Trading has stopped or must stop. | page immediately | `killswitch.engaged`, `safety.violation`, `reconciliation.diverged` |

The load-bearing definition is ERROR. "An exception was raised" is not the criterion — a retryable timeout that succeeds on the second attempt is WARNING, because the decision was still made. "A decision was not made" is, because that is the thing an operator can act on and the thing that changes portfolio state by omission.

`CRITICAL` is reserved. If `CRITICAL` appears for anything other than a halt condition, the page loses its meaning and `../../FAILSAFE.md` becomes advisory.

## Enforcement

**Processor chain** — `src/fking/platform/logging.py`, order matters:

```python
from __future__ import annotations

import json
from typing import Any, Final

import structlog
from opentelemetry import trace

MAX_RECORD_BYTES: Final[int] = 8192

DENIED_KEY_SUBSTRINGS: Final[frozenset[str]] = frozenset({
    "api_key", "apikey", "api-key", "secret", "private_key", "privatekey",
    "ed25519", "signature", "password", "passphrase", "token", "credential",
    "authorization", "auth", "cookie", "session_key", "seed_phrase", "dsn",
})
BALANCE_KEY_SUBSTRINGS: Final[frozenset[str]] = frozenset({
    "balance", "balances", "equity", "free_", "locked_", "wallet",
})
PAYLOAD_KEYS: Final[frozenset[str]] = frozenset({"prompt", "response_text", "completion", "messages"})

EventDict = dict[str, Any]


class MissingCorrelationId(RuntimeError):
    """A log event was emitted with no correlation_id bound."""


class LoggedSecret(RuntimeError):
    """A denied key reached the renderer with a live value. Tests only."""


def bind_otel_context(_l: Any, _m: str, event_dict: EventDict) -> EventDict:
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = format(span_context.trace_id, "032x")
        event_dict["span_id"] = format(span_context.span_id, "016x")
    return event_dict


def require_correlation_id(_l: Any, _m: str, event_dict: EventDict) -> EventDict:
    if not event_dict.get("correlation_id"):
        raise MissingCorrelationId(event_dict.get("event", "<unnamed event>"))
    return event_dict


def redact(_l: Any, method_name: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict):
        lowered = key.lower()
        if any(token in lowered for token in DENIED_KEY_SUBSTRINGS):
            event_dict[key] = "[redacted]"
        elif key in PAYLOAD_KEYS:
            raise LoggedSecret(f"{key!r} belongs in the audit table; log audit_ref instead")
        elif any(token in lowered for token in BALANCE_KEY_SUBSTRINGS) and method_name in {
            "debug", "info"
        }:
            event_dict[key] = "[balance suppressed below WARNING]"
    return event_dict


def cap_record_size(_l: Any, _m: str, event_dict: EventDict) -> EventDict:
    encoded = json.dumps(event_dict, default=str, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError(
            f"log record {event_dict.get('event')!r} is {len(encoded)}B, cap is {MAX_RECORD_BYTES}B"
        )
    return event_dict
```

In the demo runtime `require_correlation_id` and `cap_record_size` are configured to *repair* rather than raise — an oversized event is truncated with `truncated=True`, a missing id becomes `correlation_id="orphan"` and increments `fking_log_orphan_total`. Raising inside a production log call would let logging take the process down, which inverts the point of the rule. Under `pytest` both raise. That split is the whole enforcement design: the test suite is where a violation must hurt.

**Tests** — `tests/unit/test_logging_processors.py`:

```python
import pytest

from fking.platform.logging import (
    DENIED_KEY_SUBSTRINGS, LoggedSecret, MissingCorrelationId, cap_record_size, redact,
    require_correlation_id,
)

SECRET_SHAPED_KEYS = sorted(
    {f"{p}{token}{s}" for token in DENIED_KEY_SUBSTRINGS for p in ("", "binance_", "spot_")
     for s in ("", "_value", "s")}
)


@pytest.mark.parametrize("key", SECRET_SHAPED_KEYS)
def test_every_secret_shaped_key_is_redacted(key: str) -> None:
    out = redact(None, "info", {"event": "x", key: "LIVE-VALUE-9f3a"})
    assert out[key] == "[redacted]"
    assert "LIVE-VALUE-9f3a" not in repr(out)


def test_missing_correlation_id_raises() -> None:
    with pytest.raises(MissingCorrelationId):
        require_correlation_id(None, "info", {"event": "order.accepted"})


def test_prompt_may_not_be_logged() -> None:
    with pytest.raises(LoggedSecret, match="audit_ref"):
        redact(None, "info", {"event": "agent.done", "prompt": "You are..."})


def test_record_size_cap() -> None:
    with pytest.raises(ValueError, match="cap is 8192B"):
        cap_record_size(None, "info", {"event": "dump", "blob": "x" * 9000})
```

The denylist test is generated from the denylist, so adding a token to `DENIED_KEY_SUBSTRINGS` automatically extends coverage and cannot be added without being exercised.

**ruff** — in `[tool.ruff.lint]`:

```toml
select = ["E", "F", "I", "UP", "B", "A", "C4", "G", "LOG", "TRY", "PT", "ARG", "RUF"]

[tool.ruff.lint.flake8-logging-format]
extra-mandatory-args = []
```

`G001`–`G004` catch `.format()`, `+`, `%` and f-strings in log calls; `G010` catches `logger.warn`; `G201`/`G202` catch `exc_info=True` used where `logger.exception` belongs and redundant exception objects. `TRY400` blocks `logger.error` inside an `except` where `logger.exception` is meant; `TRY401` blocks logging the exception object *and* passing `exc_info`, which duplicates the traceback in every record. `LOG015` catches logging on the root logger.

`structlog` does not route through `logging`'s format machinery, so `G` alone is not sufficient — a companion check in `tests/unit/test_no_interpolated_events.py` walks the AST of `src/fking/**` and fails on any call to a `structlog` logger method whose first positional argument is not a plain string literal. That is the rule that actually holds the line, because it also catches `log.info(f"...")` on a logger ruff did not recognise as a logger.

**Alerting** — `fking_log_orphan_total` and `fking_log_truncated_total` are Prometheus counters with alert rules; either firing means the enforcement is being bypassed in the demo runtime and is treated as a defect in the code, not a threshold to raise.

## The one exception

**Startup logs emitted before any correlation context exists bind `correlation_id="boot"`.**

Configuration loading, the safety-kernel allowlist dump (`../../ARCHITECTURE.md` §8 requires the allowlist be logged at every boot), migration status, venue-profile resolution and the OpenTelemetry exporter handshake all run before there is a bar, a request, or a schedule tick to derive an id from.

The exception is bounded three ways: the literal is `"boot"` and nothing else; it is bound exactly once, in `fking.platform.bootstrap.configure_logging()`, and reset before the first scheduler tick; and any event with `correlation_id="boot"` whose timestamp is more than 60 seconds after process start increments `fking_log_orphan_total`, because that means a code path outside startup is using the escape hatch. There is no `"unknown"`, no `"n/a"`, and no empty string — those would make the mandatory field satisfiable everywhere, which is the same as not having it.
