"""The agent call trail becomes reconstructable: provenance, a prompt registry, a chain.

Revision ID: 0017_agent_call_chain
Revises: 0016_evolution_derived_reads

**Irreversible.** `downgrade()` raises. 0006 created `agent_call` as a reversible
migration because at that point it held no chain and no history worth defending; this one
gives it both, and rolling a hash-chained audit table back is a data-destruction operation
dressed as a schema operation (`.claude/rules/append-only-audit.md`).

Three things change, each closing a different hole in "reconstruct this decision months
later from the audit row alone" (`ARCHITECTURE.md` section 11).

**Provenance moves onto the row.** `correlation_id` and `agent_id` were reachable only by
joining `agent_run`. A join is a reconstruction that depends on a second table still
existing and still agreeing, and the correlation id is the one field the whole
reconstruction is indexed by -- it is minted at the top of the flow, at the bar close or
the schedule tick, not by the audit writer. Denormalising it here is deliberate: this
table answers questions on its own or it does not answer them.

**`prompt_template` makes a prompt resolvable.** The rendered prompt is already stored
verbatim, which proves what was sent but not *which* prompt version sent it. The template
is content-addressed -- a `BEFORE INSERT` trigger computes `prompt_hash` from
`template_text`, so a writer cannot supply a hash that disagrees with the text it claims
to identify, and the append-only guard means the text under a hash can never change. The
pair `(prompt_hash, prompt_variables)` is what lets the replay job re-render a call and
compare the result against the stored `prompt_text`.

`agent_call.prompt_hash` is deliberately **not** a foreign key. Two reasons, and the
second is the one that matters: an audit row must be writable even when the registry
write was lost, because a call that happened and was not recorded is strictly worse than
a call recorded with an unresolvable prompt; and an FK would make the replay job vacuous
while still not protecting against a registry row vanishing in a restore -- which is
exactly the event the job exists to detect.

**The chain.** Revoked grants and the trigger *forbid* a rewrite. Only the chain makes an
unforbiddable one -- a superuser, a `pg_dump`/restore from a doctored dump -- **visible**.
`agent_call` was in `APPEND_ONLY_TABLES` and not in `HASH_CHAINED_TABLES`; it is in both
now, because agent reasoning that can be silently edited afterwards is reasoning whose
contribution to a trade cannot be demonstrated, only asserted.

The migration refuses to run against a non-empty `agent_call` rather than backfilling the
new `NOT NULL` columns. Backfilling an audit table is forbidden even when the derived
value is correct: a row that reports a value it never carried is a row whose contents are
a function of when somebody last looked at it (`append-only-audit.md`, "The one
exception"). Nothing writes this table yet -- the gateway is #72 -- so the refusal costs
nothing today and is the correct answer on the day it costs something.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017_agent_call_chain"
down_revision: str | None = "0016_evolution_derived_reads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP: str = "fking_app"
_MIGRATOR: str = "fking_migrator"


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM agent_call) THEN
                RAISE EXCEPTION
                    'agent_call holds rows that predate the hash chain. Backfilling '
                    'correlation_id, agent_id, prompt_hash or a row_hash into them would '
                    'make those rows report values they never carried, which is forbidden '
                    'for an audit table. Archive the existing rows and re-run.'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END
        $$
        """
    )

    # -- prompt_template ---------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE prompt_template (
            prompt_hash       BYTEA       NOT NULL,
            agent_id          TEXT        NOT NULL,
            template_text     TEXT        NOT NULL,
            registered_at_utc TIMESTAMPTZ NOT NULL,
            recorded_at_utc   TIMESTAMPTZ DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_prompt_template PRIMARY KEY (prompt_hash),
            CONSTRAINT ck_prompt_template_template_text_is_not_empty
                CHECK (length(template_text) > 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_prompt_template_agent_id_registered_at_utc "
        "ON prompt_template (agent_id, registered_at_utc)"
    )
    op.execute(
        """
        CREATE FUNCTION fking_prompt_template_address() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            -- Content-addressed in the database, never by the caller. A writer that
            -- computes its own address can also compute a consistent one for a template
            -- it never used, and the replay job's whole claim is that the text under a
            -- hash is the text that hash was taken over.
            NEW.prompt_hash := digest(convert_to(NEW.template_text, 'UTF8'), 'sha256');
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER prompt_template_address_before_insert BEFORE INSERT ON prompt_template "
        "FOR EACH ROW EXECUTE FUNCTION fking_prompt_template_address()"
    )
    op.execute(
        "CREATE TRIGGER prompt_template_no_update_delete "
        "BEFORE UPDATE OR DELETE ON prompt_template "
        "FOR EACH ROW EXECUTE FUNCTION fking_append_only_guard()"
    )
    # Owned by the migrator like every other table (0008), so that the application role
    # cannot disable its own append-only trigger: an owner may ALTER TABLE ... DISABLE
    # TRIGGER, which would make the guard a suggestion.
    op.execute(f"ALTER TABLE prompt_template OWNER TO {_MIGRATOR}")
    op.execute(f"ALTER FUNCTION fking_prompt_template_address() OWNER TO {_MIGRATOR}")
    op.execute("REVOKE ALL ON prompt_template FROM PUBLIC")
    op.execute(f"REVOKE ALL ON prompt_template FROM {_APP}")
    op.execute(f"GRANT INSERT, SELECT ON prompt_template TO {_APP}")

    # -- agent_call: provenance, the prompt address, and the chain ----------------------
    op.execute(
        """
        ALTER TABLE agent_call
            ADD COLUMN seq              BIGINT      GENERATED ALWAYS AS IDENTITY NOT NULL,
            ADD COLUMN correlation_id   UUID        NOT NULL,
            ADD COLUMN agent_id         TEXT        NOT NULL,
            ADD COLUMN prompt_hash      BYTEA       NOT NULL,
            ADD COLUMN prompt_variables JSONB       NOT NULL,
            ADD COLUMN prev_hash        BYTEA       NOT NULL,
            ADD COLUMN row_hash         BYTEA       NOT NULL,
            ADD CONSTRAINT uq_agent_call_seq UNIQUE (seq)
        """
    )
    op.execute(
        "CREATE INDEX ix_agent_call_correlation_id_called_at_utc "
        "ON agent_call (correlation_id, called_at_utc)"
    )
    op.execute("CREATE INDEX ix_agent_call_prompt_hash ON agent_call (prompt_hash)")

    op.execute(
        """
        CREATE FUNCTION fking_agent_call_digest(
            p_prev_hash              bytea,
            p_seq                    bigint,
            p_call_id                uuid,
            p_run_id                 uuid,
            p_correlation_id         uuid,
            p_agent_id               text,
            p_provider               text,
            p_model_id               text,
            p_temperature            numeric,
            p_prompt_hash            bytea,
            p_prompt_variables       jsonb,
            p_prompt_text            text,
            p_response_text          text,
            p_prompt_token_count     integer,
            p_completion_token_count integer,
            p_latency_ms             integer,
            p_cache_hit              boolean,
            p_schema_valid           boolean,
            p_called_at              timestamptz
        ) RETURNS bytea
        LANGUAGE sql IMMUTABLE AS $$
            -- Every column a reconstruction reads is in the digest. A field outside it is
            -- a field an editor can change without breaking the chain, which is the same
            -- as not auditing it. `temperature::text` on a NUMERIC(38,18) is exact and
            -- stable -- unlike a float, whose text form is a property of the printer.
            SELECT digest(
                p_prev_hash
                || convert_to(p_seq::text, 'UTF8')
                || convert_to(p_call_id::text, 'UTF8')
                || convert_to(p_run_id::text, 'UTF8')
                || convert_to(p_correlation_id::text, 'UTF8')
                || convert_to(p_agent_id, 'UTF8')
                || convert_to(p_provider, 'UTF8')
                || convert_to(p_model_id, 'UTF8')
                || convert_to(p_temperature::text, 'UTF8')
                || p_prompt_hash
                || convert_to(p_prompt_variables::text, 'UTF8')
                || convert_to(p_prompt_text, 'UTF8')
                -- A null response and an empty one are different facts: the first is a
                -- call that produced nothing, the second is a model that answered with
                -- nothing. The tag byte keeps them distinguishable inside the digest.
                --
                -- A bytea tag rather than a text sentinel, because PostgreSQL text
                -- cannot hold 0x00 at all -- convert_to(E'\\x00...') raises
                -- CharacterNotInRepertoire, which turns every parse-failure row into an
                -- insert error. And a plain COALESCE to some readable string would let a
                -- model that literally answered that string forge the no-response digest.
                || CASE
                       WHEN p_response_text IS NULL THEN '\\x00'::bytea
                       ELSE '\\x01'::bytea || convert_to(p_response_text, 'UTF8')
                   END
                || convert_to(p_prompt_token_count::text, 'UTF8')
                || convert_to(p_completion_token_count::text, 'UTF8')
                || convert_to(p_latency_ms::text, 'UTF8')
                || convert_to(p_cache_hit::text, 'UTF8')
                || convert_to(p_schema_valid::text, 'UTF8')
                || convert_to(
                       to_char(p_called_at AT TIME ZONE 'UTC',
                               'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), 'UTF8'),
                'sha256')
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION fking_agent_call_chain() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            last_hash bytea;
        BEGIN
            -- 5510479 is a fixed key reserved for this chain, distinct from audit_log's
            -- 5510477 and trial_ledger's 8812331 so the three do not serialise against
            -- each other. Within one chain serialisation is the point: the chain has no
            -- meaning if two writers can read the same predecessor.
            PERFORM pg_advisory_xact_lock(5510479);

            SELECT row_hash INTO last_hash FROM agent_call ORDER BY seq DESC LIMIT 1;
            NEW.prev_hash := COALESCE(last_hash, '\\x00'::bytea);
            NEW.row_hash  := fking_agent_call_digest(
                NEW.prev_hash, NEW.seq, NEW.call_id, NEW.run_id, NEW.correlation_id,
                NEW.agent_id, NEW.provider, NEW.model_id, NEW.temperature,
                NEW.prompt_hash, NEW.prompt_variables, NEW.prompt_text, NEW.response_text,
                NEW.prompt_token_count, NEW.completion_token_count, NEW.latency_ms,
                NEW.cache_hit, NEW.schema_valid, NEW.called_at_utc);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER agent_call_chain_before_insert BEFORE INSERT ON agent_call "
        "FOR EACH ROW EXECUTE FUNCTION fking_agent_call_chain()"
    )
    # The digest function is what the verifier calls, so it must belong to the migrator
    # rather than to whichever role happened to run the migration; a function the
    # application owns is a function the application can redefine, and a verifier that
    # calls a redefinable digest verifies nothing.
    op.execute(
        "ALTER FUNCTION fking_agent_call_digest("
        "bytea, bigint, uuid, uuid, uuid, text, text, text, numeric, bytea, jsonb, text, "
        f"text, integer, integer, integer, boolean, boolean, timestamptz) OWNER TO {_MIGRATOR}"
    )
    op.execute(f"ALTER FUNCTION fking_agent_call_chain() OWNER TO {_MIGRATOR}")


def downgrade() -> None:
    raise RuntimeError(
        "0017_agent_call_chain is irreversible. Dropping the chain columns from "
        "agent_call discards the only evidence that the recorded prompts and responses "
        "are the ones the models were given, and dropping prompt_template makes every "
        "prompt_hash in the table permanently unresolvable -- which is the P0 finding "
        "the replay job exists to raise. Roll forward with a new migration; if the "
        "intent is to discard a local development database, drop the database."
    )
