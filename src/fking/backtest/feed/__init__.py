"""The market-data event source, its coverage gate, and the warm-up boundary.

Turns the normalized Parquet archive into a time-ordered `MarketDataEvent` stream for a
window, a symbol set and a bar interval, and refuses any window it cannot serve from bars
that were actually observed.

Three properties this package exists to make structural rather than remembered:

**Gaps are data.** No code path here interpolates, forward-fills or synthesises a bar. A
window with a hole produces a coverage report naming the ranges and a refusal, because a
run built on invented bars does not fail -- it produces a slightly better result than the
truth, in the same direction every time (`_feed`).

**Warm-up bars advance state and reach no strategy.** `WarmupGate` is a handler, not a
flag: a bar before the exposure boundary is handed to `FeedHandler.on_warmup_bar` and is
never passed to the strategy side at all, so a handler cannot decide to look (`_warmup`).

**Epoch units are resolved per `(market, date)`.** Binance spot archives became microsecond
epochs on 2025-01-01 while USDⓈ-M futures stayed on milliseconds, so a mixed run reads two
units and a single divisor would misplace one leg by a factor of a thousand. The unit is
resolved from `fking.data.format_resolver` for every partition read, reported back on the
coverage report, and an undeclared combination raises rather than defaulting (`_corpus`).

Everything not in `__all__` is private and may change without notice.
"""

from fking.backtest.feed._cli import EX_CONFIG, EX_DATAERR, FeedConfig, load_config, main
from fking.backtest.feed._corpus import (
    ArchiveBar,
    SeriesRead,
    read_series,
    resolve_partition_epoch_unit,
)
from fking.backtest.feed._coverage import (
    CoverageGap,
    CoverageReport,
    PartitionFormat,
    SymbolCoverage,
    gaps_against,
)
from fking.backtest.feed._errors import (
    AmbiguousEpochUnitError,
    CorpusIntegrityError,
    CoverageRefusedError,
    FeedError,
    FeedRequestError,
    WarmupLeakError,
)
from fking.backtest.feed._feed import FeedSlice, MarketDataFeed
from fking.backtest.feed._intervals import BAR_INTERVALS, interval_duration
from fking.backtest.feed._request import FeedRequest, SeriesRequest
from fking.backtest.feed._warmup import FeedHandler, WarmupGate

__all__ = [
    "BAR_INTERVALS",
    "EX_CONFIG",
    "EX_DATAERR",
    "AmbiguousEpochUnitError",
    "ArchiveBar",
    "CorpusIntegrityError",
    "CoverageGap",
    "CoverageRefusedError",
    "CoverageReport",
    "FeedConfig",
    "FeedError",
    "FeedHandler",
    "FeedRequest",
    "FeedRequestError",
    "FeedSlice",
    "MarketDataFeed",
    "PartitionFormat",
    "SeriesRead",
    "SeriesRequest",
    "SymbolCoverage",
    "WarmupGate",
    "WarmupLeakError",
    "gaps_against",
    "interval_duration",
    "load_config",
    "main",
    "read_series",
    "resolve_partition_epoch_unit",
]
