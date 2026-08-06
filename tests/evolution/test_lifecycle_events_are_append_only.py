"""The evolution schema refuses rewrites, has no state column, and detects tampering.

Four groups, matching the four layers of `.claude/rules/append-only-audit.md`: the
grants, the immutability trigger, the hash chain, and the migration that will not
downgrade. The third group is the one that would be easiest to leave out and the one
that matters most -- a superuser can disable the trigger, and the chain is the only
thing that makes what they did afterwards visible. `CLAUDE.md` section 10 states the
same requirement from the agent side: an agent cannot rewrite its own history to look
better.

The fifth group has no equivalent in the audit substrate: `evolution.strategy` carries
no writable current-state column, asserted against `information_schema` so that a later
migration adding one back fails here rather than in an incident.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.evolution import LifecycleState, LineageStore
from tests.conftest import alembic_config
from tests.evolution.conftest import (
    build_genesis,
    build_genome,
    build_record,
    next_transition,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_EVOLUTION_TABLES = ("genome", "genome_parent", "strategy", "strategy_lifecycle_events")

# Anything whose name reads as "the state this strategy is in right now". A column
# matching one of these on `evolution.strategy` is the thing #83 exists to prevent.
_STATE_SHAPED = ("state", "status", "lifecycle", "retired", "is_live", "current")

# A SHA-256 digest, in bytes. Asserted rather than assumed: a BYTEA of any other
# length in this column means the writer sent something that is not a genome hash.
_SHA256_BYTES = 32

# Genesis, the contract gate, and the CPCV gate.
_SEEDED_EVENTS = 3


async def _seed_one_strategy(engine: AsyncEngine, strategy_id: str) -> tuple[str, int]:
    """A genome, a strategy and its genesis event. Returns the genome hash and the seq."""
    store = LineageStore(engine)
    record = build_record()
    seq = await store.admit_strategy(
        strategy_id=strategy_id,
        record=record,
        genesis_event=build_genesis(strategy_id),
    )
    return record.genome_hash, seq


async def _append_two_transitions(store: LineageStore, strategy_id: str) -> tuple[int, int]:
    """`proposed -> backtested -> validated`, returning both chain sequence numbers."""
    backtested = next_transition(build_genesis(strategy_id), LifecycleState.BACKTESTED)
    second = await store.append_lifecycle_event(backtested)
    third = await store.append_lifecycle_event(
        next_transition(backtested, LifecycleState.VALIDATED)
    )
    return second, third


# ---------------------------------------------------------------------------
# Layer 1: grants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", _EVOLUTION_TABLES)
@pytest.mark.asyncio
async def test_the_application_role_may_only_append(engine: AsyncEngine, table: str) -> None:
    """`TRUNCATE` is included because no row trigger can intercept it."""
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
                {"t": f"evolution.{table}"},
            )
        ).one()

    assert (row.may_update, row.may_delete, row.may_truncate) == (False, False, False)
    assert (row.may_insert, row.may_select) == (True, True)


@pytest.mark.parametrize("table", _EVOLUTION_TABLES)
@pytest.mark.asyncio
async def test_the_ingestion_role_cannot_see_the_population(
    engine: AsyncEngine, table: str
) -> None:
    """A market-data writer has no business reading, still less writing, strategy history."""
    async with engine.connect() as connection:
        may_select = await connection.scalar(
            sa.text("SELECT has_table_privilege('fking_ingest', :t, 'SELECT')"),
            {"t": f"evolution.{table}"},
        )

    assert may_select is False


@pytest.mark.parametrize("table", _EVOLUTION_TABLES)
@pytest.mark.asyncio
async def test_public_holds_nothing_on_the_evolution_tables(
    engine: AsyncEngine, table: str
) -> None:
    async with engine.connect() as connection:
        grants = (
            await connection.scalars(
                sa.text(
                    """
                    SELECT privilege_type FROM information_schema.table_privileges
                     WHERE table_schema = 'evolution' AND table_name = :t
                       AND grantee = 'PUBLIC'
                    """
                ),
                {"t": table},
            )
        ).all()

    assert list(grants) == []


# ---------------------------------------------------------------------------
# Layer 2: the immutability trigger
# ---------------------------------------------------------------------------
#
# Two tests per operation, and the split is the point of the rule. Connected as
# `fking_app` the *grant* refuses first, so the message is `permission denied` and the
# trigger never runs. Connected as a role that holds the grant -- today a superuser,
# tomorrow whichever service a later migration hands `GRANT ALL ON ALL TABLES` to
# because that was the fast way to unblock a deploy -- the grant is gone and the trigger
# is what holds. Asserting only the first would leave the backstop unexercised, and
# asserting only the second would not answer the acceptance criterion, which names
# `fking_app`.


@pytest.mark.asyncio
async def test_update_as_the_application_role_is_refused_by_the_grant(
    app_engine: AsyncEngine,
) -> None:
    """The acceptance criterion, connected as the role the application actually uses."""
    await _seed_one_strategy(app_engine, "strat-update")

    async with app_engine.begin() as connection:
        with pytest.raises(DBAPIError, match="permission denied") as refused:
            await connection.execute(
                sa.text("UPDATE evolution.strategy_lifecycle_events SET reason = 'edited'")
            )

    assert "strategy_lifecycle_events" in str(refused.value)


@pytest.mark.asyncio
async def test_delete_as_the_application_role_is_refused_by_the_grant(
    app_engine: AsyncEngine,
) -> None:
    await _seed_one_strategy(app_engine, "strat-delete")

    async with app_engine.begin() as connection:
        with pytest.raises(DBAPIError, match="permission denied") as refused:
            await connection.execute(sa.text("DELETE FROM evolution.strategy_lifecycle_events"))

    assert "strategy_lifecycle_events" in str(refused.value)


@pytest.mark.asyncio
async def test_update_by_a_role_holding_the_grant_is_refused_by_the_trigger(
    engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The backstop, for the migration that later hands a broad role to a new service."""
    await _seed_one_strategy(app_engine, "strat-update-privileged")

    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="append-only") as refused:
            await connection.execute(
                sa.text("UPDATE evolution.strategy_lifecycle_events SET reason = 'edited'")
            )

    # The message names the table and the operation, so an operator reading it during an
    # incident is told what to do instead rather than only that it failed.
    assert "strategy_lifecycle_events" in str(refused.value)
    assert "UPDATE" in str(refused.value)
    assert "correcting row" in str(refused.value)


