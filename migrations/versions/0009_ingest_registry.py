"""The ingestion registry the bulk backfill resumes from and `backtest` reads.

Revision ID: 0009_ingest_registry
Revises: 0008_least_privilege

Reversible. Nothing here is an audit trail: every row is re-derivable by re-running the
backfill against the same checksum-verified archives, so dropping these tables costs
hours of downloading rather than a record of a decision. That is the line
`docs/rules/append-only-audit.md` draws, and these tables sit on the recoverable side
of it -- `coverage_gap.discovered_at_utc` is the one column that does not survive a
rebuild, and it is a diagnostic aid rather than evidence a trade is reconstructed from.

Three tables at two grains, because two different questions are being asked:

- **`ingest_file`** answers "what did we read, and what did it reject" -- one row per
  archive, carrying the `NormalizationResult` per reason. `DATA_PIPELINE.md` section 4:
  a run that reports only `rows_out` has hidden its rejections.
- **`ingest_partition`** answers "what does the corpus hold" -- one row per Parquet file,
  carrying the writer's content digest. Thirty daily archives feeding one monthly bar
  file produce thirty rows in the first table and one here.
- **`coverage_gap`** answers "what does it not hold, and since when".

The grain split is not tidiness. Resume compares `ingest_partition.content_digest_hex`
against the digest in the Parquet footer, so the registry and the corpus can be shown to
agree; a per-archive digest could not be compared against anything, because no single
archive corresponds to a file on disk.

**`bar_interval`, not `interval`.** `INTERVAL` is a reserved word: `SELECT interval FROM
ingest_file` is a syntax error, and a column that can only be read quoted is a column the
first ad-hoc query gets wrong. The empty string means "this dataset is not keyed by an
interval", tied to the dataset by a `CHECK` rather than by convention, because these are
primary-key components and a primary key cannot hold NULL.

**No `UNIQUE` on gap bounds beyond the primary key, and gaps are never merged.** Two
adjacent absent days produce two rows rather than one merged span. Merging would mean
rewriting a row whose `discovered_at_utc` is the only thing that can tell you which
completed backtests consumed a range before the gap in it was known
(`DATA_PIPELINE.md` section 11).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_ingest_registry"
down_revision: str | None = "0008_least_privilege"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP: str = "fking_app"
_INGEST: str = "fking_ingest"
_MIGRATOR: str = "fking_migrator"

_TABLES: tuple[str, ...] = ("ingest_partition", "ingest_file", "coverage_gap")
_COVERAGE_VIEW: str = "data_coverage"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ingest_partition (
            market               TEXT        NOT NULL,
            dataset              TEXT        NOT NULL,
            symbol               TEXT        NOT NULL,
            bar_interval         TEXT        NOT NULL,
            period_start_date    DATE        NOT NULL,
            partition_grain      TEXT        NOT NULL,
            covered_from_date    DATE        NOT NULL,
            covered_through_date DATE        NOT NULL,
            archive_count        INTEGER     NOT NULL,
            absent_archive_count INTEGER     NOT NULL,
            rows_in              BIGINT      NOT NULL,
            rows_out             BIGINT      NOT NULL,
            rows_rejected        BIGINT      NOT NULL,
            first_event_time_utc TIMESTAMPTZ NOT NULL,
            last_event_time_utc  TIMESTAMPTZ NOT NULL,
            content_digest_hex   TEXT        NOT NULL,
            parquet_path         TEXT        NOT NULL,
            written_at_utc       TIMESTAMPTZ NOT NULL,
            recorded_at_utc      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT pk_ingest_partition
                PRIMARY KEY (market, dataset, symbol, bar_interval, period_start_date),
            CONSTRAINT ck_ingest_partition_market_is_known
                CHECK (market IN ('spot', 'futures_um')),
            CONSTRAINT ck_ingest_partition_partition_grain_is_known
                CHECK (partition_grain IN ('daily', 'monthly')),
            CONSTRAINT ck_ingest_partition_bar_interval_matches_dataset
                CHECK ((dataset = 'klines') = (bar_interval <> '')),
            CONSTRAINT ck_ingest_partition_coverage_is_ordered
                CHECK (covered_through_date >= covered_from_date),
            CONSTRAINT ck_ingest_partition_events_are_ordered
                CHECK (last_event_time_utc >= first_event_time_utc),
            -- A zero-row partition has no Parquet file. A row here claiming one would
            -- make a scan report "we have this period" over a path that does not exist,
            -- which is the same lie an empty Parquet file tells and is why the writer
            -- refuses to produce one.
            CONSTRAINT ck_ingest_partition_rows_out_is_positive CHECK (rows_out > 0),
            CONSTRAINT ck_ingest_partition_rows_balance
                CHECK (rows_in = rows_out + rows_rejected),
            CONSTRAINT ck_ingest_partition_archive_count_is_positive CHECK (archive_count > 0),
            CONSTRAINT ck_ingest_partition_absent_archive_count_is_not_negative
                CHECK (absent_archive_count >= 0)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE ingest_file (
            market               TEXT        NOT NULL,
            dataset              TEXT        NOT NULL,
            symbol               TEXT        NOT NULL,
            bar_interval         TEXT        NOT NULL,
            archive_date         DATE        NOT NULL,
            granularity          TEXT        NOT NULL,
            period_start_date    DATE        NOT NULL,
            source_checksum_hex  TEXT        NOT NULL,
            rows_in              BIGINT      NOT NULL,
            rows_out             BIGINT      NOT NULL,
            rows_rejected        BIGINT      NOT NULL,
            rejection_reasons    JSONB       NOT NULL,
            epoch_unit_applied   TEXT        NOT NULL,
            first_event_time_utc TIMESTAMPTZ,
            last_event_time_utc  TIMESTAMPTZ,
            ingested_at_utc      TIMESTAMPTZ NOT NULL,
            recorded_at_utc      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            -- granularity is in the key because the same month is legitimately read
            -- twice: from thirty daily archives before the monthly file was published,
            -- and from the monthly archive afterwards. Both readings happened.
            CONSTRAINT pk_ingest_file PRIMARY KEY
                (market, dataset, symbol, bar_interval, archive_date, granularity),
            CONSTRAINT ck_ingest_file_market_is_known
                CHECK (market IN ('spot', 'futures_um')),
            CONSTRAINT ck_ingest_file_granularity_is_known
                CHECK (granularity IN ('daily', 'monthly')),
            CONSTRAINT ck_ingest_file_bar_interval_matches_dataset
                CHECK ((dataset = 'klines') = (bar_interval <> '')),
            CONSTRAINT ck_ingest_file_rows_balance CHECK (rows_in = rows_out + rows_rejected),
            CONSTRAINT ck_ingest_file_rows_out_is_not_negative CHECK (rows_out >= 0)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE coverage_gap (
            market            TEXT        NOT NULL,
            dataset           TEXT        NOT NULL,
            symbol            TEXT        NOT NULL,
            bar_interval      TEXT        NOT NULL,
            -- Half-open [start, end) over event time, naming the missing region itself
            -- rather than the observations bracketing it. That is what makes
            -- sum(gap_end_utc - gap_start_utc) a truthful total gapped duration.
            gap_start_utc     TIMESTAMPTZ NOT NULL,
            gap_end_utc       TIMESTAMPTZ NOT NULL,
            gap_kind          TEXT        NOT NULL,
            missing_bar_count BIGINT,
            discovered_at_utc TIMESTAMPTZ NOT NULL,
            recorded_at_utc   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT pk_coverage_gap PRIMARY KEY
                (market, dataset, symbol, bar_interval, gap_start_utc, gap_end_utc),
            CONSTRAINT ck_coverage_gap_market_is_known
                CHECK (market IN ('spot', 'futures_um')),
            CONSTRAINT ck_coverage_gap_gap_kind_is_known
                CHECK (gap_kind IN ('cadence', 'seam', 'absent_archive')),
            CONSTRAINT ck_coverage_gap_bar_interval_matches_dataset
                CHECK ((dataset = 'klines') = (bar_interval <> '')),
            CONSTRAINT ck_coverage_gap_gap_is_forward CHECK (gap_end_utc > gap_start_utc),
            -- NULL where the dataset has no cadence. A trades archive that was never
            -- published says nothing about how many prints are missing, and a zero there
            -- would read as "none" -- a stronger claim than the evidence supports.
            CONSTRAINT ck_coverage_gap_missing_bar_count_is_positive
                CHECK (missing_bar_count IS NULL OR missing_bar_count > 0)
        )
        """
    )
    op.execute("CREATE INDEX ix_coverage_gap_discovered_at_utc ON coverage_gap (discovered_at_utc)")

    # The coverage report `DATA_PIPELINE.md` section 10 requires: first timestamp, last
    # timestamp, gap count and total gapped duration per (market, symbol, dataset).
    #
    # LEFT JOIN, not INNER: a series with no gaps must appear with `gap_count = 0`, and an
    # inner join would drop exactly the series a reader is hoping to see. The COALESCE on
    # the interval matters for the same reason -- a NULL total gapped duration renders as
    # blank, which reads as "unknown" rather than as "none".
    op.execute(
        """
        CREATE VIEW data_coverage AS
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
                   count(*)                          AS gap_count,
                   sum(gap_end_utc - gap_start_utc)  AS total_gapped_duration,
                   sum(coalesce(missing_bar_count, 0)) AS missing_bar_count,
                   min(discovered_at_utc)            AS first_gap_discovered_at_utc,
                   max(discovered_at_utc)            AS last_gap_discovered_at_utc
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
               coalesce(g.gap_count, 0)                             AS gap_count,
               coalesce(g.total_gapped_duration, INTERVAL '0')       AS total_gapped_duration,
               coalesce(g.missing_bar_count, 0)                      AS missing_bar_count,
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

    # Ownership before grants, matching 0008: a view's access to its underlying tables is
    # checked against the view's owner, so `data_coverage` is readable by fking_app while
    # the three tables it reads are not writable by it.
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} OWNER TO {_MIGRATOR}")
    op.execute(f"ALTER VIEW {_COVERAGE_VIEW} OWNER TO {_MIGRATOR}")

    for table in _TABLES:
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON {table} FROM {_APP}")
        op.execute(f"REVOKE ALL ON {table} FROM {_INGEST}")
        # INGEST_OWNED, the same class as `bar`: written by the ingester, readable by
        # everything else. An application role that could write a coverage row could
        # declare a gap closed that the corpus does not hold, and a backtest reading that
        # row would narrow no window and refuse no run.
        op.execute(f"GRANT SELECT ON {table} TO {_APP}")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {_INGEST}")

    op.execute(f"REVOKE ALL ON {_COVERAGE_VIEW} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON {_COVERAGE_VIEW} FROM {_INGEST}")
    op.execute(f"GRANT SELECT ON {_COVERAGE_VIEW} TO {_APP}")


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {_COVERAGE_VIEW}")
    for table in _TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")
