"""Provisioning passwords onto the login roles the migration created without them.

`0008_least_privilege.py` creates `fking_app_login`, `fking_ingest_login` and
`fking_migrator_login` with `LOGIN` and **no password**, which under scram-sha-256 means
they cannot authenticate. That is deliberate and it is the correct end state for a
migration: a password written into a migration is a secret in version control, identical
on every machine that ever runs it, and impossible to rotate without a new migration.

So the password arrives here instead, from configuration, at deploy time.

**Why the password is not interpolated into the SQL string.** `ALTER ROLE` is DDL and
PostgreSQL accepts no bind parameters in it, so the obvious spelling is an f-string --
which puts a live credential into a statement that any error, any log line and any
`pg_stat_activity` row can carry verbatim. The way out is that `set_config()` is an
ordinary *function*: it takes a bind parameter, so the secret crosses the wire as a
parameter, and a `DO` block then reads it back with `current_setting()` and applies it
through `format(%I, %L)`, which quotes both the identifier and the literal correctly.

The setting is transaction-local (`is_local => true`), so it does not survive the
statement's transaction and cannot be read by a later session on the same pooled
connection.
"""

from __future__ import annotations

from collections.abc import Mapping

import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncConnection

from fking.platform.config.settings import DatabaseSettings, dsn_credential
from fking.platform.persistence.privileges import LOGIN_ROLES

# `%I` quotes the identifier and `%L` quotes the literal, so neither the role name nor
# the password can terminate the statement. The role name is additionally checked
# against LOGIN_ROLES before it gets here -- format() protects the syntax, and the
# allowlist protects against provisioning a password onto a role nobody meant to.
_SET_PASSWORD = sa.text(
    """
    SELECT set_config('fking.provisioning_secret', :secret, true);
    """
)

_APPLY_PASSWORD = sa.text(
    """
    DO $$
    BEGIN
        EXECUTE format(
            'ALTER ROLE %I PASSWORD %L',
            current_setting('fking.provisioning_role'),
            current_setting('fking.provisioning_secret')
        );
    END
    $$
    """
)

_SET_ROLE = sa.text(
    """
    SELECT set_config('fking.provisioning_role', :login_role, true);
    """
)


class UnknownLoginRoleError(ValueError):
    """A password was offered for a role that is not one of this project's login roles."""


async def provision_login_passwords(
    connection: AsyncConnection, passwords: Mapping[str, SecretStr]
) -> tuple[str, ...]:
    """Set each login role's password, and return the roles that were changed.

    Runs inside the caller's transaction, and every statement is transaction-local, so a
    failure part-way through leaves no role half-provisioned with a password the caller
    does not know it set.

    The connection must be an administrative one -- `ALTER ROLE ... PASSWORD` on another
    role requires `CREATEROLE` or superuser, which is exactly the privilege the login
    roles themselves do not have.
    """
    unknown = sorted(set(passwords) - set(LOGIN_ROLES))
    if unknown:
        raise UnknownLoginRoleError(
            f"{unknown} are not login roles of this project; expected a subset of "
            f"{sorted(LOGIN_ROLES)}"
        )

    provisioned: list[str] = []
    for login_role in LOGIN_ROLES:
        secret = passwords.get(login_role)
        if secret is None:
            continue
        await connection.execute(_SET_ROLE, {"login_role": login_role})
        # get_secret_value() is called at the last possible moment and its result is
        # never bound to a name that outlives the statement.
        await connection.execute(_SET_PASSWORD, {"secret": secret.get_secret_value()})
        await connection.execute(_APPLY_PASSWORD)
        provisioned.append(login_role)
    return tuple(provisioned)


def passwords_from_settings(database: DatabaseSettings) -> Mapping[str, SecretStr]:
    """The credential each DSN already carries, keyed by the role it connects as.

    The DSN is the single source of the credential rather than three more settings
    beside it. Two settings holding one password is one settings edit away from a
    database that disagrees with the connection string, and that disagreement surfaces
    as an authentication failure at whichever process happens to restart first.

    A DSN with no password is skipped rather than provisioned with an empty one: an
    empty password would leave a role that authenticates against nothing, which is
    strictly worse than a role that cannot authenticate at all.
    """
    found: dict[str, SecretStr] = {}
    for dsn in (database.dsn, database.ingest_dsn, database.migrator_dsn):
        username, password = dsn_credential(dsn)
        if username is None or password is None:
            continue
        if username not in LOGIN_ROLES:
            raise UnknownLoginRoleError(
                f"DSN connects as {username!r}, which is not one of {sorted(LOGIN_ROLES)}; "
                f"provisioning a password onto it would create a role the grant matrix "
                f"says nothing about"
            )
        found[username] = SecretStr(password)
    return found


__all__: tuple[str, ...] = (
    "UnknownLoginRoleError",
    "passwords_from_settings",
    "provision_login_passwords",
)
