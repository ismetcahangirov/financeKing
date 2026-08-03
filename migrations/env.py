"""Alembic environment. Async, on the same driver and the same DSN as the application.

Two things here are load-bearing and easy to remove by accident.

`compare_type=True`: without it Alembic's autogenerate ignores a type change, so a
`NUMERIC(38, 18)` column narrowed to `NUMERIC(18, 8)` -- or widened to
`DOUBLE PRECISION` -- produces an empty migration and a green diff. In a codebase whose
first non-negotiable is that money is never a float, an autogenerate that cannot see a
type change is worse than no autogenerate.

`transaction_per_migration=True`: each revision commits on its own. Alembic's default
wraps the whole `upgrade head` in one transaction, which means a five-revision upgrade
that fails on the fifth rolls back the four that succeeded -- including the two that
created hypertables, which is a long operation to repeat. Per-migration also matches
how a failure is reported: `alembic_version` names the last revision that actually
landed.
"""

from __future__ import annotations

import asyncio
from typing import Final

from alembic import context
from sqlalchemy.engine import Connection

from fking.platform.config import load_settings
from fking.platform.persistence.engine import build_engine
from fking.platform.persistence.schema import METADATA

config: Final = context.config

# Autogenerate compares against this. Hand-written migrations are still the rule --
# autogenerate is a first draft that a human edits, never output that is committed
# unread, because it cannot see a trigger, a grant, a hypertable or a compression
# policy, which is most of what this schema's correctness rests on.
target_metadata: Final = METADATA


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        transaction_per_migration=True,
        # TimescaleDB creates internal chunk tables under _timescaledb_internal and a
        # pile of catalog tables in its own schemas. Without this filter every
        # autogenerate proposes dropping all of them.
        include_schemas=False,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a database connection.

    `alembic upgrade head --sql` is how a migration gets reviewed as SQL before it is
    run against anything, which is the only way to review the parts that are raw DDL.
    """
    settings = load_settings()
    context.configure(
        url=str(settings.database.dsn),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run against the configured database over asyncpg."""
    settings = load_settings()
    engine = build_engine(settings.database)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
