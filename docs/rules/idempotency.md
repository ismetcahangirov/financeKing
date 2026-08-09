# Rule — Idempotency

## The rule

**Redis Streams delivery is at-least-once. Every consumer is idempotent by design, not by hope** (`../../CLAUDE.md` §2).

The pattern, in full:

1. **Derive a stable idempotency key from the event's semantic content.** Never `uuid4()` at consumption time — that is a fresh key per delivery, which is the same as no key. Never the stream message id alone.
2. **Write a `processed_events` row in the same transaction as the effect**, with `ON CONFLICT (consumer_group, idempotency_key) DO NOTHING RETURNING 1`. No row returned means this event has already been applied; skip the effect.
3. **`XACK` only after that transaction commits.** Not before, not inside, not in a `finally`.
4. **Derive `clientOrderId` deterministically** so the exchange enforces the same property on the one effect the database cannot roll back.
5. **Reclaim stuck messages with `XAUTOCLAIM`**, which makes duplicates the normal case rather than the exceptional one.
6. **Reconcile against the exchange** as the backstop wherever the exchange is the source of truth.

## Why

The message id is not an identity. It is a delivery coordinate, and it fails in both directions:

- **A reclaimed message keeps its id.** `XAUTOCLAIM` hands a dead consumer's pending entry to a live one with the same `1724... -0` id. Deduping on message id here is correct but accidental.
- **A republished event gets a new id.** A producer retrying after a network timeout, a backfill re-emitting a day of fills, an operator replaying a stream after an incident — all produce a *new* message id for the *same* event. Deduping on message id lets every one of these through, and the second `apply_fill` appends a duplicate fill. Position quantity is now wrong, reconciliation reports a discrepancy against the exchange, and the discrepancy looks like an exchange bug.

The transaction boundary is the other half. Two writes that must agree — "the effect happened" and "we recorded that the effect happened" — cannot live in two transactions, because the process can die between them. Put them in one and the database decides atomically.

The `XACK` ordering follows from the same argument, and it is worth being explicit about which way to fail:

- **`XACK` after commit** (correct): a crash between commit and `XACK` leaves the message in the pending entries list. It is redelivered, the `INSERT` conflicts, the effect is skipped, `XACK` runs. Cost: one wasted redelivery.
- **`XACK` before commit** (wrong): a crash after `XACK` and before commit removes the message from the PEL *and* rolls back the effect. The event is gone. A fill that the exchange recorded is now absent from our books forever, and no retry, no reclaim and no restart will bring it back — only a full reconciliation sweep will, and only if someone runs one.

At-least-once with a dedupe table degrades to duplicated work. At-most-once degrades to silent data loss with open positions. That asymmetry decides the ordering.

## Incorrect

```python
# src/fking/execution/consumers/fills.py
async def consume_fills(redis: Redis, session_factory: async_sessionmaker) -> None:
    while True:
        batch = await redis.xreadgroup(
            groupname="oms", consumername="oms-1", streams={"fking.fills": ">"}, count=32
        )
        for _stream, messages in batch:
            for message_id, fields in messages:
                # Acknowledge first so a slow handler cannot stall the group.
                await redis.xack("fking.fills", "oms", message_id)
                fill = FillEvent.model_validate_json(fields[b"data"])
                async with session_factory() as session:
                    seen = await session.get(ProcessedEvent, message_id)
                    if seen is not None:
                        continue
                    session.add(ProcessedEvent(message_id=message_id))
                    await session.commit()
                    await apply_fill(session, fill)
                    await session.commit()
```

Four distinct runtime failures:

- **`XACK` first.** The process is SIGKILLed by the container runtime between the `xack` and the second `commit`. The fill is acknowledged, absent from the PEL, and never applied. Position quantity is short by that fill permanently; the next reconciliation reports a mismatch with no event to explain it.
- **Dedupe keyed on `message_id`.** The producer retried after a timeout and published the same fill twice with two ids. Both pass the `get()`, both call `apply_fill`, and the position doubles that fill.
- **Two commits.** The dedupe marker commits before the effect. If `apply_fill` raises — a constraint violation on an unrelated column, a serialisation failure — the marker survives the rollback of the effect. The event is now permanently marked processed and permanently not applied. This is the worst of the four because it is invisible: no error reaches the stream, no message stays pending, and the only evidence is a number that is wrong.
- **`continue` inside the `async with`.** The already-seen branch never re-`XACK`s (it was acked at the top, so this is masked here — but the moment someone fixes the ordering, this branch leaks pending entries and the PEL grows without bound).

