"""Migrations apply, revert, and produce exactly the schema `schema.py` describes.

The drift test is the one that earns its keep. Migrations are historical snapshots and
`METADATA` is the live description, so nothing but a test forces them to agree -- and
when they disagree the symptom is a query built against a column the database does not
have, at whatever hour that query first runs.
"""

from __future__ import annotations

import asyncio
import re

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fking.platform.persistence.schema import METADATA
from tests.conftest import REPO_ROOT, alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# Applied by 0001. Asserted rather than assumed: `CREATE EXTENSION IF NOT EXISTS` is a
# no-op on a server that already has it and a hard failure on one that cannot, and the
# case worth catching is the third one -- an image that ships the extension files but
# never loaded them, where every hypertable statement silently produces a plain table.
REQUIRED_EXTENSIONS = ("timescaledb", "pgcrypto")

# The newest revision whose `downgrade()` does not raise. Named rather than derived,
# because "does not raise" cannot be discovered without running it, and a test that
# discovers the boundary by stepping down until something breaks would leave a database
# in whatever state the break happened in. Move this forward when a reversible migration
# lands above it; do not move it forward to reach an audit migration.
_LAST_REVERSIBLE_REVISION = "0017_risk_drawdown_state"


@pytest.mark.unit
def test_the_revision_history_is_a_single_line_ending_at_head() -> None:
    """One head, no branches.

    Two heads make `alembic upgrade head` ambiguous, and the way that surfaces is a
    deploy that applies one branch and silently leaves the other unapplied. Reads the
    scripts off disk with no database and no environment mutation, so it runs anywhere.
    """
    script = ScriptDirectory(str(REPO_ROOT / "migrations"))
    assert len(script.get_heads()) == 1


async def _applied_revision(dsn: str) -> str | None:
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    finally:
        await engine.dispose()
    return str(revision) if revision is not None else None


def test_upgrade_then_downgrade_one_step_then_upgrade_again(scratch_dsn: str) -> None:
    """`make migrate`, `make migrate-down`, `make migrate`. The loop an operator runs.

    The step down is taken from the newest *reversible* revision rather than from head,
    and that is a property of the schema rather than a convenience. Audit migrations
    refuse to downgrade by design (`docs/rules/append-only-audit.md` clause 4), so
    whenever head happens to be one of them -- 0018 extends the trial ledger, and
    dropping its columns would leave charges nobody can attribute to a search -- there is
    no one-step-down from head to take. Scoping to the last reversible revision keeps the
    subject exact: what is under test is that a step down and back up leaves the database
    at head, not that every migration is reversible, which is a thing this repository
    deliberately does not promise.

    Synchronous on purpose. `migrations/env.py` calls `asyncio.run`, which cannot be
    reached from inside a running event loop -- so an `async def` test driving Alembic
    fails with a never-awaited coroutine rather than with anything about migrations.
    """
    config = alembic_config(scratch_dsn)
    command.upgrade(config, _LAST_REVERSIBLE_REVISION)
    command.downgrade(config, "-1")
    command.upgrade(config, "head")

    # Read off disk rather than hardcoded. A literal here makes every migration edit this
    # test, and a test that must be edited by every change is a test people edit without
    # reading -- which is how the assertion ends up describing whatever the code now does.
    expected_head = ScriptDirectory(str(REPO_ROOT / "migrations")).get_current_head()
    assert asyncio.run(_applied_revision(scratch_dsn)) == expected_head


def test_downgrading_past_the_audit_substrate_refuses(scratch_dsn: str) -> None:
    """0002 raises rather than dropping the audit trail, and says why in the message."""
    config = alembic_config(scratch_dsn)
    command.upgrade(config, "head")
    with pytest.raises(RuntimeError, match="irreversible"):
        command.downgrade(config, "base")


def test_downgrading_the_trial_ledgers_search_context_refuses(scratch_dsn: str) -> None:
    """0018 raises too, and for a reason the message has to carry.

    Dropping `trial_execution` discards the record of which configurations were actually
    run; dropping the search-context columns is worse, because the charges survive and
    can no longer be attributed to a search, which silently collapses every per-context
    and per-lineage count into the global one. A schema rollback that quietly changes
    what a number means is a data-destruction operation dressed as a schema operation.
    """
    config = alembic_config(scratch_dsn)
    command.upgrade(config, "head")
    with pytest.raises(RuntimeError, match="irreversible"):
        command.downgrade(config, "-1")


@pytest.mark.parametrize("extension", REQUIRED_EXTENSIONS)
@pytest.mark.asyncio
async def test_required_extensions_are_installed(engine: AsyncEngine, extension: str) -> None:
    async with engine.connect() as connection:
        installed = await connection.scalar(
            sa.text("SELECT 1 FROM pg_extension WHERE extname = :name"), {"name": extension}
        )
    assert installed == 1


