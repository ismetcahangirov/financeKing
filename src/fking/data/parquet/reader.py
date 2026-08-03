"""The DuckDB read path `fking.backtest` scans, and the instrument that proves it prunes.

DuckDB rather than pandas or a Parquet reader of our own for one reason: it pushes hive
partition predicates down into *file selection*, so a filtered scan over a
thousand-symbol corpus opens the handful of files that can contribute and never touches
the rest. That is the property the layout in `layout.py` exists to buy, and it is worth
nothing if it is assumed rather than measured -- hence `scanned_file_count`.

The connection is in-memory and per-call. A long-lived connection over a corpus that a
backfill is concurrently rewriting caches file metadata that `os.replace` has since made
stale, and the resulting error names a Parquet footer rather than the race that caused it.
An in-memory DuckDB connection costs single-digit milliseconds to open, so there is
nothing to amortise.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import duckdb

from fking.platform.errors import FkingError

__all__ = ["read_connection", "scanned_file_count"]

# Tells DuckDB to derive `market`, `dataset`, `symbol`, `interval`, `year`, `month` and
# `day` as columns from the directory names, which is what makes a predicate on them
# eliminate files rather than rows.
#
# A caller writing a predicate on the kline `interval` key must double-quote it:
# `"interval" = '1m'`. Unquoted, it is a type name in DuckDB and a reserved word in
# several dialects, and the failure appears only once someone filters on it.
_HIVE_READ_OPTIONS = "hive_partitioning = true"


@contextmanager
def read_connection() -> Iterator[duckdb.DuckDBPyConnection]:
    """An in-memory DuckDB connection over the Parquet corpus, pinned to UTC.

    Read-only by construction: nothing here attaches a database file, so a query that
    tried to write would have nowhere to put it.

    **`SET TimeZone = 'UTC'` is load-bearing, not tidiness.** DuckDB renders a
    `TIMESTAMP WITH TIME ZONE` in the *session* timezone, which defaults to the machine's
    local zone. The instant is preserved -- the value still compares equal to the same
    instant expressed in UTC, so an equality assertion passes -- but the object handed
    back carries a local offset, and every wall-clock reading of it is wrong by that
    offset: `.hour`, `.date()`, `strftime`, and anything that later drops the tzinfo.
    A developer in UTC+4 and CI in UTC then disagree about which day a bar belongs to,
    with no error on either side. This is the same pin, for the same reason, as
    `ALTER DATABASE fking SET timezone TO 'UTC'` in the Postgres schema
    (`.claude/rules/time-and-timezones.md`).
    """
    connection = duckdb.connect(database=":memory:")
    try:
        connection.execute("SET TimeZone = 'UTC'")
        yield connection
    finally:
        connection.close()


def scanned_file_count(
    connection: duckdb.DuckDBPyConnection, *, glob_sql: str, predicate_sql: str = ""
) -> int:
    """How many files a scan over `glob_sql` under `predicate_sql` actually touches.

    Measured by counting the distinct `filename` values a scan produces rather than by
    parsing `EXPLAIN ANALYZE`. Two reasons, and the second is the one that decides it:

    - The profile output is a rendered tree whose wording and box-drawing characters
      change between DuckDB releases, so a parser of it is a test that breaks on an
      unrelated upgrade. It is also non-ASCII, which on Windows raises inside the
      renderer when stdout is redirected -- the same trap `make imports` documents.
    - A file that partition pruning eliminated contributes no rows and therefore no
      filename, which is exactly the quantity "did the pruning work" is asking about.

    `predicate_sql` is interpolated. Every caller is our own code building a filter from
    typed partition keys, not user input -- but if that ever stops being true, this is the
    line that has to change, which is why it is stated here rather than assumed.
    """
    where = f" WHERE {predicate_sql}" if predicate_sql else ""
    counted = connection.execute(
        # `read_parquet` takes a path pattern, not a bind parameter, so there is no
        # parameterised spelling of this call. Both interpolated values come from typed
        # partition keys built by `layout.market_dataset_glob`, never from a caller's
        # string. This is the only line in src/ that reaches SQL by interpolation, and it
        # is the line to change if that stops being true.
        f"SELECT count(DISTINCT filename) FROM "  # noqa: S608
        f"read_parquet('{glob_sql}', {_HIVE_READ_OPTIONS}, filename = true){where}"
    ).fetchone()
    if counted is None:  # pragma: no cover - a scalar aggregate always yields a row
        raise FkingError("count(DISTINCT filename) returned no row")
    return int(counted[0])
