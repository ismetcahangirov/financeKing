"""Consuming: reclaim, claim, apply, commit, acknowledge -- in that order.

The ordering is the whole design, and the argument for it is an asymmetry.

**`XACK` after the commit** (what this does): a crash between the commit and the `XACK`
leaves the message in the pending-entries list. It is redelivered, the claim conflicts,
the effect is skipped, the ack runs. Cost: one wasted redelivery.

**`XACK` before the commit**: a crash after the ack and before the commit removes the
message from the PEL *and* rolls back the effect. The event is gone. A fill the exchange
recorded is absent from our books permanently, and no retry, no reclaim and no restart
brings it back -- only a full reconciliation sweep, and only if somebody runs one.

At-least-once with a deduplication table degrades to duplicated work. At-most-once
degrades to silent data loss with open positions. That decides the ordering, and it is
why the claim and the effect must share **one** transaction: two writes that must agree
cannot live in two transactions, because the process can die between them.

`XAUTOCLAIM` runs before `XREADGROUP` on every pass, which makes duplicate delivery the
*normal* case rather than an edge case. The deduplication machinery is then exercised on
every restart instead of first running during an incident.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fking.platform.bus._envelope import PAYLOAD_FIELD, EventEnvelope
from fking.platform.bus._errors import DeadLetterReason, EventSchemaError
from fking.platform.bus._registry import resolve_event
from fking.platform.config.settings import BusSettings
from fking.platform.correlation import correlation_scope
from fking.platform.logging import ORPHAN, get_logger
from fking.platform.telemetry import counter, traced
from fking.platform.telemetry._registry import (
    BUS_DEAD_LETTERED,
    BUS_EVENTS_CONSUMED,
    BUS_MESSAGES_RECLAIMED,
)

_LOG: Final = get_logger(__name__)

_CLAIM_SQL: Final = text(
    """
    INSERT INTO processed_events (consumer_group, idempotency_key, stream, message_id)
    VALUES (:consumer_group, :idempotency_key, :stream, :message_id)
    ON CONFLICT (consumer_group, idempotency_key) DO NOTHING
    RETURNING 1
    """
)

type EventHandler = Callable[[AsyncSession, EventEnvelope], Awaitable[None]]
"""The effect. Runs inside the claim's transaction, so a handler that raises rolls the
claim back with it -- which is what makes the message eligible for redelivery rather than
recorded as applied."""


@dataclass(frozen=True, slots=True)
class ConsumeReport:
    """What one pass over the stream did. Returned so a caller can assert on it."""

    applied_count: int = 0
    duplicate_count: int = 0
    dead_lettered_count: int = 0
    reclaimed_count: int = 0

    def merged_with(self, other: ConsumeReport) -> ConsumeReport:
        return ConsumeReport(
            applied_count=self.applied_count + other.applied_count,
            duplicate_count=self.duplicate_count + other.duplicate_count,
            dead_lettered_count=self.dead_lettered_count + other.dead_lettered_count,
            reclaimed_count=self.reclaimed_count + other.reclaimed_count,
        )


def _as_text(node: object) -> str:
    return node.decode() if isinstance(node, bytes) else str(node)


def _classify(invalid: ValidationError) -> DeadLetterReason:
    """Map a validation failure onto the closed reason enum.

    The three cases are genuinely different to an operator: bytes that are not JSON mean a
    producer or a transport is broken, a missing correlation id means a producer is not
    carrying the chain, and anything else is a schema disagreement. Collapsing them would
    make the dead-letter dashboard show one bar labelled "invalid".
    """
    errors = invalid.errors()
    if any(error["type"] in {"json_invalid", "json_type"} for error in errors):
        return DeadLetterReason.UNDECODABLE
    if any(error["loc"] == ("correlation_id",) for error in errors):
        return DeadLetterReason.MISSING_CORRELATION_ID
    return DeadLetterReason.SCHEMA_INVALID


def _entries(node: object) -> tuple[tuple[str, dict[str, str]], ...]:
    """Normalise a redis-py stream response into `(message_id, fields)` pairs.

    Written once, here, because `XREADGROUP` nests entries one level deeper than
    `XAUTOCLAIM` does and both differ again between RESP2 and RESP3. A normalisation done
    at two call sites is a normalisation that will be right at one of them.
    """
    if not isinstance(node, Sequence) or isinstance(node, str | bytes):
        return ()
    collected: list[tuple[str, dict[str, str]]] = []
    for entry in node:
        if not isinstance(entry, Sequence) or len(entry) != 2:  # noqa: PLR2004 - (id, fields)
            continue
        message_id, fields = entry[0], entry[1]
        if not isinstance(fields, Mapping):
            continue
        decoded = {_as_text(key): _as_text(mapped) for key, mapped in fields.items()}
        collected.append((_as_text(message_id), decoded))
    return tuple(collected)


class StreamConsumer:
    """One consumer group's view of one stream.

    `consumer_name` identifies this process within the group and must be stable across a
    restart: `XAUTOCLAIM` reclaims from *other* consumers' pending lists, so a process
    that comes back under a fresh name orphans its own previous entries until some other
    consumer's idle timer collects them.
    """

    __slots__ = (
        "_consumed",
        "_consumer_group",
        "_consumer_name",
        "_dead_lettered",
        "_handler",
        "_reclaimed",
        "_redis",
        "_session_factory",
        "_settings",
        "_stream",
    )

    def __init__(  # noqa: PLR0913 - one keyword per collaborator; a params object
        # would mean constructing a second object to construct this one
        self,
        *,
        redis: Redis,
        session_factory: async_sessionmaker[AsyncSession],
        settings: BusSettings,
        stream: str,
        consumer_group: str,
        consumer_name: str,
        handler: EventHandler,
    ) -> None:
        self._redis = redis
        self._session_factory = session_factory
        self._settings = settings
        self._stream = stream
        self._consumer_group = f"{settings.consumer_group_prefix}.{consumer_group}"
        self._consumer_name = consumer_name
        self._handler = handler
        self._consumed = counter(BUS_EVENTS_CONSUMED)
        self._dead_lettered = counter(BUS_DEAD_LETTERED)
        self._reclaimed = counter(BUS_MESSAGES_RECLAIMED)

    @property
    def consumer_group(self) -> str:
        return self._consumer_group

    @property
    def dead_letter_stream(self) -> str:
        return f"{self._stream}{self._settings.dlq_stream_suffix}"

    async def ensure_group(self) -> None:
        """Create the consumer group, and the stream with it if it does not exist.

        `mkstream=True` so a consumer may start before its producer. Without it a
        consumer that boots first fails with "no such key", which in an unattended system
        turns start-order into a correctness dependency.
        """
        try:
            await self._redis.xgroup_create(
                name=self._stream, groupname=self._consumer_group, id="0", mkstream=True
            )
        except ResponseError as already_exists:
            # BUSYGROUP is the expected response on every start after the first. Any other
            # ResponseError is a real fault and propagates: swallowing it would leave a
            # consumer running against a group that does not exist.
            if "BUSYGROUP" not in str(already_exists):
                raise

    async def run_once(self, *, batch_size: int = 32, block_ms: int = 1_000) -> ConsumeReport:
        """One pass: reclaim what is stranded, then read what is new.

        Reclaim first, always. A message stranded by a consumer that died mid-handler is
        the one an operator is waiting on; reading new messages first would let a busy
        stream starve it indefinitely.
        """
        report = ConsumeReport()

        reclaimed = await self._reclaim(batch_size=batch_size)
        if reclaimed:
            self._reclaimed.increment(
                len(reclaimed), stream=self._stream, consumer_group=self._consumer_group
            )
            report = report.merged_with(ConsumeReport(reclaimed_count=len(reclaimed)))
        for message_id, fields in reclaimed:
            report = report.merged_with(await self.handle_one(message_id, fields))

        fresh = await self._read(batch_size=batch_size, block_ms=block_ms)
        for message_id, fields in fresh:
            report = report.merged_with(await self.handle_one(message_id, fields))
        return report

    async def _reclaim(self, *, batch_size: int) -> tuple[tuple[str, dict[str, str]], ...]:
        response = await self._redis.xautoclaim(
            name=self._stream,
            groupname=self._consumer_group,
            consumername=self._consumer_name,
            min_idle_time=self._settings.claim_idle_ms,
            start_id="0-0",
            count=batch_size,
        )
        # The reply is [cursor, entries] on Redis 6.2 and [cursor, entries, deleted] from
        # 7.0. Indexing element 1 rather than unpacking so the shape change is not a
        # ValueError on whichever server the developer happens to run.
        if not isinstance(response, Sequence) or len(response) < 2:  # noqa: PLR2004 - [cursor, entries]
            return ()
        return _entries(response[1])

    async def _read(
        self, *, batch_size: int, block_ms: int
    ) -> tuple[tuple[str, dict[str, str]], ...]:
        response = await self._redis.xreadgroup(
            groupname=self._consumer_group,
            consumername=self._consumer_name,
            streams={self._stream: ">"},
            count=batch_size,
            block=block_ms,
        )
        if not isinstance(response, Sequence):
            return ()
        collected: tuple[tuple[str, dict[str, str]], ...] = ()
        for stream_reply in response:
            if isinstance(stream_reply, Sequence) and len(stream_reply) == 2:  # noqa: PLR2004
                collected += _entries(stream_reply[1])
        return collected

    async def handle_one(self, message_id: str, fields: Mapping[str, str]) -> ConsumeReport:
        """Decide one message: dead-letter it, skip it as a duplicate, or apply it.

        Returns rather than raises on a malformed message, and propagates on a handler
        failure. The distinction is deliberate: a message this consumer can never make
        sense of must leave the pending list or it blocks the group forever, while a
        handler that failed is usually the system being wrong about the world, and
        discarding the message would hide that.
        """
        envelope = await self._decode(message_id, fields)
        if envelope is None:
            return ConsumeReport(dead_lettered_count=1)

        # Correlation ids do not cross a Redis boundary ambiently -- there is no context
        # on this side of the stream. Re-binding it from the envelope, here in the base
        # class rather than in each handler, is what stops every new consumer breaking
        # the chain the same way. OBSERVABILITY.md section 3.
        with correlation_scope(envelope.correlation_id):
            with traced(
                "bus.consume",
                stream=self._stream,
                event_type=envelope.event_type,
                consumer_group=self._consumer_group,
            ):
                applied = await self._claim_and_apply(message_id, envelope)

            # Strictly after the transaction above committed. Nothing between the two.
            await self._redis.xack(self._stream, self._consumer_group, message_id)

            outcome = "applied" if applied else "duplicate"
            self._consumed.increment(
                stream=self._stream, consumer_group=self._consumer_group, outcome=outcome
            )
            # Two static event names rather than one interpolated one. A log event name
            # is a query key: `{message="bus.event_duplicate"}` is a Loki selector and
            # `bus.event_{outcome}` is a string nobody can select on.
            _LOG.info(
                "bus.event_applied" if applied else "bus.event_duplicate",
                stream=self._stream,
                consumer_group=self._consumer_group,
                event_type=envelope.event_type,
                event_id=envelope.event_id,
                message_id=message_id,
            )
        return ConsumeReport(applied_count=int(applied), duplicate_count=int(not applied))

    async def _claim_and_apply(self, message_id: str, envelope: EventEnvelope) -> bool:
        """Claim the event and run the handler in one transaction. True if applied."""
        async with self._session_factory() as session, session.begin():
            claimed = (
                await session.execute(
                    _CLAIM_SQL,
                    {
                        "consumer_group": self._consumer_group,
                        "idempotency_key": envelope.event_id,
                        "stream": self._stream,
                        "message_id": message_id,
                    },
                )
            ).first()
            if claimed is None:
                return False
            await self._handler(session, envelope)
            return True

    async def _decode(self, message_id: str, fields: Mapping[str, str]) -> EventEnvelope | None:
        """Parse and validate, or dead-letter with a reason and return None."""
        raw = fields.get(PAYLOAD_FIELD)
        if raw is None:
            await self._dead_letter(message_id, fields, DeadLetterReason.UNDECODABLE)
            return None
        try:
            envelope = EventEnvelope.model_validate_json(raw)
        except ValidationError as invalid:
            await self._dead_letter(message_id, fields, _classify(invalid), detail=str(invalid))
            return None

        try:
            registration = resolve_event(envelope.event_type, envelope.schema_version)
        except EventSchemaError as unregistered:
            await self._dead_letter(
                message_id,
                fields,
                DeadLetterReason.UNREGISTERED_EVENT_TYPE,
                detail=str(unregistered),
                correlation_id=str(envelope.correlation_id),
            )
            return None
        try:
            registration.payload_model.model_validate(dict(envelope.payload))
        except ValidationError as invalid_payload:
            await self._dead_letter(
                message_id,
                fields,
                DeadLetterReason.SCHEMA_INVALID,
                detail=str(invalid_payload),
                correlation_id=str(envelope.correlation_id),
            )
            return None
        return envelope

    async def _dead_letter(
        self,
        message_id: str,
        fields: Mapping[str, str],
        reason: DeadLetterReason,
        *,
        detail: str = "",
        correlation_id: str | None = None,
    ) -> None:
        """Move a message this consumer cannot make sense of aside, and acknowledge it.

        The original bytes are preserved verbatim under `original`. A dead-letter record
        that has been reformatted cannot be replayed once the schema is fixed, which is
        the only reason to keep one.
        """
        await self._redis.xadd(
            name=self.dead_letter_stream,
            fields={
                "original": fields.get(PAYLOAD_FIELD, ""),
                "reason": str(reason),
                "detail": detail,
                "source_stream": self._stream,
                "source_message_id": message_id,
                "consumer_group": self._consumer_group,
            },
            maxlen=self._settings.max_stream_length,
            approximate=True,
        )
        await self._redis.xack(self._stream, self._consumer_group, message_id)
        self._dead_lettered.increment(stream=self._stream, reason=str(reason))
        self._consumed.increment(
            stream=self._stream, consumer_group=self._consumer_group, outcome="dead_lettered"
        )
        # A dead-lettered message may still have a usable correlation id -- an
        # unregistered event type says nothing about the chain it belongs to. Only the
        # undecodable and missing-id cases genuinely have none, and those are the two
        # where inventing one would be the error.
        _LOG.error(
            "bus.event_dead_lettered",
            correlation_id=correlation_id or ORPHAN,
            stream=self._stream,
            dlq_stream=self.dead_letter_stream,
            consumer_group=self._consumer_group,
            message_id=message_id,
            reason=str(reason),
        )
