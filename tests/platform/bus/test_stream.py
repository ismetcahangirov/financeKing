"""End to end over a real Redis and a real PostgreSQL: publish, reclaim, deduplicate, ack.

Everything asserted here is a property of the two servers rather than of this code, which
is why neither is mocked. A mocked Redis would implement the parts of `XAUTOCLAIM` the
author already understood; the parts that matter are the ones nobody holds in their head.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fking.platform.bus import (
    DeadLetterReason,
    EventEnvelope,
    EventHandler,
    EventPublisher,
    StreamConsumer,
    register_event,
)
from fking.platform.bus._envelope import PAYLOAD_FIELD
from fking.platform.bus._registry import schema_digest
from fking.platform.config.settings import BusSettings
from fking.platform.correlation import current_correlation_id

pytestmark = [pytest.mark.integration, pytest.mark.slow]

EVENT_TYPE: Final[str] = "fking.data.bar.ingested"
CONSUMER_GROUP: Final[str] = "test-oms"


class BarPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    close_quote_price: str


@pytest.fixture(autouse=True)
def _registered_event() -> None:
    register_event(
        event_type=EVENT_TYPE,
        schema_version=1,
        payload_model=BarPayload,
        declared_digest=schema_digest(BarPayload),
    )


@pytest_asyncio.fixture
async def effect_table(engine: AsyncEngine) -> AsyncIterator[str]:
    """A stand-in for whatever a real consumer writes.

    An append, not an upsert, and deliberately so: the effect has to be one whose
    duplicate is *visible*. An idempotent upsert would pass the double-delivery test
    whether or not the deduplication claim worked at all.
    """
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "CREATE TABLE bus_test_effect ("
                "  seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
                "  event_id TEXT NOT NULL,"
                "  symbol TEXT NOT NULL)"
            )
        )
    yield "bus_test_effect"


def _envelope(*, symbol: str = "BTCUSDT", correlation_id: UUID | None = None) -> EventEnvelope:
    return EventEnvelope.create(
        event_type=EVENT_TYPE,
        schema_version=1,
        correlation_id=correlation_id or uuid4(),
        occurred_at_utc=datetime.now(UTC),
        payload={"symbol": symbol, "close_quote_price": Decimal("64000.10")},
    )


def _appending_handler(table: str, *, fails: bool = False) -> EventHandler:
    async def handler(session: AsyncSession, envelope: EventEnvelope) -> None:
        await session.execute(
            sa.text(f"INSERT INTO {table} (event_id, symbol) VALUES (:event_id, :symbol)"),  # noqa: S608
            {"event_id": envelope.event_id, "symbol": str(envelope.payload["symbol"])},
        )
        if fails:
            raise RuntimeError("the handler could not make sense of the world")

    return handler


def _consumer(  # noqa: PLR0913 - one keyword per collaborator, mirroring StreamConsumer
    *,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    settings: BusSettings,
    table: str,
    consumer_name: str = "consumer-1",
    fails: bool = False,
) -> StreamConsumer:
    return StreamConsumer(
        redis=redis,
        session_factory=session_factory,
        settings=settings,
        stream=EVENT_TYPE,
        consumer_group=CONSUMER_GROUP,
        consumer_name=consumer_name,
        handler=_appending_handler(table, fails=fails),
    )


async def _dead_letters(redis: Redis, stream: str) -> list[dict[str, str]]:
    """Dead-letter entries as plain string mappings.

    redis-py types the reply as optional and byte-or-str keyed, because it cannot know
    the client was built with `decode_responses=True`. Normalised once here rather than
    at six assertion sites.
    """
    entries = await redis.xrange(stream)
    return [
        {str(key): str(mapped) for key, mapped in (fields or {}).items()}
        for _message_id, fields in entries or []
    ]


async def _effect_rows(engine: AsyncEngine, table: str) -> list[tuple[str, str]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(f"SELECT event_id, symbol FROM {table} ORDER BY seq")  # noqa: S608
        )
        return [(str(row[0]), str(row[1])) for row in rows]


@pytest.mark.asyncio
async def test_a_published_event_is_consumed_once_and_acknowledged(
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    bus_settings: BusSettings,
    engine: AsyncEngine,
    effect_table: str,
) -> None:
    consumer = _consumer(
        redis=redis_client,
        session_factory=session_factory,
        settings=bus_settings,
        table=effect_table,
    )
    await consumer.ensure_group()
    envelope = _envelope()
    await EventPublisher(redis_client, bus_settings).publish(envelope)

    report = await consumer.run_once()

    assert report.applied_count == 1
    assert await _effect_rows(engine, effect_table) == [(envelope.event_id, "BTCUSDT")]
    pending = await redis_client.xpending(EVENT_TYPE, consumer.consumer_group)
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_delivering_the_same_event_twice_applies_it_once(
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    bus_settings: BusSettings,
    engine: AsyncEngine,
    effect_table: str,
) -> None:
    """Republication, not redelivery: a producer retrying after a timeout emits the same
    fact under a *new* stream message id. Deduplicating on the message id lets this
    through, and the second append is a fill the position counts twice."""
    consumer = _consumer(
        redis=redis_client,
        session_factory=session_factory,
        settings=bus_settings,
        table=effect_table,
    )
    await consumer.ensure_group()
    publisher = EventPublisher(redis_client, bus_settings)
    envelope = _envelope()

    first_id = await publisher.publish(envelope)
    second_id = await publisher.publish(envelope)
    assert first_id != second_id, "the two deliveries must have different message ids"

    report = await consumer.run_once()

    assert (report.applied_count, report.duplicate_count) == (1, 1)
    assert await _effect_rows(engine, effect_table) == [(envelope.event_id, "BTCUSDT")]


@pytest.mark.asyncio
async def test_a_message_stranded_by_a_dead_consumer_is_reclaimed_and_applied(
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    bus_settings: BusSettings,
    engine: AsyncEngine,
    effect_table: str,
) -> None:
    """The consumer dies mid-handler: the effect rolls back with the claim, the message
    stays in the pending-entries list, and a live consumer collects it."""
    dying = _consumer(
        redis=redis_client,
        session_factory=session_factory,
        settings=bus_settings,
        table=effect_table,
        consumer_name="dies",
        fails=True,
    )
    await dying.ensure_group()
    envelope = _envelope()
    await EventPublisher(redis_client, bus_settings).publish(envelope)

    with pytest.raises(RuntimeError, match="could not make sense"):
        await dying.run_once()

    pending = await redis_client.xpending(EVENT_TYPE, dying.consumer_group)
    assert pending["pending"] == 1, "an unacknowledged message must stay in the PEL"
    assert await _effect_rows(engine, effect_table) == [], "the effect must have rolled back"

    # min_idle_time is measured by the server against wall clock; the settings fixture
    # uses 1ms, so one short sleep is enough to make the entry eligible.
    await asyncio.sleep(0.05)

    survivor = _consumer(
        redis=redis_client,
        session_factory=session_factory,
        settings=bus_settings,
        table=effect_table,
        consumer_name="survives",
    )
    report = await survivor.run_once()

    assert report.reclaimed_count == 1
    assert report.applied_count == 1
    assert await _effect_rows(engine, effect_table) == [(envelope.event_id, "BTCUSDT")]


@pytest.mark.asyncio
async def test_a_failed_handler_leaves_the_claim_uncommitted_so_the_retry_applies(
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    bus_settings: BusSettings,
    engine: AsyncEngine,
    effect_table: str,
) -> None:
    """The claim and the effect are one transaction. If the claim outlived a failed
    effect, the event would be permanently marked processed and permanently not applied
    -- the worst of the failure modes, because nothing reports it."""
    failing = _consumer(
        redis=redis_client,
        session_factory=session_factory,
        settings=bus_settings,
        table=effect_table,
        consumer_name="c1",
        fails=True,
    )
    await failing.ensure_group()
    envelope = _envelope()
    await EventPublisher(redis_client, bus_settings).publish(envelope)

    with pytest.raises(RuntimeError):
        await failing.run_once()

    async with engine.connect() as connection:
        claims = await connection.scalar(sa.text("SELECT count(*) FROM processed_events"))
    assert claims == 0

    await asyncio.sleep(0.05)
    retrying = _consumer(
        redis=redis_client,
        session_factory=session_factory,
        settings=bus_settings,
        table=effect_table,
        consumer_name="c2",
    )
    assert (await retrying.run_once()).applied_count == 1


@pytest.mark.asyncio
async def test_a_correlation_id_survives_a_three_hop_event_chain(
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    bus_settings: BusSettings,
) -> None:
    """The boundary where propagation breaks in practice. There is no ambient context on
    the other side of a stream, so the id travels as a field and the consumer re-binds it
    -- and every hop must carry the id the first one minted, never a new one."""
    observed: list[str] = []
    origin = uuid4()
    publisher = EventPublisher(redis_client, bus_settings)

    async def relaying_handler(
        _session: AsyncSession,
        _envelope: EventEnvelope,
    ) -> None:
        active = current_correlation_id()
        assert active is not None
        observed.append(active)

    consumer = StreamConsumer(
        redis=redis_client,
        session_factory=session_factory,
        settings=bus_settings,
        stream=EVENT_TYPE,
        consumer_group=CONSUMER_GROUP,
        consumer_name="relay",
        handler=relaying_handler,
    )
    await consumer.ensure_group()

    for hop, symbol in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT")):
        await publisher.publish(_envelope(symbol=symbol, correlation_id=origin))
        report = await consumer.run_once()
        assert report.applied_count == 1, f"hop {hop} was not applied"

    assert observed == [str(origin)] * 3


@pytest.mark.asyncio
async def test_a_message_without_a_correlation_id_is_dead_lettered_with_a_reason(
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    bus_settings: BusSettings,
    effect_table: str,
) -> None:
    """Never invented. An invented id creates a chain that looks complete, which is the
    one outcome worse than a broken chain."""
    consumer = _consumer(
        redis=redis_client,
        session_factory=session_factory,
        settings=bus_settings,
        table=effect_table,
    )
    await consumer.ensure_group()
    await redis_client.xadd(
        name=EVENT_TYPE,
        fields={
            PAYLOAD_FIELD: (
                '{"event_id":"ev1_x","event_type":"fking.data.bar.ingested",'
                '"schema_version":1,"occurred_at_utc":"2026-08-03T10:15:00+00:00",'
                '"payload":{"symbol":"BTCUSDT","close_quote_price":"1"}}'
            )
        },
    )

    report = await consumer.run_once()

    assert report.dead_lettered_count == 1
    dead = await _dead_letters(redis_client, consumer.dead_letter_stream)
    assert len(dead) == 1
    assert dead[0]["reason"] == DeadLetterReason.MISSING_CORRELATION_ID
    pending = await redis_client.xpending(EVENT_TYPE, consumer.consumer_group)
    assert pending["pending"] == 0, "a dead-lettered message must not block the group"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_fields", "expected_reason"),
    [
        ({"nothing": "useful"}, DeadLetterReason.UNDECODABLE),
        ({PAYLOAD_FIELD: "not json at all"}, DeadLetterReason.UNDECODABLE),
    ],
)
async def test_an_undecodable_message_is_dead_lettered(  # noqa: PLR0913, PLR0917
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    bus_settings: BusSettings,
    effect_table: str,
    stream_fields: dict[str, str],
    expected_reason: DeadLetterReason,
) -> None:
    consumer = _consumer(
        redis=redis_client,
        session_factory=session_factory,
        settings=bus_settings,
        table=effect_table,
    )
    await consumer.ensure_group()
    # redis-py types the key and value unions wider than str; the client is built with
    # decode_responses=True, so str on both sides is what this stream actually carries.
    await redis_client.xadd(name=EVENT_TYPE, fields=stream_fields)  # type: ignore[arg-type]

    assert (await consumer.run_once()).dead_lettered_count == 1
    dead = await _dead_letters(redis_client, consumer.dead_letter_stream)
    assert dead[0]["reason"] == expected_reason


@pytest.mark.asyncio
async def test_an_unregistered_event_type_is_dead_lettered_and_keeps_its_correlation_id(
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    bus_settings: BusSettings,
    effect_table: str,
) -> None:
    """An unregistered type says nothing about the chain the event belongs to, so the id
    it carried is preserved rather than replaced with `orphan`."""
    consumer = _consumer(
        redis=redis_client,
        session_factory=session_factory,
        settings=bus_settings,
        table=effect_table,
    )
    await consumer.ensure_group()
    stranger = EventEnvelope.create(
        event_type=EVENT_TYPE,
        schema_version=9,
        correlation_id=uuid4(),
        occurred_at_utc=datetime.now(UTC),
        payload={"symbol": "BTCUSDT"},
    )
    await redis_client.xadd(name=EVENT_TYPE, fields={PAYLOAD_FIELD: stranger.model_dump_json()})

    assert (await consumer.run_once()).dead_lettered_count == 1
    dead = await _dead_letters(redis_client, consumer.dead_letter_stream)
    assert dead[0]["reason"] == DeadLetterReason.UNREGISTERED_EVENT_TYPE
    assert dead[0]["original"] == stranger.model_dump_json()


@pytest.mark.asyncio
async def test_a_payload_failing_the_registered_schema_is_dead_lettered(
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    bus_settings: BusSettings,
    effect_table: str,
) -> None:
    consumer = _consumer(
        redis=redis_client,
        session_factory=session_factory,
        settings=bus_settings,
        table=effect_table,
    )
    await consumer.ensure_group()
    wrong_shape = EventEnvelope.create(
        event_type=EVENT_TYPE,
        schema_version=1,
        correlation_id=uuid4(),
        occurred_at_utc=datetime.now(UTC),
        payload={"symbol": "BTCUSDT"},  # close_quote_price missing
    )
    await redis_client.xadd(name=EVENT_TYPE, fields={PAYLOAD_FIELD: wrong_shape.model_dump_json()})

    assert (await consumer.run_once()).dead_lettered_count == 1
    dead = await _dead_letters(redis_client, consumer.dead_letter_stream)
    assert dead[0]["reason"] == DeadLetterReason.SCHEMA_INVALID


@pytest.mark.asyncio
async def test_the_stream_is_capped_so_a_stalled_consumer_cannot_exhaust_memory(
    redis_client: Redis, bus_settings: BusSettings
) -> None:
    """`MAXLEN ~` trims approximately -- whole radix-tree nodes, O(1) -- so the assertion
    is a bound with headroom, not an equality. Asserting an exact length would be
    asserting an implementation detail of Redis's trimming."""
    capped = bus_settings.model_copy(update={"max_stream_length": 10})
    publisher = EventPublisher(redis_client, capped)
    published_count = 500
    tolerated_after_trim = 200
    for index in range(published_count):
        await publisher.publish(_envelope(symbol=f"SYM{index}"))

    length = await redis_client.xlen(EVENT_TYPE)
    assert length < published_count, "the cap did not trim at all"
    assert length <= tolerated_after_trim, (
        f"the approximate cap retained {length} entries for a limit of 10"
    )


@pytest.mark.asyncio
async def test_the_group_is_created_with_the_stream_so_a_consumer_may_start_first(
    redis_client: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    bus_settings: BusSettings,
    effect_table: str,
) -> None:
    """Without `mkstream`, boot order becomes a correctness dependency in a system whose
    services start concurrently."""
    consumer = _consumer(
        redis=redis_client,
        session_factory=session_factory,
        settings=bus_settings,
        table=effect_table,
    )
    await consumer.ensure_group()
    await consumer.ensure_group()  # BUSYGROUP on every start after the first

    groups = await redis_client.xinfo_groups(EVENT_TYPE)
    assert [group["name"] for group in groups] == [consumer.consumer_group]
