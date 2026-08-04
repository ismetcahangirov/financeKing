"""Gate 11: a standing query, asked of the store rather than of the code that writes to it.

`SELECT count(*) FROM bar WHERE source NOT IN ('archive', 'stream')` must be zero forever.

It exists because the interpolation ban (`DATA_PIPELINE.md` section 4) is the kind of rule
that gets violated by somebody being helpful. A gap in a series is awkward -- a chart has a
hole, a rolling window returns NaN, a join drops rows -- and the helpful fix is a
forward-fill applied once, in a notebook, to "make the plot readable", which then becomes
the loader everyone uses. A synthesised bar has zero realised volatility and perfect mean
reversion, which is catnip to exactly the strategies this system exists to reject.

The `ck_bar_source_is_known` CHECK constraint already refuses such a row, so under normal
operation this gate reads zero by construction. That is not a reason to drop it. The
constraint is the *control*; this is the *verification*, and they fail independently: a
migration that adds a column and recreates the table, a `COPY` into a partition, a restore
from a dump taken before the constraint existed, or a future migration that relaxes it for
a third source all leave the constraint's promise intact in the schema and broken in the
data. Forbidding a write is not the same as demonstrating none happened.

It runs on the scheduled beat and before every backtest, not at ingestion, because it asks
about the whole store and no single file's arrival can answer it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fking.data.parquet.schema import RecordSource
from fking.data.quality.gates import Gate, QualityGateError

__all__ = ["SynthesisedRowReport", "assert_no_synthesised_rows", "count_synthesised_rows"]

# The tables carrying a `source` provenance column. One entry today; the tuple rather than
# a bare name because `funding_rate` and the trade tables acquire the same column as they
# acquire ingestion, and a query hard-coded to one table is a gate that silently stops
# covering the store it is named after.
_PROVENANCE_TABLES: Final[tuple[str, ...]] = ("bar",)


@dataclass(frozen=True, slots=True)
class SynthesisedRowReport:
    """Rows whose provenance is neither `archive` nor `stream`, per table.

    Carries the sources observed, not only a count. "Seventeen rows are synthesised" starts
    an investigation; "seventeen rows claim source='interpolated'" finishes it.
    """

    table_name: str
    row_count: int
    observed_sources: tuple[str, ...]


async def count_synthesised_rows(connection: AsyncConnection) -> tuple[SynthesisedRowReport, ...]:
    """One report per provenance table that holds a row with an unknown source.

    Returns an empty tuple when the store is clean, which is the expected answer and the
    only acceptable one.
    """
    permitted = sorted(source.value for source in RecordSource)
    reports: list[SynthesisedRowReport] = []
    for table_name in _PROVENANCE_TABLES:
        # The table name is interpolated from a module constant, never from a caller or a
        # row: a parameter cannot name a relation in PostgreSQL, and this is the one place
        # in the query that is not a bound parameter.
        rows = (
            await connection.execute(
                text(
                    f"SELECT source, count(*) AS row_count "  # noqa: S608 - constant table set
                    f"FROM {table_name} "
                    f"WHERE source <> ALL(:permitted) "
                    f"GROUP BY source ORDER BY source"
                ),
                {"permitted": permitted},
            )
        ).all()
        if not rows:
            continue
        reports.append(
            SynthesisedRowReport(
                table_name=table_name,
                row_count=sum(int(row.row_count) for row in rows),
                observed_sources=tuple(str(row.source) for row in rows),
            )
        )
    return tuple(reports)


async def assert_no_synthesised_rows(connection: AsyncConnection) -> None:
    """Gate 11. Raise if any provenance table holds a row the pipeline could not have written.

    Raises:
        QualityGateError: a row exists whose `source` is outside `RecordSource`.
    """
    reports = await count_synthesised_rows(connection)
    if not reports:
        return
    detail = "; ".join(
        f"{report.table_name}: {report.row_count} row(s) with source in "
        f"{list(report.observed_sources)}"
        for report in reports
    )
    raise QualityGateError(
        Gate.NO_SYNTHESISED_ROWS,
        f"{detail}. Interpolation has entered the store. A synthesised bar has zero realised "
        f"volatility and perfect mean reversion, so every strategy scored against a series "
        f"containing one is scored against a price path that never traded. The rows are not "
        f"repaired in place -- find what wrote them, then re-derive the affected range from "
        f"the archive",
    )
