"""The event bus: Redis Streams, consumer groups, at-least-once, idempotent consumers.

ADR-0004 records why Redis Streams rather than Kafka, and the single sentence that
governs every consumer written against this package:

> **Delivery is at-least-once. Every consumer is idempotent by design, not by hope.**

That is a design constraint stated up front, not a property to discover in production.
`XAUTOCLAIM` makes duplicate delivery the *normal* case -- every restart reprocesses
whatever was in flight -- so the deduplication machinery is exercised continuously
instead of first running during an incident.

The bus is a **transport with bounded retention**, not a replay log. Streams are capped
with `MAXLEN ~`, and durable history lives in the append-only audit tables. Rebuilding a
projection reads Postgres, never the stream.

Correlation ids do not propagate ambiently across a Redis hop -- there is no context on
the other side -- so they travel as an envelope field, and `StreamConsumer` re-binds the
scope before calling a handler. Extraction lives here rather than in each consumer
because otherwise every new consumer breaks the chain the same way.

Everything not in `__all__` is private and may change without notice.
"""

from fking.platform.bus._client import build_redis
from fking.platform.bus._consumer import ConsumeReport, EventHandler, StreamConsumer
from fking.platform.bus._envelope import PAYLOAD_FIELD, EventEnvelope, PayloadError, content_digest
from fking.platform.bus._errors import BusError, DeadLetterReason, EventSchemaError
from fking.platform.bus._publisher import EventPublisher
from fking.platform.bus._registry import (
    RegisteredEvent,
    register_event,
    registered_events,
    resolve_event,
    schema_digest,
    unregister_all,
)

__all__ = [
    "PAYLOAD_FIELD",
    "BusError",
    "ConsumeReport",
    "DeadLetterReason",
    "EventEnvelope",
    "EventHandler",
    "EventPublisher",
    "EventSchemaError",
    "PayloadError",
    "RegisteredEvent",
    "StreamConsumer",
    "build_redis",
    "content_digest",
    "register_event",
    "registered_events",
    "resolve_event",
    "schema_digest",
    "unregister_all",
]
