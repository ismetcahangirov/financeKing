"""Where a record lands on disk, derived from its coordinate and nothing else.

```
data/parquet/market=spot/dataset=klines/symbol=BTCUSDT/interval=1m/year=2025/month=01/
    part-2025-01.parquet
data/parquet/market=spot/dataset=trades/symbol=BTCUSDT/year=2025/month=01/day=02/
    part-2025-01-02.parquet
```

Three decisions here have an obvious wrong answer.

**`market` and `dataset` are partition keys, not columns.** A column can be forgotten in
a `WHERE` clause; a partition key one directory above the glob cannot be reached at all.
Spot and USDⓈ-M futures had different epoch units for part of history (VF-015), so a
query that unions them is comparing timestamps that were normalised under different
divisors -- and the union produces rows, not an error. The directory boundary is what
makes that impossible rather than merely discouraged (`DATA_PIPELINE.md` section 6).

**Bars are partitioned monthly and trades daily, and there is no single rule.** Trades
are the volume driver: a month of BTCUSDT prints runs to multiple gigabytes, which
defeats partition pruning because every filtered query still opens the one enormous file.
Bars are small enough that a daily partition would produce tens of thousands of files
across the universe, and below roughly 32 MB per file the per-file open dominates a scan.
The target is 128-512 MB, and the two granularities are what hit it from opposite sides.

**The month segment is zero-padded and stays a string.** `month=1` sorts after `month=10`
lexically and DuckDB types an unpadded segment as `BIGINT`, so a predicate written
`month = '01'` against one corpus and `month = 1` against another is a filter that
matches nothing on one of them and says so with an empty result rather than an error.
`year` is unpadded and typed `BIGINT`, which is correct for a value nobody pads.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Final

from fking.data.archive import ArchiveCoordinate
from fking.data.format_resolver import Dataset
from fking.data.parquet.schema import schema_for
from fking.platform.errors import DataIntegrityError

__all__ = [
    "DATASET_PARTITION_GRAIN",
    "PartitionGrain",
    "market_dataset_glob",
    "partition_path",
]


class PartitionGrain(StrEnum):
    """How much time one file covers."""

    MONTHLY = "monthly"
    DAILY = "daily"


DATASET_PARTITION_GRAIN: Final[Mapping[Dataset, PartitionGrain]] = {
    Dataset.KLINES: PartitionGrain.MONTHLY,
    Dataset.TRADES: PartitionGrain.DAILY,
}
"""Grain per dataset. Deliberately a table rather than a rule with an exception.

