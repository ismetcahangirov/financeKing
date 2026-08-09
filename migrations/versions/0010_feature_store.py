"""The feature store: as-of reads the application role cannot go around.

Revision ID: 0010_feature_store
Revises: 0009_ingest_registry

Reversible. Every row here is recomputable from the Parquet corpus by re-running the
feature's `compute`, so dropping the table costs CPU rather than evidence -- the same
line `0009_ingest_registry` sits on.

**`fking_app` holds no privilege at all on `feature_values`.** Not `SELECT`. The role
every strategy, backtest and risk process connects as reaches feature data only through
`fking_feature_as_of()`, which is `SECURITY DEFINER` and takes an `as_of` it cannot be
asked to ignore. That is what turns the most dangerous defect class in this project into
`permission denied for table feature_values` instead of a review miss
(`docs/rules/no-lookahead.md`, `DATA_PIPELINE.md` section 7).

**`available_at_utc`, never `event_time_utc`, governs visibility.** `event_time_utc` is
when the thing happened; `available_at_utc` is the earliest instant this system could
have known it. `WHERE event_time <= :t` is the single most common spelling of look-ahead
and it looks completely correct, so the filter is not left to a caller: it is inside a
function the caller cannot rewrite.

**Revisions are appended, never updated.** A corrected value is a new row with a later
`available_at_utc`; both rows survive, and `DISTINCT ON (event_time_utc) ... ORDER BY
available_at_utc DESC` returns the value as it was *believed* at `as_of`. Backfilling a
correction over the original would make every historical backtest a test of a belief
nobody held at the time.

**A definition change is a new `feature_version`, not an overwrite.** The version is part
of the primary key, so values computed under the old definition remain and stay
attributable to it.

Two details that are easy to get wrong and expensive to discover:

- The function is named `fking_feature_as_of` rather than `feature_as_of` because
  `0008_least_privilege` finds functions by the `fking_` prefix when it assigns
  ownership. A function outside that prefix is a function the next ownership sweep
  silently skips.
- `SET search_path` on a `SECURITY DEFINER` function is not decoration. Without it the
  caller's `search_path` decides which `feature_values` the body reads, and a caller who
  can create a table in a schema earlier on that path chooses the answer.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_feature_store"
down_revision: str | None = "0009_ingest_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP: str = "fking_app"
_INGEST: str = "fking_ingest"
_MIGRATOR: str = "fking_migrator"

_TABLE: str = "feature_values"
_READER: str = "fking_feature_as_of(text, integer, text, text, timestamptz, interval)"

# One day per chunk, matching `bar`: the read this table exists to serve is "this
# feature, this symbol, this lookback, as of this instant", which is a narrow time range
# over one series.
_CHUNK_INTERVAL: str = "INTERVAL '1 day'"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE feature_values (
            feature_name     TEXT            NOT NULL,
            feature_version  INTEGER         NOT NULL,
            market           TEXT            NOT NULL,
            symbol           TEXT            NOT NULL,
            event_time_utc   TIMESTAMPTZ     NOT NULL,
            available_at_utc TIMESTAMPTZ     NOT NULL,
            feature_value    NUMERIC(38, 18) NOT NULL,
            recorded_at_utc  TIMESTAMPTZ     DEFAULT clock_timestamp() NOT NULL,
            -- available_at_utc is in the key, so a revision is a second row rather than
            -- an UPDATE of the first. Without it the only way to record a correction
            -- would be to destroy the value the system actually acted on.
            CONSTRAINT pk_feature_values PRIMARY KEY (
                feature_name, feature_version, market, symbol,
                event_time_utc, available_at_utc
            ),
            CONSTRAINT ck_feature_values_market_is_known
                CHECK (market IN ('spot', 'futures_um')),
            CONSTRAINT ck_feature_values_feature_version_is_positive
                CHECK (feature_version >= 1),
            -- The invariant the whole read path rests on: nothing can be known before it
            -- happened. It is also what makes an event_time bound unnecessary inside
            -- fking_feature_as_of -- see the comment there.
            CONSTRAINT ck_feature_values_availability_follows_event
                CHECK (available_at_utc >= event_time_utc)
        )
        """
    )

    op.execute(
        f"SELECT create_hypertable('{_TABLE}', by_range('event_time_utc', {_CHUNK_INTERVAL}))"
    )

    # No compression policy, unlike `bar`. A revision arrives with a later
    # `available_at_utc` and an `event_time_utc` that may be months old, so this table
    # takes inserts into chunks that have long stopped being recent -- which is exactly
    # the shape a compression policy handles worst. `bar` never receives a late row for a
    # closed minute; this table is designed to.

    op.execute(
        """
        CREATE FUNCTION fking_feature_as_of(
            p_feature_name    text,
            p_feature_version integer,
            p_market          text,
            p_symbol          text,
            p_as_of           timestamptz,
            p_lookback        interval
        ) RETURNS TABLE (event_time_utc timestamptz, feature_value numeric)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT DISTINCT ON (fv.event_time_utc) fv.event_time_utc, fv.feature_value
              FROM public.feature_values fv
             WHERE fv.feature_name    = p_feature_name
               AND fv.feature_version = p_feature_version
               AND fv.market          = p_market
               AND fv.symbol          = p_symbol
               -- The whole point. There is no parameter that relaxes this, and no
               -- overload that omits it.
               AND fv.available_at_utc <= p_as_of
               AND fv.event_time_utc   >  p_as_of - p_lookback
             -- Latest revision that had been published by p_as_of, not the latest
             -- revision that exists.
             ORDER BY fv.event_time_utc, fv.available_at_utc DESC;
        $$
        """
    )
    # There is deliberately no `AND fv.event_time_utc <= p_as_of` above. The CHECK
    # constraint makes `event_time_utc <= available_at_utc <= p_as_of` hold for every row
    # the WHERE clause already admits, so the extra predicate would be unreachable -- and
    # an unreachable guard reads as the load-bearing one, which is how the real filter
    # gets "simplified" away later.

    op.execute(f"ALTER TABLE {_TABLE} OWNER TO {_MIGRATOR}")
    # The definer's rights are what the function body runs with, so this line is the
    # grant. fking_migrator owns the table; the body is fixed SQL over one table, so
    # ownership here buys a read and nothing else.
    op.execute(f"ALTER FUNCTION {_READER} OWNER TO {_MIGRATOR}")

    op.execute(f"REVOKE ALL ON {_TABLE} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON {_TABLE} FROM {_APP}")
    op.execute(f"REVOKE ALL ON {_TABLE} FROM {_INGEST}")
    # SELECT and INSERT for the writer, nothing more: a feature value that can be
    # UPDATEd is a feature value whose history can be rewritten to match a backtest.
    op.execute(f"GRANT SELECT, INSERT ON {_TABLE} TO {_INGEST}")

    # EXECUTE on a function is granted to PUBLIC by default, and PUBLIC is every role
    # this cluster will ever have.
    op.execute(f"REVOKE ALL ON FUNCTION {_READER} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_READER} TO {_APP}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {_READER}")
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