## Correct

```python
# src/fking/platform/bus/idempotency.py
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ConsumedEvent:
    stream: str
    message_id: str  # a delivery coordinate, never an identity
    event_type: str
    event_id: str  # minted by the producer, stable across republishes
    subject_id: str
    schema_version: int
    payload: Mapping[str, Any]


def _canonical(value: Any) -> Any:
    """Decimal-safe canonicalisation. str(Decimal) preserves trailing zeros, so
    Decimal('1.50') and Decimal('1.5') would hash differently for the same
    economic quantity. Normalising here is what keeps the key stable across
    producers that format quantities differently."""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, Mapping):
        return {k: _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def idempotency_key(event: ConsumedEvent) -> str:
    """Stable across republication, stable across reclaim, stable across restart."""
    material = json.dumps(
        {
            "type": event.event_type,
            "id": event.event_id,
            "subject": event.subject_id,
            "version": event.schema_version,
            "payload": _canonical(event.payload),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
```

```sql
-- migrations/versions/0012_processed_events.py (op.execute body)
CREATE TABLE processed_events (
    consumer_group  text        NOT NULL,
    idempotency_key text        NOT NULL,
    stream          text        NOT NULL,
    message_id      text        NOT NULL,
    processed_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (consumer_group, idempotency_key)
);

-- Retention: a consumer cannot dedupe against rows it has pruned, so the window
-- must exceed the longest possible redelivery. XAUTOCLAIM min-idle is 30s and
-- the operator replay window is 7 days, so 30 days is the floor.
CREATE INDEX processed_events_processed_at_idx ON processed_events (processed_at);
```

```python
# src/fking/execution/consumers/fills.py
from __future__ import annotations

import structlog
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fking.platform.bus.idempotency import ConsumedEvent, idempotency_key

_LOG = structlog.get_logger(__name__)
_GROUP = "oms"
_STREAM = "fking.fills"

_CLAIM_SQL = text(
    """
    INSERT INTO processed_events
        (consumer_group, idempotency_key, stream, message_id)
    VALUES (:group, :key, :stream, :message_id)
    ON CONFLICT (consumer_group, idempotency_key) DO NOTHING
    RETURNING 1
    """
)


async def handle_one(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    event: ConsumedEvent,
) -> None:
    key = idempotency_key(event)
    async with session_factory() as session:
        async with session.begin():  # one transaction for claim AND effect
            claimed = (
                await session.execute(
                    _CLAIM_SQL,
                    {
                        "group": _GROUP,
                        "key": key,
                        "stream": event.stream,
                        "message_id": event.message_id,
                    },
                )
            ).first()
            if claimed is None:
                _LOG.info("duplicate_event_skipped", key=key, msg=event.message_id)
            else:
                await apply_fill(session, event)
        # session.begin() committed here. Nothing above this line may XACK.
    await redis.xack(_STREAM, _GROUP, event.message_id)


async def consume_fills(
    redis: Redis, session_factory: async_sessionmaker[AsyncSession], consumer: str
) -> None:
    while True:
        # Messages stranded in a dead consumer's PEL come back here. This is why
        # duplicate delivery is the normal case, not an edge case: every restart
        # after a crash reprocesses whatever was in flight.
        _cursor, reclaimed, _deleted = await redis.xautoclaim(
            name=_STREAM,
            groupname=_GROUP,
            consumername=consumer,
            min_idle_time=30_000,
            start_id="0-0",
            count=32,
        )
        fresh = await redis.xreadgroup(
            groupname=_GROUP,
            consumername=consumer,
            streams={_STREAM: ">"},
            count=32,
            block=5_000,
        )
        for message_id, fields in _flatten(reclaimed, fresh):
            await handle_one(session_factory, redis, parse_fill_event(message_id, fields))
```

