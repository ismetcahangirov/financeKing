"""Trade-sequence bootstrap: resample realised trades with replacement, drawn one at a
time.

Answers "how much of the equity curve's shape is luck of ordering", and nothing else.
Because every draw comes from the trades that actually happened, the *set* of values a
path is built from is the real one up to resampling noise -- so a path's mean return
carries almost no information a caller does not already have from the original sequence.
What genuinely varies path to path is the *order* the draws landed in, and order is
exactly what a compounding equity curve's drawdown depends on and its total return does
not (`BACKTEST_ENGINE.md` section 6.4). So this module reports the drawdown distribution
across paths, and deliberately does not report a per-path Sharpe: publishing one would
invite a reader to treat "luck of ordering" as if it were "luck of which trades occurred",
which is a different and much more alarming question that this method cannot answer.

`MIN_TRADES_FOR_BOOTSTRAP` mirrors the CPCV path floor in
`fking.backtest.cpcv._distribution` for the same reason: a drawdown resampled from a
handful of trades is one trade's noise wearing a distribution's name.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from fking.backtest.montecarlo._errors import MonteCarloConfigError, ResamplingRefusedError
from fking.backtest.montecarlo._rng import path_rng
from fking.backtest.montecarlo._stats import STATISTIC_QUANTUM, mean, percentile

# `BACKTEST_ENGINE.md` section 6.7's fold floor, reused here: below it the resampled
# drawdown's own standard error swamps the estimate and the distribution is not evidence
# either way.
MIN_TRADES_FOR_BOOTSTRAP: Final[int] = 30

_ONE: Final = Decimal("1")
_ZERO: Final = Decimal("0")

#: Charges one trial for one path. Called before the path is drawn, once per path,
#: always -- the same contract `fking.backtest.cpcv.TrialCharge` and
#: `fking.backtest.walkforward.TrialCharge` use.
TrialCharge = Callable[[int], None]


def _max_drawdown(trade_returns: Sequence[Decimal]) -> Decimal:
    """Peak-to-trough decline of the wealth index the trade sequence compounds to."""
    wealth = _ONE
    peak = _ONE
    worst = _ZERO
    for trade_return in trade_returns:
        wealth *= _ONE + trade_return
        peak = max(peak, wealth)
        drawdown = _ONE - wealth / peak
        worst = max(worst, drawdown)
    return worst.quantize(STATISTIC_QUANTUM)


def _total_return(trade_returns: Sequence[Decimal]) -> Decimal:
    growth = _ONE
    for trade_return in trade_returns:
        growth *= _ONE + trade_return
    return (growth - _ONE).quantize(STATISTIC_QUANTUM)


@dataclass(frozen=True, slots=True)
class TradeBootstrapPath:
    """One resampled ordering of the realised trades, and what it drew down to."""

    path_index: int
    max_drawdown_fraction: Decimal
    total_return_fraction: Decimal


@dataclass(frozen=True, slots=True)
class TradeBootstrapReport:
    """The drawdown distribution across paths, with every path's own reading kept.

    `trials_charged` is asserted equal to `path_total` here rather than left for a reader
    to derive, matching `fking.backtest.cpcv.CpcvReport` and
    `fking.backtest.walkforward.WalkForwardReport`: the two numbers drifting apart is the
    shape of the bug this report exists to make impossible.
    """

    trade_count: int
    path_total: int
    trials_charged: int
    paths: tuple[TradeBootstrapPath, ...]
    max_drawdown_mean: Decimal
    max_drawdown_p05: Decimal
    max_drawdown_p95: Decimal

    def __post_init__(self) -> None:
        if len(self.paths) != self.path_total:
            raise MonteCarloConfigError(
                f"{len(self.paths)} paths recorded against {self.path_total} planned"
            )
        if self.trials_charged != self.path_total:
            raise MonteCarloConfigError(
                f"{self.trials_charged} trials charged against {self.path_total} paths "
                f"planned; every path is a distinct draw and charges once"
            )


def run_trade_bootstrap(
    trade_returns: Sequence[Decimal],
    *,
    path_count: int,
    run_seed: int,
    charge: TrialCharge,
) -> TradeBootstrapReport:
    """Draw `path_count` resampled trade orderings, each the same length as the original.

    `charge` fires before the path is drawn, mirroring every other harness in this
    package: a charge taken afterwards is a charge a crash avoids, and a crash is the
    case that ordering exists for.
    """
    if len(trade_returns) < MIN_TRADES_FOR_BOOTSTRAP:
        raise ResamplingRefusedError(
            f"{len(trade_returns)} realised trades is below the "
            f"{MIN_TRADES_FOR_BOOTSTRAP}-trade floor; a bootstrap over fewer is not weak "
            f"evidence, it is not evidence"
        )
    if path_count < 1:
        raise MonteCarloConfigError(f"path_count must be at least 1; got {path_count}")

    trade_total = len(trade_returns)
    paths: list[TradeBootstrapPath] = []
    for path_index in range(path_count):
        charge(path_index)
        rng = path_rng(run_seed, label="trade_bootstrap", path_index=path_index)
        drawn = tuple(trade_returns[rng.randrange(0, trade_total)] for _ in range(trade_total))
        paths.append(
            TradeBootstrapPath(
                path_index=path_index,
                max_drawdown_fraction=_max_drawdown(drawn),
                total_return_fraction=_total_return(drawn),
            )
        )

    ordered_drawdowns = sorted(path.max_drawdown_fraction for path in paths)
    return TradeBootstrapReport(
        trade_count=trade_total,
        path_total=path_count,
        trials_charged=path_count,
        paths=tuple(paths),
        max_drawdown_mean=mean(ordered_drawdowns),
        max_drawdown_p05=percentile(ordered_drawdowns, Decimal("0.05")),
        max_drawdown_p95=percentile(ordered_drawdowns, Decimal("0.95")),
    )
