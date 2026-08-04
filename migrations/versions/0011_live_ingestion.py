"""What live WebSocket ingestion needs from the schema: two gap kinds, and one read.

Revision ID: 0011_live_ingestion
Revises: 0010_feature_store

One migration rather than two, because both statements exist for one reason: `#27`'s
live session cannot record what it observes without them, and applying half of it
leaves a session that fails on its first frame instead of at deploy.

**The two gap kinds.** `0009_ingest_registry` enumerated the three a *file-based*
backfill can discover: `cadence` (bars missing inside a partition), `seam` (bars missing
between two partitions), and `absent_archive` (a period the host does not publish). Live
ingestion discovers two more, and they are genuinely different claims rather than new
names for the same one:

- **`sequence`** -- the aggTrade id jumped. The only kind carrying an exact missing
  count for a dataset with no cadence: `a` is a monotone integer the venue assigns, so
  `next - last - 1` is the number of prints that existed and were not received.
  `absent_archive` deliberately records no count for a countless dataset; flattening a
  sequence gap into it would throw away the one number that makes the gap actionable.
- **`disconnect`** -- the socket dropped. `missing_bar_count` stays NULL even for a
  kline series, because the claim is about *observation*, not about bars: a 400 ms
  reconnect inside one minute loses no bar at all, and recording `0` there would be a
  stronger statement than the evidence supports while the positivity `CHECK` would
  reject it outright. The row is written regardless, because a reconnect that recovers
  invisibly is how a missing minute becomes unexplainable nine months later
  (`DATA_PIPELINE.md` section 5).

Widening the `CHECK` rather than dropping it: the constraint is what stops a typo'd kind
becoming a fourth silent category that no report groups by.

**The reference read.** `bar` rows are keyed on `instrument_id`, which a live session
resolves from the symbol the stream names -- and `fking_ingest` held nothing at all on
`instrument`, so the first frame of the first session would have failed with `permission
denied`. `SELECT` and no more: a market-data writer that could `INSERT` an instrument
could invent the thing it claims to be recording, and a symbol with no instrument row
must stop the session rather than create one. The declared matrix in
`fking.platform.persistence.privileges` moves the three reference tables into a new
`REFERENCE` class that says exactly this, and the privileges test asserts the live
catalogue against it.

`downgrade` narrows both back. It will fail loudly if any live gap has been recorded,
which is correct: rolling this back with `sequence` or `disconnect` rows present would
mean deleting evidence of an outage to make a schema fit, and a refused migration is a
better outcome than a `DELETE` that succeeds.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_live_ingestion"
down_revision: str | None = "0010_feature_store"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INGEST: str = "fking_ingest"
_GAP_KIND_CONSTRAINT: str = "ck_coverage_gap_gap_kind_is_known"
_ARCHIVE_KINDS: str = "'cadence', 'seam', 'absent_archive'"
_LIVE_KINDS: str = "'sequence', 'disconnect'"
_REFERENCE_TABLES: tuple[str, ...] = ("venue", "instrument", "venue_maintenance_window")


def upgrade() -> None:
    op.execute(f"ALTER TABLE coverage_gap DROP CONSTRAINT {_GAP_KIND_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE coverage_gap ADD CONSTRAINT {_GAP_KIND_CONSTRAINT} "
        f"CHECK (gap_kind IN ({_ARCHIVE_KINDS}, {_LIVE_KINDS}))"
    )

    for table in _REFERENCE_TABLES:
        op.execute(f"GRANT SELECT ON {table} TO {_INGEST}")


def downgrade() -> None:
    for table in _REFERENCE_TABLES:
        op.execute(f"REVOKE SELECT ON {table} FROM {_INGEST}")

    op.execute(f"ALTER TABLE coverage_gap DROP CONSTRAINT {_GAP_KIND_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE coverage_gap ADD CONSTRAINT {_GAP_KIND_CONSTRAINT} "
        f"CHECK (gap_kind IN ({_ARCHIVE_KINDS}))"
    )
