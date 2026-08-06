"""Least privilege, asserted against a real database rather than against a mock.

The distinction this file is built around: there are two different claims, and only one
of them is what the system depends on.

- *Catalogue claims* — `has_table_privilege('fking_app', 'audit_log', 'UPDATE')` is
  false. Cheap, exhaustive, and asserted here over every table and every privilege.
- *Behavioural claims* — a connection **as** `fking_app` cannot in fact update an audit
  row. Slower, narrower, and the only kind that survives a mistake in the first kind.

Before this module the roles were `NOLOGIN`, so every process connected as the superuser
and only the first kind of claim was ever made. A grant matrix that no connection is
subject to is a description of a database nobody uses.

The tests that would be easiest to leave out are the two ownership ones, and they are
the reason the migrator role exists at all: a table's owner may
`ALTER TABLE ... DISABLE TRIGGER` regardless of what has been revoked from it, so an
application role that owns `audit_log` can switch off the append-only guard while every
grant in the catalogue still reads as correct.

`.claude/rules/append-only-audit.md`, `.claude/rules/no-lookahead.md`, `SECURITY.md`.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import AsyncIterator, Collection
from datetime import UTC, datetime
from typing import Final, cast

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from fking.platform.persistence.privileges import (
    APP_ROLE,
    CLASSIFICATION,
    FUNCTION_PRIVILEGES,
    GROUP_ROLES,
    INGEST_ROLE,
    LOGIN_ROLE_FOR,
    MIGRATOR_ROLE,
    TABLE_PRIVILEGES,
    VIEW_PRIVILEGES,
    PrivilegeClass,
    granted_privileges,
    tables_in_class,
)
from fking.platform.persistence.roles import UnknownLoginRoleError, provision_login_passwords
from fking.platform.persistence.schema import APPEND_ONLY_TABLES, METADATA
from tests.conftest import alembic_config
from tests.support.roles import engine_as

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# The revision that establishes the grant matrix. Pinned rather than `head` so the
# re-application test never has to walk an irreversible migration to get below it.
_GRANT_MATRIX_REVISION: Final[str] = "0008_least_privilege"


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


# ---------------------------------------------------------------------------
# The matrix is exhaustive
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_every_table_in_the_schema_has_a_privilege_class() -> None:
    """The mechanism the issue asks for, generalised past audit tables.

    A table that nobody classified is not locked down -- it is undecided, and the two
    look identical from the catalogue. Failing here means a new table cannot merge until
    somebody has said what may touch it, which is a cheap decision while the table is
    being designed and an expensive one afterwards.
    """
    unclassified = sorted(set(METADATA.tables) - set(CLASSIFICATION))
    assert unclassified == [], (
        f"classify these in fking.platform.persistence.privileges.CLASSIFICATION: {unclassified}"
    )


@pytest.mark.unit
def test_the_classification_names_no_table_that_does_not_exist() -> None:
    """The other direction. A stale entry is a grant nobody applies and a reader's
    false impression that a table is covered."""
    phantom = sorted(set(CLASSIFICATION) - set(METADATA.tables))
    assert phantom == [], f"remove these from CLASSIFICATION: {phantom}"


@pytest.mark.unit
def test_the_append_only_class_matches_the_append_only_table_set() -> None:
    """Two lists describe the same set, so they are compared rather than derived.

    Deriving one from the other would make them agree by construction and prove
    nothing; the disagreement is the finding.
    """
    assert set(tables_in_class(PrivilegeClass.APPEND_ONLY)) == set(APPEND_ONLY_TABLES)


# ---------------------------------------------------------------------------
# Catalogue claims: the live grants equal the declared grants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", sorted(CLASSIFICATION))
@pytest.mark.parametrize("group_role", [APP_ROLE, INGEST_ROLE])
@pytest.mark.asyncio
async def test_live_grants_equal_the_declared_matrix(
    engine: AsyncEngine, table: str, group_role: str
) -> None:
    """All seven privileges, including the ones that should be absent.

    Asserting only the granted subset cannot see over-granting, which is the failure
    that matters: a missing grant fails loudly at the first query, an extra one never
    fails at all.
    """
    expected = granted_privileges(table, group_role)
    async with engine.connect() as connection:
        held = {
            privilege
            for privilege in TABLE_PRIVILEGES
            if await connection.scalar(
                sa.text("SELECT has_table_privilege(:role, :table, :privilege)"),
                {"role": group_role, "table": table, "privilege": privilege},
            )
        }
    assert held == set(expected)


@pytest.mark.parametrize("view", sorted(VIEW_PRIVILEGES))
@pytest.mark.asyncio
async def test_view_grants_equal_the_declared_matrix(engine: AsyncEngine, view: str) -> None:
    """A view's own ACL governs access to it, and its *owner's* rights govern access to
    the tables underneath. That is what lets `global_trial_count` be readable while
    `trial_ledger` itself is not, and it is the same mechanism `feature_as_of()` will
    use for the feature store."""
    for group_role, expected in VIEW_PRIVILEGES[view].items():
        async with engine.connect() as connection:
            held = {
                privilege
                for privilege in TABLE_PRIVILEGES
                if await connection.scalar(
                    sa.text("SELECT has_table_privilege(:role, :view, :privilege)"),
                    {"role": group_role, "view": view, "privilege": privilege},
                )
            }
        assert held == set(expected), f"{view} for {group_role}"


@pytest.mark.parametrize("routine", sorted(FUNCTION_PRIVILEGES))
@pytest.mark.asyncio
async def test_function_grants_equal_the_declared_matrix(engine: AsyncEngine, routine: str) -> None:
    """Functions are granted `EXECUTE` to PUBLIC by default, so a routine nobody thought
    about is a routine every role can call -- and `fking_feature_as_of` is a
    `SECURITY DEFINER` read of a table `fking_app` is otherwise blind to.

    PUBLIC is checked through `aclexplode` rather than `has_function_privilege`, because
    PUBLIC is grantee OID 0 and is not a row in `pg_roles`.
    """
    for group_role, expected in FUNCTION_PRIVILEGES[routine].items():
        async with engine.connect() as connection:
            may_execute = await connection.scalar(
                sa.text("SELECT has_function_privilege(:role, :routine, 'EXECUTE')"),
                {"role": group_role, "routine": routine},
            )
        assert bool(may_execute) is ("EXECUTE" in expected), f"{routine} for {group_role}"

    async with engine.connect() as connection:
        granted_to_public = await connection.scalar(
            sa.text(
                """
                SELECT count(*)
                  FROM pg_proc p
                  JOIN LATERAL aclexplode(p.proacl) a ON true
                 WHERE p.oid = to_regprocedure(:routine)
                   AND a.grantee = 0
                """
            ),
            {"routine": routine},
        )
    assert granted_to_public == 0


@pytest.mark.asyncio
async def test_every_declared_routine_exists(engine: AsyncEngine) -> None:
    """A declaration naming a routine that was never created reads as covered and grants
    nothing, which is the one impression the matrix must never give."""
    async with engine.connect() as connection:
        for routine in sorted(FUNCTION_PRIVILEGES):
            exists = await connection.scalar(
                sa.text("SELECT to_regprocedure(:routine) IS NOT NULL"), {"routine": routine}
            )
            assert exists is True, routine


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_table_is_owned_by_the_migrator_role(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    "SELECT tablename, tableowner FROM pg_tables "
                    "WHERE schemaname = 'public' ORDER BY tablename"
                )
            )
        ).all()
    wrong = {str(row[0]): str(row[1]) for row in rows if row[1] != MIGRATOR_ROLE}
    assert wrong == {}


@pytest.mark.asyncio
async def test_the_application_role_owns_nothing(engine: AsyncEngine) -> None:
    """Asserted over the whole catalogue rather than over the two tables the issue
    names, because ownership is inherited by every object a role creates and the next
    table is the one nobody checked."""
    async with engine.connect() as connection:
        owned = (
            (
                await connection.execute(
                    sa.text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = 'public' AND tableowner = :role"
                    ),
                    {"role": APP_ROLE},
                )
            )
            .scalars()
            .all()
        )
    assert list(owned) == []


@pytest.mark.asyncio
async def test_the_application_role_cannot_disable_the_append_only_trigger(
    app_engine: AsyncEngine,
) -> None:
    """The test that makes ownership worth a separate assertion.

    `ALTER TABLE ... DISABLE TRIGGER` is an owner's right and no `REVOKE` touches it. If
    `fking_app` owned `audit_log`, every grant assertion in this file would still pass
    while the append-only guard could be switched off in one statement.
    """
    async with app_engine.connect() as connection:
        with pytest.raises((ProgrammingError, DBAPIError)) as refused:
            await connection.execute(
                sa.text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update_delete")
            )
    assert "must be owner" in str(refused.value).lower()


# ---------------------------------------------------------------------------
# Behavioural claims: as the role, not about the role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_application_role_can_connect_and_append_to_an_audit_table(
    app_engine: AsyncEngine,
) -> None:
    """The claim the catalogue tests cannot make: that a connection as this role exists
    at all, and that the grants it holds are the ones it needs to do its job."""
    async with app_engine.begin() as connection:
        seq = await connection.scalar(
            sa.text(
                """
                INSERT INTO audit_log (occurred_at_utc, correlation_id, causation_id, actor,
                                       event_type, subject_id, payload, prev_hash, row_hash)
                VALUES (:occurred_at_utc, :correlation_id, NULL, 'risk', 'order.approved',
                        'test', '{}'::jsonb, '\\x00'::bytea, '\\x00'::bytea)
                RETURNING seq
                """
            ),
            {"occurred_at_utc": datetime.now(UTC), "correlation_id": uuid.uuid4()},
        )
    assert seq is not None


@pytest.mark.parametrize("statement", ["UPDATE audit_log SET actor = 'x'", "DELETE FROM audit_log"])
@pytest.mark.asyncio
async def test_the_application_role_is_refused_before_the_trigger_is_reached(
    app_engine: AsyncEngine, statement: str
) -> None:
    """`permission denied`, not the trigger's message.

    Which error arrives is the assertion: the grant is the primary control and the
    trigger is the backstop, so on a correctly configured database the trigger should
    never be the thing that stops the application. If this starts reporting the
    trigger's `append-only` message, a grant has been widened somewhere.
    """
    async with app_engine.connect() as connection:
        with pytest.raises((ProgrammingError, DBAPIError)) as refused:
            await connection.execute(sa.text(statement))
    assert "permission denied" in str(refused.value).lower()


@pytest.mark.asyncio
async def test_the_application_role_cannot_write_market_data(app_engine: AsyncEngine) -> None:
    """The ingest/app split, as behaviour.

    This is what makes "a strategy cannot rewrite the history it is evaluated against" a
    permission error rather than a convention -- and rewriting history is not a
    hypothetical failure here, it is the shape every look-ahead defect takes.
    """
    async with app_engine.connect() as connection:
        with pytest.raises((ProgrammingError, DBAPIError)) as refused:
            await connection.execute(sa.text("DELETE FROM bar"))
    assert "permission denied" in str(refused.value).lower()


@pytest.mark.asyncio
async def test_the_ingest_role_cannot_read_the_audit_log(ingest_engine: AsyncEngine) -> None:
    """The split cuts both ways. An ingestion worker has no business reading why a
    trade happened, and a role that can read the audit log is a role whose compromise
    exposes the reconstruction of every decision the system has made."""
    async with ingest_engine.connect() as connection:
        with pytest.raises((ProgrammingError, DBAPIError)) as refused:
            await connection.execute(sa.text("SELECT 1 FROM audit_log"))
    assert "permission denied" in str(refused.value).lower()


@pytest.mark.asyncio
async def test_a_login_role_without_a_provisioned_password_cannot_authenticate(
    migrated_dsn: str,
) -> None:
    """The migration's end state is fail-closed.

    A role created with `LOGIN` and no password cannot authenticate under
    scram-sha-256, so a database that has been migrated but not provisioned refuses
    connections instead of accepting them on an empty credential. The failure of a
    forgotten provisioning step is therefore an authentication error at startup, which
    is the loudest available outcome.
    """
    url = make_url(migrated_dsn).set(
        username=LOGIN_ROLE_FOR[MIGRATOR_ROLE], password=secrets.token_urlsafe(16)
    )
    engine = create_async_engine(url)
    try:
        with pytest.raises(Exception) as rejected:  # noqa: PT011 - driver-specific type
            async with engine.connect():
                pass
    finally:
        await engine.dispose()
    assert "password authentication failed" in str(rejected.value).lower()


# ---------------------------------------------------------------------------
# Roles and idempotence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("group_role", GROUP_ROLES)
@pytest.mark.asyncio
async def test_each_login_role_is_a_member_of_its_group_role(
    engine: AsyncEngine, group_role: str
) -> None:
    """Membership with INHERIT, so the login role holds the group's privileges without
    a `SET ROLE` at every call site. `pg_has_role(..., 'USAGE')` is the check that
    distinguishes inherited membership from membership that requires SET ROLE."""
    async with engine.connect() as connection:
        inherits = await connection.scalar(
            sa.text("SELECT pg_has_role(:login, :group, 'USAGE')"),
            {"login": LOGIN_ROLE_FOR[group_role], "group": group_role},
        )
    assert inherits is True


@pytest.mark.parametrize("group_role", GROUP_ROLES)
@pytest.mark.asyncio
async def test_the_group_roles_themselves_cannot_log_in(
    engine: AsyncEngine, group_role: str
) -> None:
    """A privilege-holding role that can log in is a role a leaked password belongs to,
    and revoking that credential would mean touching the role that holds the grants."""
    async with engine.connect() as connection:
        can_login = await connection.scalar(
            sa.text("SELECT rolcanlogin FROM pg_roles WHERE rolname = :role"),
            {"role": group_role},
        )
    assert can_login is False


@pytest.mark.asyncio
async def test_public_holds_nothing_on_any_table(engine: AsyncEngine) -> None:
    """PUBLIC is every role that will ever exist in this cluster, including the ones a
    future migration creates without thinking about this file.

    Read from `relacl` via `aclexplode` rather than through `has_table_privilege`:
    PUBLIC is grantee OID 0 and is not a row in `pg_roles`, so the privilege function
    cannot be asked about it by name.
    """
    async with engine.connect() as connection:
        granted_to_public = (
            (
                await connection.execute(
                    sa.text(
                        """
                        SELECT c.relname
                          FROM pg_class c
                          JOIN pg_namespace n ON n.oid = c.relnamespace
                          JOIN LATERAL aclexplode(c.relacl) a ON true
                         WHERE n.nspname = 'public'
                           AND c.relkind IN ('r', 'p', 'v')
                           AND a.grantee = 0
                         ORDER BY c.relname
                        """
                    )
                )
            )
            .scalars()
            .all()
        )
    assert list(granted_to_public) == []


async def _application_privileges(dsn: str, tables: Collection[str]) -> dict[str, set[str]]:
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as connection:
            return {
                table: {
                    privilege
                    for privilege in TABLE_PRIVILEGES
                    if await connection.scalar(
                        sa.text("SELECT has_table_privilege(:role, :table, :privilege)"),
                        {"role": APP_ROLE, "table": table, "privilege": privilege},
                    )
                }
                for table in sorted(tables)
            }
    finally:
        await engine.dispose()


async def _tables_present(dsn: str) -> set[str]:
    """The tables that actually exist, so the assertion can be scoped to a revision.

    `has_table_privilege` raises on a relation that does not exist, so a test that
    stops below `head` cannot iterate the whole classification.
    """
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            )
            return set(rows.scalars().all())
    finally:
        await engine.dispose()


def test_the_grant_matrix_survives_a_downgrade_and_a_second_upgrade(scratch_dsn: str) -> None:
    """Idempotence of 0008, asserted by re-running it rather than by reading it.

    `alembic upgrade head` against a database already at head runs nothing, so it cannot
    tell you whether 0008 is safe to re-apply. Stepping down and back up is what does,
    and it exercises the downgrade path at the same time -- otherwise dead code that
    nobody notices is broken until the incident in which somebody needs it.

    It runs on a `scratch_dsn` upgraded only as far as 0008, rather than stepping a
    `head` database down to 0007, and that is the load-bearing detail. Reaching 0007
    from head means passing through every revision above it, and audit migrations
    refuse to downgrade by design (`.claude/rules/append-only-audit.md` clause 4).
    The old form therefore carried an undeclared requirement that every migration ever
    added above 0008 stay reversible -- a requirement nothing stated and nobody could
    satisfy, which 0015 was simply the first to break.

    Scoping to 0008 also makes the test's subject exact. The full head grant matrix is
    already asserted, table by table and role by role, in
    `test_live_grants_equal_the_declared_matrix`; the unique thing proven here is
    re-application, and proving it does not require head.

    Synchronous, unlike everything else in this file: `migrations/env.py` calls
    `asyncio.run()` itself, which raises from inside a running loop. Alembic drives its
    own event loop and a test that wants to invoke it has to stay outside one.
    """
    config = alembic_config(scratch_dsn)
    command.upgrade(config, _GRANT_MATRIX_REVISION)
    command.downgrade(config, "0007_processed_events")
    command.upgrade(config, _GRANT_MATRIX_REVISION)

    classified_and_present = asyncio.run(_tables_present(scratch_dsn)) & set(CLASSIFICATION)
    assert classified_and_present, "0008 created no classified table; the scope is wrong"

    held = asyncio.run(_application_privileges(scratch_dsn, classified_and_present))
    for table in sorted(classified_and_present):
        assert held[table] == set(granted_privileges(table, APP_ROLE)), table


@pytest.mark.unit
def test_asking_for_the_privileges_of_an_unclassified_table_raises() -> None:
    """ "No privileges" and "nobody has decided" are different answers.

    Returning an empty set for an unknown table would make an unclassified table look
    correctly locked down, which is the one reading that must not be available.
    """
    with pytest.raises(KeyError, match="has no privilege class"):
        granted_privileges("a_table_that_was_never_classified", APP_ROLE)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provisioning_a_password_onto_an_unknown_role_is_refused() -> None:
    """Refused before a connection is used, so a typo in a role name cannot silently
    create a credential for a role the grant matrix says nothing about."""
    with pytest.raises(UnknownLoginRoleError, match="fking_app_logn"):
        await provision_login_passwords(
            cast(AsyncConnection, None), {"fking_app_logn": SecretStr("x")}
        )
