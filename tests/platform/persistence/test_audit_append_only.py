"""The audit substrate refuses rewrites, and detects the ones it cannot refuse.

Four layers, four groups of tests. The last group is the one that would be easiest to
leave out and the one that matters most: a superuser can disable the trigger, and the
chain is the only thing that makes what they did afterwards visible.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from fking.platform.persistence.schema import APPEND_ONLY_TABLES

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_INSERT_AUDIT = sa.text(
    """
    INSERT INTO audit_log (occurred_at_utc, correlation_id, causation_id, actor,
                           event_type, subject_id, payload, prev_hash, row_hash)
    VALUES (:occurred_at_utc, :correlation_id, NULL, :actor, :event_type, :subject_id,
            cast(:payload as jsonb), '\\x00'::bytea, '\\x00'::bytea)
    RETURNING seq
    """
)


async def _append(
    connection: AsyncConnection, *, subject_id: str, payload: Mapping[str, object]
) -> int:
    seq = await connection.scalar(
        _INSERT_AUDIT,
        {
            "occurred_at_utc": datetime.now(UTC),
            "correlation_id": uuid.uuid4(),
            "actor": "risk",
            "event_type": "order.approved",
            "subject_id": subject_id,
            "payload": json.dumps(payload, sort_keys=True),
        },
    )
    return int(seq or 0)


# ---------------------------------------------------------------------------
# Layer 1 and 2: grants and the immutability trigger
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", sorted(APPEND_ONLY_TABLES))
@pytest.mark.asyncio
async def test_the_application_role_cannot_update_or_delete(
    engine: AsyncEngine, table: str
) -> None:
    """The primary control. `TRUNCATE` is included because no trigger can intercept it."""
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                sa.text(
                    """
                    SELECT has_table_privilege('fking_app', :t, 'UPDATE')   AS may_update,
                           has_table_privilege('fking_app', :t, 'DELETE')   AS may_delete,
                           has_table_privilege('fking_app', :t, 'TRUNCATE') AS may_truncate,
                           has_table_privilege('fking_app', :t, 'INSERT')   AS may_insert,
                           has_table_privilege('fking_app', :t, 'SELECT')   AS may_select
                    """
                ),
                {"t": table},
            )
        ).one()

    assert (row.may_update, row.may_delete, row.may_truncate) == (False, False, False)
    assert (row.may_insert, row.may_select) == (True, True)


@pytest.mark.parametrize("table", sorted(APPEND_ONLY_TABLES))
@pytest.mark.asyncio
async def test_every_append_only_table_carries_the_immutability_trigger(
    engine: AsyncEngine, table: str
) -> None:
    """The backstop, for the migration that later grants a broad role to a new service."""
    async with engine.connect() as connection:
        triggers = (
            await connection.scalars(
                sa.text(
                    """
                    SELECT tgname FROM pg_trigger
                     WHERE tgrelid = quote_ident(:t)::regclass AND NOT tgisinternal
                    """
                ),
                {"t": table},
            )
        ).all()
    assert f"{table}_no_update_delete" in triggers


@pytest.mark.asyncio
async def test_updating_an_audit_row_raises(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        seq = await _append(connection, subject_id="update-target", payload={"a": 1})

    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="append-only"):
            await connection.execute(
                sa.text("UPDATE audit_log SET actor = 'rewritten' WHERE seq = :s"), {"s": seq}
            )


@pytest.mark.asyncio
async def test_deleting_an_audit_row_raises(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        seq = await _append(connection, subject_id="delete-target", payload={"a": 1})

    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="append-only"):
            await connection.execute(sa.text("DELETE FROM audit_log WHERE seq = :s"), {"s": seq})


# ---------------------------------------------------------------------------
# Layer 3: what the database supplies and the client cannot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seq_is_monotone_and_not_client_supplied(engine: AsyncEngine) -> None:
    """`GENERATED ALWAYS` means a writer cannot choose where in history its row lands."""
    async with engine.begin() as connection:
        first = await _append(connection, subject_id="monotone-1", payload={})
        second = await _append(connection, subject_id="monotone-2", payload={})
        assert second > first

        with pytest.raises(DBAPIError, match="GENERATED ALWAYS"):
            await connection.execute(
                sa.text(
                    "INSERT INTO audit_log (seq, occurred_at_utc, correlation_id, actor, "
                    "event_type, subject_id, payload, prev_hash, row_hash) "
                    "VALUES (1, now(), gen_random_uuid(), 'a', 'e', 's', '{}'::jsonb, "
                    "'\\x00'::bytea, '\\x00'::bytea)"
                )
            )


@pytest.mark.asyncio
async def test_recorded_at_is_supplied_by_the_database(engine: AsyncEngine) -> None:
    """A writer that stamps its own insertion time can stamp a convenient one."""
    before = datetime.now(UTC)
    async with engine.begin() as connection:
        seq = await _append(connection, subject_id="recorded-at", payload={})
        recorded_at, occurred_at = (
            await connection.execute(
                sa.text("SELECT recorded_at_utc, occurred_at_utc FROM audit_log WHERE seq = :s"),
                {"s": seq},
            )
        ).one()

    assert recorded_at.tzinfo is not None
    assert recorded_at >= before
    # Distinct columns holding distinct facts: when it happened, and when we learned.
    assert recorded_at >= occurred_at


@pytest.mark.asyncio
async def test_each_row_links_to_its_predecessor(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        first = await _append(connection, subject_id="chain-1", payload={"b": 2, "a": 1})
        second = await _append(connection, subject_id="chain-2", payload={"a": 1})
        rows = (
            await connection.execute(
                sa.text(
                    "SELECT seq, prev_hash, row_hash FROM audit_log "
                    "WHERE seq IN (:a, :b) ORDER BY seq"
                ),
                {"a": first, "b": second},
            )
        ).all()

    assert rows[1].prev_hash == rows[0].row_hash
    assert rows[0].row_hash != rows[1].row_hash


@pytest.mark.asyncio
async def test_a_superuser_rewrite_breaks_the_chain(engine: AsyncEngine) -> None:
    """The layer the grants and the trigger cannot provide.

    A superuser, a doctored `pg_dump`/restore or direct file access can change history
    regardless of any grant. Re-deriving the digest from the row's own contents is what
    turns that from invisible into a named `seq`.
    """
    async with engine.begin() as connection:
        target = await _append(connection, subject_id="tamper", payload={"original": True})

    async with engine.begin() as connection:
        await connection.execute(
            sa.text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update_delete")
        )
        try:
            await connection.execute(
                sa.text(
                    "UPDATE audit_log SET payload = '{\"forged\": true}'::jsonb WHERE seq = :s"
                ),
                {"s": target},
            )
        finally:
            await connection.execute(
                sa.text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update_delete")
            )

    async with engine.connect() as connection:
        broken = (
            await connection.scalars(
                sa.text(
                    """
                    SELECT seq FROM audit_log
                     WHERE row_hash <> fking_audit_log_digest(
                               prev_hash, seq, occurred_at_utc, correlation_id,
                               causation_id, actor, event_type, subject_id, payload)
                     ORDER BY seq
                    """
                )
            )
        ).all()

    assert list(broken) == [target]


# ---------------------------------------------------------------------------
# The trial ledger
# ---------------------------------------------------------------------------

_INSERT_TRIAL = sa.text(
    """
    INSERT INTO trial_ledger (charged_at_utc, correlation_id, spec_hash, registered_by,
                              statement, parameter_grid, n_parameters, n_symbols,
                              n_variants, trials_charged, cumulative_trials,
                              holdout_touched, human_authorisation_ref,
                              prev_hash, row_hash)
    VALUES (now(), gen_random_uuid(), :spec_hash, 'evolution', :statement,
            '{}'::jsonb, 2, 1, 1, :trials_charged, 0, :holdout, :authorisation,
            '\\x00'::bytea, '\\x00'::bytea)
    RETURNING seq, cumulative_trials
    """
)


async def _charge(
    connection: AsyncConnection,
    *,
    trials_charged: int,
    holdout: bool = False,
    authorisation: str | None = None,
) -> tuple[int, int]:
    row = (
        await connection.execute(
            _INSERT_TRIAL,
            {
                "spec_hash": uuid.uuid4().bytes,
                "statement": f"declared grid of {trials_charged}",
                "trials_charged": trials_charged,
                "holdout": holdout,
                "authorisation": authorisation,
            },
        )
    ).one()
    return int(row.seq), int(row.cumulative_trials)


@pytest.mark.asyncio
async def test_the_running_total_is_computed_by_the_database(engine: AsyncEngine) -> None:
    """Monotone and not caller-supplied.

    A caller that supplies its own running total can supply a smaller one, and a trial
    count that understates the selection pool inflates every deflated Sharpe computed
    from it -- in the optimistic direction, silently.
    """
    async with engine.begin() as connection:
        before = await connection.scalar(sa.text("SELECT n FROM global_trial_count"))
        _, after_first = await _charge(connection, trials_charged=200)
        _, after_second = await _charge(connection, trials_charged=12)

    assert after_first == int(before or 0) + 200
    assert after_second == after_first + 12


@pytest.mark.asyncio
async def test_the_same_specification_cannot_be_charged_twice(engine: AsyncEngine) -> None:
    spec_hash = uuid.uuid4().bytes
    parameters = {
        "spec_hash": spec_hash,
        "statement": "one grid",
        "trials_charged": 8,
        "holdout": False,
        "authorisation": None,
    }
    async with engine.begin() as connection:
        await connection.execute(_INSERT_TRIAL, parameters)

    async with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="spec_hash"):
            await connection.execute(_INSERT_TRIAL, parameters)


@pytest.mark.asyncio
async def test_touching_the_holdout_without_a_named_human_is_refused(
    engine: AsyncEngine,
) -> None:
    """Reading the permanently held-out period burns it, and burning it is one person's
    decision taken once. A row claiming to have touched it with nobody's name on it is a
    row that should not exist."""
    async with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="holdout_needs_authorisation"):
            await _charge(connection, trials_charged=1, holdout=True, authorisation=None)

    async with engine.begin() as connection:
        seq, _ = await _charge(
            connection, trials_charged=1, holdout=True, authorisation="issue #17 comment"
        )
    assert seq > 0
