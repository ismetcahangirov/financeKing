"""The declared privilege matrix: which role may do what, to which table.

This module is the *specification*. `migrations/versions/0008_least_privilege.py`
applies it once, and `tests/platform/persistence/test_privileges.py` asserts that the
live catalogue still equals it. Keeping the three apart is deliberate: a migration is a
historical snapshot and cannot be the answer to "what should the grants be *today*", and
a test that derives its expectations from the migration it is testing asserts only that
the migration ran.

**Why a declared matrix rather than per-table grants scattered through migrations.**
Grants written inline, one migration at a time, have no failure mode when a grant is
*missing* -- a table nobody granted is a table the application cannot read, which
surfaces as a runtime `permission denied` in whichever code path happens to touch it
first, months later. Worse is the opposite: a table nobody thought about that ends up
readable because a broad grant elsewhere caught it. Neither shows up in review, because
review sees the grants that exist and not the ones that do not.

Classifying every table exhaustively turns both into a test failure at the moment the
table is added. `CLASSIFICATION` must name every table in `METADATA`, and the test
fails in both directions, so a new table cannot merge until somebody has decided what
may touch it. That decision is cheap while the table is being designed and expensive
afterwards.

**The two-tier role model.** Privileges are held by three `NOLOGIN` group roles
(`fking_migrator`, `fking_app`, `fking_ingest`); processes connect as `LOGIN` roles that
are members of them. `0001_extensions_and_reference.py` committed to this shape and it
is kept, because it separates two things that change at different rates: a credential is
rotated, revoked, or duplicated per service, while the privilege matrix changes only
when the schema does. A single `LOGIN` role per class would tie the two together, and
rotating a password would mean touching the role that owns the grants.

`.claude/rules/append-only-audit.md`, `.claude/rules/no-lookahead.md`, `SECURITY.md`.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final

# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

MIGRATOR_ROLE: Final[str] = "fking_migrator"
APP_ROLE: Final[str] = "fking_app"
INGEST_ROLE: Final[str] = "fking_ingest"

# Order matters: fking_migrator first, because it owns every object and the other two
# are granted against objects it owns.
GROUP_ROLES: Final[tuple[str, ...]] = (MIGRATOR_ROLE, APP_ROLE, INGEST_ROLE)

# The login role for each group role. A password is never set here, never in a
# migration, and never in this repository -- `fking.platform.persistence.roles`
# provisions them from settings. A role created with LOGIN and no password cannot
# authenticate under scram-sha-256, so the state this migration leaves behind is
# fail-closed rather than open.
LOGIN_ROLE_FOR: Final[Mapping[str, str]] = MappingProxyType(
    {
        MIGRATOR_ROLE: "fking_migrator_login",
        APP_ROLE: "fking_app_login",
        INGEST_ROLE: "fking_ingest_login",
    }
)

LOGIN_ROLES: Final[tuple[str, ...]] = tuple(LOGIN_ROLE_FOR[group] for group in GROUP_ROLES)


class PrivilegeClass(StrEnum):
    """What a table is, from the point of view of who may write to it.

    Four classes rather than a per-table grant list, because the interesting question
    about a new table is which of these it is -- and there are only four answers.
    """

    # INSERT and SELECT for the application, nothing else, ever. The trigger in 0002 is
    # the backstop; this is the primary control, because TRUNCATE fires no row trigger.
    APPEND_ONLY = "append_only"
    # Ordinary mutable operational state the application owns outright.
    APP_MUTABLE = "app_mutable"
    # Written by the ingestion workers, read by everything else. The split is what makes
    # "a strategy cannot rewrite history" a permission error rather than a convention.
    INGEST_OWNED = "ingest_owned"
    # The application cannot see it at all and reaches it only through a
    # SECURITY DEFINER function. This is the class the feature store lands in (#29):
    # `no-lookahead.md` requires that `fking_app` hold no SELECT on `feature_values`, so
    # that a look-ahead bug is `permission denied` rather than a review miss.
    APP_INVISIBLE = "app_invisible"


# Every privilege PostgreSQL defines for a table. The test asserts against all seven
# rather than against the granted subset: checking only what should be present cannot
# see over-granting, which is the failure that actually matters here.
TABLE_PRIVILEGES: Final[tuple[str, ...]] = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)

_NONE: Final[frozenset[str]] = frozenset()
_READ: Final[frozenset[str]] = frozenset({"SELECT"})
_APPEND: Final[frozenset[str]] = frozenset({"SELECT", "INSERT"})
_WRITE: Final[frozenset[str]] = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})

# class -> group role -> the exact privilege set that role holds on a table of that
# class. `fking_migrator` is absent from every row on purpose: it does not need grants,
# because it *owns* the tables, and an owner's rights are not expressible as a grant.
PRIVILEGES: Final[Mapping[PrivilegeClass, Mapping[str, frozenset[str]]]] = MappingProxyType(
    {
        PrivilegeClass.APPEND_ONLY: MappingProxyType({APP_ROLE: _APPEND, INGEST_ROLE: _NONE}),
        PrivilegeClass.APP_MUTABLE: MappingProxyType({APP_ROLE: _WRITE, INGEST_ROLE: _NONE}),
        PrivilegeClass.INGEST_OWNED: MappingProxyType({APP_ROLE: _READ, INGEST_ROLE: _WRITE}),
        PrivilegeClass.APP_INVISIBLE: MappingProxyType({APP_ROLE: _NONE, INGEST_ROLE: _APPEND}),
    }
)

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Exhaustive over every table in `fking.platform.persistence.schema.METADATA`, and
# asserted so in both directions. Adding a table without adding it here fails the suite,
# which is the point: the alternative is a table that silently inherits nothing and
# fails at 03:00 in whichever code path reaches it first.
CLASSIFICATION: Final[Mapping[str, PrivilegeClass]] = MappingProxyType(
    {
        # Reference. Mutable because instrument filters are reconciled against the
        # venue's own exchangeInfo at startup, so a stale tick size has to be
        # correctable without a migration.
        "venue": PrivilegeClass.APP_MUTABLE,
        "instrument": PrivilegeClass.APP_MUTABLE,
        "venue_maintenance_window": PrivilegeClass.APP_MUTABLE,
        # Market data. The one split that already existed before this module, in 0003.
        "bar": PrivilegeClass.INGEST_OWNED,
        "funding_rate": PrivilegeClass.INGEST_OWNED,
        # The backfill's own record of what it read and what is missing. Ingest-owned for
        # the same reason `bar` is: an application role that can write a coverage row can
        # declare a gap closed that the corpus does not hold, and a backtest reading that
        # row would narrow no window and refuse no run.
        "ingest_partition": PrivilegeClass.INGEST_OWNED,
        "ingest_file": PrivilegeClass.INGEST_OWNED,
        "coverage_gap": PrivilegeClass.INGEST_OWNED,
        # Strategy and evolution state the application advances.
        "strategy": PrivilegeClass.APP_MUTABLE,
        "strategy_version": PrivilegeClass.APP_MUTABLE,
        "generation": PrivilegeClass.APP_MUTABLE,
        "evaluation": PrivilegeClass.APP_MUTABLE,
        # An order moves through statuses, so it is the one execution table that is not
        # append-only. Its history lives in `fill` and in `audit_log`, both of which are.
        "order": PrivilegeClass.APP_MUTABLE,
        # A run is opened, then closed with an outcome. The calls it made are append-only.
        "agent_run": PrivilegeClass.APP_MUTABLE,
        # Pruned by age, so DELETE is required. A consumer cannot deduplicate against
        # rows it has pruned, which is why the retention window is a decision rather
        # than a cleanup job.
        "processed_events": PrivilegeClass.APP_MUTABLE,
        # Append-only: the 12 tables in schema.APPEND_ONLY_TABLES, repeated here rather
        # than derived, so that the two lists disagreeing is a test failure instead of
        # one list silently defining the other.
        "audit_log": PrivilegeClass.APPEND_ONLY,
        "trial_ledger": PrivilegeClass.APPEND_ONLY,
        "agent_call": PrivilegeClass.APPEND_ONLY,
        "fill": PrivilegeClass.APPEND_ONLY,
        "risk_decision": PrivilegeClass.APPEND_ONLY,
        "limit_breach": PrivilegeClass.APPEND_ONLY,
        "kill_switch_event": PrivilegeClass.APPEND_ONLY,
        "strategy_lifecycle_transition": PrivilegeClass.APPEND_ONLY,
        "promotion": PrivilegeClass.APPEND_ONLY,
        "retirement": PrivilegeClass.APPEND_ONLY,
        "position_snapshot": PrivilegeClass.APPEND_ONLY,
        "account_snapshot": PrivilegeClass.APPEND_ONLY,
    }
)

# Views are granted separately: a view's own ACL governs access, and the underlying
# table's ACL is checked against the view's *owner*, not the caller. That is the same
# mechanism `feature_as_of()` will use, and it is why `global_trial_count` can be
# readable while nothing else about `trial_ledger` changes.
VIEW_PRIVILEGES: Final[Mapping[str, Mapping[str, frozenset[str]]]] = MappingProxyType(
    {
        "global_trial_count": MappingProxyType({APP_ROLE: _READ, INGEST_ROLE: _NONE}),
        # `backtest` reads this before every run to decide whether its window contains a
        # gap, so the application role reads it. The ingest role reads the underlying
        # tables directly and has no use for the aggregate.
        "data_coverage": MappingProxyType({APP_ROLE: _READ, INGEST_ROLE: _NONE}),
    }
)


def tables_in_class(privilege_class: PrivilegeClass) -> tuple[str, ...]:
    """Every table of one class, sorted, so migrations and tests iterate identically."""
    return tuple(
        sorted(name for name, member in CLASSIFICATION.items() if member is privilege_class)
    )


def granted_privileges(table: str, group_role: str) -> frozenset[str]:
    """What `group_role` is declared to hold on `table`.

    Raises rather than returning an empty set for an unknown table: "no privileges" and
    "nobody has classified this" are different answers, and conflating them is how an
    unclassified table looks correctly locked down.
    """
    try:
        privilege_class = CLASSIFICATION[table]
    except KeyError as unclassified:
        raise KeyError(
            f"{table!r} has no privilege class; add it to CLASSIFICATION in "
            f"fking.platform.persistence.privileges"
        ) from unclassified
    return PRIVILEGES[privilege_class].get(group_role, _NONE)


__all__: tuple[str, ...] = (
    "APP_ROLE",
    "CLASSIFICATION",
    "GROUP_ROLES",
    "INGEST_ROLE",
    "LOGIN_ROLES",
    "LOGIN_ROLE_FOR",
    "MIGRATOR_ROLE",
    "PRIVILEGES",
    "TABLE_PRIVILEGES",
    "VIEW_PRIVILEGES",
    "PrivilegeClass",
    "granted_privileges",
    "tables_in_class",
)
