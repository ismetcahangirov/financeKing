"""The pinned reference workload: one strategy, one symbol, one window, one seed.

Everything about it is fixed, and it is fixed *here* rather than in a config file so that
changing it is a diff somebody reviews. A benchmark whose workload can drift is a
benchmark whose budget means nothing -- the number goes down, and nobody can say whether
the engine got faster or the window got shorter.

**The bars are synthesised, not read from the Parquet corpus, and that is deliberate.**
The corpus is not committed (it is gigabytes, and `DATA_PIPELINE.md` builds it by
backfill), so a corpus-backed benchmark cannot run in CI at all -- and a budget that CI
cannot assert is the terminal-only number this issue exists to replace. The cost being
measured is the engine's: queue ordering, `Decimal` arithmetic in the handler, per-event
encoding and digesting. That cost is a function of the event count and the event shapes,
both of which a synthetic series reproduces exactly. What it does *not* measure is the
Parquet read path, and `PERFORMANCE_GUIDE.md` says so rather than letting the omission be
discovered later.

**The workload produces timings and cannot produce evidence.** Every path reports zero
trades, so `path_distribution` refuses and the report carries a refusal instead of a
Sharpe. That is not a limitation to be fixed: a benchmark that emitted a plausible-looking
distribution would eventually have one of its numbers quoted, and the numbers come from a
random walk.
"""

from __future__ import annotations

import hashlib
import random
from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Final

from fking.backtest import (
    DEFAULT_EVENT_BUDGET,
    Event,
    EventLoop,
    ExecutionReport,
    MarketDataEvent,
    RunConfig,
    RunContext,
    SpecRegistration,
    TimerEvent,
    WorkerMemoryBudget,
)
from fking.backtest.cpcv import (
    CpcvPartition,
    CpcvPlan,
    CpcvReport,
    CpcvSplit,
    PathPerformance,
    build_splits,
    run_cpcv,
)
from fking.backtest.walkforward import WalkForwardDeclaration
from fking.domain import Bar, Instrument, Venue

#: Binance spot testnet BTCUSDT, with the filters the venue publishes. The venue matters
#: only in that the instrument must name one; nothing here talks to it.
REFERENCE_INSTRUMENT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_notional_quote=Decimal("10.00"),
)

BAR_INTERVAL: Final = timedelta(minutes=1)

#: Fourteen days of one-minute bars: 20,160 of them. Long enough that the eight CPCV
#: groups are each a real block of market time, short enough that `make bench` finishes
#: while somebody is still looking at the terminal -- which is what makes it a benchmark
#: people actually run rather than one they read about.
WINDOW_START_UTC: Final = datetime(2024, 1, 1, tzinfo=UTC)
WINDOW_END_UTC: Final = datetime(2024, 1, 15, tzinfo=UTC)

#: The random walk's seed. Pinned, because the bar series must be identical on every
#: machine and in every process -- otherwise two workers benchmark two different markets
#: and the wall clocks are not comparable.
SERIES_SEED: Final = 20240101

#: The run seed the engine derives every component's stream from. Distinct from
#: `SERIES_SEED` so that regenerating the market cannot change the engine's draws.
RUN_SEED: Final = 109

#: Opening price of the walk, in ticks. 64,000.00 USDT at a 0.01 tick.
_OPEN_PRICE_TICKS: Final = 6_400_000

#: Each minute moves by a uniform draw in +/- this many ticks -- 0.20 USDT, roughly the
#: one-minute range Binance spot BTCUSDT actually shows in quiet hours. Integer ticks
#: rather than a float return, so the whole series is exact `Decimal` with no rounding
#: step anywhere (`docs/rules/decimal-and-money.md`).
_TICK_STEP_BOUND: Final = 20

#: The reference strategy: a 20/60 minute mean crossover. Two `Decimal` rolling sums per
#: bar and a comparison, which is the arithmetic shape of most of the population this
#: engine will run, and enough of it that the handler is not free next to the loop.
FAST_WINDOW_BARS: Final = 20
SLOW_WINDOW_BARS: Final = 60

#: `N=8, k=2` is 28 paths, the partition `BACKTEST_ENGINE.md` section 6.2 uses as its
#: worked example and the one this budget is defended at. It is not a tunable: reducing it
#: to make the budget is exactly the defence-weakening this issue exists to prevent.
GROUP_TOTAL: Final = 8
TEST_GROUP_SIZE: Final = 2

#: A label reaches four hours past its decision and a position is held at most two, so the
#: embargo floor is six hours. Stated at the floor rather than above it, because the
#: benchmark's job is to cost the standard shape rather than a conservative one.
REFERENCE_DECLARATION: Final = WalkForwardDeclaration(
    label_horizon=timedelta(hours=4),
    availability_lag=timedelta(minutes=1),
    max_feature_lookback=timedelta(hours=4),
    max_holding_horizon=timedelta(hours=2),
)
REFERENCE_EMBARGO: Final = timedelta(hours=6)

