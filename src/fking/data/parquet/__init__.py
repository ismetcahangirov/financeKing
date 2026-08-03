"""The canonical Parquet corpus: one layout, one writer, one reader.

```
data/parquet/market=spot/dataset=klines/symbol=BTCUSDT/interval=1m/year=2025/month=01/
    part-2025-01.parquet
data/parquet/market=spot/dataset=trades/symbol=BTCUSDT/year=2025/month=01/day=02/
    part-2025-01-02.parquet
```

`DATA_PIPELINE.md` section 6 is the specification. The four decisions that are not
stylistic, each stated where it is enforced:

1. **`market` and `dataset` are partition keys rather than columns**, so a query cannot
   accidentally union spot and futures rows -- the two corpora had different epoch units
   for part of history (VF-015, `docs/adr/0013`). `layout.market_dataset_glob` has no
   spelling that spans two markets.
2. **Bars monthly, trades daily.** Two grains, from opposite sides of the 128-512 MB
   target: a month of trade prints is multiple gigabytes and defeats pruning, a day of
   bars is a few kilobytes and per-file overhead dominates the scan.
3. **Rows sorted by event time within the file**, because predicate pushdown works on
   row-group statistics and an unsorted file's ranges overlap on every row group.
4. **Money columns are `decimal128(38, 18)`**, matching `NUMERIC(38, 18)` and the process
   decimal context. Never `double`, which is what an inferred schema produces.

The module is named for what it holds. `import pyarrow.parquet` inside it is an absolute
import and resolves to the library, not to this package.

This package writes and reads; it does not decide *what* to write. The backfill that
iterates coordinates is #26 and the quality gate that can block a write is #25 -- gate 11
of that gate queries the `source` column, which is why provenance is written here.
"""

from __future__ import annotations

from fking.data.parquet.layout import (
    DATASET_PARTITION_GRAIN,
    PartitionGrain,
    market_dataset_glob,
    partition_path,
)
from fking.data.parquet.reader import read_connection, scanned_file_count
from fking.data.parquet.schema import (
    CONTENT_DIGEST_KEY,
    DATASET_SCHEMAS,
    MONEY_COLUMN_SUFFIXES,
    MONEY_TYPE,
    TIMESTAMP_TYPE,
    RecordSource,
    schema_for,
)
from fking.data.parquet.writer import WriteOutcome, write_records

__all__ = [
    "CONTENT_DIGEST_KEY",
    "DATASET_PARTITION_GRAIN",
    "DATASET_SCHEMAS",
    "MONEY_COLUMN_SUFFIXES",
    "MONEY_TYPE",
    "TIMESTAMP_TYPE",
    "PartitionGrain",
    "RecordSource",
    "WriteOutcome",
    "market_dataset_glob",
    "partition_path",
    "read_connection",
    "scanned_file_count",
    "schema_for",
    "write_records",
]
