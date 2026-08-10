"""Block bootstrap on returns: resample contiguous blocks, not single observations.

Answers "does the edge survive when autocorrelation structure is preserved but the
specific path is not". The block length is load-bearing and is not exposed as a knob a
caller can shrink for convenience: an i.i.d. bootstrap -- `block_length=1` -- destroys
exactly the serial structure a momentum or mean-reversion strategy trades, and reports
that essentially every such strategy is indistinguishable from noise. That result is
technically true of the resampled series and irrelevant to the real one
(`BACKTEST_ENGINE.md` section 6.4, `docs/rules/overfitting-defences.md`).

The object resampled here is the *market* return series a strategy trades against, not
the strategy's own realised daily P&L -- reordering or duplicating a fixed multiset of
daily P&L values leaves its mean and variance, and therefore its Sharpe, essentially
unchanged, because those statistics are order-invariant. What block length actually
controls is how much of the market's own trend or mean-reversion structure survives into
the resampled series that a signal is then evaluated against. So `evaluate` is injected
rather than fixed: it replays a strategy's point-in-time decision rule against one
resampled market-return series and returns the edge that rule produced, which is what
lets this module score a momentum rule, a mean-reversion rule, or any other rule without
a strategy-specific branch inside the engine (`CLAUDE.md`, "Backtest and live share one
code path").

`resample_blocks` is a moving block bootstrap: at each step a block of `block_length`
consecutive observations is drawn from a uniformly random start position, and blocks are
concatenated until the resampled series reaches the original length (the final block is
truncated rather than overrun). Blocks may overlap and a block may be redrawn, which is
what "with replacement" means at the block level.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from random import Random

from fking.backtest.montecarlo._errors import MonteCarloConfigError
from fking.backtest.montecarlo._rng import path_rng
from fking.backtest.montecarlo._stats import mean, percentile

#: Scores one resampled market-return series and returns the edge it produced. Injected
#: so the harness never special-cases a strategy's own decision rule.
BlockBootstrapEvaluator = Callable[[Sequence[Decimal]], Decimal]

#: Charges one trial for one path. Called before the path is resampled, once per path,
#: always -- the same contract every other harness in this package uses.
TrialCharge = Callable[[int], None]


def resample_blocks(
    returns: Sequence[Decimal], *, block_length: int, rng: Random
) -> tuple[Decimal, ...]:
    """One moving-block-bootstrap resampling of `returns`, the same length as the input.

    `block_length=1` degenerates to an i.i.d. bootstrap of single observations, which is
    the case this method exists to warn against using as a default -- it is reachable
    here because the regression fixture pinning that warning has to be able to construct
    it, not because it is a recommended setting.
    """
    if block_length < 1:
        raise MonteCarloConfigError(f"block_length must be at least 1; got {block_length}")
    if not returns:
        raise MonteCarloConfigError("resample_blocks needs at least one observation")
    if block_length > len(returns):
        raise MonteCarloConfigError(
            f"block_length {block_length} exceeds the {len(returns)} observations available"
        )

    target_length = len(returns)
    last_start = len(returns) - block_length
    resampled: list[Decimal] = []
    while len(resampled) < target_length:
        start = rng.randint(0, last_start)
        resampled.extend(returns[start : start + block_length])
    return tuple(resampled[:target_length])


@dataclass(frozen=True, slots=True)
class BlockBootstrapPlan:
    """What one block-bootstrap run declares, before any path is drawn.

    Grouped into one object for the reason `fking.backtest.cpcv.CpcvPlan` and
    `fking.backtest.walkforward.WalkForwardSchedule` are: `run_block_bootstrap` takes the
    plan plus its two injected collaborators, rather than four scalars and two
    collaborators in one call, so the declaration a caller wrote down is a value that
    exists on its own rather than a set of arguments that happened to travel together.
    """

    returns: tuple[Decimal, ...]
    block_length: int
    path_count: int
    run_seed: int

    def __post_init__(self) -> None:
        if not self.returns:
            raise MonteCarloConfigError("a block bootstrap plan needs at least one observation")
        if self.block_length < 1:
            raise MonteCarloConfigError(f"block_length must be at least 1; got {self.block_length}")
        if self.block_length > len(self.returns):
            raise MonteCarloConfigError(
                f"block_length {self.block_length} exceeds the {len(self.returns)} "
                f"observations available"
            )
        if self.path_count < 1:
            raise MonteCarloConfigError(f"path_count must be at least 1; got {self.path_count}")


@dataclass(frozen=True, slots=True)
class BlockBootstrapPath:
    """One resampled market path and the edge the injected evaluator found in it."""

    path_index: int
    edge: Decimal


@dataclass(frozen=True, slots=True)
class BlockBootstrapReport:
    """The edge distribution across block-resampled paths, at a fixed block length.

    `trials_charged` is asserted equal to `path_total`, matching
    `fking.backtest.cpcv.CpcvReport` and `fking.backtest.walkforward.WalkForwardReport`:
    the two numbers drifting apart is the shape of the bug this report exists to make
    impossible.
    """

    block_length: int
    observation_count: int
    path_total: int
    trials_charged: int
    paths: tuple[BlockBootstrapPath, ...]
    edge_mean: Decimal
    edge_p05: Decimal
    edge_p95: Decimal

    def __post_init__(self) -> None:
        if len(self.paths) != self.path_total:
            raise MonteCarloConfigError(
                f"{len(self.paths)} paths recorded against {self.path_total} planned"
            )
        if self.trials_charged != self.path_total:
            raise MonteCarloConfigError(
                f"{self.trials_charged} trials charged against {self.path_total} paths "
                f"planned; every path is a distinct resampling and charges once"
            )


def run_block_bootstrap(
    plan: BlockBootstrapPlan, *, evaluate: BlockBootstrapEvaluator, charge: TrialCharge
) -> BlockBootstrapReport:
    """Resample `plan.path_count` block-bootstrapped market paths and score each with
    `evaluate`.

    `charge` fires before the path is resampled or scored, mirroring
    `fking.backtest.cpcv.run_cpcv` and `fking.backtest.walkforward.run_walk_forward`.
    """
    paths: list[BlockBootstrapPath] = []
    for path_index in range(plan.path_count):
        charge(path_index)
        rng = path_rng(
            plan.run_seed, label=f"block_bootstrap:{plan.block_length}", path_index=path_index
        )
        resampled = resample_blocks(plan.returns, block_length=plan.block_length, rng=rng)
        edge = evaluate(resampled)
        paths.append(BlockBootstrapPath(path_index=path_index, edge=edge))

    ordered_edges = sorted(path.edge for path in paths)
    return BlockBootstrapReport(
        block_length=plan.block_length,
        observation_count=len(plan.returns),
        path_total=plan.path_count,
        trials_charged=plan.path_count,
        paths=tuple(paths),
        edge_mean=mean(ordered_edges),
        edge_p05=percentile(ordered_edges, Decimal("0.05")),
        edge_p95=percentile(ordered_edges, Decimal("0.95")),
    )
