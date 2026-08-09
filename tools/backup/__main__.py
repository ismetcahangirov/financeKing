"""`python -m tools.backup` — take a dump, list what exists, or apply retention.

    python -m tools.backup dump  [--directory backups] [--label fking]
    python -m tools.backup list  [--directory backups]
    python -m tools.backup prune [--directory backups] [--keep-days 30] [--apply]

`prune` without `--apply` prints what it *would* delete and deletes nothing. That is the
default because `docs/rules/append-only-audit.md` requires archival to be
distinguishable from truncation, and a retention pass whose effect can only be observed
after it has run is not distinguishable from either.

The DSN comes from the settings tree, the same place `alembic/env.py` reads it, so a
backup and the database it is meant to protect cannot be pointed at different servers.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from fking.platform.config import load_settings
from tools.backup.dump import take_dump
from tools.backup.manifest import MANIFEST_SUFFIX, expired, manifests_in

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
DEFAULT_DIRECTORY: Path = REPO_ROOT / "backups"


def _dsn() -> str:
    return str(load_settings().database.dsn)


def _dump(directory: Path, label: str) -> int:
    outcome = take_dump(_dsn(), directory, label=label)
    manifest = outcome.manifest
    print(f"wrote {outcome.dump_path.name} ({manifest.dump_bytes} bytes)")
    print(f"  sha256   {manifest.dump_sha256}")
    print(f"  revision {manifest.alembic_revision}")
    print(f"  server   {manifest.server_version}")
    for tip in manifest.chain_tips:
        print(f"  tip      {tip.table_name} seq={tip.seq} hash={tip.row_hash_hex[:16] or '-'}")
    print(f"  elapsed  {outcome.elapsed_seconds:.1f}s")
    return 0


def _list(directory: Path) -> int:
    found = manifests_in(directory)
    if not found:
        print(f"no backups in {directory}")
        return 0
    for manifest in found:
        tips = " ".join(f"{tip.table_name}={tip.seq}" for tip in manifest.chain_tips)
        print(
            f"{manifest.created_at_utc}  {manifest.dump_filename}  "
            f"rev={manifest.alembic_revision}  {tips}"
        )
    return 0


def _prune(directory: Path, keep_days: int, apply_deletions: bool) -> int:
    removable = expired(manifests_in(directory), keep_days=keep_days, now=datetime.now(UTC))
    if not removable:
        print(f"nothing older than {keep_days} days beyond the retained minimum")
        return 0
    for manifest in removable:
        print(f"{'deleting' if apply_deletions else 'would delete'} {manifest.dump_filename}")
        if apply_deletions:
            (directory / manifest.dump_filename).unlink(missing_ok=True)
            (directory / f"{manifest.dump_filename}{MANIFEST_SUFFIX}").unlink(missing_ok=True)
    if not apply_deletions:
        print("nothing was deleted; pass --apply to act")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.backup", description=__doc__)
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    subcommands = parser.add_subparsers(dest="command", required=True)

    dump_parser = subcommands.add_parser("dump", help="take a verified dump")
    dump_parser.add_argument("--label", default="fking")

    subcommands.add_parser("list", help="list the backups present, newest first")

    prune_parser = subcommands.add_parser("prune", help="apply retention")
    prune_parser.add_argument("--keep-days", type=int, default=30)
    prune_parser.add_argument("--apply", action="store_true")

    parsed = parser.parse_args(argv)
    directory: Path = parsed.directory
    if parsed.command == "dump":
        return _dump(directory, parsed.label)
    if parsed.command == "list":
        return _list(directory)
    return _prune(directory, parsed.keep_days, parsed.apply)


if __name__ == "__main__":
    raise SystemExit(main())
