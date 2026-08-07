"""Hash-chain verification for the append-only tables.

`.claude/rules/append-only-audit.md` clause 3 says the chain exists because forbidding
a rewrite is not the same as detecting one: a superuser, a `pg_dump`/restore or direct
file access can still change history, and the chain is what makes that *visible*. This
module is the reader that makes it visible.

Three distinct corruptions, and they are found by three different comparisons:

**A rewritten row.** `row_hash` no longer matches the digest re-derived from the row's
own columns. Re-derivation is done by the same `fking_*_digest` SQL function the insert
trigger used, never by a Python reimplementation -- two copies of a hash recipe drift,
and the way you find out is a verifier that reports every row as tampered, after which
the verifier gets muted.

**A dropped row.** Row *n*'s `prev_hash` no longer equals row *n-1*'s `row_hash`. Note
that a `seq` *gap* proves nothing on its own: `seq` is `GENERATED ALWAYS AS IDENTITY`,
so a rolled-back transaction consumes a value and leaves a hole in a chain that is
perfectly intact. The link is the evidence; the numbering is not.

**A truncated tail** -- and this is the one that has no local evidence at all. Drop the
last thousand rows and what remains is a chain that verifies end to end, because a
prefix of a valid chain is a valid chain. That is exactly what a restore from a stale
dump looks like, and it is also what an archival job looks like if the archival job is
wrong. Distinguishing them requires a value from *outside* the restored database: the
tip that was recorded when the backup was taken. Hence `expected_tip`, and hence
`ChainTip` being written into the backup manifest by `tools/backup`.

The verification functions are split into a pure decision half and a thin I/O half so
that the decision half -- which is the part with the failure modes -- is testable
without a database and cannot be quietly bypassed by a query change.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "CHAINED_TABLES",
    "ChainRow",
    "ChainTip",
    "ChainVerification",
    "read_chain",
    "read_tip",
    "verify_chain",
    "verify_rows",
]

# The genesis `prev_hash` written by both triggers for the first row of a chain.
_GENESIS_PREV_HASH: Final[bytes] = b"\x00"

CHAINED_TABLES: Final[tuple[str, ...]] = ("audit_log", "trial_ledger")

# One SELECT per table, each re-deriving the digest through the table's own IMMUTABLE
# digest function. The column lists differ, so these cannot be a single parameterised
# query without building SQL from strings, and building SQL from strings around a
# verification routine is how a verifier ends up checking the wrong columns.
_CHAIN_QUERIES: Final[dict[str, str]] = {
    "audit_log": """
        SELECT seq,
               prev_hash,
               row_hash,
               fking_audit_log_digest(
                   prev_hash, seq, occurred_at_utc, correlation_id, causation_id,
                   actor, event_type, subject_id, payload
               ) AS rederived_hash
          FROM audit_log
         WHERE seq > :since_seq
         ORDER BY seq
    """,
    "trial_ledger": """
        SELECT seq,
               prev_hash,
               row_hash,
               fking_trial_ledger_digest(
                   prev_hash, seq, charged_at_utc, correlation_id, spec_hash,
                   registered_by, statement, parameter_grid, trials_charged,
                   cumulative_trials, holdout_touched
               ) AS rederived_hash
          FROM trial_ledger
         WHERE seq > :since_seq
         ORDER BY seq
    """,
}


class UnchainedTableError(ValueError):
    """A table was asked to be verified that carries no hash chain."""


@dataclass(frozen=True, slots=True)
class ChainRow:
    """One row's chain material, as read back from the database."""

    seq: int
    prev_hash: bytes
    row_hash: bytes
    rederived_hash: bytes


@dataclass(frozen=True, slots=True)
class ChainTip:
    """The terminal link of a chain at a point in wall-clock history.

    Recorded in a backup manifest so that a later restore can prove it received the
    whole chain rather than a valid prefix of it. `row_hash` is hex rather than bytes
    because it is serialised to JSON and read by a human during an incident.
    """

    table_name: str
    seq: int
    row_hash_hex: str

    @classmethod
    def empty(cls, table_name: str) -> ChainTip:
        """The tip of a chain with no rows. `seq` 0 is below every identity value."""
        return cls(table_name=table_name, seq=0, row_hash_hex="")


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """The verdict. `first_broken_seq` is non-null exactly when the chain is broken."""

    table_name: str
    checked_rows: int
    first_broken_seq: int | None
    reason: str | None

    @property
    def is_intact(self) -> bool:
        return self.first_broken_seq is None

    def describe(self) -> str:
        if self.is_intact:
            return f"{self.table_name}: {self.checked_rows} rows verified, chain intact"
        return (
            f"{self.table_name}: BROKEN at seq {self.first_broken_seq} "
            f"after {self.checked_rows} rows -- {self.reason}"
        )


