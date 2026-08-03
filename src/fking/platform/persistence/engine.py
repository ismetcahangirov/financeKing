"""Engine construction. One driver, one place.

`asyncpg` is the only PostgreSQL driver in this project, and Alembic runs through it
too. Adding a synchronous driver "just for migrations" gives the migration path
different type coercion from the application path -- `numeric` and `timestamptz` in
particular -- on the one path whose whole job is to agree with the application about
what a column means.

There is deliberately no session factory and no transaction helper here yet. Both would
have exactly zero callers: the first consumer of this schema is the event bus in #18,
and an interface extracted before its second caller exists is that caller's
implementation wearing a different name (`CLAUDE.md` section 3).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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