```python
# src/fking/execution/order_id.py
from __future__ import annotations

import hashlib
from decimal import Decimal


def client_order_id(intent: OrderIntent, step_size: Decimal) -> str:
    """The exchange-side idempotency key.

    Binance rejects a duplicate newClientOrderId for an open order, so a retried
    placement after a timeout cannot double-fill. The quantity is quantised to
    the symbol's step size FIRST: Decimal('0.100') and Decimal('0.1') are the
    same order to the exchange and must produce the same id here.
    """
    quantity = intent.base_quantity.quantize(step_size)
    material = "|".join(
        [
            str(intent.correlation_id),
            intent.strategy_id,
            intent.symbol,
            intent.side,
            format(quantity, "f"),
            intent.order_type,
            intent.time_in_force,
            intent.decision_at.isoformat(),  # tz-aware UTC
        ]
    )
    digest = hashlib.blake2b(material.encode("utf-8"), digest_size=12).hexdigest()
    return f"fk-{digest}"  # 27 chars, inside Binance's 36-char newClientOrderId limit
```

### Which effects are already idempotent, and which are not

The dedupe table is only needed where the effect itself is not idempotent. Knowing which is which is what stops the table becoming ceremony applied everywhere and trusted nowhere.

| Effect | Idempotent alone? | Why |
|---|---|---|
| `INSERT ... ON CONFLICT (pk) DO UPDATE` of a bar or feature value | Yes | Same key, same value, same final state |
| Setting a strategy's lifecycle state to `RETIRED` | Yes | Assignment, not accumulation |
| Appending a fill row | **No** | Two appends, two fills, wrong position |
| `UPDATE positions SET quantity = quantity + :q` | **No** | Accumulation. This is the canonical wrong shape; store fills and derive quantity instead |
| Incrementing a metric counter | **No** | Silently inflates every rate derived from it |
| Placing an order | **No** at the database layer, **yes** at the exchange layer given a deterministic `clientOrderId` |
| Charging a trial (`./overfitting-defences.md`) | **No** | The ledger is monotone by design; a duplicate charge is permanent |
| Writing an audit row (`./append-only-audit.md`) | **No** | Append-only means a duplicate cannot be removed |

## Ordering is not idempotency

Deduplication makes a repeated event harmless. It does nothing about a *reordered* one, and Redis Streams gives per-stream order but not cross-stream or cross-partition order, and `XAUTOCLAIM` deliberately breaks even that: a reclaimed message from a 40-second-old PEL is delivered after messages published since.

So any effect that overwrites state carries a monotone version and refuses to go backwards:

```sql
INSERT INTO order_state (client_order_id, status, filled_quantity, venue_seq)
VALUES (:coid, :status, :filled, :venue_seq)
ON CONFLICT (client_order_id) DO UPDATE
   SET status          = EXCLUDED.status,
       filled_quantity = EXCLUDED.filled_quantity,
       venue_seq       = EXCLUDED.venue_seq
 WHERE order_state.venue_seq < EXCLUDED.venue_seq;
```

Without the `WHERE`, a reclaimed `NEW` event arriving after a `FILLED` event resurrects the order as open, and the OMS cancels a position it already closed. The `venue_seq` comes from the exchange's own update sequence, not from our clock — our clock is not the ordering authority for the exchange's state machine.

**Reconciliation is the backstop.** Where the exchange is the source of truth (`../../ARCHITECTURE.md` §7), correctness ultimately does not depend on the stream at all: the reconciler fetches open orders and `myTrades`, dedupes on the venue's `tradeId`, and converges local state to the exchange. This matters more here than in most systems — Binance spot testnet wipes roughly every 30 days, keeping keys while destroying balances and open orders, so the ability to rebuild the whole local view from the venue is exercised regularly rather than theoretically.

## Enforcement

**The replay harness** is the primary gate. It runs against real Postgres and a real Redis, feeds every event in a recorded scenario twice, and additionally permutes the order of independently-published events:

