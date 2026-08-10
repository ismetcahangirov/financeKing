"""Three resamplings, three questions, reported separately -- never collapsed into one
confidence interval.

| Method | Question it answers |
|---|---|
| `run_trade_bootstrap` | How much of the equity curve's shape is luck of ordering? |
| `run_block_bootstrap` | Does the edge survive with structure kept but the path resampled? |
| `run_perturbation` | Is this a plateau or a spike? |

Conflating the three into one interval answers a question about none of them
(`BACKTEST_ENGINE.md` section 6.4). A caller wanting a single verdict must read all three
reports and say why they agree or disagree; nothing in this package does that
collapsing for them.

**Determinism.** Every draw in this package traces back to `path_rng`, which derives its
seed from one `run_seed` via `fking.backtest._config.derive_seed` -- the same function
every other seeded source in the engine uses. Two processes given the same `run_seed`
reach the same draws, trade for trade and block for block
(`tests/backtest/test_monte_carlo_determinism.py`).

**The block length is not a tuning knob.** `run_block_bootstrap`'s `block_length` controls
how much of the market's own autocorrelation survives into a resampled path; shrinking it
to 1 for speed silently turns the test into an i.i.d. bootstrap that reports essentially
every momentum or mean-reversion strategy as noise. That behaviour is pinned as a
regression fixture rather than left to be rediscovered.

**Perturbation runs charge the trial ledger like any other run.** `run_perturbation`
declares its full `2 * n_parameters` grid before evaluating any of it and charges every
declared point, following the same `charge`-before-`evaluate` contract as
`fking.backtest.cpcv.run_cpcv` and `fking.backtest.walkforward.run_walk_forward`.

This subpackage is deliberately not re-exported from `fking.backtest`. "Path" already
means the equity path there (`PathStatistics`, `path_economics`), and in CPCV it means one
combination of test groups; a third meaning -- one Monte Carlo resampling -- one import
away would put three senses of the same word in one namespace. Import the subpackage, the
way `fking.backtest.cpcv` already is.

Everything not in `__all__` is private and may change without notice.
"""

from fking.backtest.montecarlo._block_bootstrap import (
    BlockBootstrapEvaluator,
    BlockBootstrapPath,
    BlockBootstrapPlan,
    BlockBootstrapReport,
    resample_blocks,
    run_block_bootstrap,
)
from fking.backtest.montecarlo._confidence import (
    DEFAULT_CONFIDENCE,
    MIN_PATHS_FOR_CONFIDENCE_INTERVAL,
    DrawdownConfidenceInterval,
    drawdown_confidence_interval,
)
from fking.backtest.montecarlo._errors import (
    MonteCarloConfigError,
    MonteCarloError,
    PerturbationRefusedError,
    ResamplingRefusedError,
)
from fking.backtest.montecarlo._perturbation import (
    DEFAULT_JITTER_FRACTION,
    MIN_RETAINED_EDGE_FRACTION,
    PerturbationEvaluator,
    PerturbationReport,
    PerturbedPoint,
    perturbation_grid,
    run_perturbation,
)
from fking.backtest.montecarlo._rng import path_rng
from fking.backtest.montecarlo._trade_bootstrap import (
    MIN_TRADES_FOR_BOOTSTRAP,
    TradeBootstrapPath,
    TradeBootstrapReport,
    run_trade_bootstrap,
)

__all__: tuple[str, ...] = (
    "DEFAULT_CONFIDENCE",
    "DEFAULT_JITTER_FRACTION",
    "MIN_PATHS_FOR_CONFIDENCE_INTERVAL",
    "MIN_RETAINED_EDGE_FRACTION",
    "MIN_TRADES_FOR_BOOTSTRAP",
    "BlockBootstrapEvaluator",
    "BlockBootstrapPath",
    "BlockBootstrapPlan",
    "BlockBootstrapReport",
    "DrawdownConfidenceInterval",
    "MonteCarloConfigError",
    "MonteCarloError",
    "PerturbationEvaluator",
    "PerturbationRefusedError",
    "PerturbationReport",
    "PerturbedPoint",
    "ResamplingRefusedError",
    "TradeBootstrapPath",
    "TradeBootstrapReport",
    "drawdown_confidence_interval",
    "path_rng",
    "perturbation_grid",
    "resample_blocks",
    "run_block_bootstrap",
    "run_perturbation",
    "run_trade_bootstrap",
)
