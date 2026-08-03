"""Engine construction. One driver, one place.

`asyncpg` is the only PostgreSQL driver in this project, and Alembic runs through it
too. Adding a synchronous driver "just for migrations" gives the migration path
different type coercion from the application path -- `numeric` and `timestamptz` in
particular -- on the one path whose whole job is to agree with the application about
what a column means.

The session factory arrives with its first caller, the event bus in #18: a consumer's
deduplication claim and its effect must commit in one transaction, and that is what a
factory-per-message exists to give. There is still no transaction *helper* wrapping it,
because `async with factory() as session, session.begin():` is the whole pattern and a
wrapper would only hide which of the two lines opens the transaction.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from fking.platform.config.settings import DatabaseSettings


def build_engine(settings: DatabaseSettings) -> AsyncEngine:
    """An `AsyncEngine` configured from the settings tree.

    `pool_pre_ping` is on because Postgres restarts and the connection a pooled session
    hands back is otherwise a socket that closed while nobody was looking -- which
    surfaces as a failed write on the next order rather than as a failed connect.

    `statement_timeout` is applied as a server-side setting rather than trusted to a
    client-side cancel: a client that gives up on a statement does not stop the server
    executing it, so a runaway query keeps its locks either way.
    """
    return create_async_engine(
        str(settings.dsn),
        pool_size=settings.pool_max_size,
        # Never above zero. An overflow connection is one the pool ceiling did not
        # bound, and each connection carries work_mem -- so overflow converts a
        # connection-exhaustion problem into an OOM kill on Postgres, which the kernel
        # selects by score rather than by importance.
        max_overflow=0,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "statement_timeout": str(settings.statement_timeout_seconds * 1000),
                "application_name": "fking",
                # Every datetime in this system is tz-aware UTC. Pinning the session
                # timezone means a TIMESTAMPTZ renders identically regardless of the
                # server's locale or the developer's machine.
                "timezone": "UTC",
            }
        },
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to `engine`.

    `expire_on_commit=False` because the objects a caller holds after `commit()` are
    plain rows, not mapped instances -- this schema is Core, so there is nothing to
    refresh, and leaving expiry on would cost an extra round trip on every attribute read
    for no benefit.

    `autobegin` is left at its default: the bus opens its transaction explicitly with
    `session.begin()`, because the point of that block is that the claim and the effect
    share one, and an implicit transaction makes the boundary a thing you infer rather
    than a thing you read.
    """
    return async_sessionmaker(engine, expire_on_commit=False)
