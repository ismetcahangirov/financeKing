"""The operational database: schema, connection management, reference-data seeding.

Mechanism, not policy. This package knows that a fill row carries a `NUMERIC(38, 18)`
`quote_price` and that `audit_log` refuses `UPDATE`. It has no opinion about what a
fill means, and it imports no other `fking` package outside `fking.platform`, which is
what keeps it importable from every layer without carrying a decision across a boundary
(`.claude/rules/module-boundaries.md`).

Three properties carry everything else:

- **Append-only is enforced by PostgreSQL, never by the ORM and never by convention.**
  Revoked grants are the primary control -- `TRUNCATE` does not fire row triggers -- and
  a `BEFORE UPDATE OR DELETE` trigger is the backstop for the migration that later hands
  a broad role to a new service. The two hash-chained ledgers add the third layer:
  forbidding a rewrite is not the same as detecting one.
- **Every schema change is a reviewed Alembic migration.** Nothing is applied by hand and
  nothing calls `METADATA.create_all()` outside a test that immediately throws the
  database away. A migration that touches an audit table is irreversible and says why.
- **Monetary columns are `NUMERIC(38, 18)` and temporal columns are `TIMESTAMPTZ`,**
  asserted against `information_schema` rather than reviewed.

Everything not listed in `__all__` is private and may change without notice.
"""

from fking.platform.persistence._types import MONEY_PRECISION, MONEY_SCALE
from fking.platform.persistence.engine import build_engine, build_session_factory
from fking.platform.persistence.schema import (
    APPEND_ONLY_TABLES,
    HASH_CHAINED_TABLES,
    METADATA,
    MONEY_COLUMN_SUFFIXES,
    NAMING_CONVENTION,
)
from fking.platform.persistence.seed import SeedReport, count_reference_rows, seed_reference_data

__all__ = [
    "APPEND_ONLY_TABLES",
    "HASH_CHAINED_TABLES",
    "METADATA",
    "MONEY_COLUMN_SUFFIXES",
    "MONEY_PRECISION",
    "MONEY_SCALE",
    "NAMING_CONVENTION",
    "SeedReport",
    "build_engine",
    "build_session_factory",
    "count_reference_rows",
    "seed_reference_data",
]
