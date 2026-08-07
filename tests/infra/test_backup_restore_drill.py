"""The recovery drill: a backup taken, restored, and verified against its own chain.

A backup nobody has restored is a hypothesis. This file is the experiment, and it is
run by `make restore-drill` and on a schedule by
`.github/workflows/restore-drill.yml` -- not by hand, because a drill that depends on
somebody remembering to run it has the same evidentiary value as a documented intention.

What each test establishes, and why it is not obvious from reading the dump command:

1. **A dump restores at all**, into a database whose roles were created by migration
   rather than by the dump. TimescaleDB is the reason this is not a formality:
   hypertable chunks and the extension have restore-ordering requirements that a
   plain-SQL dump replays wrongly, and discovering that during an incident is the
   scenario the issue describes.
2. **The chains verify end to end after restore**, against the tips recorded in the
   manifest at dump time. Verifying the chain *without* the manifest tip would pass on a
   truncated restore, because a prefix of a valid chain is a valid chain.
3. **A backup that silently dropped rows fails the drill at the exact seq.** This is
   simulated the only way it can be -- by disabling the append-only trigger as
   superuser and deleting the tail, which is precisely what a stale or doctored dump
   looks like from the restored database's point of view.
4. **A dump taken under the previous schema version upgrades cleanly**, and the
   revision it was taken at is recorded so a restore into a newer schema is a decision
   rather than a discovery.
5. **The restore holds TimescaleDB in restore mode and hands it back**, on both the
   success and the failure path. This is the one whose absence made the drill fail on
   pull requests that had touched nothing near it: TimescaleDB's per-database background
   scheduler writes into the very catalog table the restore is loading, and whether it
   had fired yet was a function of wall clock rather than of the diff (#192).

Marked `slow`: it runs `pg_dump`, `pg_restore` and the migration chain several times.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

from fking.platform.persistence.chain import (
    CHAINED_TABLES,
    ChainVerification,
    read_chain,
    verify_rows,
)
from tests.conftest import (
    REPO_ROOT,
    _create_database,
    _drop_database,
    alembic_config,
    dsn_for,
    refuse_or_skip,
)
from tools.backup.dump import DumpIntegrityError, DumpResult, restore_into, take_dump
from tools.backup.manifest import BackupManifest
from tools.backup.pgtools import PgRunner, PgToolsUnavailableError, resolve_runner

pytestmark = [pytest.mark.integration, pytest.mark.slow]

AUDIT_ROWS = 6
LEDGER_ROWS = 3


@pytest.fixture
def restore_dsn(postgres_server: str) -> Iterator[str]:
    """An empty database to restore into, dropped afterwards.

    Not `scratch_dsn`: the drill needs two databases at once -- the one being backed up
    and the one being restored into -- and a single fixture cannot supply both.
    """
    name = f"fking_restore_{uuid.uuid4().hex[:12]}"
    # template0, not the default template1: the TimescaleDB image installs its extension
    # into template1, so a target cloned from it arrives with a populated
    # `_timescaledb_catalog` and a background scheduler already attached, and a restore
    # target that is not empty is a restore target whose contents nobody chose.
    #
    # This is hygiene, not the cure for the duplicate-`exported_uuid` failure that this
    # comment previously claimed: measured on the pinned image, `pg_restore` into a
    # template1-derived target that already carries that row exits 0, because the
    # extension's `metadata_insert_trigger` dedupes the replayed rows. What actually
    # failed was a race against the background scheduler, and `restore_into` closes it
    # with `timescaledb_pre_restore()` (#192).
    asyncio.run(_create_database(postgres_server, name, template="template0"))
    try:
        yield dsn_for(postgres_server, name)
    finally:
        asyncio.run(_drop_database(postgres_server, name))


async def _seed_chains(dsn: str) -> None:
    """Rows in both chained tables, so the drill has a chain to verify."""
    engine = create_async_engine(dsn)
    try:
        async with engine.begin() as connection:
            for index in range(AUDIT_ROWS):
                await connection.execute(
                    sa.text(
                        """
                        INSERT INTO audit_log (
                            occurred_at_utc, correlation_id, actor, event_type,
                            subject_id, payload, prev_hash, row_hash
                        ) VALUES (
                            now(), gen_random_uuid(), 'drill', 'backup.rehearsal',
                            :subject, cast(:payload as jsonb), '\\x00'::bytea, '\\x00'::bytea
                        )
                        """
                    ),
                    {"subject": f"audit-{index}", "payload": f'{{"index": {index}}}'},
                )
            for index in range(LEDGER_ROWS):
                await connection.execute(
                    sa.text(
                        """
                        INSERT INTO trial_ledger (
                            charged_at_utc, correlation_id, spec_hash, registered_by,
                            statement, parameter_grid, n_parameters, n_symbols,
                            n_variants, trials_charged, cumulative_trials,
                            prev_hash, row_hash,
                            search_context_hash, lineage_id, entry_kind
                        ) VALUES (
                            now(), gen_random_uuid(), :spec, 'drill',
                            'rehearsal grid', '{"fast": [1, 2]}'::jsonb, 1, 1,
                            1, 2, 0, '\\x00'::bytea, '\\x00'::bytea,
                            -- 0018 makes the search context mandatory on every new row.
                            -- One context for the whole rehearsal: the drill is about
                            -- whether the chain survives a dump and restore, not about
                            -- which search the charges belong to.
                            decode(repeat('33', 32), 'hex'), 'lin-drill', 'registration'
                        )
                        """
                    ),
                    {"spec": f"spec-{index}".encode()},
                )
    finally:
        await engine.dispose()


async def _verify_against(dsn: str, manifest: BackupManifest) -> dict[str, ChainVerification]:
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as connection:
            return {
                name: verify_rows(
                    name,
                    await read_chain(connection, name),
                    expected_tip=manifest.tip_for(name),
                )
                for name in CHAINED_TABLES
            }
    finally:
        await engine.dispose()


async def _delete_tail_as_superuser(dsn: str, table_name: str, keep: int) -> None:
    """Simulate a dump that silently dropped the tail.

    The append-only trigger has to be disabled to do this, which is the point: the only
    actors who can produce this state are ones the triggers cannot stop -- a superuser,
    a doctored `pg_dump`, direct file access -- and the chain is what makes them visible
    afterwards.
    """
    engine = create_async_engine(dsn)
    try:
        async with engine.begin() as connection:
            guard = f"{table_name}_no_update_delete"
            await connection.execute(sa.text(f"ALTER TABLE {table_name} DISABLE TRIGGER {guard}"))
            await connection.execute(
                sa.text(
                    # table_name comes from CHAINED_TABLES, never from input.
                    f"DELETE FROM {table_name} WHERE seq > "  # noqa: S608
                    f"(SELECT min(seq) + :keep - 1 FROM {table_name})"
                ),
                {"keep": keep},
            )
            await connection.execute(sa.text(f"ALTER TABLE {table_name} ENABLE TRIGGER {guard}"))
    finally:
        await engine.dispose()


async def _session_restoring_setting(dsn: str) -> str:
    """What a *new* connection to the target sees for `timescaledb.restoring`.

    A new connection is the point: `timescaledb_pre_restore()` sets the flag with `ALTER
    DATABASE`, so the only thing that proves pg_restore's own connection will inherit it
    is reading it from a connection pg_restore did not open.
    """
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            return str(
                (await connection.execute(sa.text("SHOW timescaledb.restoring"))).scalar_one()
            )
    finally:
        await engine.dispose()


async def _database_level_restoring_settings(dsn: str) -> tuple[str, ...]:
    """Any `timescaledb.restoring` entry still attached to the database itself.

    `SHOW` would report `off` for a session whose database carries no setting at all, so
    it cannot distinguish "cleared" from "never set". `pg_db_role_setting` can.
    """
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        sa.text(
                            """
                        SELECT unnest(setting.setconfig)
                          FROM pg_db_role_setting AS setting
                          JOIN pg_database AS database ON database.oid = setting.setdatabase
                         WHERE database.datname = current_database()
                        """
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()
    return tuple(entry for entry in rows if entry.startswith("timescaledb.restoring"))


def _dump_or_skip(dsn: str, directory: Path) -> DumpResult:
    try:
        return take_dump(dsn, directory, label="drill")
    except PgToolsUnavailableError as unavailable:
        refuse_or_skip(f"the drill needs pg_dump/pg_restore: {unavailable}")
        raise


def test_a_dump_restores_and_both_chains_verify_against_their_manifest_tips(
    migrated_dsn: str, restore_dsn: str, tmp_path: Path
) -> None:
    """The drill proper. Backup, restore, verify -- executed, not described."""
    asyncio.run(_seed_chains(migrated_dsn))
    outcome = _dump_or_skip(migrated_dsn, tmp_path)
    manifest = outcome.manifest

    assert manifest.alembic_revision, "the manifest must record what schema it was taken at"
    assert manifest.tip_for("audit_log").seq == AUDIT_ROWS
    assert manifest.tip_for("trial_ledger").seq == LEDGER_ROWS

    restore_into(restore_dsn, outcome.dump_path, manifest)

    verifications = asyncio.run(_verify_against(restore_dsn, manifest))
    for name in CHAINED_TABLES:
        verification = verifications[name]
        assert verification.is_intact, verification.describe()

    assert verifications["audit_log"].checked_rows == AUDIT_ROWS
    assert verifications["trial_ledger"].checked_rows == LEDGER_ROWS


def test_a_backup_missing_its_tail_fails_the_drill_at_the_first_missing_seq(
    migrated_dsn: str, restore_dsn: str, tmp_path: Path
) -> None:
    """The check that distinguishes a good restore from a plausible one."""
    asyncio.run(_seed_chains(migrated_dsn))
    outcome = _dump_or_skip(migrated_dsn, tmp_path)
    manifest = outcome.manifest
    restore_into(restore_dsn, outcome.dump_path, manifest)

    kept = AUDIT_ROWS - 2
    asyncio.run(_delete_tail_as_superuser(restore_dsn, "audit_log", keep=kept))

    verifications = asyncio.run(_verify_against(restore_dsn, manifest))
    audit = verifications["audit_log"]

    assert not audit.is_intact
    assert audit.first_broken_seq == kept + 1, (
        "the drill must name the first missing seq, which is the window an operator "
        "has to report as lost"
    )
    assert audit.reason is not None
    assert "truncated" in audit.reason
    # And the untouched chain still verifies, so the failure is localised rather than
    # a blanket "something is wrong with the restore".
    assert verifications["trial_ledger"].is_intact


def test_a_dump_taken_under_the_previous_schema_upgrades_cleanly(
    postgres_server: str, restore_dsn: str, tmp_path: Path
) -> None:
    """Restoring an older backup must upgrade or fail loudly, never half-apply.

    The dump is taken at head's predecessor rather than at a hard-coded revision, so
    this keeps testing the newest migration as the chain grows instead of testing an
    increasingly ancient one.
    """
    scripts = ScriptDirectory(str(REPO_ROOT / "migrations"))
    head = scripts.get_current_head()
    assert head is not None
    previous = scripts.get_revision(head).down_revision
    assert isinstance(previous, str), "head must have a single linear predecessor"

    older = f"fking_older_{uuid.uuid4().hex[:12]}"
    # template0 here too, for the same reason the restore target uses it: what this dump
    # contains should be what the migrations put there, not whatever the image happened
    # to install into template1.
    asyncio.run(_create_database(postgres_server, older, template="template0"))
    try:
        older_dsn = dsn_for(postgres_server, older)
        command.upgrade(alembic_config(older_dsn), previous)
        outcome = _dump_or_skip(older_dsn, tmp_path)
        manifest = outcome.manifest
        assert manifest.alembic_revision == previous
    finally:
        asyncio.run(_drop_database(postgres_server, older))

    restore_into(restore_dsn, outcome.dump_path, manifest)

    async def stamped() -> str | None:
        engine = create_async_engine(restore_dsn)
        try:
            async with engine.connect() as connection:
                row = (
                    await connection.execute(sa.text("SELECT version_num FROM alembic_version"))
                ).first()
                return None if row is None else str(row[0])
        finally:
            await engine.dispose()

    assert asyncio.run(stamped()) == previous
    command.upgrade(alembic_config(restore_dsn), "head")
    assert asyncio.run(stamped()) == head


def test_a_corrupted_archive_is_refused_before_it_is_restored(
    migrated_dsn: str, restore_dsn: str, tmp_path: Path
) -> None:
    """The digest is checked first, so a truncated file cannot half-restore."""
    outcome = _dump_or_skip(migrated_dsn, tmp_path)
    dump_path = outcome.dump_path
    dump_path.write_bytes(dump_path.read_bytes()[: dump_path.stat().st_size // 2])

    with pytest.raises(DumpIntegrityError, match="changed after it was written"):
        restore_into(restore_dsn, dump_path, outcome.manifest)


def test_the_restore_holds_timescale_in_restore_mode_and_hands_it_back(
    migrated_dsn: str, restore_dsn: str, tmp_path: Path
) -> None:
    """The bracket that removes the second writer, asserted rather than assumed.

    TimescaleDB starts a background scheduler per database and that scheduler writes an
    `exported_uuid` row into `_timescaledb_catalog.metadata` -- the same table
    `pg_restore` is loading, whose dump condition is `WHERE key <> 'uuid'` so the archive
    carries rows for it. The extension's dedupe trigger is a non-atomic check-then-insert,
    so a scheduler insert landing inside the `COPY` fails it on the primary key. The
    defence is not to tolerate the racing writer, it is to not have one:
    `timescaledb_pre_restore()` stops the scheduler, `timescaledb_post_restore()` starts
    it again (#192).

    The runner is stubbed so this asserts the bracket rather than re-running a restore
    that three other tests already run. A database left in restore mode has its workers
    stopped and `session_replication_role` pinned to `replica`, so the second half of the
    assertion matters as much as the first.
    """
    outcome = _dump_or_skip(migrated_dsn, tmp_path)
    observed_during: list[str] = []

    class _RecordingRunner(PgRunner):
        # ARG002 is suppressed per parameter rather than fixed by renaming: the names are
        # fixed by PgRunner.run and `password` is keyword-only, so an underscore prefix
        # would change the override's signature.
        def run(
            self,
            program: str,  # noqa: ARG002
            arguments: list[str],  # noqa: ARG002
            *,
            password: str | None,  # noqa: ARG002
        ) -> str:
            observed_during.append(asyncio.run(_session_restoring_setting(restore_dsn)))
            return ""

    restore_into(
        restore_dsn,
        outcome.dump_path,
        outcome.manifest,
        runner=_RecordingRunner(kind="recording", workdir=tmp_path),
    )

    assert observed_during == ["on"], (
        "pg_restore must inherit timescaledb.restoring='on' from the database, or the "
        "background scheduler is still writing into the table being restored"
    )
    assert asyncio.run(_database_level_restoring_settings(restore_dsn)) == (), (
        "timescaledb_post_restore() must clear the database-level flag; leaving it set "
        "keeps the workers stopped and session_replication_role at 'replica'"
    )


def test_a_failed_restore_still_hands_timescale_back_out_of_restore_mode(
    migrated_dsn: str, restore_dsn: str, tmp_path: Path
) -> None:
    """The failure path is the one that strands a database, so it is the one to test.

    Recording the setting before raising keeps this from passing vacuously: without the
    bracket there is nothing to clear, and a test whose assertion is satisfied by the
    absence of the mechanism is not guarding it.
    """
    outcome = _dump_or_skip(migrated_dsn, tmp_path)
    observed_during: list[str] = []

    class _FailingRunner(PgRunner):
        def run(  # see _RecordingRunner on the noqa placement
            self,
            program: str,
            arguments: list[str],  # noqa: ARG002
            *,
            password: str | None,  # noqa: ARG002
        ) -> str:
            observed_during.append(asyncio.run(_session_restoring_setting(restore_dsn)))
            raise PgToolsUnavailableError(f"{program} exited 1 (test): synthetic failure")

    with pytest.raises(PgToolsUnavailableError, match="synthetic failure"):
        restore_into(
            restore_dsn,
            outcome.dump_path,
            outcome.manifest,
            runner=_FailingRunner(kind="failing", workdir=tmp_path),
        )

    assert observed_during == ["on"]
    assert asyncio.run(_database_level_restoring_settings(restore_dsn)) == (), (
        "a restore that failed must still leave the database usable; a stranded "
        "timescaledb.restoring='on' keeps its background workers stopped"
    )


def test_the_pg_client_is_version_matched_to_the_server(tmp_path: Path) -> None:
    """A client from another major restores silently incompletely, or refuses outright."""
    try:
        runner = resolve_runner(tmp_path, server_major=16)
    except PgToolsUnavailableError as unavailable:
        refuse_or_skip(str(unavailable))
        raise
    assert runner.kind.startswith(("local", "docker"))