def verify_rows(
    table_name: str,
    rows: Sequence[ChainRow],
    *,
    expected_tip: ChainTip | None = None,
    since_hash: bytes | None = None,
) -> ChainVerification:
    """Verify a contiguous run of chain rows. Pure: no I/O, no clock.

    `since_hash` is the `row_hash` the run is expected to link back to, for the
    incremental case where verification resumes from a previously verified point. When
    it is None the run must begin at the genesis hash.

    `expected_tip` is the out-of-band anchor described in the module docstring. Without
    it a truncated chain is indistinguishable from a short one.
    """
    expected_prev_hash = _GENESIS_PREV_HASH if since_hash is None else since_hash

    for index, row in enumerate(rows):
        if row.rederived_hash != row.row_hash:
            return ChainVerification(
                table_name=table_name,
                checked_rows=index,
                first_broken_seq=row.seq,
                reason="row_hash does not match the digest re-derived from the row",
            )
        if row.prev_hash != expected_prev_hash:
            return ChainVerification(
                table_name=table_name,
                checked_rows=index,
                first_broken_seq=row.seq,
                reason=(
                    "prev_hash does not match its predecessor's row_hash; a row "
                    "between them is missing or was rewritten"
                ),
            )
        expected_prev_hash = row.row_hash

    if expected_tip is not None:
        breach = _tip_mismatch(rows, expected_tip=expected_tip, since_hash=since_hash)
        if breach is not None:
            return ChainVerification(
                table_name=table_name,
                checked_rows=len(rows),
                first_broken_seq=breach[0],
                reason=breach[1],
            )

    return ChainVerification(
        table_name=table_name,
        checked_rows=len(rows),
        first_broken_seq=None,
        reason=None,
    )


def _tip_mismatch(
    rows: Sequence[ChainRow], *, expected_tip: ChainTip, since_hash: bytes | None
) -> tuple[int, str] | None:
    """Compare the observed terminal link against the recorded one.

    Returns the seq to report and why, or None when they agree. The seq reported for a
    truncation is the *first missing* one -- the point at which history stops -- which
    is the number an operator needs in order to say what window was lost.
    """
    if not rows:
        if expected_tip.seq == 0:
            return None
        # A chain that should have had rows and has none. When the run resumed from a
        # known hash, nothing at all arrived after it.
        first_missing = 1 if since_hash is None else expected_tip.seq
        return (
            first_missing,
            f"chain is empty but the manifest records a tip at seq {expected_tip.seq}; "
            f"every row after seq {first_missing - 1} is missing",
        )

    observed = rows[-1]
    if observed.seq < expected_tip.seq:
        return (
            observed.seq + 1,
            f"chain ends at seq {observed.seq} but the manifest records a tip at seq "
            f"{expected_tip.seq}; the tail was truncated, and a truncated chain "
            f"verifies internally because a prefix of a valid chain is a valid chain",
        )
    if observed.seq > expected_tip.seq:
        return (
            expected_tip.seq + 1,
            f"chain ends at seq {observed.seq}, beyond the manifest tip at seq "
            f"{expected_tip.seq}; rows were appended after the backup was taken",
        )
    if observed.row_hash.hex() != expected_tip.row_hash_hex:
        return (
            observed.seq,
            f"terminal row_hash {observed.row_hash.hex()} does not match the manifest "
            f"tip {expected_tip.row_hash_hex}; the chain was rebuilt over different "
            f"content",
        )
    return None


async def read_chain(
    connection: AsyncConnection, table_name: str, *, since_seq: int = 0
) -> tuple[ChainRow, ...]:
    """Read a table's chain material, re-deriving each digest in the database."""
    query = _CHAIN_QUERIES.get(table_name)
    if query is None:
        raise UnchainedTableError(
            f"{table_name!r} carries no hash chain; chained tables are {', '.join(CHAINED_TABLES)}"
        )
    rows = (await connection.execute(sa.text(query), {"since_seq": since_seq})).all()
    return tuple(
        ChainRow(
            seq=int(row.seq),
            prev_hash=bytes(row.prev_hash),
            row_hash=bytes(row.row_hash),
            rederived_hash=bytes(row.rederived_hash),
        )
        for row in rows
    )


async def read_tip(connection: AsyncConnection, table_name: str) -> ChainTip:
    """The current terminal link, for recording into a backup manifest."""
    if table_name not in CHAINED_TABLES:
        raise UnchainedTableError(
            f"{table_name!r} carries no hash chain; chained tables are {', '.join(CHAINED_TABLES)}"
        )
    row = (
        await connection.execute(
            sa.text(
                f"SELECT seq, row_hash FROM {table_name} "  # noqa: S608 - fixed literals
                "ORDER BY seq DESC LIMIT 1"
            )
        )
    ).first()
    if row is None:
        return ChainTip.empty(table_name)
    return ChainTip(table_name=table_name, seq=int(row.seq), row_hash_hex=bytes(row.row_hash).hex())


async def verify_chain(
    connection: AsyncConnection, table_name: str, *, expected_tip: ChainTip | None = None
) -> ChainVerification:
    """Read and verify one table's chain end to end."""
    rows = await read_chain(connection, table_name)
    return verify_rows(table_name, rows, expected_tip=expected_tip)
