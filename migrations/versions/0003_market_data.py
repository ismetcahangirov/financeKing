"""Market-data hypertables: bar and funding_rate.

Revision ID: 0003_market_data
Revises: 0002_audit_substrate

Reversible. These rows are re-derivable from the checksum-verified public archive, which
is the property that makes dropping them a slow inconvenience rather than a loss.

**Compression after 30 days, retention unlimited.** Compression keeps years of 1-minute
data tractable on one laptop; retention is deliberately absent because historical depth
is the asset this system's entire validation methodology rests on. A retention policy on
`bar` would silently shorten every walk-forward window a year from now, and the symptom
would be a strategy that stopped validating for no visible reason.

Compression is applied here and **nowhere near an audit table**: compression rewrites
chunks, which is mutation of append-only data under a different name
(`DATA_PIPELINE.md` section 6). `test_no_audit_table_is_compressed` is what keeps
that from being merely stated.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_market_data"
down_revision: str | None = "0002_audit_substrate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# One day per chunk. The live loop asks for "the last 500 bars for this symbol as of
# this instant" constantly, and a chunk that spans a week makes every one of those reads
# touch a week of every other symbol. Matches DataSettings.hypertable_chunk_interval.
_CHUNK_INTERVAL: str = "INTERVAL '1 day'"

# 30 days. Recent data is read by the live loop and must stay uncompressed; anything
# older is read by backfills and audits, which tolerate decompression latency.
_COMPRESS_AFTER: str = "INTERVAL '30 days'"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE bar (
            instrument_id         UUID            NOT NULL,
            timeframe             TEXT            NOT NULL,
            open_time_utc         TIMESTAMPTZ     NOT NULL,
            close_time_utc        TIMESTAMPTZ     NOT NULL,
            open_quote_price      NUMERIC(38, 18) NOT NULL,
            high_quote_price      NUMERIC(38, 18) NOT NULL,
            low_quote_price       NUMERIC(38, 18) NOT NULL,
            close_quote_price     NUMERIC(38, 18) NOT NULL,
            base_volume           NUMERIC(38, 18) NOT NULL,
            quote_volume          NUMERIC(38, 18) NOT NULL,
            taker_buy_base_volume NUMERIC(38, 18) NOT NULL,
            taker_buy_quote_volume NUMERIC(38, 18) NOT NULL,
            trade_count           BIGINT          NOT NULL,
            source                TEXT            NOT NULL,
            recorded_at_utc       TIMESTAMPTZ     DEFAULT clock_timestamp() NOT NULL,
            -- The duplicate-bar defect is the single most common data-pipeline failure
            -- and it does not announce itself: it skews every backtest touching the
            -- affected range. This primary key is what makes it impossible, and it
            -- leads with the partitioning column so TimescaleDB accepts it.
            CONSTRAINT pk_bar PRIMARY KEY (instrument_id, timeframe, open_time_utc),
            CONSTRAINT fk_bar_instrument_id_instrument
                FOREIGN KEY (instrument_id) REFERENCES instrument (instrument_id),
            CONSTRAINT ck_bar_source_is_known CHECK (source IN ('archive', 'stream')),
            CONSTRAINT ck_bar_bar_is_ordered CHECK (close_time_utc > open_time_utc),
            CONSTRAINT ck_bar_open_quote_price_is_positive CHECK (open_quote_price > 0),
            CONSTRAINT ck_bar_high_quote_price_is_positive CHECK (high_quote_price > 0),
            CONSTRAINT ck_bar_low_quote_price_is_positive CHECK (low_quote_price > 0),
            CONSTRAINT ck_bar_close_quote_price_is_positive CHECK (close_quote_price > 0),
            CONSTRAINT ck_bar_base_volume_is_not_negative CHECK (base_volume >= 0),
            CONSTRAINT ck_bar_trade_count_is_not_negative CHECK (trade_count >= 0),
            -- The cheapest data-quality gate in the system, and it fires on real
            -- corruption: a mis-keyed epoch unit puts a 2026 bar next to a 1970 one and
            -- the merge that follows produces a high below its own open.
            CONSTRAINT ck_bar_ohlc_brackets_open_and_close
                CHECK (high_quote_price >= GREATEST(open_quote_price, close_quote_price)
                       AND low_quote_price <= LEAST(open_quote_price, close_quote_price))
        )
        """
    )

    op.execute(
        """
        CREATE TABLE funding_rate (
            instrument_id         UUID            NOT NULL,
            funding_time_utc      TIMESTAMPTZ     NOT NULL,
            funding_rate_fraction NUMERIC(38, 18) NOT NULL,
            mark_quote_price      NUMERIC(38, 18) NOT NULL,
            recorded_at_utc       TIMESTAMPTZ     DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_funding_rate PRIMARY KEY (instrument_id, funding_time_utc),
            CONSTRAINT fk_funding_rate_instrument_id_instrument
                FOREIGN KEY (instrument_id) REFERENCES instrument (instrument_id),
            CONSTRAINT ck_funding_rate_mark_quote_price_is_positive CHECK (mark_quote_price > 0)
        )
        """
    )

    # `by_range` rather than the bare column name: the two-argument form is deprecated
    # and its replacement has been available since TimescaleDB 2.13, which is older than
    # the image digest pinned in docker-compose.yml.
    for table, column in (("bar", "open_time_utc"), ("funding_rate", "funding_time_utc")):
        op.execute(f"SELECT create_hypertable('{table}', by_range('{column}', {_CHUNK_INTERVAL}))")

    op.execute(
        """
        ALTER TABLE bar SET (
            timescaledb.compress,
            -- Segment by the columns every query filters on, so a filtered read
            -- decompresses one segment rather than the whole chunk.
            timescaledb.compress_segmentby = 'instrument_id, timeframe',
            timescaledb.compress_orderby = 'open_time_utc DESC'
        )
        """
    )
    op.execute(
        """
        ALTER TABLE funding_rate SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'instrument_id',
            timescaledb.compress_orderby = 'funding_time_utc DESC'
        )
        """
    )
    for table in ("bar", "funding_rate"):
        op.execute(f"SELECT add_compression_policy('{table}', {_COMPRESS_AFTER})")

    # The ingest/application split: market data is written by the ingester and read by
    # everything else. An application role that can INSERT bars is an application role
    # that can write a bar the archive never contained, and a look-ahead defect then has
    # a second possible origin. #106 owns the rest of the matrix.
    for table in ("bar", "funding_rate"):
        op.execute(f"GRANT SELECT ON {table} TO fking_app")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO fking_ingest")


def downgrade() -> None:
    # Dropping a hypertable removes its chunks, its compression policy and its
    # background jobs with it; removing the policy first is not required and adds a
    # failure mode if the job has already been removed by hand.
    op.execute("DROP TABLE IF EXISTS funding_rate")
    op.execute("DROP TABLE IF EXISTS bar")
