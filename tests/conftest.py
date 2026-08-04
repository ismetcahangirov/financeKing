"""Shared pytest configuration: the Hypothesis profiles and the real-database fixtures.

The database fixtures live here rather than under `tests/platform/` because they now have
callers on both sides of that boundary: the persistence and event-bus suites, and the
ingestion quality gates in `tests/data/`, whose gate 11 is a standing query over the `bar`
hypertable and is therefore only answerable by a real database. pytest resolves fixtures
from the root conftest for every test, and a non-root conftest cannot register itself as a
plugin, so this is the one location that serves both.

Never a mock. The interesting failures in this schema live in `CHECK` constraints,
`BEFORE UPDATE OR DELETE` triggers, revoked grants, hypertable policies and `ON CONFLICT`
semantics, and a mocked database here would prove that the mock is append-only
(`TESTING.md`, `CLAUDE.md` section 5).

The server comes from `testcontainers`, on the same TimescaleDB image digest as
`docker-compose.yml`. That is deliberate on two counts: a developer does not have to
remember to run `make up` before `make check` -- and a database test that is skipped
because nobody started a server is a test that reports green having verified nothing --
and the container binds a random host port, which matters on Windows where 5432 is
frequently already taken or reserved by the OS.

`FKING_TEST_ADMIN_DSN` overrides the container for anyone who already has a server. With
`FKING_REQUIRE_DB=1` -- set by CI -- an unreachable database fails the run instead of
skipping it.

The Hypothesis profiles are registered here so that every property test inherits them
without repeating the settings, and so the derandomize split below is decided once.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Final

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from hypothesis import HealthCheck, Verbosity, settings
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from fking.platform.persistence.privileges import APP_ROLE, INGEST_ROLE, LOGIN_ROLE_FOR
from tests.support.roles import engine_as

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

# The same digest docker-compose.yml pins, so a migration cannot pass here and fail on
# the developer stack over an extension version nobody thought to compare. The -ha image
# rather than the plain one because it carries pgvector, which agent memory needs.
TIMESCALE_IMAGE: Final[str] = (
    "timescale/timescaledb-ha@sha256:"
    "15c65f4176e5f19742d44fde1cb12c3db27b55033dbc8f11f0143635223add85"
)


def database_required() -> bool:
    return os.environ.get("FKING_REQUIRE_DB", "") not in {"", "0", "false"}


async def _server_is_reachable(dsn: str) -> str | None:
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT 1"))
    except Exception as unreachable:  # noqa: BLE001 - reported, never swallowed
        return f"{type(unreachable).__name__}: {unreachable}"
    finally:
        await engine.dispose()
    return None


async def _create_database(dsn: str, name: str) -> None:
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


async def _seal_template(dsn: str, name: str) -> None:
    """Make `name` usable as a `CREATE DATABASE ... TEMPLATE` source.

    `CREATE DATABASE ... TEMPLATE` refuses while any other session is connected, and
    TimescaleDB attaches a background worker to every database carrying its extension --
    so a freshly migrated database is never quiet, however carefully the migration
    disposed its own engine. `ALLOW_CONNECTIONS false` stops the worker reattaching, and
    the terminate clears the one already there.
    """
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text(f'ALTER DATABASE "{name}" IS_TEMPLATE true'))
            await connection.execute(sa.text(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS false'))
            await connection.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
    finally:
        await engine.dispose()


async def _unseal_template(dsn: str, name: str) -> None:
    """`DROP DATABASE` refuses a database still marked as a template."""
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text(f'ALTER DATABASE "{name}" IS_TEMPLATE false'))
    finally:
        await engine.dispose()


async def _clone_database(dsn: str, name: str, *, template: str) -> None:
    """`CREATE DATABASE name TEMPLATE template`.

    Far faster than re-running the migration chain per test, and it copies the whole
    catalog -- triggers, grants, hypertable metadata and sequence positions included.
    """
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
    finally:
        await engine.dispose()


async def _drop_database(dsn: str, name: str) -> None:
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            # WITH (FORCE) terminates any connection the pool did not close. Without it
            # a leaked session makes teardown hang until the timeout, and the error
            # then blames whichever test happens to run next.
            await connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await engine.dispose()


def dsn_for(admin_dsn: str, database_name: str) -> str:
    """`admin_dsn` redirected at `database_name`."""
    url = sa.engine.make_url(admin_dsn).set(database=database_name)
    return url.render_as_string(hide_password=False)


def alembic_config(dsn: str) -> Config:
    """An Alembic config whose `env.py` will read `dsn`.

    The DSN reaches `env.py` through the environment rather than through
    `sqlalchemy.url`, because `env.py` deliberately takes it from the settings tree so
    that migrations and the application cannot be pointed at different databases.
    """
    os.environ["FKING_DATABASE__DSN"] = dsn
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


@pytest.fixture(scope="session")
def postgres_server() -> Iterator[str]:
    """A maintenance DSN on a reachable PostgreSQL, or a skip explaining what is missing.

    The container is started once per session and stopped at the end. Per-test
    containers would be cleaner in isolation terms and would add roughly ten seconds to
    every one of them, which is how an integration suite stops being run.
    """
    override = os.environ.get("FKING_TEST_ADMIN_DSN")
    if override:
        failure = asyncio.run(_server_is_reachable(override))
        if failure is not None:
            refuse_or_skip(f"FKING_TEST_ADMIN_DSN is set but unreachable: {failure}")
        yield override
        return

    try:
        container = PostgresContainer(image=TIMESCALE_IMAGE, driver="asyncpg", dbname="postgres")
        container.start()
    except Exception as unavailable:  # noqa: BLE001 - reported, never swallowed
        refuse_or_skip(
            f"could not start a TimescaleDB container ({type(unavailable).__name__}: "
            f"{unavailable}). Start Docker, or set FKING_TEST_ADMIN_DSN to an "
            f"existing server."
        )
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


def refuse_or_skip(message: str) -> None:
    """Skip locally, fail under `FKING_REQUIRE_DB`.

    A database test that silently skips in CI is the failure mode this repository
    already hit once, where a command exited 0 having evaluated no contracts.
    """
    if database_required():
        pytest.fail(f"FKING_REQUIRE_DB is set and {message}")
    pytest.skip(message)


@pytest.fixture(scope="session")
def migrated_template(postgres_server: str) -> Iterator[str]:
    """A database at `head`, used only as a `CREATE DATABASE ... TEMPLATE` source.

    Migrations run once per session; each test then clones this in roughly 30ms.
    TESTING.md section 4.1 specifies this over the faster savepoint-rollback pattern for
    a reason that matters here specifically: code calling `commit()` escapes a rollback,
    and a rollback-per-test suite cannot test the append-only triggers' interaction with
    transaction boundaries at all -- which is most of what this package is.
    """
    name = f"fking_template_{uuid.uuid4().hex[:8]}"
    asyncio.run(_create_database(postgres_server, name))
    try:
        command.upgrade(alembic_config(dsn_for(postgres_server, name)), "head")
        asyncio.run(_seal_template(postgres_server, name))
        yield name
    finally:
        asyncio.run(_unseal_template(postgres_server, name))
        asyncio.run(_drop_database(postgres_server, name))


@pytest.fixture
def migrated_dsn(postgres_server: str, migrated_template: str) -> Iterator[str]:
    """A pristine database at `head`, one per test, dropped afterwards.

    True isolation, including of sequences and of `alembic_version` -- which matters
    here because `audit_log.seq` and `trial_ledger.cumulative_trials` are monotone and a
    shared database would make every assertion about them relative to whatever ran
    first.

    Never `TRUNCATE` a hypertable to reuse one instead: it leaves chunk metadata and
    compression policies in a state that makes the next test's inserts behave
    differently from a fresh table.
    """
    name = f"fking_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(_clone_database(postgres_server, name, template=migrated_template))
    try:
        yield dsn_for(postgres_server, name)
    finally:
        asyncio.run(_drop_database(postgres_server, name))


@pytest.fixture
def scratch_dsn(postgres_server: str) -> Iterator[str]:
    """An empty database with no migrations applied, dropped at the end of the test."""
    name = f"fking_scratch_{uuid.uuid4().hex[:12]}"
    asyncio.run(_create_database(postgres_server, name))
    try:
        yield dsn_for(postgres_server, name)
    finally:
        asyncio.run(_drop_database(postgres_server, name))


@pytest_asyncio.fixture
async def engine(migrated_dsn: str) -> AsyncIterator[AsyncEngine]:
    """An engine on the migrated database, disposed at the end of the test.

    Function-scoped on purpose. A session-scoped async engine outlives the
    function-scoped event loop pytest-asyncio gives each test, and the symptom is a
    connection bound to a closed loop failing in whichever test happens to run second.
    """
    created = create_async_engine(migrated_dsn)
    try:
        yield created
    finally:
        await created.dispose()


@pytest_asyncio.fixture
async def app_engine(migrated_dsn: str) -> AsyncIterator[AsyncEngine]:
    """An engine running as `fking_app`: reads through `fking_feature_as_of` and no further.

    Here rather than beside the feature-store suite because two directories need it now --
    `tests/data/features/` for the grant matrix and `tests/lookahead/` for the store-level
    probe -- and pytest resolves fixtures from the root conftest for every test. Two
    copies of a role fixture is two places for the role to be wrong.
    """
    created = await engine_as(migrated_dsn, LOGIN_ROLE_FOR[APP_ROLE])
    try:
        yield created
    finally:
        await created.dispose()


@pytest_asyncio.fixture
async def ingest_engine(migrated_dsn: str) -> AsyncIterator[AsyncEngine]:
    """An engine running as `fking_ingest`: appends feature values and cannot rewrite one."""
    created = await engine_as(migrated_dsn, LOGIN_ROLE_FOR[INGEST_ROLE])
    try:
        yield created
    finally:
        await created.dispose()


settings.register_profile(
    "dev",
    max_examples=100,
    deadline=timedelta(milliseconds=500),
    print_blob=True,
)
settings.register_profile(
    "ci",
    max_examples=1000,
    deadline=None,
    # A pull-request gate that fails on a newly drawn example is a flaky gate. The
    # search for genuinely new counterexamples belongs in the nightly job, whose
    # failure opens an issue rather than blocking a merge.
    derandomize=True,
    # No .hypothesis cache in CI: the run must be self-contained and reproducible
    # from the log alone.
    database=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
settings.register_profile(
    "nightly",
    max_examples=20_000,
    deadline=None,
    derandomize=False,
    verbosity=Verbosity.verbose,
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
