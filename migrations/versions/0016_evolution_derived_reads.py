"""The derived read surface over the evolution record: current state and ancestry.

Revision ID: 0016_evolution_derived_reads
Revises: 0015_evolution_lineage_store

Reversible, and the split from `0015` is the point rather than an accident of file size.
`0015` holds the *record*: genomes, parent edges, strategies and the append-only,
hash-chained lifecycle event stream, none of which can be reconstructed once dropped.
Everything here is a *projection* of those rows -- a view and two recursive functions --
so dropping it loses query surface and no history, and `alembic downgrade -1` stays a
usable operator action instead of a wall.

**`strategy_current_state` is the only way to ask what state a strategy is in.**
`evolution.strategy` carries no state column, deliberately (`0015`). Current state is the
`to_state` of that strategy's highest-`seq` event, and it is a view precisely so that the
question cannot be answered by a column somebody can `UPDATE` during an incident -- the
correction being exactly the row an investigation would have needed.

The join is `LEFT`, not inner. A strategy with no events is a bug -- the genesis event is
written in the same transaction as the row -- and an inner join would make that strategy
disappear from the population rather than show up with a NULL state. A population count
that silently omits its broken members is worse than one that shows them.

**The ancestry functions bound depth and deduplicate.** `UNION` rather than `UNION ALL`
in the recursive term terminates even if a cycle reaches the table, which a depth-one
`CHECK` cannot prevent on its own. That is a backstop: cycles are refused before the
insert by `fking.evolution.store`, whose ancestry walk raises `LineageCycleError` rather
than storing the edge. Two independent mechanisms, because a walk that hangs is the
failure mode that takes a scheduler down with it.

`EVOLUTION_ENGINE.md` sections 1, 5.6 and 8.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016_evolution_derived_reads"
down_revision: str | None = "0015_evolution_lineage_store"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP: str = "fking_app"
_MIGRATOR: str = "fking_migrator"
_SCHEMA: str = "evolution"

# Section 5.6: "if more than 50% of live strategies share a common ancestor within 5
# generations". The 5 is the default here and a parameter everywhere, because the
# authoritative computation lives in `fking.evolution.lineage.lineage_collapse_report`
# and this function only supplies the edges it walks.
_DEFAULT_MAX_GENERATIONS: int = 5

_ANCESTORS: str = "genome_ancestors(bytea, integer)"
_DESCENDANTS: str = "genome_descendants(bytea)"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE VIEW {_SCHEMA}.strategy_current_state AS
        SELECT s.strategy_id,
               s.genome_hash,
               s.lineage_id,
               s.created_at_utc,
               latest.to_state       AS current_state,
               latest.seq            AS current_state_seq,
               latest.occurred_at_utc AS entered_state_at_utc,
               latest.correlation_id AS entered_state_correlation_id
          FROM {_SCHEMA}.strategy s
          LEFT JOIN LATERAL (
              SELECT e.to_state, e.seq, e.occurred_at_utc, e.correlation_id
                FROM {_SCHEMA}.strategy_lifecycle_events e
               WHERE e.strategy_id = s.strategy_id
               -- seq, not occurred_at_utc. Two transitions can share a decision instant
               -- and the chain order is the one the record was written in.
               ORDER BY e.seq DESC
               LIMIT 1
          ) latest ON true
        """
    )

    op.execute(
        f"""
        CREATE FUNCTION {_SCHEMA}.genome_ancestors(
            p_genome_hash      bytea,
            p_max_generations  integer DEFAULT {_DEFAULT_MAX_GENERATIONS}
        ) RETURNS TABLE (genome_hash bytea, generations_back integer)
        LANGUAGE sql
        STABLE
        SET search_path = pg_catalog, {_SCHEMA}, public
        AS $$
            WITH RECURSIVE walk (genome_hash, generations_back) AS (
                -- Depth zero is the genome itself. A strategy is trivially its own
                -- ancestor, and including it is what makes "share a common ancestor"
                -- true for a parent and its own child rather than only for two siblings.
                SELECT p_genome_hash, 0
                UNION
                SELECT gp.parent_genome_hash, w.generations_back + 1
                  FROM walk w
                  JOIN {_SCHEMA}.genome_parent gp
                    ON gp.child_genome_hash = w.genome_hash
                 WHERE w.generations_back < p_max_generations
            )
            SELECT w.genome_hash, min(w.generations_back)::integer
              FROM walk w
             GROUP BY w.genome_hash;
        $$
        """
    )

    op.execute(
        f"""
        CREATE FUNCTION {_SCHEMA}.genome_descendants(p_genome_hash bytea)
        RETURNS TABLE (genome_hash bytea, generations_forward integer)
        LANGUAGE sql
        STABLE
        SET search_path = pg_catalog, {_SCHEMA}, public
        AS $$
            -- Unbounded depth on purpose, unlike the ancestor walk. This is what a
            -- `defect` retirement calls to quarantine everything downstream
            -- (EVOLUTION_ENGINE.md section 8), and a depth bound there would leave the
            -- great-grandchildren of a look-ahead leak in production.
            WITH RECURSIVE walk (genome_hash, generations_forward) AS (
                SELECT p_genome_hash, 0
                UNION
                SELECT gp.child_genome_hash, w.generations_forward + 1
                  FROM walk w
                  JOIN {_SCHEMA}.genome_parent gp
                    ON gp.parent_genome_hash = w.genome_hash
            )
            SELECT w.genome_hash, min(w.generations_forward)::integer
              FROM walk w
             GROUP BY w.genome_hash;
        $$
        """
    )

    op.execute(f"ALTER VIEW {_SCHEMA}.strategy_current_state OWNER TO {_MIGRATOR}")
    op.execute(f"REVOKE ALL ON {_SCHEMA}.strategy_current_state FROM PUBLIC")
    op.execute(f"GRANT SELECT ON {_SCHEMA}.strategy_current_state TO {_APP}")

    for signature in (_ANCESTORS, _DESCENDANTS):
        op.execute(f"ALTER FUNCTION {_SCHEMA}.{signature} OWNER TO {_MIGRATOR}")
        # EXECUTE is granted to PUBLIC by default, and PUBLIC is every role this cluster
        # will ever have.
        op.execute(f"REVOKE ALL ON FUNCTION {_SCHEMA}.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {_SCHEMA}.{signature} TO {_APP}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {_SCHEMA}.{_DESCENDANTS}")
    op.execute(f"DROP FUNCTION IF EXISTS {_SCHEMA}.{_ANCESTORS}")
    op.execute(f"DROP VIEW IF EXISTS {_SCHEMA}.strategy_current_state")
