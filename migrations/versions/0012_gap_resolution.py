"""What a REST gap backfill needs from the schema: a resolution, and a third provenance.

Revision ID: 0012_gap_resolution
Revises: 0011_live_ingestion

`0009_ingest_registry` recorded gaps and never took one back, because until now nothing
could: the archive path discovers holes and the live path discovers holes, and neither
one fills anything. `#28` adds the first writer that closes a gap, and closing a gap is
the operation with the most attractive wrong implementations. Three of them are refused
here rather than in review.

**A resolved gap is marked, never deleted.** The obvious implementation of "the gap is
gone" is `DELETE FROM coverage_gap`, and it destroys the one column a rebuild cannot
reproduce -- `discovered_at_utc`, which answers "which completed backtests consumed this
range before anybody knew there was a hole in it" (`DATA_PIPELINE.md` section 11). After
a backfill that question is *more* interesting, not less: the results computed over the
hole are still wrong, and the row marked `backfilled` is the only remaining evidence that
they were. So the row stays, `resolved_at_utc` and `resolution` are added beside it, and
`data_coverage` stops counting it.

**A partial backfill narrows, and narrowing is an insert plus a mark, never an update of
the bounds.** REST returning 40 of 60 missing minutes leaves 20 missing. The original row
is marked `superseded` and residual rows are inserted for what is still absent, each
carrying the *original* `discovered_at_utc`: those minutes were discovered missing then
and are missing still, so moving the instant forward would launder a known hole into a
freshly found one. Rewriting `gap_end_utc` in place would do the same thing while also
breaking the primary key's meaning.

**The rewrite is constrained by a trigger, not by convention.** `ck_..._resolution_*`
cannot express "only these two columns may ever change", so a `BEFORE UPDATE` row trigger
does, and a `BEFORE DELETE` trigger refuses removal outright. The `DELETE` grant is
revoked from `fking_ingest` as the primary control -- the trigger is the backstop, in the
order `docs/rules/append-only-audit.md` argues for, because a grant survives exactly
until a later migration hands a broad role to a new service.

**`bar.source` gains `rest_backfill`.** A bar recovered from `/api/v3/klines` after the
fact is not a bar the stream delivered on time, and folding it into `'stream'` would make
the two indistinguishable in the one column that answers "how did this row reach the
corpus". It is a real venue response, so it is not a synthesised row and the standing gate
(`fking.data.quality.standing`) admits it -- what that gate refuses is a value outside
`RecordSource`, and this migration and that enum move together.

`downgrade` refuses once any gap has been resolved. Dropping the columns would silently
reopen every closed gap, which is the conservative direction for a reader and a total loss
of the backfill record -- and a refused migration is a better outcome than a `DROP` that
succeeds by discarding evidence.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_gap_resolution"
down_revision: str | None = "0011_live_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP: str = "fking_app"
_INGEST: str = "fking_ingest"

_BAR_SOURCE_CONSTRAINT: str = "ck_bar_source_is_known"
_RESOLUTIONS: str = "'backfilled', 'superseded'"


def upgrade() -> None:
    op.execute(f"ALTER TABLE bar DROP CONSTRAINT {_BAR_SOURCE_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE bar ADD CONSTRAINT {_BAR_SOURCE_CONSTRAINT} "
        f"CHECK (source IN ('archive', 'stream', 'rest_backfill'))"
    )

    op.execute("ALTER TABLE coverage_gap ADD COLUMN resolved_at_utc TIMESTAMPTZ")
    op.execute("ALTER TABLE coverage_gap ADD COLUMN resolution TEXT")
    op.execute(
        f"""
        ALTER TABLE coverage_gap ADD CONSTRAINT ck_coverage_gap_resolution_is_known
            CHECK (resolution IS NULL OR resolution IN ({_RESOLUTIONS}))
        """
    )
    # Both columns or neither. A `resolution` with no instant cannot be ordered against
    # the backtests it invalidates, and an instant with no resolution says a gap was
    # closed without saying whether anything was actually recovered.
    op.execute(
        """
        ALTER TABLE coverage_gap ADD CONSTRAINT ck_coverage_gap_resolution_pairs_with_instant
            CHECK ((resolution IS NULL) = (resolved_at_utc IS NULL))
        """
    )
    # Partial, on the predicate every reader of this table now uses. An unresolved gap is
    # the rare row once a corpus has been repaired for a while, and a full index over
    # every gap ever closed would be mostly rows no availability check can be refused by.
    op.execute(
        """
        CREATE INDEX ix_coverage_gap_unresolved
            ON coverage_gap (market, dataset, symbol, bar_interval, gap_start_utc)
         WHERE resolved_at_utc IS NULL
        """
    )

    op.execute(
        """
        CREATE FUNCTION coverage_gap_resolution_is_the_only_rewrite() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.market, NEW.dataset, NEW.symbol, NEW.bar_interval,
                   NEW.gap_start_utc, NEW.gap_end_utc, NEW.gap_kind,
                   NEW.missing_bar_count, NEW.discovered_at_utc)
               IS DISTINCT FROM
               ROW(OLD.market, OLD.dataset, OLD.symbol, OLD.bar_interval,
                   OLD.gap_start_utc, OLD.gap_end_utc, OLD.gap_kind,
                   OLD.missing_bar_count, OLD.discovered_at_utc)
            THEN
                RAISE EXCEPTION
                    'coverage_gap is rewritable only in its resolution: the bounds, the '
                    'kind and discovered_at_utc are what a superseded backtest is traced '
                    'through. Insert a narrower gap instead of moving this one'
                    USING ERRCODE = 'restrict_violation';
            END IF;

            IF OLD.resolution IS NOT NULL
               AND ROW(NEW.resolution, NEW.resolved_at_utc)
                   IS DISTINCT FROM ROW(OLD.resolution, OLD.resolved_at_utc)
            THEN
                RAISE EXCEPTION
                    'gap [%, %) is already resolved as %; a resolution is written once, '
                    'and re-opening one would hide that the range was believed complete',
                    OLD.gap_start_utc, OLD.gap_end_utc, OLD.resolution
                    USING ERRCODE = 'restrict_violation';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER coverage_gap_resolution_only
            BEFORE UPDATE ON coverage_gap
            FOR EACH ROW EXECUTE FUNCTION coverage_gap_resolution_is_the_only_rewrite()
        """
    )

    op.execute(
        """
        CREATE FUNCTION coverage_gap_is_never_deleted() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION
                'coverage_gap rows are never deleted: a gap that has been filled is '
                'marked resolved, because the range was still incomplete for every '
                'backtest that ran before it was'
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER coverage_gap_no_delete
            BEFORE DELETE ON coverage_gap
            FOR EACH ROW EXECUTE FUNCTION coverage_gap_is_never_deleted()
        """
    )
    # The grant is the control and the trigger is the backstop, in that order: TRUNCATE
    # fires no row trigger at all, so only the revoke reaches it.
    op.execute(f"REVOKE DELETE, TRUNCATE ON coverage_gap FROM {_INGEST}")

    # `CREATE OR REPLACE` keeps the column list, the owner and the grants; only the
    # `gapped` CTE changes, to count what the corpus still does not hold rather than
    # everything it has ever been missing.
    op.execute(
        """
        CREATE OR REPLACE VIEW data_coverage AS
        WITH partitioned AS (
            SELECT market, dataset, symbol, bar_interval,
                   min(first_event_time_utc) AS first_event_time_utc,
                   max(last_event_time_utc)  AS last_event_time_utc,
                   min(covered_from_date)    AS covered_from_date,
                   max(covered_through_date) AS covered_through_date,
                   sum(rows_out)             AS row_count,
                   count(*)                  AS partition_count,
                   sum(absent_archive_count) AS absent_archive_count
              FROM ingest_partition
             GROUP BY market, dataset, symbol, bar_interval
        ), gapped AS (
            SELECT market, dataset, symbol, bar_interval,
                   count(*)                            AS gap_count,
                   sum(gap_end_utc - gap_start_utc)    AS total_gapped_duration,
                   sum(coalesce(missing_bar_count, 0)) AS missing_bar_count,
                   min(discovered_at_utc)              AS first_gap_discovered_at_utc,
                   max(discovered_at_utc)              AS last_gap_discovered_at_utc
              FROM coverage_gap
             WHERE resolved_at_utc IS NULL
             GROUP BY market, dataset, symbol, bar_interval
        )
        SELECT p.market,
               p.dataset,
               p.symbol,
               p.bar_interval,
               p.first_event_time_utc,
               p.last_event_time_utc,
               p.covered_from_date,
               p.covered_through_date,
               p.row_count,
               p.partition_count,
               p.absent_archive_count,
               coalesce(g.gap_count, 0)                       AS gap_count,
               coalesce(g.total_gapped_duration, INTERVAL '0') AS total_gapped_duration,
               coalesce(g.missing_bar_count, 0)                AS missing_bar_count,
               g.first_gap_discovered_at_utc,
               g.last_gap_discovered_at_utc
          FROM partitioned p
          LEFT JOIN gapped g
                 ON  g.market       = p.market
                 AND g.dataset      = p.dataset
                 AND g.symbol       = p.symbol
                 AND g.bar_interval = p.bar_interval
        """
    )
    op.execute(f"GRANT SELECT ON data_coverage TO {_APP}")


def downgrade() -> None:
    resolved = (
        op.get_bind()
        .exec_driver_sql("SELECT count(*) FROM coverage_gap WHERE resolved_at_utc IS NOT NULL")
        .scalar_one()
    )
    if resolved:
        raise RuntimeError(
            f"0012 refuses to downgrade with {resolved} resolved gap(s) on record: "
            f"dropping resolved_at_utc reopens every one of them and discards the only "
            f"evidence that a backfill ran. Roll forward instead."
        )

    op.execute("DROP TRIGGER coverage_gap_no_delete ON coverage_gap")
    op.execute("DROP FUNCTION coverage_gap_is_never_deleted()")
    op.execute("DROP TRIGGER coverage_gap_resolution_only ON coverage_gap")
    op.execute("DROP FUNCTION coverage_gap_resolution_is_the_only_rewrite()")
    op.execute(f"GRANT DELETE ON coverage_gap TO {_INGEST}")

    # The view first, then the columns. `data_coverage` reads `resolved_at_utc`, so
    # dropping the column while the old definition is still installed fails with
    # `DependentObjectsStillExistError` -- and reaching for `DROP ... CASCADE` there would
    # drop the view and silently leave `fking_app` with no coverage report at all.
    op.execute(
        """
        CREATE OR REPLACE VIEW data_coverage AS
        WITH partitioned AS (
            SELECT market, dataset, symbol, bar_interval,
                   min(first_event_time_utc) AS first_event_time_utc,
                   max(last_event_time_utc)  AS last_event_time_utc,
                   min(covered_from_date)    AS covered_from_date,
                   max(covered_through_date) AS covered_through_date,
                   sum(rows_out)             AS row_count,
                   count(*)                  AS partition_count,
                   sum(absent_archive_count) AS absent_archive_count
              FROM ingest_partition
             GROUP BY market, dataset, symbol, bar_interval
        ), gapped AS (
            SELECT market, dataset, symbol, bar_interval,
                   count(*)                            AS gap_count,
                   sum(gap_end_utc - gap_start_utc)    AS total_gapped_duration,
                   sum(coalesce(missing_bar_count, 0)) AS missing_bar_count,
                   min(discovered_at_utc)              AS first_gap_discovered_at_utc,
                   max(discovered_at_utc)              AS last_gap_discovered_at_utc
              FROM coverage_gap
             GROUP BY market, dataset, symbol, bar_interval
        )
        SELECT p.market,
               p.dataset,
               p.symbol,
               p.bar_interval,
               p.first_event_time_utc,
               p.last_event_time_utc,
               p.covered_from_date,
               p.covered_through_date,
               p.row_count,
               p.partition_count,
               p.absent_archive_count,
               coalesce(g.gap_count, 0)                       AS gap_count,
               coalesce(g.total_gapped_duration, INTERVAL '0') AS total_gapped_duration,
               coalesce(g.missing_bar_count, 0)                AS missing_bar_count,
               g.first_gap_discovered_at_utc,
               g.last_gap_discovered_at_utc
          FROM partitioned p
          LEFT JOIN gapped g
                 ON  g.market       = p.market
                 AND g.dataset      = p.dataset
                 AND g.symbol       = p.symbol
                 AND g.bar_interval = p.bar_interval
        """
    )

    op.execute("DROP INDEX ix_coverage_gap_unresolved")
    op.execute(
        "ALTER TABLE coverage_gap DROP CONSTRAINT ck_coverage_gap_resolution_pairs_with_instant"
    )
    op.execute("ALTER TABLE coverage_gap DROP CONSTRAINT ck_coverage_gap_resolution_is_known")
    op.execute("ALTER TABLE coverage_gap DROP COLUMN resolution")
    op.execute("ALTER TABLE coverage_gap DROP COLUMN resolved_at_utc")

    op.execute(f"ALTER TABLE bar DROP CONSTRAINT {_BAR_SOURCE_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE bar ADD CONSTRAINT {_BAR_SOURCE_CONSTRAINT} "
        f"CHECK (source IN ('archive', 'stream'))"
    )