@pytest.mark.asyncio
async def test_delete_by_a_role_holding_the_grant_is_refused_by_the_trigger(
    engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    await _seed_one_strategy(app_engine, "strat-delete-privileged")

    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="append-only") as refused:
            await connection.execute(sa.text("DELETE FROM evolution.strategy_lifecycle_events"))

    assert "DELETE" in str(refused.value)


@pytest.mark.parametrize("table", _EVOLUTION_TABLES)
@pytest.mark.asyncio
async def test_every_evolution_table_carries_the_immutability_trigger(
    engine: AsyncEngine, table: str
) -> None:
    async with engine.connect() as connection:
        triggers = (
            await connection.scalars(
                sa.text(
                    """
                    SELECT tgname FROM pg_trigger tg
                      JOIN pg_class c ON c.oid = tg.tgrelid
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = 'evolution' AND c.relname = :t AND NOT tg.tgisinternal
                    """
                ),
                {"t": table},
            )
        ).all()

    assert f"{table}_no_update_delete" in list(triggers)


@pytest.mark.asyncio
async def test_a_genome_cannot_be_rewritten_either(
    engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A content-addressed row that can be edited is a hash that means nothing."""
    await _seed_one_strategy(app_engine, "strat-genome")

    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="append-only"):
            await connection.execute(sa.text("UPDATE evolution.genome SET generation_number = 99"))


# ---------------------------------------------------------------------------
# Layer 3: the hash chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_application_cannot_supply_its_own_chain_hashes(
    app_engine: AsyncEngine,
) -> None:
    """The trigger overwrites whatever the writer sent, so a forged row cannot be made
    consistent by the party forging it."""
    await _seed_one_strategy(app_engine, "strat-chain")

    async with app_engine.connect() as connection:
        row = (
            await connection.execute(
                sa.text("SELECT prev_hash, row_hash FROM evolution.strategy_lifecycle_events")
            )
        ).one()

    assert bytes(row.prev_hash) == b"\x00"
    assert bytes(row.row_hash) != b"\x00"
    assert len(bytes(row.row_hash)) == _SHA256_BYTES


@pytest.mark.asyncio
async def test_a_chain_of_appends_verifies(app_engine: AsyncEngine) -> None:
    strategy_id = "strat-verify"
    await _seed_one_strategy(app_engine, strategy_id)
    store = LineageStore(app_engine)
    await _append_two_transitions(store, strategy_id)

    verification = await store.verify_lifecycle_chain()

    assert verification.checked_rows == _SEEDED_EVENTS
    assert verification.is_intact
    assert verification.reason is None


@pytest.mark.asyncio
async def test_the_chain_detects_a_superuser_rewrite(
    engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Neither the grant nor the trigger can see this. The chain is what makes it visible."""
    strategy_id = "strat-tampered"
    await _seed_one_strategy(app_engine, strategy_id)
    store = LineageStore(app_engine)
    second, _third = await _append_two_transitions(store, strategy_id)
    assert (await store.verify_lifecycle_chain()).is_intact

    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "ALTER TABLE evolution.strategy_lifecycle_events "
                "DISABLE TRIGGER strategy_lifecycle_events_no_update_delete"
            )
        )
        await connection.execute(
            sa.text(
                "UPDATE evolution.strategy_lifecycle_events "
                "SET reason = 'looked better than it was' WHERE seq = :seq"
            ),
            {"seq": second},
        )

    verification = await store.verify_lifecycle_chain()

    assert verification.first_broken_seq == second
    assert verification.reason == "row_hash does not match the row contents"