```python
# tests/platform/bus/test_replay.py
import pytest

from tests.support.state import canonical_state_digest


@pytest.mark.parametrize("scenario", ALL_RECORDED_SCENARIOS, ids=lambda s: s.name)
async def test_double_delivery_is_byte_identical(scenario, consumer, db) -> None:
    for event in scenario.events:
        await consumer.handle_one(event)
    once = await canonical_state_digest(db)

    for event in scenario.events:  # every event, a second time
        await consumer.handle_one(event)
    twice = await canonical_state_digest(db)

    assert once == twice


@pytest.mark.parametrize("scenario", REORDERABLE_SCENARIOS, ids=lambda s: s.name)
@pytest.mark.parametrize("permutation", [0, 1])
async def test_independent_events_are_order_insensitive(
    scenario, permutation, consumer, db
) -> None:
    events = scenario.events if permutation == 0 else scenario.reversed_independent()
    for event in events:
        await consumer.handle_one(event)
    assert await canonical_state_digest(db) == scenario.expected_digest
```

`canonical_state_digest` selects every mutable table ordered by primary key, serialises with `Decimal` normalised, and hashes — so "byte-identical final state" is an assertion, not a claim.

**The `XACK`-after-commit ordering** is enforced two ways, because a fault-injection test proves it for the paths exercised and a static check proves it for the paths not yet written:

```python
# tests/platform/bus/test_ack_ordering.py
import ast
import pathlib

import pytest

CONSUMER_ROOT = pathlib.Path("src/fking")


def _acks_inside_a_transaction(tree: ast.AST) -> list[int]:
    offending: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncWith):
            continue
        opens_transaction = any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr in {"begin", "begin_nested"}
            for item in node.items
        )
        if not opens_transaction:
            continue
        offending.extend(
            inner.lineno
            for inner in ast.walk(node)
            if isinstance(inner, ast.Attribute) and inner.attr == "xack"
        )
    return offending


@pytest.mark.parametrize(
    "path", sorted(CONSUMER_ROOT.rglob("consumers/*.py")), ids=str
)
def test_xack_is_not_reachable_inside_an_open_transaction(path: pathlib.Path) -> None:
    lines = _acks_inside_a_transaction(ast.parse(path.read_text(encoding="utf-8")))
    assert not lines, f"{path}: xack inside a transaction at lines {lines}"


async def test_commit_failure_leaves_the_message_pending(
    consumer, redis, failing_session_factory, event
) -> None:
    with pytest.raises(OperationalError):
        await consumer.handle_one(failing_session_factory, redis, event)
    pending = await redis.xpending("fking.fills", "oms")
    assert pending["pending"] == 1  # not acked, will be redelivered
```

**Schema-level guarantees.** The `PRIMARY KEY (consumer_group, idempotency_key)` on `processed_events` is what makes the claim atomic; without it `ON CONFLICT` has no arbiter and the statement errors at runtime. A migration test asserts the constraint exists and is a primary key, not a plain index. A second test asserts the `client_order_id` column on `orders` carries a `UNIQUE` constraint, so a duplicate placement fails locally before it reaches the venue.

## The one exception

**A read-model projection that is fully recomputable from scratch and writes only idempotent upserts keyed by primary key may skip the `processed_events` table.**

The conditions are conjunctive and all three are load-bearing:

1. **Fully recomputable.** There is a command that drops the projection and rebuilds it from the source-of-truth tables, and it runs in CI. If rebuilding requires the stream, the projection is not a read model — it is state, and state needs the dedupe table.
2. **Only upserts keyed by primary key.** No appends, no counters, no `quantity = quantity + ...`. One `INSERT ... ON CONFLICT (pk) DO UPDATE SET` per event, where re-applying the same event writes the same values.
3. **It still passes the double-replay test.** The exception removes a table, not a proof. `test_double_delivery_is_byte_identical` covers projections exactly as it covers consumers, and a projection that fails it does not qualify for the exception no matter how idempotent it looks.

Concretely: the dashboard's per-strategy equity-curve projection qualifies. The OMS order-state table does not, despite also being an upsert by primary key, because it is read by the reconciler to decide whether to cancel an order — that makes it operational state, and its `venue_seq` guard is a correctness requirement rather than a projection convenience.

`XACK` still happens only after commit. That part has no exception, in projections or anywhere else.
