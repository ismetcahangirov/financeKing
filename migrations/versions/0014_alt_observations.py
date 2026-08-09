"""Raw alternative series, read only through an as-of function that cannot be relaxed.

Revision ID: 0014_alt_observations
Revises: 0013_scheduler_job_run

`#32` ingests funding rates, open interest, an index, news and macro releases. Every one
of them has an `event_time` that differs materially from its `available_at`, and the gap
is larger than for market data because a *publisher* chose it: a funding rate settles and
is broadcast, an index is stamped with a day and refreshed at the end of it, Q2 GDP has an
observation period in June and a release at 08:30 on 26 August.

**This table is `0010_feature_store` applied to a different question, and deliberately not
`feature_store` itself.** `feature_values` is keyed by `(market, symbol)` and read through
a function that resolves a `FeatureSpec`, whose computation is typed over bars. A funding
rate is not a bar and a macro release has no symbol. Folding them in would mean widening
the feature contract to admit things that are not features, so they get their own table
with the same guarantee enforced the same way.

**The guarantee is not in this table, it is in the grants.** `fking_app` holds no
privilege on `alt_observations` at all, and reads only through `fking_alt_as_of()`, which
is `SECURITY DEFINER` and takes an `as_of` it cannot be asked to ignore. `WHERE
event_time <= :t` is the single most common spelling of look-ahead and it looks completely
correct, which is why the filter is inside a function the caller cannot rewrite
(`docs/rules/no-lookahead.md`).

**`available_at_utc > event_time_utc`, strictly, unlike `feature_values`.** There, a
feature computed from a bar that has already closed is legitimately knowable at its own
event time, so the constraint is `>=`. Nothing in this table can be: every row here is
something a third party published *after* the instant it stamped, and
`AltSourceSpec.availability_lag` refuses a non-positive declaration for the same reason.
A `>=` here would admit exactly the row the whole contract exists to prevent.

**A revision is a second row.** `available_at_utc` closes the primary key, so the first
print and its restatement coexist and `DISTINCT ON (event_time_utc) ... ORDER BY
available_at_utc DESC` returns the value as it was *believed* at `as_of`. Backfilling a
correction over the original would make every historical backtest a test of a belief
nobody held at the time -- and for a macro series, where revisions are routine and
sometimes large, that is not a corner case.

Two details carried over from `0010` because they are easy to get wrong and expensive to
discover:

- The function is named `fking_alt_as_of` rather than `alt_as_of` because
  `0008_least_privilege` finds functions by the `fking_` prefix when it assigns ownership.
  A function outside that prefix is one the next ownership sweep silently skips.
- `SET search_path` on a `SECURITY DEFINER` function is not decoration. Without it the
  caller's `search_path` decides which `alt_observations` the body reads, and a caller who
  can create a table in a schema earlier on that path chooses the answer.

`downgrade` drops both. That is safe here in a way it is not for the audit substrate:
every row is re-derivable by re-fetching the archive and re-running the declared lag, and
nothing references this table. What is lost is fetch time, not history.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_alt_observations"
down_revision: str | None = "0013_scheduler_job_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP: str = "fking_app"
_INGEST: str = "fking_ingest"
_MIGRATOR: str = "fking_migrator"

_TABLE: str = "alt_observations"
_READER: str = "fking_alt_as_of(text, text, timestamptz, interval)"

# Mirrors `fking.data.alt.registry.ALT_SOURCES` and `schema.ALT_SOURCE_IDS`, written as a
# literal because a migration that imported live application state would stop being a
# record of what it did. `test_schema_contract.py` asserts the three agree.
_SOURCE_IDS: str = (
    "'alternative.me.fearGreed', 'binance.fundingRate', 'binance.openInterest', "
    "'cryptopanic.posts', 'stlouisfed.fredReleases'"
)

# Thirty days, against `feature_values`' one. The read is "this series, this lookback, as
# of this instant" over series whose cadence ranges from five minutes to a quarter, and a
# one-day chunk would put a quarterly macro series in ninety chunks holding one row each.
_CHUNK_INTERVAL: str = "INTERVAL '30 days'"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE {_TABLE} (
            source_id        TEXT            NOT NULL,
            series_id        TEXT            NOT NULL,
            event_time_utc   TIMESTAMPTZ     NOT NULL,
            available_at_utc TIMESTAMPTZ     NOT NULL,
            observed_value   NUMERIC(38, 18) NOT NULL,
            recorded_at_utc  TIMESTAMPTZ     DEFAULT clock_timestamp() NOT NULL,
            -- available_at_utc is in the key, so a revision is a second row rather than
            -- an UPDATE of the first. Without it the only way to record a restatement
            -- would be to destroy the print the system actually acted on.
            CONSTRAINT pk_{_TABLE} PRIMARY KEY (
                source_id, series_id, event_time_utc, available_at_utc
            ),
            CONSTRAINT ck_{_TABLE}_source_id_is_known
                CHECK (source_id IN ({_SOURCE_IDS})),
            -- Strictly greater, not >=. Every row here was published by a third party
            -- after the instant it stamps; a zero lag is the permissive answer and it is
            -- refused at both ends, here and in AltSourceSpec.
            CONSTRAINT ck_{_TABLE}_availability_follows_event
                CHECK (available_at_utc > event_time_utc)
        )
        """
    )

    op.execute(
        f"SELECT create_hypertable('{_TABLE}', by_range('event_time_utc', {_CHUNK_INTERVAL}))"
    )

    # No compression policy, for the same reason `feature_values` has none: a revision
    # arrives with a later `available_at_utc` and an `event_time_utc` that may be months
    # old, so this table takes inserts into chunks that stopped being recent long ago,
    # which is the shape a compression policy handles worst.

    op.execute(
        f"""
        CREATE FUNCTION fking_alt_as_of(
            p_source_id text,
            p_series_id text,
            p_as_of     timestamptz,
            p_lookback  interval
        ) RETURNS TABLE (event_time_utc timestamptz, observed_value numeric)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT DISTINCT ON (ao.event_time_utc) ao.event_time_utc, ao.observed_value
              FROM public.{_TABLE} ao
             WHERE ao.source_id = p_source_id
               AND ao.series_id = p_series_id
               -- The whole point. There is no parameter that relaxes this, and no
               -- overload that omits it.
               AND ao.available_at_utc <= p_as_of
               AND ao.event_time_utc   >  p_as_of - p_lookback
             -- Latest revision that had been published by p_as_of, not the latest
             -- revision that exists.
             ORDER BY ao.event_time_utc, ao.available_at_utc DESC;
        $$
        """
    )
    # There is deliberately no `AND ao.event_time_utc <= p_as_of`. The CHECK constraint
    # makes `event_time_utc < available_at_utc <= p_as_of` hold for every row the WHERE
    # clause already admits, so the extra predicate would be unreachable -- and an
    # unreachable guard reads as the load-bearing one, which is how the real filter gets
    # "simplified" away later.

    op.execute(f"ALTER TABLE {_TABLE} OWNER TO {_MIGRATOR}")
    # The definer's rights are what the function body runs with, so this line is the
    # grant. The body is fixed SQL over one table, so ownership here buys a read and
    # nothing else.
    op.execute(f"ALTER FUNCTION {_READER} OWNER TO {_MIGRATOR}")

    op.execute(f"REVOKE ALL ON {_TABLE} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON {_TABLE} FROM {_APP}")
    op.execute(f"REVOKE ALL ON {_TABLE} FROM {_INGEST}")
    # SELECT and INSERT for the writer, nothing more: an observation that can be UPDATEd
    # is an observation whose first print can be rewritten to match a backtest.
    op.execute(f"GRANT SELECT, INSERT ON {_TABLE} TO {_INGEST}")

    # EXECUTE on a function is granted to PUBLIC by default, and PUBLIC is every role this
    # cluster will ever have.
    op.execute(f"REVOKE ALL ON FUNCTION {_READER} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_READER} TO {_APP}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {_READER}")
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
