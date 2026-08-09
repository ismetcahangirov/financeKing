"""The columns a kill-switch trip row needs, so the journal can be read back at boot.

Revision ID: 0019_kill_switch_journal_columns
Revises: 0018_trial_ledger_search_context

`0005_execution_and_risk` created `kill_switch_event` with the four fields a log line
needs -- type, reason, actor, correlation id -- and none of the fields a *reconstruction*
needs. #53 then built the derivation that turns those rows into a `KillSwitchState`, and
it keys on things this table cannot store: the incident id a `RESUME` clears, the trigger
that fired with its observed and threshold values, and the book snapshot written before
any remediation ran. Without them a restart cannot answer "which incidents are still
open", which is the whole of #177.

**Every added column is nullable and carries no `DEFAULT`.** That is the one exception in
`docs/rules/append-only-audit.md` and the whole of it. A `DEFAULT` would make rows written
before this migration *report* a trigger they never carried, and a backfill is forbidden
outright even where the value is derivable. `NULL` is the truthful record: we did not
capture it then.

**`resumed` is deliberately not a new event type.** The vocabulary gains `armed` only, and
a resume row keeps the existing spelling `cleared`. Two spellings for one state would be
permanent here in a way they are not elsewhere: the table is append-only, so a historical
`cleared` row could never be rewritten to say `resumed`, and every reader would have to
know both forever. One spelling, chosen by which one already exists.

**The completeness constraints are `NOT VALID`.** They are what makes the new columns
`NOT NULL` from here forward without rewriting -- or even reading -- a single historical
row, which is the same effect `0018` reached through a trigger and reaches here in a form
Postgres enforces directly. A validated constraint would have to pass over rows written
before the columns existed, and those rows are `NULL` by construction; the choice is
between `NOT VALID` and a backfill, and the backfill is the forbidden one.

Two of the constraints are not about completeness and are worth stating separately,
because they move a rule out of the application and into the schema:

- `root_cause` must survive `btrim` at 20 characters. #53 checks this twice in Python --
  once in `resume_refusals` and once in `ResumeEvent.__post_init__` -- and this is the
  third check, the one that still holds for a writer that never constructs a
  `ResumeEvent`. It is the only condition in the resume procedure a script cannot satisfy.
- `operator_id` must name a person as `human:<handle>`. Attribution, not authentication:
  an automated actor can spell the string, but it then has a person's name against its
  row in a table nobody can edit.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019_kill_switch_journal_columns"
down_revision: str | None = "0018_trial_ledger_search_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The columns that carry the trigger. Named as a group because all five travel together:
# a row with a threshold and no observed value describes a comparison nobody can repeat.
_TRIGGER_COLUMNS: str = (
    "trigger_id, trigger_unit, trigger_observed_value, trigger_threshold_value, trigger_detail"
)


def upgrade() -> None:
    op.execute("ALTER TABLE kill_switch_event ADD COLUMN incident_id UUID")
    op.execute("ALTER TABLE kill_switch_event ADD COLUMN operator_id TEXT")
    op.execute("ALTER TABLE kill_switch_event ADD COLUMN root_cause TEXT")
    op.execute("ALTER TABLE kill_switch_event ADD COLUMN trigger_id TEXT")
    op.execute("ALTER TABLE kill_switch_event ADD COLUMN trigger_unit TEXT")
    # NUMERIC(38, 18) like every other quantity in this schema, even though these two are
    # dimensionless: a drawdown trip carries a fraction, a rejection-spike trip a count
    # and a spread trip basis points, and `trigger_unit` states which. A float column
    # would round the threshold a trip is argued against months later.
    op.execute("ALTER TABLE kill_switch_event ADD COLUMN trigger_observed_value NUMERIC(38, 18)")
    op.execute("ALTER TABLE kill_switch_event ADD COLUMN trigger_threshold_value NUMERIC(38, 18)")
    op.execute("ALTER TABLE kill_switch_event ADD COLUMN trigger_detail TEXT")
    # The book as it stood before any remediation. ADR 0014 gives up freeze-and-inspect --
    # the flatten closes the book before an investigator sees it -- and this column is the
    # artefact that replaces it, which is why it sits inside the trip row rather than
    # being fetched afterwards from a venue whose state has since moved.
    op.execute("ALTER TABLE kill_switch_event ADD COLUMN book_snapshot JSONB")

    op.execute(
        "CREATE INDEX ix_kill_switch_event_incident_id_occurred_at_utc "
        "ON kill_switch_event (incident_id, occurred_at_utc)"
    )

    op.execute(
        "ALTER TABLE kill_switch_event DROP CONSTRAINT ck_kill_switch_event_event_type_is_known"
    )
    op.execute(
        """
        ALTER TABLE kill_switch_event
            ADD CONSTRAINT ck_kill_switch_event_event_type_is_known
            CHECK (event_type IN ('tripped', 'armed', 'cleared'))
        """
    )

    op.execute(
        f"""
        ALTER TABLE kill_switch_event
            ADD CONSTRAINT ck_kill_switch_event_trip_row_is_complete
            CHECK (event_type <> 'tripped'
                   OR (incident_id IS NOT NULL
                       AND num_nulls({_TRIGGER_COLUMNS}) = 0
                       AND book_snapshot IS NOT NULL
                       AND operator_id IS NULL
                       AND root_cause IS NULL))
            NOT VALID
        """
    )
    # An arm grants nothing on its own and expires after 120 seconds, so it carries no
    # trigger and no snapshot -- but it carries the operator, because the two-step is
    # only auditable if both steps name the same person.
    op.execute(
        f"""
        ALTER TABLE kill_switch_event
            ADD CONSTRAINT ck_kill_switch_event_arm_row_is_complete
            CHECK (event_type <> 'armed'
                   OR (incident_id IS NOT NULL
                       AND operator_id IS NOT NULL
                       AND root_cause IS NULL
                       AND num_nulls({_TRIGGER_COLUMNS}) = 5
                       AND book_snapshot IS NULL))
            NOT VALID
        """
    )
    op.execute(
        f"""
        ALTER TABLE kill_switch_event
            ADD CONSTRAINT ck_kill_switch_event_resume_row_is_complete
            CHECK (event_type <> 'cleared'
                   OR (incident_id IS NOT NULL
                       AND operator_id IS NOT NULL
                       AND root_cause IS NOT NULL
                       AND num_nulls({_TRIGGER_COLUMNS}) = 5
                       AND book_snapshot IS NULL))
            NOT VALID
        """
    )
    op.execute(
        """
        ALTER TABLE kill_switch_event
            ADD CONSTRAINT ck_kill_switch_event_root_cause_is_explained
            CHECK (root_cause IS NULL OR char_length(btrim(root_cause)) >= 20)
            NOT VALID
        """
    )
    op.execute(
        """
        ALTER TABLE kill_switch_event
            ADD CONSTRAINT ck_kill_switch_event_operator_id_names_a_person
            CHECK (operator_id IS NULL
                   OR (operator_id LIKE 'human:%' AND btrim(substr(operator_id, 7)) <> ''))
            NOT VALID
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "0019_kill_switch_journal_columns is irreversible. Dropping these columns "
        "discards the incident id every RESUME is matched against, the trigger a trip "
        "is argued about afterwards, and the pre-remediation book snapshot that ADR "
        "0014 makes the only record of what the flatten closed -- none of it "
        "recoverable, because the rows cannot be rewritten to put it back. The trip "
        "rows would survive and would no longer reconstruct into a state, so the "
        "system would boot halted with an unreadable journal and no resume path. Roll "
        "forward with a new migration; if the intent is to discard a local development "
        "database, drop the database rather than the kill switch's journal."
    )
