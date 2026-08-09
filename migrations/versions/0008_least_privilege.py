"""Login roles, object ownership, and the least-privilege grant matrix.

Revision ID: 0008_least_privilege
Revises: 0007_processed_events

`0001_extensions_and_reference.py` created the three group roles as `NOLOGIN` and
recorded that this migration owns the rest: login roles, ownership, per-schema defaults
and the ingest/app split. This is that migration.

Three things change, and only the second is obvious.

**Login roles.** The group roles cannot log in, so until now every process connected as
the superuser and the entire grant matrix was decorative -- `has_table_privilege(
'fking_app', ...)` passed while no connection was ever `fking_app`. Three `LOGIN` roles
are created as members of the group roles. They are created **without a password**,
which under scram-sha-256 means they cannot authenticate at all: the state this
migration leaves behind is fail-closed, and `fking.platform.persistence.roles`
provisions the passwords from settings. A password in a migration is a secret in git
that is identical on every deployment.

**Ownership.** Every table is currently owned by whoever ran the migration. A table's
owner may `ALTER TABLE ... DISABLE TRIGGER` no matter what has been revoked -- which
turns off the append-only guard in 0002 while every grant in the catalogue still reads
as correct. Ownership is the privilege people forget to check, and it is why the
migration role is separate from the application role rather than being the same role
with more grants. After this migration `fking_migrator` owns everything and `fking_app`
owns nothing.

**The matrix.** Grants are reset and reapplied from four classes rather than accumulated
per table. The classes and the table-to-class map are duplicated here as literals rather
than imported from `fking.platform.persistence.privileges`, for the same reason
migrations do not import `schema.METADATA`: a migration that reads a live specification
stops being a record of what it did. The two are kept honest by
`tests/platform/persistence/test_privileges.py`, which compares the live catalogue
against the specification and fails on any drift in either direction.

**What this migration does not buy.** The `ALTER DEFAULT PRIVILEGES` statement below is
close to a no-op today, because a role that was never granted anything has nothing to
revoke by default. It is kept because it is the standing instruction that survives the
*next* migration -- the one that writes `GRANT ALL ON ALL TABLES IN SCHEMA public TO
<new service role>` because that was the fast way to unblock a deploy. The mechanism
that actually catches a new unclassified table is the exhaustiveness test, not this
statement, and pretending otherwise would be the kind of comment that stops someone
looking further.

`docs/rules/append-only-audit.md`, `docs/rules/no-lookahead.md`, `SECURITY.md`.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_least_privilege"
down_revision: str | None = "0007_processed_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATOR: str = "fking_migrator"
_APP: str = "fking_app"
_INGEST: str = "fking_ingest"

# group role -> login role. Members, not replacements: a credential is rotated and
# revoked on a different schedule from the privilege matrix, and tying them together
# means a password change touches the role that holds the grants.
_LOGIN_ROLES: tuple[tuple[str, str], ...] = (
    (_MIGRATOR, "fking_migrator_login"),
    (_APP, "fking_app_login"),
    (_INGEST, "fking_ingest_login"),
)

# INSERT and SELECT for the application and nothing else. TRUNCATE is the reason the
# grant is the primary control rather than the trigger: it fires no row trigger.
_APPEND_ONLY: tuple[str, ...] = (
    "account_snapshot",
    "agent_call",
    "audit_log",
    "fill",
    "kill_switch_event",
    "limit_breach",
    "position_snapshot",
    "promotion",
    "retirement",
    "risk_decision",
    "strategy_lifecycle_transition",
    "trial_ledger",
)

_APP_MUTABLE: tuple[str, ...] = (
    "agent_run",
    "evaluation",
    "generation",
    "instrument",
    "order",
    "processed_events",
    "strategy",
    "strategy_version",
    "venue",
    "venue_maintenance_window",
)

# Written by ingestion, read by everything else. This is what makes "a strategy cannot
# rewrite history" a permission error rather than a convention.
_INGEST_OWNED: tuple[str, ...] = ("bar", "funding_rate")

_ALL_TABLES: tuple[str, ...] = tuple(sorted((*_APPEND_ONLY, *_APP_MUTABLE, *_INGEST_OWNED)))

_VIEWS: tuple[str, ...] = ("global_trial_count",)

# Not part of the schema and not in `CLASSIFICATION`, because Alembic creates it rather
# than a migration -- but it is a table in this database and the application role holding
# UPDATE on it would mean the application can lie about which migrations have run. The
# recovery from that is worse than from almost anything else in here, because every tool
# that would tell you what is wrong reads this table first.
_INFRASTRUCTURE_TABLES: tuple[str, ...] = ("alembic_version",)


def _quoted(table: str) -> str:
    """`order` is reserved in SQL, and it is the one table name that needs quoting."""
    return f'"{table}"'


def upgrade() -> None:
    # -- login roles -------------------------------------------------------------------
    for group_role, login_role in _LOGIN_ROLES:
        # CREATE ROLE has no IF NOT EXISTS on PostgreSQL 16, and a role is a
        # cluster-level object that may survive a dropped database -- so the check is
        # explicit rather than left to an error. INHERIT (the default) is what makes
        # membership give the login role the group's privileges without a SET ROLE at
        # every call site.
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{login_role}') THEN
                    CREATE ROLE {login_role} LOGIN INHERIT;
                END IF;
            END
            $$
            """
        )
        op.execute(f"GRANT {group_role} TO {login_role}")

    # -- schema ------------------------------------------------------------------------
    # USAGE is held by PUBLIC by default, so these are explicit rather than new: they
    # survive a later `REVOKE ALL ON SCHEMA public FROM PUBLIC`, which is a hardening
    # step somebody will eventually take without auditing what depended on the default.
    op.execute(f"GRANT USAGE ON SCHEMA public TO {_APP}, {_INGEST}")
    # CREATE, so that migrations after this one can run as fking_migrator_login rather
    # than as a superuser. The exception is 0001: CREATE EXTENSION requires superuser,
    # so the first bootstrap of an empty database still needs an admin connection.
    # DEPLOYMENT.md states that split; it is a real constraint, not an oversight.
    op.execute(f"GRANT CREATE ON SCHEMA public TO {_MIGRATOR}")

    # -- ownership ---------------------------------------------------------------------
    for table in (*_ALL_TABLES, *_INFRASTRUCTURE_TABLES):
        # On a hypertable this propagates to every existing chunk, and TimescaleDB
        # applies it to chunks created afterwards.
        op.execute(f"ALTER TABLE {_quoted(table)} OWNER TO {_MIGRATOR}")
    for view in _VIEWS:
        # A view's underlying-table access is checked against the view's *owner*, so
        # this is not cosmetic: it is what lets `global_trial_count` stay readable while
        # `trial_ledger` itself is not. The same mechanism carries `feature_as_of()`.
        op.execute(f"ALTER VIEW {view} OWNER TO {_MIGRATOR}")
    # Functions follow their tables. A trigger function is SECURITY INVOKER by default,
    # so ownership here changes who may replace it, not whose rights it runs with -- and
    # "who may replace the append-only guard" is exactly the privilege being narrowed.
    op.execute(
        f"""
        DO $$
        DECLARE
            routine record;
        BEGIN
            FOR routine IN
                SELECT p.oid::regprocedure AS signature
                  FROM pg_proc p
                  JOIN pg_namespace n ON n.oid = p.pronamespace
                 WHERE n.nspname = 'public'
                   AND p.proname LIKE 'fking\\_%'
            LOOP
                EXECUTE format('ALTER FUNCTION %s OWNER TO {_MIGRATOR}', routine.signature);
            END LOOP;
        END
        $$
        """
    )

    # -- the matrix --------------------------------------------------------------------
    # Reset first. Reapplying grants on top of whatever accumulated in 0001-0007 would
    # leave an over-grant in place and still read as correct, because a GRANT statement
    # says nothing about what it did not mention.
    for table in (*_ALL_TABLES, *_INFRASTRUCTURE_TABLES):
        op.execute(f"REVOKE ALL ON {_quoted(table)} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON {_quoted(table)} FROM {_APP}")
        op.execute(f"REVOKE ALL ON {_quoted(table)} FROM {_INGEST}")

    for table in _APPEND_ONLY:
        op.execute(f"GRANT SELECT, INSERT ON {_quoted(table)} TO {_APP}")
    for table in _APP_MUTABLE:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_quoted(table)} TO {_APP}")
    for table in _INGEST_OWNED:
        op.execute(f"GRANT SELECT ON {_quoted(table)} TO {_APP}")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_quoted(table)} TO {_INGEST}")

    for view in _VIEWS:
        op.execute(f"REVOKE ALL ON {view} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON {view} FROM {_INGEST}")
        op.execute(f"GRANT SELECT ON {view} TO {_APP}")

    # -- defaults for tables this migration has never seen -----------------------------
    # Scoped FOR ROLE fking_migrator, because default privileges apply to objects created
    # by a named role and every future table is created by the migrator.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {_MIGRATOR} IN SCHEMA public "
        f"REVOKE UPDATE, DELETE, TRUNCATE ON TABLES FROM {_APP}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {_MIGRATOR} IN SCHEMA public "
        f"REVOKE ALL ON TABLES FROM PUBLIC"
    )


def downgrade() -> None:
    # Reversible, unlike 0002: nothing here holds a row.
    #
    # Ownership goes back to whoever is running the downgrade rather than to the role
    # that held it before, because that role is not recorded anywhere -- and CURRENT_USER
    # is by definition able to own these objects, since it is able to run this.
    op.execute(f"REASSIGN OWNED BY {_MIGRATOR} TO CURRENT_USER")

    # The default-privilege statements are dropped by re-stating them as grants of
    # nothing; ALTER DEFAULT PRIVILEGES has no "unset", so the inverse of a REVOKE is a
    # GRANT of the same set back to the same grantee.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {_MIGRATOR} IN SCHEMA public "
        f"GRANT UPDATE, DELETE, TRUNCATE ON TABLES TO {_APP}"
    )

    # The login roles are deliberately left in place, for the reason 0001 gives about
    # the group roles: a role is a cluster-level object, and another database on the
    # same server may be relying on it. `DROP ROLE` here would also fail unpredictably,
    # because it checks every database in the cluster for dependencies and a downgrade
    # can only clean up the one it is connected to. Re-running the upgrade finds them
    # already present and moves on.