@pytest.mark.asyncio
async def test_the_migrated_schema_matches_the_metadata(engine: AsyncEngine) -> None:
    """Column-by-column, in both directions.

    Server defaults and constraint bodies are deliberately not compared: Postgres
    normalises both -- `clock_timestamp()` comes back as `clock_timestamp()` but
    `false` comes back as `false` on one version and `'f'` on another -- and a test that
    fails on formatting is a test people delete. Names, types, nullability and presence
    are the parts a query depends on.

    Types are compared as the PostgreSQL dialect renders them, not as `str()` renders
    them: a generic `DateTime` prints as `DATETIME` while the reflected column prints as
    `TIMESTAMP`, and comparing those two strings fails on every row for no reason.
    """
    # The engine's own dialect rather than a freshly constructed one: SQLAlchemy's
    # dialect constructors are untyped, and this is the dialect that actually created
    # the schema, so both sides of the comparison render the same way by construction.
    dialect = engine.dialect

    def _render(column_type: sa.types.TypeEngine[object]) -> str:
        return str(column_type.compile(dialect=dialect))

    def _reflect(connection: sa.Connection) -> dict[str, dict[str, tuple[str, bool]]]:
        inspector = sa.inspect(connection)
        return {
            table_name: {
                column["name"]: (_render(column["type"]), bool(column["nullable"]))
                for column in inspector.get_columns(table_name)
            }
            for table_name in inspector.get_table_names()
            if table_name != "alembic_version"
        }

    async with engine.connect() as connection:
        reflected = await connection.run_sync(_reflect)

    assert set(reflected) == set(METADATA.tables), "table set differs from METADATA"

    mismatches: list[str] = []
    for table_name, table in METADATA.tables.items():
        expected = {
            column.name: (_render(column.type), bool(column.nullable)) for column in table.columns
        }
        actual = reflected[table_name]
        if set(expected) != set(actual):
            mismatches.append(
                f"{table_name}: metadata-only={sorted(set(expected) - set(actual))} "
                f"database-only={sorted(set(actual) - set(expected))}"
            )
            continue
        for column_name, expected_shape in expected.items():
            if actual[column_name] != expected_shape:
                mismatches.append(
                    f"{table_name}.{column_name}: metadata={expected_shape} "
                    f"database={actual[column_name]}"
                )
    assert mismatches == []


@pytest.mark.asyncio
async def test_no_money_column_is_double_precision(engine: AsyncEngine) -> None:
    """The information_schema half of the money-type rule.

    `test_schema_contract.py` asserts this against `METADATA`; this asserts it against
    what actually got created, which is what a hand-written migration can get wrong
    without `METADATA` noticing.
    """
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    """
                    SELECT table_name, column_name, data_type,
                           numeric_precision, numeric_scale
                      FROM information_schema.columns
                     WHERE table_schema = 'public'
                       AND (column_name LIKE '%\\_price' OR column_name LIKE '%\\_quantity'
                            OR column_name LIKE '%\\_quote' OR column_name LIKE '%\\_usd'
                            OR column_name LIKE '%\\_pnl'  OR column_name LIKE '%\\_fee'
                            OR column_name LIKE '%\\_bp'   OR column_name LIKE '%\\_volume')
                    """
                )
            )
        ).all()

    assert rows, "the money-column scan matched nothing, so it verified nothing"
    offenders = [
        (row.table_name, row.column_name, row.data_type)
        for row in rows
        if (row.data_type, row.numeric_precision, row.numeric_scale) != ("numeric", 38, 18)
    ]
    assert offenders == []


@pytest.mark.asyncio
async def test_no_utc_column_is_timestamp_without_time_zone(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    """
                    SELECT table_name, column_name, data_type
                      FROM information_schema.columns
                     WHERE table_schema = 'public' AND column_name LIKE '%\\_utc'
                    """
                )
            )
        ).all()

    assert rows, "the timestamp scan matched nothing, so it verified nothing"
    offenders = [
        (row.table_name, row.column_name, row.data_type)
        for row in rows
        if row.data_type != "timestamp with time zone"
    ]
    assert offenders == []


@pytest.mark.asyncio
async def test_constraint_names_follow_the_project_convention(engine: AsyncEngine) -> None:
    """Every constraint is named, and named by the convention rather than by Postgres.

    An auto-generated name differs between the machine that first ran the migration and
    the next one, so an error message quotes a name that exists nowhere in the
    repository.
    """
    async with engine.connect() as connection:
        names = (
            await connection.scalars(
                sa.text(
                    """
                    SELECT conname FROM pg_constraint c
                      JOIN pg_class t ON t.oid = c.conrelid
                      JOIN pg_namespace n ON n.oid = t.relnamespace
                     WHERE n.nspname = 'public' AND t.relname <> 'alembic_version'
                    """
                )
            )
        ).all()

    pattern = re.compile(r"^(pk|fk|uq|ck)_[a-z0-9_]+$")
    unconventional = sorted(name for name in names if not pattern.match(name))
    assert unconventional == []