#: The specification the benchmark's runs are charged under. Derived from the workload's
#: own identity so it is stable across machines, and it is a *stand-in*: the engine refuses
#: an unregistered run, and this benchmark has no ledger behind it. Legitimate here and
#: nowhere else, because the workload cannot produce a performance number -- see the
#: module docstring.
_SPEC_HASH: Final = hashlib.sha256(b"tools.bench reference workload v1").hexdigest()


class _NullReporter:
    """Swallows execution reports.

    The trial ledger is a database effect and the benchmark has no database. Discarding
    the report is safe only because the run it describes cannot be reported as evidence;
    a real run's reporter writes to Postgres.
    """

    def report_execution(self, execution: ExecutionReport) -> None:
        """Discard one report."""


@lru_cache(maxsize=1)
def reference_bars() -> tuple[Bar, ...]:
    """The pinned bar series, generated once per process.

    Cached because a fold worker evaluates several paths over the same market, exactly as
    a real worker holds one loaded series and slices it per fold. Without the cache the
    benchmark would spend most of its time in `Bar.__post_init__` and report the cost of
    constructing domain objects as the cost of the engine.
    """
    walk = random.Random(SERIES_SEED)  # noqa: S311 - reproducibility, not cryptography
    bars: list[Bar] = []
    open_ticks = _OPEN_PRICE_TICKS
    open_time_utc = WINDOW_START_UTC
    while open_time_utc + BAR_INTERVAL <= WINDOW_END_UTC:
        close_ticks = open_ticks + walk.randint(-_TICK_STEP_BOUND, _TICK_STEP_BOUND)
        high_ticks = max(open_ticks, close_ticks) + walk.randint(0, _TICK_STEP_BOUND)
        low_ticks = min(open_ticks, close_ticks) - walk.randint(0, _TICK_STEP_BOUND)
        bars.append(
            Bar(
                instrument=REFERENCE_INSTRUMENT,
                open_time_utc=open_time_utc,
                close_time_utc=open_time_utc + BAR_INTERVAL,
                open_quote_price=Decimal(open_ticks).scaleb(-2),
                high_quote_price=Decimal(high_ticks).scaleb(-2),
                low_quote_price=Decimal(low_ticks).scaleb(-2),
                close_quote_price=Decimal(close_ticks).scaleb(-2),
                base_volume=Decimal(walk.randint(1, 5000)).scaleb(-3),
                trade_count=walk.randint(50, 500),
            )
        )
        open_ticks = close_ticks
        open_time_utc += BAR_INTERVAL
    return tuple(bars)


@dataclass
class CrossoverHandler:
    """The reference strategy, as an `EventHandler`.

    Not a `fking.strategy` implementation, and deliberately so: the benchmark measures the
    loop and the arithmetic, and pulling in the strategy contract would make the budget a
    function of that contract's evolution too. The `Decimal` rolling sums are the part that
    matters -- they are the money-path arithmetic the budget is defended over, and no
    optimisation is permitted to replace them with floats.
    """

    closes: list[Decimal]
    fast_total_quote: Decimal
    slow_total_quote: Decimal
    fast_above_slow: bool | None
    crossings: int

    def __init__(self) -> None:
        self.closes = []
        self.fast_total_quote = Decimal("0")
        self.slow_total_quote = Decimal("0")
        self.fast_above_slow = None
        self.crossings = 0

    def observe(self, close_quote_price: Decimal) -> bool:
        """Feed one close; `True` when the fast mean has just crossed the slow one.

        Separate from `on_event` so the event total can be derived by replaying the same
        arithmetic without an engine around it. A second copy of this loop written for the
        counter would drift from this one, and the drift would show up as an
        events/second figure that quietly stopped matching the run.
        """
        self.closes.append(close_quote_price)
        self.fast_total_quote += close_quote_price
        self.slow_total_quote += close_quote_price
        if len(self.closes) > FAST_WINDOW_BARS:
            self.fast_total_quote -= self.closes[-FAST_WINDOW_BARS - 1]
        if len(self.closes) > SLOW_WINDOW_BARS:
            self.slow_total_quote -= self.closes[-SLOW_WINDOW_BARS - 1]
        if len(self.closes) < SLOW_WINDOW_BARS:
            return False
        # Cross-multiplied rather than divided: two exact integer-scaled products beat two
        # divisions that would each need a rounding decision, and the comparison is the
        # only thing the quotients were for.
        fast_above = (
            self.fast_total_quote * SLOW_WINDOW_BARS > self.slow_total_quote * FAST_WINDOW_BARS
        )
        crossed = self.fast_above_slow is not None and fast_above != self.fast_above_slow
        self.fast_above_slow = fast_above
        if crossed:
            self.crossings += 1
        return crossed

    def on_event(self, event: Event, context: RunContext) -> None:
        if not isinstance(event, MarketDataEvent):
            return
        observation = event.observation
        if not isinstance(observation, Bar):
            return
        if self.observe(observation.close_quote_price):
            context.schedule(
                TimerEvent(
                    strategy_id="bench-crossover",
                    occurs_at_utc=event.occurs_at_utc,
                    label="cross-up" if self.fast_above_slow else "cross-down",
                )
            )


