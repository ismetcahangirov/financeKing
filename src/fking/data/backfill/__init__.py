"""One-command resumable bulk backfill, and the coverage registry it maintains.

```
make ingest SYMBOLS=BTCUSDT,ETHUSDT INTERVAL=1m
make coverage
```

The pieces already existed before this package: `archive` fetches and verifies, `loaders`
parses, `quality` gates, `parquet` writes. What was missing is the thing that decides *which*
archives to ask for, what to do when one is not published, and how a run that is killed
after four hours resumes without re-deriving what it already holds.

| Module | Holds |
|---|---|
| `plan` | Which archives exist and which Parquet file each belongs in. Pure |
| `runner` | The loop: fetch, gate, write once per partition, register, resume |
| `registry` | The three registry tables and the coverage view |
| `report` | What the run says it did, including what it rejected |
| `rest` | The venue's public kline endpoint, for gap repair and nothing else |
| `seam` | Merging a fetched page into what the corpus holds, refusing disagreement |
| `gaps` | The kline repair: fetch wide, reconcile, write, narrow the gap by what arrived |
| `agg_trades` | The venue's public `aggTrades` endpoint, paged on `fromId` |
| `trade_gaps` | The tape repair: derive the missing id range, fetch it, rewrite the day |

Four properties are load-bearing, and each closes a failure that is silent without it.

**An archive is not a Parquet file.** Bars are partitioned monthly and published daily for
the recent tail, so a month arrives as up to thirty-one archives that all belong in one
file. `write_records` writes a partition whole, so writing each archive as it arrives would
leave the month holding whichever one was written last -- a file with a plausible name, a
plausible schema and one day in it. The partition is therefore the unit of work.

**Resume is derived from the registry and the corpus, never from a progress file.** A
partition is skipped only when the registry says it is complete *and* the Parquet file on
disk still carries the digest the registry recorded. A progress file is a third opinion
that can disagree with both.

**Gaps are recorded and never filled.** No interpolation, no forward fill, no synthesised
bars, ever. A gap is information about the world -- an outage, a halt, a delisting -- and
filling it manufactures a price path that never traded. A synthesised bar has zero realised
volatility and perfect mean reversion, which is catnip to exactly the strategies this
system exists to reject (`DATA_PIPELINE.md` section 4).

**A gap's discovery instant is recorded, not only its bounds.** A gap found inside a range
a completed backtest already consumed makes those results suspect, and only the discovery
timestamp can say which runs are affected (`DATA_PIPELINE.md` section 11).

**A gap the REST repair fills is marked resolved, never deleted, and a partial fill
narrows it rather than closing it.** The row keeps its bounds and its discovery instant
because the range was still holed for every backtest that ran before the repair; and a
partially backfilled gap recorded as closed is worse than an open one, because the
coverage report then tells `backtest` it may run (#28).

**The two repairs page on different things, and that is not a style difference.** A bar is
a time window, so `gaps` walks one. A trade tape is not: a `sequence` gap between two prints
that share a millisecond has one-millisecond bounds, and no time window can address the
prints inside it. `trade_gaps` derives the missing aggregate-id range from the venue's own
monotone counter and pages on `fromId` (#149).
"""

from __future__ import annotations

from typing import Final

from fking.data.backfill.agg_trades import (
    AggTradeRestSource,
    GuardedAggTradeRest,
    parse_agg_trade_page,
)
from fking.data.backfill.gaps import (
    KLINE_BACKFILLABLE_GAP_KINDS,
    BackfillOutcome,
    KlineGapBackfiller,
)
from fking.data.backfill.plan import (
    ArchiveSeries,
    PartitionPlan,
    PlannedArchive,
    discover_earliest_archive_date,
    plan_partitions,
)
from fking.data.backfill.registry import (
    NO_INTERVAL,
    STREAM_TIMESTAMP_RESOLUTION,
    CoverageRow,
    GapKind,
    GapResolution,
    IngestedFile,
    IngestRegistry,
    OpenGap,
    PartitionRecord,
    PartitionState,
    RecordedGap,
)
from fking.data.backfill.report import BackfillReport, SymbolReport
from fking.data.backfill.rest import GuardedKlineRest, KlineRestSource, parse_kline_page
from fking.data.backfill.runner import BackfillRequest, run_backfill
from fking.data.backfill.seam import KlineSeam, TradeSeam, reconcile_klines, reconcile_trades
from fking.data.backfill.trade_gaps import (
    TRADE_BACKFILLABLE_GAP_KINDS,
    TradeBackfillOutcome,
    TradeGapBackfiller,
)

# Every gap kind some repair in this package can close. The union lives here rather than in
# either repair module, because each of those legitimately knows only its own dataset and
# neither can import the other -- while a reader asking "is this gap repairable at all",
# which is what a coverage report and the availability contract ask, needs both answers.
BACKFILLABLE_GAP_KINDS: Final[frozenset[GapKind]] = (
    KLINE_BACKFILLABLE_GAP_KINDS | TRADE_BACKFILLABLE_GAP_KINDS
)

__all__ = [
    "BACKFILLABLE_GAP_KINDS",
    "KLINE_BACKFILLABLE_GAP_KINDS",
    "NO_INTERVAL",
    "STREAM_TIMESTAMP_RESOLUTION",
    "TRADE_BACKFILLABLE_GAP_KINDS",
    "AggTradeRestSource",
    "ArchiveSeries",
    "BackfillOutcome",
    "BackfillReport",
    "BackfillRequest",
    "CoverageRow",
    "GapKind",
    "GapResolution",
    "GuardedAggTradeRest",
    "GuardedKlineRest",
    "IngestRegistry",
    "IngestedFile",
    "KlineGapBackfiller",
    "KlineRestSource",
    "KlineSeam",
    "OpenGap",
    "PartitionPlan",
    "PartitionRecord",
    "PartitionState",
    "PlannedArchive",
    "RecordedGap",
    "SymbolReport",
    "TradeBackfillOutcome",
    "TradeGapBackfiller",
    "TradeSeam",
    "discover_earliest_archive_date",
    "parse_agg_trade_page",
    "parse_kline_page",
    "plan_partitions",
    "reconcile_klines",
    "reconcile_trades",
    "run_backfill",
]
