"""Fixtures for the event bus tests. A real Redis and a real PostgreSQL, never a mock.

Mocking Redis here would prove the mock implements the parts of `XAUTOCLAIM` the author
already understood. What is under test is precisely the parts nobody holds in their head:
that a message a consumer read and never acknowledged stays in the pending-entries list,
that `XAUTOCLAIM` hands it to a different consumer only after `min-idle-time`, that
`MAXLEN ~` trims approximately rather than exactly, and that `ON CONFLICT DO NOTHING`
returns no row rather than raising. Every one of those is a property of the server.

The Redis image is the same digest `docker-compose.yml` pins, so a behaviour difference
between the test server and the developer stack cannot hide here.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import Final

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.community.redis import RedisContainer

from fking.platform.bus import build_redis, unregister_all
from fking.platform.config.settings import BusSettings
from fking.platform.persistence import build_session_factory
from fking.platform.telemetry import reset_instrument_cache
from tests.conftest import refuse_or_skip

# The digest docker-compose.yml pins for the `redis` service, restated rather than parsed
# out of the compose file: the compose contract test already asserts the file pins a
# digest, and reading YAML here to save one constant would make this fixture fail for a
# reason that has nothing to do with Redis.
REDIS_IMAGE: Final[str] = (
    "redis@sha256:e7723ff73d963f5cc6d9c4643ea3d989527a402a319239054e9472a7fb9219a2"
)


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    """A URL for a reachable Redis, or a skip explaining what is missing.

    `FKING_TEST_REDIS_URL` overrides the container for anyone who already has a server.
    Under `FKING_REQUIRE_DB` -- set by CI -- an unreachable server fails the run instead
    of skipping it, because a bus test that silently skips reports green having verified
    nothing.
    """
    override = os.environ.get("FKING_TEST_REDIS_URL")
    if override:
        yield override
        return
    try:
        container = RedisContainer(image=REDIS_IMAGE)
        container.start()
    except Exception as unavailable:  # noqa: BLE001 - reported, never swallowed
        refuse_or_skip(
            f"could not start a Redis container ({type(unavailable).__name__}: "
            f"{unavailable}). Start Docker, or set FKING_TEST_REDIS_URL."
        )
    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(container.port)
        yield f"redis://{host}:{port}/0"
    finally:
        container.stop()


@pytest.fixture
def bus_settings(redis_url: str) -> BusSettings:
    """Bus settings pointed at the test server, with a short claim window.

    `claim_idle_ms=1` so `XAUTOCLAIM` reclaims almost immediately. The production value is 30s;
    waiting for it in a test would either make the suite slow or make the reclaim test
    sleep, and a sleeping test is a test that becomes flaky on a loaded CI machine.
    """
    return BusSettings.model_validate(
        {"redis_url": redis_url, "claim_idle_ms": 1, "max_stream_length": 1000}
    )


@pytest_asyncio.fixture
async def redis_client(bus_settings: BusSettings) -> AsyncIterator[Redis]:
    """A client on a flushed database, closed at the end of the test.

    Flushed before rather than after: a test that fails and leaves keys behind should
    leave them for inspection, and the next test still starts clean.
    """
    client = build_redis(bus_settings)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory on the per-test migrated database."""
    yield build_session_factory(engine)


@pytest.fixture(autouse=True)
def _isolated_registries() -> Iterator[None]:
    """Empty the event and instrument registries around every test in this package.

    Both are process-global by design -- an event type registered twice with two models
    is a fault, and an instrument cached per name is what stops a double registration.
    Global state that leaks between tests produces a suite whose result depends on
    ordering, and `pytest-randomly` shuffles the order on every run.
    """
    unregister_all()
    reset_instrument_cache()
    yield
    unregister_all()
    reset_instrument_cache()
