"""Publishing: validate against the registry, then `XADD` with a length cap.

The cap is not tuning. A stalled consumer with an uncapped stream exhausts Redis memory,
and Redis is also the cache and the lock store, so the failure is not "the bus is behind"
-- it is every component that touches Redis failing at once, during whatever incident
stalled the consumer.

`MAXLEN ~` rather than `MAXLEN =`: the approximate form trims whole radix-tree nodes and
is O(1), where the exact form walks entries and is O(n) on the publish path. The
difference is a few hundred extra entries retained, and the stream is not a replay log --
durable history lives in the append-only audit tables (ADR-0004).
"""

from __future__ import annotations

from typing import Final

from redis.asyncio import Redis

from fking.platform.bus._envelope import PAYLOAD_FIELD, EventEnvelope
from fking.platform.bus._registry import resolve_event
from fking.platform.config.settings import BusSettings
from fking.platform.correlation import correlation_scope
from fking.platform.logging import get_logger
from fking.platform.telemetry import counter, traced
from fking.platform.telemetry._registry import BUS_EVENTS_PUBLISHED

_LOG: Final = get_logger(__name__)


class EventPublisher:
    """Publishes envelopes to a Redis stream named after the event type.

    One stream per event type rather than one shared stream: a consumer group on a shared
    stream receives every event and discards most of them, so a slow consumer of a rare
    event holds up delivery of a frequent one. Streams are cheap.
    """

    __slots__ = ("_published", "_redis", "_settings")

    def __init__(self, redis: Redis, settings: BusSettings) -> None:
        self._redis = redis
        self._settings = settings
        self._published = counter(BUS_EVENTS_PUBLISHED)

    def stream_for(self, event_type: str) -> str:
        """The stream an event type is published to. Identity today, by design.

        Named as a method rather than inlined because the consumer must derive the same
        name, and two places computing a stream name is how a producer and a consumer end
        up on different streams with neither reporting an error.
        """
        return event_type

    async def publish(self, envelope: EventEnvelope) -> str:
        """Validate and append the envelope, returning the stream message id.

        Validation happens here rather than only on receipt so an unregistered or
        malformed event fails in the producer's own call stack, where the traceback names
        the code that built it -- instead of surfacing hours later as a dead-lettered
        message with no author.
        """
        registration = resolve_event(envelope.event_type, envelope.schema_version)
        registration.payload_model.model_validate(dict(envelope.payload))

        stream = self.stream_for(envelope.event_type)
        # The envelope's id, not the ambient one. The event carries the chain it belongs
        # to; a publisher called from a scope that happens to differ -- a scheduler beat
        # republishing a stored fact, say -- must not relabel the fact with its own.
        with (
            correlation_scope(envelope.correlation_id),
            traced("bus.publish", event_type=envelope.event_type, stream=stream),
        ):
            message_id = await self._redis.xadd(
                name=stream,
                fields={PAYLOAD_FIELD: envelope.model_dump_json()},
                maxlen=self._settings.max_stream_length,
                approximate=True,
            )
        rendered = message_id if isinstance(message_id, str) else message_id.decode()

        self._published.increment(stream=stream, event_type=envelope.event_type)
        _LOG.info(
            "bus.event_published",
            correlation_id=str(envelope.correlation_id),
            causation_id=str(envelope.causation_id) if envelope.causation_id else None,
            stream=stream,
            event_type=envelope.event_type,
            schema_version=envelope.schema_version,
            event_id=envelope.event_id,
            message_id=rendered,
        )
        return rendered
