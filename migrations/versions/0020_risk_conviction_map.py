"""The conviction calibration map, kept as history rather than as current state.

Revision ID: 0020_risk_conviction_map
Revises: 0019_kill_switch_journal_columns

`#49` maps a strategy's reported conviction onto its own realised record before that
number is allowed to influence size. `fking.risk.calibration` holds the fit; this is where
the fitted map is kept so that a restart does not lose it and so that a past decision can
be shown to have been taken on a map that could actually have existed at the time.

**Why history, and not one mutable row per strategy.** The obvious schema is
`(strategy_id) PRIMARY KEY` with an `UPDATE` on each refit, and it is wrong in the one
way that matters here. The map used to size a decision at `t` is a claim about what was
knowable at `t`; an in-place update destroys the evidence for that claim, and the only
symptom is that a later audit cannot distinguish a correct point-in-time fit from one that
read the whole trade record. Issue #49 calls this the look-ahead nobody looks for --
inside the risk engine rather than the feature store, which is why none of the P1 defences
reach it. So the primary key carries `available_at_utc`, the table is append-only, and the
read path is the `as_of` shape `feature_as_of()` already uses: the newest row whose
`available_at_utc` is at or before the decision instant.

**Append-only is enforced twice**, as everywhere else in this schema: the grants are the
primary control because `TRUNCATE` fires no row trigger, and `fking_append_only_guard()`
is the backstop for the migration that later hands a broad role to a new service.

**The table names are shorter than the concept.** `risk_conviction_map` rather than
`risk_conviction_calibration`, and `map_id` rather than `calibration_id`, because
PostgreSQL truncates identifiers at 63 bytes and the generated foreign-key name from the
longer pair is 80. Truncation is silent, and what it leaves behind is a constraint whose
name appears nowhere in this repository -- so an error quoting it is an error nobody can
grep for.

**The buckets are a child table, not a `jsonb` column.** Every bucket carries fractions,
and a fraction inside a blob is invisible to the `information_schema` scan that asserts no
numeric column is `DOUBLE PRECISION` (`docs/rules/decimal-and-money.md`). `NUMERIC(38, 18)`
per bucket is what makes that scan able to see them -- and it is also the resolution
`fking.risk.calibration` quantizes to at fit time, so the map that is written and the map
that is read back are the same map.

`downgrade` drops both tables. That is a real loss: the map is re-derivable from the trade
record, but the *record of which map was used when* is not, so a downgrade converts an
audited point-in-time claim into an assertion. It is still reversible in the sense the
release drill requires -- nothing else references these rows -- so it drops rather than
refusing.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020_risk_conviction_map"
down_revision: str | None = "0019_kill_switch_journal_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP: str = "fking_app"
_INGEST: str = "fking_ingest"
_MIGRATOR: str = "fking_migrator"
_MAP: str = "risk_conviction_map"
_BUCKET: str = "risk_conviction_map_bucket"

# Mirrors the guards in `fking.risk.calibration`. Deliberately redundant with them: the
# type guards the process that is running now, and the constraint guards the row against
# every writer this schema will ever have, including a repair script run by hand during an
# incident. Monotonicity across buckets is not expressible as a row check and is
# re-asserted by `from_calibration_row` on the way back in.
_FRACTIONS_ARE_IN_RANGE: str = (
    "conviction_upper_bound BETWEEN 0 AND 1 AND hit_rate_fraction BETWEEN 0 AND 1 "
    "AND calibrated_fraction BETWEEN 0 AND 1"
)
_RETURNS_ARE_ABOVE_RUIN: str = "mean_return_fraction >= -1 AND fitted_return_fraction >= -1"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE {_MAP} (
            map_id            UUID        NOT NULL,
            strategy_id       TEXT        NOT NULL,
            available_at_utc  TIMESTAMPTZ NOT NULL,
            observation_count INTEGER     NOT NULL,
            recorded_at_utc   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT pk_{_MAP} PRIMARY KEY (map_id),
            -- One fit per strategy per instant. Two rows at the same instant would both
            -- be "the" map at that moment, and the as-of read would return whichever the
            -- planner reached first.
            CONSTRAINT uq_{_MAP}_strategy_id_available_at_utc
                UNIQUE (strategy_id, available_at_utc),
            CONSTRAINT ck_{_MAP}_strategy_id_is_not_blank CHECK (btrim(strategy_id) <> ''),
            CONSTRAINT ck_{_MAP}_observations_are_countable CHECK (observation_count >= 0)
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE {_BUCKET} (
            map_id                 UUID            NOT NULL,
            bucket_index           INTEGER         NOT NULL,
            conviction_upper_bound NUMERIC(38, 18) NOT NULL,
            trade_count            INTEGER         NOT NULL,
            hit_rate_fraction      NUMERIC(38, 18) NOT NULL,
            mean_return_fraction   NUMERIC(38, 18) NOT NULL,
            fitted_return_fraction NUMERIC(38, 18) NOT NULL,
            calibrated_fraction    NUMERIC(38, 18) NOT NULL,
            recorded_at_utc        TIMESTAMPTZ     NOT NULL DEFAULT clock_timestamp(),
            -- Ordinal-keyed, and the order is the map: bucket n's calibrated fraction is
            -- only meaningful as the step above bucket n-1's.
            CONSTRAINT pk_{_BUCKET} PRIMARY KEY (map_id, bucket_index),
            -- CASCADE rather than RESTRICT: buckets without their parent are six numbers
            -- nothing can interpret. The parent is append-only, so the only delete that
            -- can reach here is a migration.
            CONSTRAINT fk_{_BUCKET}_map_id_{_MAP}
                FOREIGN KEY (map_id) REFERENCES {_MAP} (map_id) ON DELETE CASCADE,
            CONSTRAINT ck_{_BUCKET}_bucket_index_is_ordinal CHECK (bucket_index >= 0),
            -- An empty bucket contributes a mean computed from nothing.
            CONSTRAINT ck_{_BUCKET}_trade_count_is_positive CHECK (trade_count > 0),
            CONSTRAINT ck_{_BUCKET}_fractions_are_in_range CHECK ({_FRACTIONS_ARE_IN_RANGE}),
            CONSTRAINT ck_{_BUCKET}_returns_are_above_ruin CHECK ({_RETURNS_ARE_ABOVE_RUIN})
        )
        """
    )

    # APPEND_ONLY in fking.platform.persistence.privileges: INSERT and SELECT, never
    # UPDATE, DELETE or TRUNCATE. Ownership is the group role rather than the login role
    # the migration connected as, so the objects move with the privilege class and not
    # with a credential.
    for table in (_MAP, _BUCKET):
        op.execute(f"ALTER TABLE {table} OWNER TO {_MIGRATOR}")
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON {table} FROM {_APP}")
        op.execute(f"REVOKE ALL ON {table} FROM {_INGEST}")
        op.execute(f"GRANT INSERT, SELECT ON {table} TO {_APP}")
        op.execute(
            f"CREATE TRIGGER {table}_no_update_delete BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION fking_append_only_guard()"
        )


def downgrade() -> None:
    op.execute(f"DROP TABLE {_BUCKET}")
    op.execute(f"DROP TABLE {_MAP}")
