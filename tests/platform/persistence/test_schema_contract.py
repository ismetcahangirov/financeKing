"""Properties of the schema that hold before any database exists.

These run everywhere, with no container, because they are the checks that must not be
skippable. `test_migrations.py` asserts the same properties against a real
`information_schema` -- the pair is deliberate: this file catches a bad column at the
moment it is written, and that one catches a migration that disagrees with this file.

The enum tests are the reason `fking.platform.persistence.schema` may duplicate the
domain vocabularies at all. `platform` imports no other `fking` package, so the `CHECK`
constraints are written as literal tuples; a test may import both, and does, so a new
`Side` member fails here rather than passing a constraint that rejects it at 03:00.
"""

from __future__ import annotations

from enum import StrEnum

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from fking.domain.enums import Direction, OrderType, RiskVerdict, Side, TimeInForce, Venue
from fking.platform.persistence._types import MONEY_PRECISION, MONEY_SCALE
from fking.platform.persistence.schema import (
    APPEND_ONLY_TABLES,
    DIRECTIONS,
    HASH_CHAINED_TABLES,
    METADATA,
    MONEY_COLUMN_SUFFIXES,
    ORDER_TYPES,
    RISK_VERDICTS,
    SCHEDULER_JOB_OUTCOMES,
    SIDES,
    TIME_IN_FORCES,
    VENUE_IDS,
)
from fking.platform.scheduler import JobOutcome

pytestmark = pytest.mark.unit

# PostgreSQL truncates an identifier at 63 bytes. A constraint that is called one thing
# in this repository and another in the database is a constraint nobody can grep for
# from an error message.
MAX_IDENTIFIER_LENGTH = 63

ALL_COLUMNS = [
    (table.name, column) for table in METADATA.tables.values() for column in table.columns
]


def _is_money_named(column_name: str) -> bool:
    return column_name.endswith(MONEY_COLUMN_SUFFIXES)


@pytest.mark.parametrize(
    ("table_name", "column"),
    [(name, column) for name, column in ALL_COLUMNS if _is_money_named(column.name)],
    ids=lambda argument: getattr(argument, "name", argument),
)
def test_every_money_named_column_is_numeric_38_18(
    table_name: str, column: sa.Column[object]
) -> None:
    assert isinstance(column.type, sa.Numeric), (
        f"{table_name}.{column.name} is named as money but is {column.type!r}"
    )
    assert (column.type.precision, column.type.scale) == (MONEY_PRECISION, MONEY_SCALE)


def test_no_column_anywhere_is_a_float() -> None:
    """The one-directional money-suffix rule is not enough on its own.

    A column called `sharpe` or `weight` carries no money suffix, so the rule above says
    nothing about it -- and `DOUBLE PRECISION` in this schema is banned outright, not
    only on the columns whose names happen to advertise what they hold.
    """
    offenders = [
        f"{table_name}.{column.name}"
        for table_name, column in ALL_COLUMNS
        if isinstance(column.type, sa.Float)
    ]
    assert offenders == []


@pytest.mark.parametrize(
    ("table_name", "column"),
    ALL_COLUMNS,
    ids=lambda argument: getattr(argument, "name", argument),
)
def test_temporal_columns_are_named_utc_and_are_timezone_aware(
    table_name: str, column: sa.Column[object]
) -> None:
    """Both directions, because either half alone leaves a hole.

    A `TIMESTAMP WITHOUT TIME ZONE` stores wall-clock digits and discards the offset on
    insert, silently and irreversibly. A tz-aware column *not* named `_utc` is invisible
    to the information_schema scan that would have caught the first case.
    """
    if isinstance(column.type, sa.DateTime):
        assert column.type.timezone, f"{table_name}.{column.name} is not TIMESTAMPTZ"
        assert column.name.endswith("_utc"), (
            f"{table_name}.{column.name} is a timestamp with no _utc suffix, so the "
            f"information_schema scan cannot see it"
        )
    else:
        assert not column.name.endswith("_utc"), (
            f"{table_name}.{column.name} claims _utc but is {column.type!r}"
        )


