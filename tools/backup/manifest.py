"""The backup manifest, and the retention rule that decides what may be deleted.

A dump on its own cannot prove it is complete. The manifest is what carries the
out-of-band facts a restore is verified against:

* the **chain tips** at dump time, without which a truncated restore verifies cleanly
  (`fking.platform.persistence.chain` explains why at length);
* the **Alembic revision** the dump was taken at, so a restore into a newer schema is a
  decision rather than a surprise;
* the **SHA-256 of the dump file**, so a corrupted or partially-written archive is
  caught before it is restored rather than during;
* the **image digest** the server ran, because a Timescale dump is not portable across
  arbitrary extension versions and the restore procedure needs to know what to use.

Retention is a pure function over manifests, separately from any deletion, because
`.claude/rules/append-only-audit.md` requires that archival never be indistinguishable
from truncation: a policy that can be read and asserted is one you can prove deleted
only what it claimed to.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from fking.platform.persistence.chain import ChainTip

__all__ = [
    "MANIFEST_SUFFIX",
    "BackupManifest",
    "digest_of",
    "expired",
    "load_manifest",
    "manifests_in",
    "write_manifest",
]

MANIFEST_SUFFIX: Final[str] = ".manifest.json"
_DIGEST_CHUNK_BYTES: Final[int] = 1024 * 1024

# The floor exists so a clock skew, a mass-deletion bug or a retention window shortened
# in a hurry cannot leave zero restorable backups. Retention removes old copies; it is
# never the thing that removes the last one.
MINIMUM_RETAINED: Final[int] = 3


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """Everything a restore needs that the dump file itself cannot carry."""

    created_at_utc: str
    dump_filename: str
    dump_sha256: str
    dump_bytes: int
    alembic_revision: str
    server_version: str
    image: str
    chain_tips: tuple[ChainTip, ...]

    def tip_for(self, table_name: str) -> ChainTip:
        for tip in self.chain_tips:
            if tip.table_name == table_name:
                return tip
        raise KeyError(f"{table_name!r} has no recorded chain tip in {self.dump_filename}")

    @property
    def created_at(self) -> datetime:
        return datetime.fromisoformat(self.created_at_utc)

    def to_json(self) -> str:
        payload: dict[str, object] = dict(asdict(self))
        payload["chain_tips"] = [asdict(tip) for tip in self.chain_tips]
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> BackupManifest:
        payload = json.loads(text)
        return cls(
            created_at_utc=payload["created_at_utc"],
            dump_filename=payload["dump_filename"],
            dump_sha256=payload["dump_sha256"],
            dump_bytes=int(payload["dump_bytes"]),
            alembic_revision=payload["alembic_revision"],
            server_version=payload["server_version"],
            image=payload["image"],
            chain_tips=tuple(ChainTip(**tip) for tip in payload["chain_tips"]),
        )


def digest_of(path: Path) -> str:
    """SHA-256 of a file, read in chunks so a multi-gigabyte dump does not load."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_DIGEST_CHUNK_BYTES):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_manifest(directory: Path, manifest: BackupManifest) -> Path:
    path = directory / f"{manifest.dump_filename}{MANIFEST_SUFFIX}"
    path.write_text(manifest.to_json(), encoding="utf-8")
    return path


def load_manifest(path: Path) -> BackupManifest:
    return BackupManifest.from_json(path.read_text(encoding="utf-8"))


def manifests_in(directory: Path) -> tuple[BackupManifest, ...]:
    """Every manifest in `directory`, newest first."""
    found = [load_manifest(path) for path in sorted(directory.glob(f"*{MANIFEST_SUFFIX}"))]
    return tuple(sorted(found, key=lambda manifest: manifest.created_at, reverse=True))


def expired(
    manifests: Sequence[BackupManifest],
    *,
    keep_days: int,
    now: datetime,
    keep_minimum: int = MINIMUM_RETAINED,
) -> tuple[BackupManifest, ...]:
    """The manifests retention may delete. Pure; deletes nothing itself.

    `keep_minimum` newest copies are retained regardless of age. A retention rule that
    can empty the directory is a truncation with a schedule attached.
    """
    if now.tzinfo is None:
        raise ValueError("retention requires an aware UTC now")
    ordered = sorted(manifests, key=lambda manifest: manifest.created_at, reverse=True)
    cutoff = now.astimezone(UTC) - timedelta(days=keep_days)
    return tuple(
        manifest
        for manifest in ordered[keep_minimum:]
        if manifest.created_at.astimezone(UTC) < cutoff
    )
