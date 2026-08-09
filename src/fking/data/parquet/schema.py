"""The canonical Arrow schema for each dataset, and the provenance columns.

One schema per dataset, declared once. The alternative -- inferring a schema from the
records being written -- is what puts `double` in an OHLCV column: pyarrow infers a
`Decimal` column's precision and scale from the *values it was handed*, so a month whose
prices all happen to have two decimal places produces `decimal128(8, 2)`, and the next
month produces something else. Two files, two schemas, one glob, and a scan that fails
on a type mismatch nobody changed.

**Column names mirror the record field names character for character.** `open_quote_price`
rather than `open`, per `docs/rules/naming.md`: a column named `price` is ambiguous
between mark, index, last and decision price, and -- more mechanically -- it is invisible
to any check that keys on a unit suffix. `MONEY_COLUMN_SUFFIXES` below is exactly such a
check's key, and it is what makes "every money column is `DECIMAL(38, 18)`" a total
assertion rather than a hand-maintained list that stops covering the column added next
year. `DATA_PIPELINE.md` section 6 carries the same table.

`decimal128(38, 18)` matches `NUMERIC(38, 18)` in Postgres and `getcontext().prec = 38`
in `fking.platform.numeric`, so a value representable in one is representable in all
three and the round trip cannot lose digits. Parquet will store a `Decimal` as `double`
without complaint if asked, and reading a float back out into a `Fill` is explicitly not
covered by the numeric exception in `docs/rules/decimal-and-money.md`.

Timestamps are `timestamp[us, tz=UTC]`. Microseconds rather than milliseconds because
spot archives from 2025-01-01 are microsecond epochs (VF-015, `docs/adr/0013`) and a
millisecond column truncates the last three digits of every one of them -- silently, and
in a way that makes two adjacent trades look simultaneous.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

import pyarrow as pa

from fking.data.format_resolver import Dataset
from fking.platform.errors import DataIntegrityError

__all__ = [
    "CONTENT_DIGEST_KEY",
    "DATASET_SCHEMAS",
    "MONEY_COLUMN_SUFFIXES",
    "MONEY_TYPE",
    "TIMESTAMP_TYPE",
    "RecordSource",
    "schema_for",
]

# Matches NUMERIC(38, 18) in the Postgres schema and the process-wide decimal context.
MONEY_TYPE: Final[pa.DataType] = pa.decimal128(38, 18)

TIMESTAMP_TYPE: Final[pa.DataType] = pa.timestamp("us", tz="UTC")

MONEY_COLUMN_SUFFIXES: Final[frozenset[str]] = frozenset({"_price", "_volume", "_quantity"})
"""Suffixes that mean "this column holds an exact decimal".

The set a type assertion keys on. Adding a money column to a schema below without one of
these suffixes is how that column escapes the check, which is why the naming convention
and the check are stated in the same file.
"""

CONTENT_DIGEST_KEY: Final[bytes] = b"fking.content_digest"
"""Parquet key-value metadata key holding the digest of the records in the file.