@pytest.mark.parametrize("table", list(METADATA.tables.values()), ids=lambda t: t.name)
def test_every_table_has_a_primary_key(table: sa.Table) -> None:
    assert list(table.primary_key.columns), f"{table.name} has no primary key"


@pytest.mark.parametrize("table", list(METADATA.tables.values()), ids=lambda t: t.name)
def test_every_foreign_key_column_is_indexed(table: sa.Table) -> None:
    """An unindexed FK makes every delete on the parent a sequential scan of the child.

    Leading-column membership is what counts: a composite index on
    `(instrument_id, event_time_utc)` serves a lookup by `instrument_id`, and requiring a
    separate single-column index would be requiring a duplicate.
    """
    indexed_leads = {next(iter(index.columns)).name for index in table.indexes if index.columns}
    primary_key_lead = {next(iter(table.primary_key.columns)).name}
    unique_leads = {
        next(iter(constraint.columns)).name
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint) and constraint.columns
    }
    covered = indexed_leads | primary_key_lead | unique_leads

    unindexed = sorted(
        {
            column.name
            for constraint in table.foreign_key_constraints
            for column in constraint.columns
            if column.name not in covered
        }
    )
    assert unindexed == [], f"{table.name} has unindexed foreign keys {unindexed}"


@pytest.mark.parametrize("table", list(METADATA.tables.values()), ids=lambda t: t.name)
def test_identifiers_fit_postgres(table: sa.Table) -> None:
    names = (
        [table.name]
        + [column.name for column in table.columns]
        + [str(constraint.name) for constraint in table.constraints if constraint.name]
        + [str(index.name) for index in table.indexes if index.name]
    )
    too_long = sorted(name for name in names if len(name) > MAX_IDENTIFIER_LENGTH)
    assert too_long == []


@pytest.mark.parametrize(
    ("literals", "enum_type"),
    [
        (VENUE_IDS, Venue),
        (DIRECTIONS, Direction),
        (SIDES, Side),
        (ORDER_TYPES, OrderType),
        (TIME_IN_FORCES, TimeInForce),
        (RISK_VERDICTS, RiskVerdict),
        # Not a domain enum: the scheduler is platform mechanism, and its outcomes live
        # with it. The duplication is checked for the same reason as the rest.
        (SCHEDULER_JOB_OUTCOMES, JobOutcome),
    ],
    ids=lambda argument: getattr(argument, "__name__", "literals"),
)
def test_check_vocabularies_match_the_domain_enums(
    literals: tuple[str, ...], enum_type: type[StrEnum]
) -> None:
    assert set(literals) == {member.value for member in enum_type}


def test_append_only_tables_all_exist() -> None:
    assert set(METADATA.tables) >= APPEND_ONLY_TABLES


def test_hash_chained_tables_are_a_subset_of_the_append_only_ones() -> None:
    """A hash chain over a table the application may rewrite proves nothing.

    The chain detects a rewrite that bypassed the grants and the trigger. If there were
    no grants and no trigger to bypass, a legitimate `UPDATE` and a tampering `UPDATE`
    would produce the same broken link, and the alert would be noise.
    """
    assert HASH_CHAINED_TABLES <= APPEND_ONLY_TABLES


@pytest.mark.parametrize("name", sorted(HASH_CHAINED_TABLES))
def test_hash_chained_tables_carry_both_chain_columns(name: str) -> None:
    columns = METADATA.tables[name].columns
    for chain_column in ("prev_hash", "row_hash"):
        assert chain_column in columns
        assert isinstance(columns[chain_column].type, postgresql.BYTEA)
        assert not columns[chain_column].nullable


@pytest.mark.parametrize("table", list(METADATA.tables.values()), ids=lambda t: t.name)
def test_every_table_records_when_the_database_saw_the_row(table: sa.Table) -> None:
    """`recorded_at_utc` is supplied by the database on every table without exception.

    A writer that stamps its own insertion time can also stamp a convenient one, and the
    gap between when an event happened and when this system learned of it is the join
    key for every point-in-time question the feature store asks.
    """
    assert "recorded_at_utc" in table.columns
    assert table.columns["recorded_at_utc"].server_default is not None
