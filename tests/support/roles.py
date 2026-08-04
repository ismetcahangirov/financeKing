"""Connecting to a migrated database *as* one of the least-privilege login roles.

Two claims can be made about a grant matrix, and only the second is what the system
depends on: that `has_table_privilege('fking_app', ...)` is false, and that a connection
*as* `fking_app` is in fact refused. The first is cheap and exhaustive; the second is the
one that survives a mistake in the first.

The migration deliberately leaves every login role without a password, so a caller
provisions one for the duration of a test rather than the repository carrying a
credential. `token_urlsafe` and not a literal: a password constant in a test file is a
password that ends up in a deployment, because it is right there and it works.
"""

from __future__ import annotations

import secrets

from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fking.platform.persistence.roles import provision_login_passwords

__all__ = ["engine_as"]


async def engine_as(migrated_dsn: str, login_role: str) -> AsyncEngine:
    """An engine connected as `login_role`, with a password minted for this test."""
    password = secrets.token_urlsafe(24)
    admin = create_async_engine(migrated_dsn)
    try:
        async with admin.begin() as connection:
            await provision_login_passwords(connection, {login_role: SecretStr(password)})
    finally:
        await admin.dispose()

    url = make_url(migrated_dsn).set(username=login_role, password=password)
    return create_async_engine(url)