In the file rather than in a sidecar: a sidecar can be deleted, copied without its file,
or left behind by a partial restore, and each of those makes the digest describe a file
it is no longer attached to.
"""


class RecordSource(StrEnum):
    """How a row reached the corpus.

    Not decoration. When a backtest result is disputed the first question is which rows
    came from a live stream and when they landed, because a stream-sourced bar backfilled
    after the fact has different provenance from one that arrived on time
    (`DATA_PIPELINE.md` section 6). Gate 11 of the quality gate queries this column to
    prove no synthesised rows exist, which is only possible because it is written here.
    """

    ARCHIVE = "archive"
    STREAM = "stream"
    REST_BACKFILL = "rest_backfill"
    """Recovered from the venue's REST kline endpoint after a gap was detected (#28).

    A third member rather than a reuse of `STREAM`, because the difference is exactly the
    question provenance exists to answer: a bar the socket delivered on time and a bar
    fetched hours later to repair an outage have different latency, different reconcilers
    behind them and different reasons to be doubted. It is still a real venue response, so
    the standing no-synthesised-rows gate admits it -- what that gate refuses is a `source`
    outside this enum, which is why widening the enum and widening the table's `CHECK`
    happen in the same change (`migrations/versions/0012_gap_resolution.py`).
    """


# The two provenance columns every dataset carries, appended in this order so that a
# reader scanning columns positionally finds them in the same place in every file.
_PROVENANCE: Final[tuple[pa.Field[pa.DataType], ...]] = (
    pa.field("source", pa.string(), nullable=False),
    pa.field("ingested_at_utc", TIMESTAMP_TYPE, nullable=False),
)

# `ignored_field` is deliberately absent. The parser retains Binance's trailing
# always-zero column so that the field count it checks is the file's field count -- an
# 11-column row means a column was removed upstream, which is worth failing on. On disk
# it is a column of zeroes with no reader, so it stops here.
_KLINE_FIELDS: Final[tuple[pa.Field[pa.DataType], ...]] = (
    pa.field("open_time_utc", TIMESTAMP_TYPE, nullable=False),
    pa.field("close_time_utc", TIMESTAMP_TYPE, nullable=False),
    pa.field("open_quote_price", MONEY_TYPE, nullable=False),
    pa.field("high_quote_price", MONEY_TYPE, nullable=False),
    pa.field("low_quote_price", MONEY_TYPE, nullable=False),
    pa.field("close_quote_price", MONEY_TYPE, nullable=False),
    pa.field("base_volume", MONEY_TYPE, nullable=False),
    pa.field("quote_volume", MONEY_TYPE, nullable=False),
    pa.field("trade_count", pa.int64(), nullable=False),
    pa.field("taker_buy_base_volume", MONEY_TYPE, nullable=False),
    pa.field("taker_buy_quote_volume", MONEY_TYPE, nullable=False),
)

# `venue_trade_id` stays a string, matching `TradeRecord`: it is an identifier, not a
# quantity -- nothing adds two of them -- and it is the join key a REST-backfill seam
# reconciles on, so the value that must match is the value the venue sent rather than
# whatever an int round trip produces.
_TRADE_FIELDS: Final[tuple[pa.Field[pa.DataType], ...]] = (
    pa.field("venue_trade_id", pa.string(), nullable=False),
    pa.field("event_time_utc", TIMESTAMP_TYPE, nullable=False),
    pa.field("quote_price", MONEY_TYPE, nullable=False),
    pa.field("base_quantity", MONEY_TYPE, nullable=False),
    pa.field("quote_quantity", MONEY_TYPE, nullable=False),
    pa.field("is_buyer_maker", pa.bool_(), nullable=False),
    pa.field("is_best_match", pa.bool_(), nullable=False),
)

DATASET_SCHEMAS: Final[dict[Dataset, pa.Schema]] = {
    Dataset.KLINES: pa.schema([*_KLINE_FIELDS, *_PROVENANCE]),
    Dataset.TRADES: pa.schema([*_TRADE_FIELDS, *_PROVENANCE]),
    Dataset.AGG_TRADES: pa.schema([*_TRADE_FIELDS, *_PROVENANCE]),
}
"""Exactly the datasets something in this system produces records for.

Two producers, not one. `klines` and `trades` have archive parsers, so their schemas are
written from a recorded `data.binance.vision` fragment. `aggTrades` has **no** archive
parser and no entry in `DECLARED_FORMATS` -- its CSV boolean encoding has never been
read, and inferring it would invert the side of every print in the file
(`fking.data.format_resolver`) -- but the *live* `aggTrade` stream produces the same
`TradeRecord`, from frames recorded in `tests/fixtures/streams/`, and #146 gives that tape
somewhere to land. So the schema here is still written from a recording; it is a
recording of a socket rather than of a CSV, and the archive loader still refuses the
dataset.

That the two trade datasets share `_TRADE_FIELDS` is a consequence rather than a
convenience: an aggregate print and a raw print carry the same seven observations, and
giving them separate schemas would mean a reader had to know which corpus it was in to
name a column. They stay separate *datasets* -- different partition trees, never unioned
by accident -- because one aggregate print covers a range of raw ones and summing volume
across both would double-count.

`bookDepth` and `bookTicker` remain absent: nothing in this system produces their records
from either direction, so a schema for them would be a column layout recalled from
memory, which is the failure `fking.data.loaders.parse_archive` refuses in the same words.
"""


def schema_for(dataset: Dataset) -> pa.Schema:
    """The canonical schema for `dataset`.

    Raises:
        DataIntegrityError: no canonical schema is declared for this dataset.
    """
    schema = DATASET_SCHEMAS.get(dataset)
    if schema is None:
        raise DataIntegrityError(
            f"no canonical schema is declared for dataset {dataset.value!r}; declared "
            f"datasets are {sorted(declared.value for declared in DATASET_SCHEMAS)}. A "
            f"schema is written from a recorded archive fragment and a declared format, "
            f"never from a column layout recalled from memory -- an inferred schema is "
            f"how a decimal column becomes a double"
        )
    return schema
