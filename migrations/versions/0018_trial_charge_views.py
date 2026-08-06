"""The charge arithmetic: `max(declared, executed)` per specification, summed globally.

Revision ID: 0018_trial_charge_views
Revises: 0017_trial_execution

Reversible, and the split from `0017` is the point rather than an accident of file size.
`0017` holds the *record* -- one append-only, hash-chained row per configuration run,
which cannot be reconstructed once dropped. Everything here is a *projection* of those
rows and of `trial_ledger`, so dropping it loses query surface and no history, and
`alembic downgrade -1` stays a usable operator action instead of a wall. `0015`/`0016`
made the same split for the same reason.

**`trial_charge` is the only place `max(declared, executed)` is written.** Not in the
optimizer, not in the backtest engine, not in a reporting query. Two implementations of
a charge rule drift, and the way you find out is a deflated Sharpe that disagrees with
itself depending on which surface computed it -- after which the smaller number is the
one somebody quotes.

**`global_trial_count` is redefined, not replaced.** `0002` defined it as
`max(cumulative_trials)`, which was exactly right while a declaration was the only thing
that could be charged: `cumulative_trials` is the database-computed running total of
declared grids and is monotone by trigger. It now understates, because a specification
that ran past its declared grid is charged its execution count. The view keeps its name,
its single `n bigint` column and its grants -- every caller reads the same thing from the
same place and gets a number that is now correct rather than optimistic.

Both figures remain monotone. `trials_charged` never changes after insert (the row is
append-only), executions are only ever appended, and `max` of a constant and a
non-decreasing count is non-decreasing. There is no code path, and no configuration, by
which the global count can fall.

**Zero means unreadable.** `COALESCE(..., 0)` returns zero for an empty ledger, which is
indistinguishable from a ledger the reader could not reach -- so the *reader* refuses
zero rather than deflating against it (`fking.evolution.trials.TrialLedger
.global_trial_count`). A benchmark of zero reports a searched result as an unsearched
one, which is the failure this whole subsystem exists to prevent.

`.claude/rules/overfitting-defences.md` clause 3.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018_trial_charge_views"
down_revision: str | None = "0017_trial_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP: str = "fking_app"
_INGEST: str = "fking_ingest"
_MIGRATOR: str = "fking_migrator"

# The two definitions of `global_trial_count`, kept adjacent so they are read together.
# The first is 0002's, correct for a ledger whose only charge is a declaration and
# understating for one that also records executions; downgrade() restores it verbatim.
_DECLARATIONS_ONLY: str = (
    "SELECT COALESCE(max(cumulative_trials), 0)::bigint AS n FROM trial_ledger"
)
_GLOBAL_FROM_CHARGES: str = (
    "SELECT COALESCE(sum(charged_trial_count), 0)::bigint AS n FROM trial_charge"
)


def upgrade() -> None:
    op.execute(
        """
        CREATE VIEW trial_charge AS
        SELECT l.spec_hash,
               l.correlation_id,
               l.registered_by,
               l.charged_at_utc,
               l.trials_charged::bigint            AS declared_trial_count,
               count(x.seq)::bigint                AS executed_trial_count,
               -- The charge. max() and not a sum: summing double-charges the honest
               -- case of declaring 200 and running 200, and a defence that punishes
               -- correct behaviour gets routed around.
               GREATEST(l.trials_charged::bigint, count(x.seq))::bigint
                                                   AS charged_trial_count
          FROM trial_ledger l
          -- LEFT, so a declared-but-never-run grid still appears and is still charged
          -- its full declaration. An inner join would price abandonment at zero, which
          -- is the evasion the declared charge exists to close.
          LEFT JOIN trial_execution x ON x.spec_hash = l.spec_hash
         -- seq is trial_ledger's primary key, so every other column of l is
         -- functionally dependent on it and needs no repetition here.
         GROUP BY l.seq
        """
    )

    op.execute(f"CREATE OR REPLACE VIEW global_trial_count AS {_GLOBAL_FROM_CHARGES}")

    op.execute(f"ALTER VIEW trial_charge OWNER TO {_MIGRATOR}")
    op.execute("REVOKE ALL ON trial_charge FROM PUBLIC")
    op.execute(f"REVOKE ALL ON trial_charge FROM {_INGEST}")
    op.execute(f"GRANT SELECT ON trial_charge TO {_APP}")


def downgrade() -> None:
    # Order matters: global_trial_count depends on trial_charge, so the dependency has to
    # be broken before the view it points at can be dropped.
    op.execute(f"CREATE OR REPLACE VIEW global_trial_count AS {_DECLARATIONS_ONLY}")
    op.execute("DROP VIEW IF EXISTS trial_charge")