@pytest.mark.asyncio
async def test_the_chain_detects_a_deleted_row(
    engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A missing row and a row that never existed are indistinguishable without the chain."""
    strategy_id = "strat-excised"
    await _seed_one_strategy(app_engine, strategy_id)
    store = LineageStore(app_engine)
    second, third = await _append_two_transitions(store, strategy_id)

    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "ALTER TABLE evolution.strategy_lifecycle_events "
                "DISABLE TRIGGER strategy_lifecycle_events_no_update_delete"
            )
        )
        await connection.execute(
            sa.text("DELETE FROM evolution.strategy_lifecycle_events WHERE seq = :seq"),
            {"seq": second},
        )

    verification = await store.verify_lifecycle_chain()

    assert verification.first_broken_seq == third
    assert verification.reason == "prev_hash does not match its predecessor"


# ---------------------------------------------------------------------------
# Layer 4: the migration will not downgrade
# ---------------------------------------------------------------------------


def test_downgrading_past_the_lineage_store_refuses(scratch_dsn: str) -> None:
    config = alembic_config(scratch_dsn)
    command.upgrade(config, "head")
    with pytest.raises(RuntimeError, match="irreversible"):
        command.downgrade(config, "0014_alt_observations")


# ---------------------------------------------------------------------------
# No writable current-state column
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evolution_strategy_has_no_state_shaped_column(engine: AsyncEngine) -> None:
    """State is derived from the event stream and lives nowhere else.

    Asserted against `information_schema` rather than against the migration, so a later
    migration that adds `lifecycle_state` back fails here -- which is the moment somebody
    has to argue for it rather than the moment an incident reveals it.
    """
    async with engine.connect() as connection:
        columns = (
            await connection.scalars(
                sa.text(
                    """
                    SELECT column_name FROM information_schema.columns
                     WHERE table_schema = 'evolution' AND table_name = 'strategy'
                    """
                )
            )
        ).all()

    offenders = [
        column for column in columns if any(token in column.lower() for token in _STATE_SHAPED)
    ]
    assert offenders == [], f"evolution.strategy exposes a writable state column: {offenders}"
    assert set(columns) == {
        "strategy_id",
        "genome_hash",
        "lineage_id",
        "created_at_utc",
        "recorded_at_utc",
    }


@pytest.mark.asyncio
async def test_current_state_is_a_view_over_the_event_stream(engine: AsyncEngine) -> None:
    """A view, not a table: there is no row for anyone to correct."""
    async with engine.connect() as connection:
        # `relkind` is `"char"`, which asyncpg hands back as bytes; cast it in SQL so the
        # assertion compares what it says it compares.
        relkind = await connection.scalar(
            sa.text(
                """
                SELECT c.relkind::text FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'evolution' AND c.relname = 'strategy_current_state'
                """
            )
        )
        definition = await connection.scalar(
            sa.text("SELECT pg_get_viewdef('evolution.strategy_current_state'::regclass)")
        )

    assert relkind == "v"
    assert "strategy_lifecycle_events" in str(definition)


@pytest.mark.asyncio
async def test_the_derived_state_follows_the_last_event(app_engine: AsyncEngine) -> None:
    strategy_id = "strat-derived"
    await _seed_one_strategy(app_engine, strategy_id)
    store = LineageStore(app_engine)

    assert await store.current_state(strategy_id) is LifecycleState.PROPOSED

    await store.append_lifecycle_event(
        next_transition(build_genesis(strategy_id), LifecycleState.BACKTESTED)
    )

    assert await store.current_state(strategy_id) is LifecycleState.BACKTESTED

    async with app_engine.connect() as connection:
        derived = await connection.scalar(
            sa.text(
                "SELECT current_state FROM evolution.strategy_current_state WHERE strategy_id = :s"
            ),
            {"s": strategy_id},
        )

    assert derived == LifecycleState.BACKTESTED.value


@pytest.mark.asyncio
async def test_the_application_cannot_write_through_the_state_view(
    app_engine: AsyncEngine,
) -> None:
    await _seed_one_strategy(app_engine, "strat-readonly")

    async with app_engine.begin() as connection:
        with pytest.raises((DBAPIError, ProgrammingError)):
            await connection.execute(
                sa.text("UPDATE evolution.strategy_current_state SET current_state = 'champion'")
            )


# ---------------------------------------------------------------------------
# Constraints that state a rule the documents also state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retired_is_terminal_at_the_database(app_engine: AsyncEngine) -> None:
    """Section 8 has no outgoing edges from `retired`, and the schema says so too."""
    strategy_id = "strat-terminal"
    genome_hash_hex, _ = await _seed_one_strategy(app_engine, strategy_id)

    async with app_engine.begin() as connection:
        with pytest.raises(IntegrityError, match="retired_is_terminal"):
            await connection.execute(
                sa.text(
                    """
                    INSERT INTO evolution.strategy_lifecycle_events (
                        event_id, strategy_id, genome_hash, correlation_id, from_state,
                        to_state, reason_class, reason, score_components,
                        independent_episode_count, forward_independent_episode_count,
                        global_trial_index, family_trial_index, scoring_version,
                        occurred_at_utc, prev_hash, row_hash
                    ) VALUES (
                        gen_random_uuid(), :s, :g, gen_random_uuid(), 'retired',
                        'proposed', 'operator', 'give it another chance', '{}'::jsonb,
                        0, 0, 1, 0, 'scoring-2026.08', now(), '\\x00'::bytea, '\\x00'::bytea
                    )
                    """
                ),
                {"s": strategy_id, "g": bytes.fromhex(genome_hash_hex)},
            )


@pytest.mark.asyncio
async def test_a_scored_state_cannot_be_entered_without_evidence(
    app_engine: AsyncEngine,
) -> None:
    """A promotion with no score, no components and no sample is a decision nobody can
    re-derive."""
    strategy_id = "strat-evidence"
    genome_hash_hex, _ = await _seed_one_strategy(app_engine, strategy_id)

    async with app_engine.begin() as connection:
        with pytest.raises(IntegrityError, match="scored_state_carries_evidence"):
            await connection.execute(
                sa.text(
                    """
                    INSERT INTO evolution.strategy_lifecycle_events (
                        event_id, strategy_id, genome_hash, correlation_id, from_state,
                        to_state, reason_class, reason, score_components,
                        independent_episode_count, forward_independent_episode_count,
                        global_trial_index, family_trial_index, scoring_version,
                        occurred_at_utc, prev_hash, row_hash
                    ) VALUES (
                        gen_random_uuid(), :s, :g, gen_random_uuid(), 'proposed',
                        'champion', 'operator', 'promoted on a hunch', '{}'::jsonb,
                        0, 0, 0, 0, 'scoring-2026.08', now(), '\\x00'::bytea, '\\x00'::bytea
                    )
                    """
                ),
                {"s": strategy_id, "g": bytes.fromhex(genome_hash_hex)},
            )


@pytest.mark.asyncio
async def test_a_genome_must_declare_at_least_one_feature(app_engine: AsyncEngine) -> None:
    """A genome that reads nothing computes nothing, and would still hash stably."""
    record = build_record(build_genome())
    async with app_engine.begin() as connection:
        with pytest.raises(IntegrityError, match="declares_at_least_one_feature"):
            await connection.execute(
                sa.text(
                    """
                    INSERT INTO evolution.genome (
                        genome_hash, structure_hash, lineage_id, generation_number,
                        trial_index_at_creation, mutation_operators, scoring_version,
                        expression, parameters, feature_ids,
                        holding_horizon_microseconds, created_at_utc
                    ) VALUES (
                        :h, :s, :l, 0, 0, '[]'::jsonb, 'scoring-2026.08',
                        '{}'::jsonb, '{}'::jsonb, '[]'::jsonb, 1, now()
                    )
                    """
                ),
                {
                    "h": bytes.fromhex(record.genome_hash),
                    "s": bytes.fromhex(record.structure_hash),
                    "l": record.lineage_id,
                },
            )