A dataset added here without a considered grain gets the wrong one, so it is absent
until someone has decided -- and `schema_for` refuses it first regardless, because a
dataset with no schema has no rows to place.
"""

# Kline archives are the only dataset keyed by interval, matching `ArchiveCoordinate`.
# The key lands below `symbol` rather than above it because the common scan is "one
# symbol, all intervals" far more often than "one interval, all symbols".
_INTERVAL_DATASETS: Final[frozenset[Dataset]] = frozenset({Dataset.KLINES})


def _grain_for(dataset: Dataset) -> PartitionGrain:
    schema_for(dataset)  # refuses an undeclared dataset with the fuller message
    grain = DATASET_PARTITION_GRAIN.get(dataset)
    if grain is None:  # pragma: no cover - unreachable while the two tables agree
        raise DataIntegrityError(
            f"dataset {dataset.value!r} has a canonical schema but no declared partition "
            f"grain; the two tables in fking.data.parquet must be extended together"
        )
    return grain


def _corpus_segments(coordinate: ArchiveCoordinate) -> tuple[str, ...]:
    """The two segments that separate one corpus from another.

    Kept separate from the rest because these are the ones a glob must never sit above
    -- everything below them narrows a scan, but only these two make it *impossible* to
    union spot and futures rows.
    """
    _grain_for(coordinate.dataset)  # refuses an undeclared dataset before any path exists
    return (
        f"market={coordinate.market.value}",
        f"dataset={coordinate.dataset.value}",
    )


def _instrument_segments(coordinate: ArchiveCoordinate) -> tuple[str, ...]:
    """Symbol, and interval where the dataset is keyed by one."""
    if coordinate.dataset not in _INTERVAL_DATASETS:
        return (f"symbol={coordinate.symbol}",)
    # ArchiveCoordinate.__post_init__ already refuses a kline coordinate with no
    # interval, so this is a narrowing for the type checker rather than a check.
    if coordinate.interval is None:  # pragma: no cover - refused at construction
        raise DataIntegrityError(
            f"dataset {coordinate.dataset.value!r} is keyed by interval and the "
            f"coordinate carries none"
        )
    return (f"symbol={coordinate.symbol}", f"interval={coordinate.interval}")


def _period_segments(coordinate: ArchiveCoordinate) -> tuple[str, ...]:
    """Year, month, and day where the grain is daily."""
    archive_date = coordinate.archive_date
    segments = (f"year={archive_date.year:04d}", f"month={archive_date.month:02d}")
    if _grain_for(coordinate.dataset) is PartitionGrain.DAILY:
        return (*segments, f"day={archive_date.day:02d}")
    return segments


def _partition_segments(coordinate: ArchiveCoordinate) -> tuple[str, ...]:
    """Every Hive key/value directory segment, ordered coarse to fine."""
    return (
        *_corpus_segments(coordinate),
        *_instrument_segments(coordinate),
        *_period_segments(coordinate),
    )


def _file_name(dataset: Dataset, archive_date: date) -> str:
    """`part-<period>.parquet`, where the period is the finest partition key.

    The period is repeated in the filename even though every one of its components is
    already a directory above it. That redundancy is what makes a file identifiable once
    it has been copied out of its tree -- into a bug report, a support ticket, or a
    restore staging directory -- where the partition path is exactly what was lost.
    """
    if _grain_for(dataset) is PartitionGrain.MONTHLY:
        return f"part-{archive_date.year:04d}-{archive_date.month:02d}.parquet"
    return f"part-{archive_date.isoformat()}.parquet"


def partition_path(coordinate: ArchiveCoordinate, *, root: Path) -> Path:
    """The one file `coordinate`'s records belong in.

    Deterministic and total: two coordinates in the same partition return the same path,
    which is what makes the write idempotent rather than append-only.

    Raises:
        DataIntegrityError: the dataset has no canonical schema or no declared grain.
    """
    path = root
    for segment in _partition_segments(coordinate):
        path = path / segment
    return path / _file_name(coordinate.dataset, coordinate.archive_date)


def market_dataset_glob(
    coordinate: ArchiveCoordinate, *, root: Path, narrow_to_symbol: bool = False
) -> str:
    """A DuckDB glob rooted at one `(market, dataset)`, never above it.

    There is no parameter that widens this to two markets, and that absence is the
    contract: a caller that needs both writes two reads and a `UNION ALL`, which is a
    place a reviewer can see the decision. `DATA_PIPELINE.md` section 6 makes the same
    point about the partition boundary being a standing reminder rather than a filter.

    `narrow_to_symbol` additionally pins `symbol` -- and `interval`, for klines -- into
    the path. It deliberately stops there rather than continuing into `year`/`month`:
    the common scan is one symbol over a date *range*, and a glob pinned to the
    coordinate's own month would silently return one month of it. Pruning on the period
    keys works through the predicate, which is where a range belongs.

    That makes `narrow_to_symbol` an optimisation for a scan over a large corpus rather
    than a correctness requirement. The market and dataset segments are the two that must
    be in the path, and they always are.

    Returns a POSIX-separated string: DuckDB's glob syntax is `/` on every platform, and
    a Windows `\\` inside a quoted SQL literal is an escape rather than a separator.
    """
    kept = _corpus_segments(coordinate)
    if narrow_to_symbol:
        kept = (*kept, *_instrument_segments(coordinate))
    prefix = "/".join((root.as_posix(), *kept))
    return f"{prefix}/**/*.parquet"
