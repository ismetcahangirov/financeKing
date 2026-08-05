"""The rollback drill: the schema half of the asymmetric procedure, actually executed.

`RELEASE_PROCESS.md` section 7 claims that code rolls back and schema rolls forward,
and the whole claim rests on one empirical fact: `alembic downgrade` cannot walk past
`0002_audit_substrate`. That fact is asserted here against a real PostgreSQL, by
running the rollback rather than by reading the migration.

Two findings this drill produced, both in the notes it justifies:

**The refusal is not a clean abort.** `migrations/env.py` sets
`transaction_per_migration=True`, so each revision commits on its own. A
`downgrade base` from `head` therefore *succeeds* through every revision above 0002 --
dropping hypertables, functions, triggers and grants as it goes -- and only then
raises. What an operator is left with is not "the schema I started with"; it is a
half-torn-down schema pinned at 0002, with the audit tables intact and everything above
them gone. That is much worse than a refusal at the first step, and it is why the
procedure in the notes says `alembic downgrade` must not be run at all rather than
"it will refuse anyway".

**Forward always works from wherever the refusal left you.** The third assertion below
is the one that makes the published procedure survivable: after the partial teardown,
`upgrade head` restores the schema. Roll-forward is the recovery path, which is exactly
what `RELEASE_PROCESS.md` section 7.1 tells you to do when the old code cannot run
against the new schema.

Marked `slow`: it runs the migration chain three times.
"""

from __future__ import annotations

import asyncio
from typing import Final

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# The floor. `.claude/rules/append-only-audit.md`: `downgrade()` on an audit migration
# raises, because dropping the table every trade is reconstructed from is a
# data-destruction operation dressed as a schema operation.
AUDIT_SUBSTRATE: Final[str] = "0002_audit_substrate"


def _stamped_revision(dsn: str) -> str | None:
    async def read() -> str | None:
        engine = create_async_engine(dsn)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                )
                row = result.first()
                return None if row is None else str(row[0])
        finally:
            await engine.dispose()

    return asyncio.run(read())


def test_the_schema_refuses_to_roll_back_past_the_audit_substrate(scratch_dsn: str) -> None:
    """The drill. Executed, not asserted from the source."""
    config = alembic_config(scratch_dsn)
    command.upgrade(config, "head")
    head = _stamped_revision(scratch_dsn)
    assert head is not None
    assert head != AUDIT_SUBSTRATE

    # A genuine rollback attempt, which is what `make migrate-down` repeated does.
    with pytest.raises(RuntimeError, match="irreversible"):
        command.downgrade(config, "base")

    # Where the refusal leaves the database: pinned at the audit substrate, with
    # everything above it already dropped and committed. The notes say "do not run
    # this" rather than "it will refuse" because of exactly this line.
    assert _stamped_revision(scratch_dsn) == AUDIT_SUBSTRATE

    # And the recovery path the published procedure depends on.
    command.upgrade(config, "head")
    assert _stamped_revision(scratch_dsn) == head


def test_the_audit_tables_survive_the_refused_rollback(scratch_dsn: str) -> None:
    """The refusal is worth something only if it actually protected the rows.

    A `downgrade` that raised *after* dropping `audit_log` would be a refusal with no
    subject, and nothing else in this repository would notice.
    """
    config = alembic_config(scratch_dsn)
    command.upgrade(config, "head")
    with pytest.raises(RuntimeError, match="irreversible"):
        command.downgrade(config, "base")

    async def audit_tables_present() -> set[str]:
        engine = create_async_engine(scratch_dsn)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    sa.text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' "
                        "AND table_name IN ('audit_log', 'trial_ledger')"
                    )
                )
                return {str(row[0]) for row in result}
        finally:
            await engine.dispose()

    assert asyncio.run(audit_tables_present()) == {"audit_log", "trial_ledger"}