def reference_plan() -> CpcvPlan:
    """The pinned partition plan: 8 groups, 2 test groups, 28 paths."""
    return CpcvPlan(
        start_utc=WINDOW_START_UTC,
        end_utc=WINDOW_END_UTC,
        group_total=GROUP_TOTAL,
        test_group_size=TEST_GROUP_SIZE,
        declaration=REFERENCE_DECLARATION,
        embargo=REFERENCE_EMBARGO,
    )


def reference_partition() -> CpcvPartition:
    """The 28 splits the benchmark evaluates."""
    return build_splits(reference_plan())


def _test_window(split: CpcvSplit) -> tuple[datetime, datetime]:
    starts = tuple(interval.start_utc for interval in split.test_intervals)
    ends = tuple(interval.end_utc for interval in split.test_intervals)
    return min(starts), max(ends)


def bars_in_test_intervals(split: CpcvSplit) -> tuple[Bar, ...]:
    """The pinned series restricted to one path's test intervals.

    Sliced by binary search over the close times rather than by scanning the whole series
    per interval. The scan was 10% of the reference run's wall clock -- benchmark overhead
    charged to the engine, which is the way a budget ends up defending the wrong number.
    A real feed slices by row group for the same reason.
    """
    bars = reference_bars()
    close_times = _reference_close_times()
    selected: list[Bar] = []
    for interval in split.test_intervals:
        first = bisect_left(close_times, interval.start_utc)
        last = bisect_left(close_times, interval.end_utc)
        selected.extend(bars[first:last])
    return tuple(selected)


@lru_cache(maxsize=1)
def _reference_close_times() -> tuple[datetime, ...]:
    """The series' close times, ascending, as the search key for `bars_in_test_intervals`."""
    return tuple(bar.close_time_utc for bar in reference_bars())


def evaluate_path(split: CpcvSplit) -> PathPerformance:
    """Run the reference strategy over one path's test intervals.

    Module-level and stateless so it survives being pickled to a worker process, which is
    what `run_cpcv(worker_total=...)` requires. A closure over the bar series would be both
    unpicklable and wrong: each worker regenerates the same pinned series from the same
    seed, so the market is identical without shipping 20,160 bars down a pipe per path.

    Returns `trade_count=0` on purpose -- see the module docstring.
    """
    start_utc, end_utc = _test_window(split)
    initial_events: list[Event] = [
        MarketDataEvent(observation=bar) for bar in bars_in_test_intervals(split)
    ]
    config = RunConfig(
        strategy_id="bench-crossover",
        strategy_version="1",
        symbols=(REFERENCE_INSTRUMENT.symbol,),
        parameters={
            "fast_window_bars": Decimal(FAST_WINDOW_BARS),
            "slow_window_bars": Decimal(SLOW_WINDOW_BARS),
        },
        start_utc=start_utc,
        end_utc=end_utc,
        run_seed=RUN_SEED,
        event_budget=DEFAULT_EVENT_BUDGET,
    )
    loop = EventLoop(
        config,
        CrossoverHandler(),
        registration=SpecRegistration(spec_hash=_SPEC_HASH, trials_charged=1),
        reporter=_NullReporter(),
        path_label=f"bench-path-{split.path_index}",
    )
    loop.run(initial_events)
    return PathPerformance(
        path_index=split.path_index,
        trade_count=0,
        # Not a Sharpe. `Decimal(trace.event_count)` would be worse -- it would look like
        # a statistic -- and zero with a zero trade count is refused by the distribution.
        sharpe_ratio=Decimal("0"),
    )


def _charge_nothing(split: CpcvSplit) -> None:
    """No-op trial charge: the benchmark has no ledger and reports no result."""


def run_reference_workload(
    *,
    worker_total: int = 1,
    memory_budget: WorkerMemoryBudget | None = None,
) -> CpcvReport:
    """Evaluate all 28 paths of the pinned partition.

    Returns only the CPCV report. The event total is derived separately by
    `dispatched_event_total`, deliberately outside whatever times this call: summing it
    out of the traces would need them shipped back from the worker processes, and pickling
    28 traces would land in the wall clock as if it were engine cost.
    """
    return run_cpcv(
        reference_partition(),
        evaluate=evaluate_path,
        charge=_charge_nothing,
        worker_total=worker_total,
        memory_budget=memory_budget,
    )


def dispatched_event_total(partition: CpcvPartition) -> int:
    """How many events the workload dispatches across every path.

    One market-data event per bar in a test interval, plus one timer per crossing, replayed
    through the same `CrossoverHandler.observe` the run uses so the two cannot drift.
    """
    total = 0
    for split in partition.splits:
        handler = CrossoverHandler()
        in_window = bars_in_test_intervals(split)
        for bar in in_window:
            handler.observe(bar.close_quote_price)
        total += len(in_window) + handler.crossings
    return total
