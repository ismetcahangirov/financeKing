"""Engines connected as the two roles the feature store distinguishes.

`fking_app` reads and cannot see the table; `fking_ingest` writes and cannot rewrite.
The whole point of #29 is that this distinction is enforced by PostgreSQL rather than by
the code under test, so every claim about it has to be made by a connection that is
actually subject to it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.platform.persistence.privileges import APP_ROLE, INGEST_ROLE, LOGIN_ROLE_FOR
from tests.support.roles import engine_as


@pytest_asyncio.fixture
async def app_engine(migrated_dsn: str) -> AsyncIterator[AsyncEngine]:
    engine = await engine_as(migrated_dsn, LOGIN_ROLE_FOR[APP_ROLE])
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def ingest_engine(migrated_dsn: str) -> AsyncIterator[AsyncEngine]:
    engine = await engine_as(migrated_dsn, LOGIN_ROLE_FOR[INGEST_ROLE])
    try:
        yield engine
    finally:
        await engine.dispose()
