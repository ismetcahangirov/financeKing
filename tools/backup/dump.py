"""Taking a verified dump, and restoring one into a scratch database.

The `pg_dump` flag choices are the part of this file that will matter during an
incident, so each is argued rather than copied:

* **`--format=custom`.** `pg_restore` can then reorder, and reordering is not optional
  on TimescaleDB: a plain-SQL dump replays in file order and hypertable chunks arrive
  before the extension has finished creating the parent, which fails with an error
  naming a chunk table nobody wrote.
* **`--no-owner` and `--no-privileges`.** The dump is restored into a scratch database
  whose roles are created by migration `0008_least_privilege`, not by the dump. Without
  these, a restore into an environment where `fking_app` does not yet exist fails on
  every `ALTER ... OWNER TO`, which is exactly the eight-months-of-untested-backups
  failure the issue describes. The grants come back from the migrations, which is where
  they are defined.
* **`--no-comments` is *not* passed.** The append-only rationale is carried in comments
  on several objects and losing it is losing provenance.
* **`--serializable-deferrable` is *not* passed.** It waits for a snapshot with no
  serialisation anomaly, which on a busy audit chain can wait indefinitely; the chain
  tip is read inside the same transaction-consistent snapshot anyway.

The dump is verified after it is written, not trusted: `pg_restore --list` must be able
to read the archive's table of contents. An archive that cannot be listed cannot be
restored, and finding that out at backup time costs a retry rather than an outage.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from fking.platform.persistence.chain import CHAINED_TABLES, ChainTip, read_tip
from tools.backup.manifest import BackupManifest, digest_of, write_manifest
from tools.backup.pgtools import TIMESCALE_IMAGE, PgRunner, resolve_runner

__all__ = ["DumpResult", "restore_into", "take_dump"]


@dataclass(frozen=True, slots=True)
class DumpResult:
    dump_path: Path
    manifest_path: Path
    manifest: BackupManifest
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class _ServerFacts:
    version_text: str
    major: int
    alembic_revision: str
    chain_tips: tuple[ChainTip, ...]


async def _read_server_facts(dsn: str) -> _ServerFacts:
    """Server version, stamped revision and every chain tip, in one snapshot.

    One transaction for all of them: a tip read after the dump's snapshot would record
    rows the dump does not contain, and the restore would then be reported as truncated
    when it is in fact complete. The window here is small rather than zero -- closing it
    entirely needs `pg_dump --snapshot`, which is a follow-up noted in DEPLOYMENT.md.
    """
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as connection:
            version_text = str(
                (await connection.execute(sa.text("SHOW server_version"))).scalar_one()
            )
            revision_row = (
                await connection.execute(sa.text("SELECT version_num FROM alembic_version"))
            ).first()
            tips = tuple([await read_tip(connection, name) for name in CHAINED_TABLES])
        return _ServerFacts(
            version_text=version_text,
            major=int(version_text.split(".", maxsplit=1)[0]),
            alembic_revision="" if revision_row is None else str(revision_row[0]),
            chain_tips=tips,
        )
    finally:
        await engine.dispose()


def _password_of(dsn: str) -> str | None:
    return sa.engine.make_url(dsn).password


def take_dump(dsn: str, directory: Path, *, label: str = "fking") -> DumpResult:
    """Dump `dsn` into `directory`, verify the archive, and write its manifest."""
    directory.mkdir(parents=True, exist_ok=True)
    facts = asyncio.run(_read_server_facts(dsn))
    runner = resolve_runner(directory, server_major=facts.major)

    started_at = datetime.now(UTC)
    dump_name = f"{label}-{started_at.strftime('%Y%m%dT%H%M%SZ')}.dump"
    dump_path = directory / dump_name

    runner.run(
        "pg_dump",
        [
            "--format=custom",
            "--compress=6",
            "--no-owner",
            "--no-privileges",
            f"--file={runner.container_path(dump_path)}",
            runner.dsn_for(dsn),
        ],
        password=_password_of(dsn),
    )

    # An archive whose table of contents cannot be read cannot be restored. Better to
    # learn that now, with the source database still there, than during a recovery.
    runner.run(
        "pg_restore",
        ["--list", runner.container_path(dump_path)],
        password=None,
    )

    manifest = BackupManifest(
        created_at_utc=started_at.isoformat(),
        dump_filename=dump_name,
        dump_sha256=digest_of(dump_path),
        dump_bytes=dump_path.stat().st_size,
        alembic_revision=facts.alembic_revision,
        server_version=facts.version_text,
        image=TIMESCALE_IMAGE,
        chain_tips=facts.chain_tips,
    )
    manifest_path = write_manifest(directory, manifest)
    elapsed_seconds = (datetime.now(UTC) - started_at).total_seconds()
    return DumpResult(
        dump_path=dump_path,
        manifest_path=manifest_path,
        manifest=manifest,
        elapsed_seconds=elapsed_seconds,
    )


class DumpIntegrityError(RuntimeError):
    """The archive on disk is not the archive the manifest describes."""


def restore_into(
    target_dsn: str,
    dump_path: Path,
    manifest: BackupManifest,
    *,
    runner: PgRunner | None = None,
    server_major: int | None = None,
) -> None:
    """Restore `dump_path` into an existing empty database at `target_dsn`.

    The digest is checked first. Restoring an archive whose bytes have changed since it
    was written produces a database that looks restored, and the point of recording the
    digest is that this is checkable before rather than after.

    `--exit-on-error` is deliberate. `pg_restore` defaults to continuing past errors and
    exiting 0 with a warning count, which is the single most effective way to end up
    believing a partial restore succeeded.
    """
    observed = digest_of(dump_path)
    if observed != manifest.dump_sha256:
        raise DumpIntegrityError(
            f"{dump_path.name} hashes to {observed} but its manifest records "
            f"{manifest.dump_sha256}; the archive changed after it was written"
        )

    major = server_major if server_major is not None else int(manifest.server_version.split(".")[0])
    active = runner if runner is not None else resolve_runner(dump_path.parent, server_major=major)
    active.run(
        "pg_restore",
        [
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            f"--dbname={active.dsn_for(target_dsn)}",
            active.container_path(dump_path),
        ],
        password=_password_of(target_dsn),
    )
