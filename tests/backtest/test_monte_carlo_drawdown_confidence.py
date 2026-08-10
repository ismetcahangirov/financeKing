"""The drawdown confidence interval widens measurably between 1000 paths and 100 paths.

`drawdown_confidence_interval` is the standard-error band around the *mean* drawdown
estimate, and its half-width is `z * s / sqrt(path_total)` by construction -- see
`fking.backtest.montecarlo._confidence` for why that is a different object from the
percentile band across paths, and the only one of the two guaranteed to react to path
count for a fixed underlying process. This file is the acceptance criterion for issue
#43: path count is not a knob that can be turned down for speed without the report
showing it, on the same trades and the same seed derivation.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fking.backtest.montecarlo import (
    MIN_PATHS_FOR_CONFIDENCE_INTERVAL,
    MonteCarloConfigError,
    drawdown_confidence_interval,
    run_trade_bootstrap,
)
from tests.backtest.montecarlo_support import pseudo_trade_returns

pytestmark = [pytest.mark.unit]

RUN_SEED = 42
TRADE_COUNT = 200


def _drawdowns(path_count: int) -> tuple[Decimal, ...]:
    trades = pseudo_trade_returns(TRADE_COUNT)
    report = run_trade_bootstrap(
        trades, path_count=path_count, run_seed=RUN_SEED, charge=lambda _index: None
    )
    return tuple(path.max_drawdown_fraction for path in report.paths)


def test_the_confidence_interval_at_one_hundred_paths_is_wider_than_at_one_thousand() -> None:
    """Pinned from an actual run: widths of 0.019989757086 vs 0.006595063566."""
    narrow = drawdown_confidence_interval(_drawdowns(1000))
    wide = drawdown_confidence_interval(_drawdowns(100))

    assert wide.width_fraction > narrow.width_fraction
    # Not a marginal difference: sqrt(1000/100) ~= 3.16x is what the standard-error
    # formula predicts, so anything less than a clear multiple would suggest the width
    # is not actually being driven by path count.
    assert wide.width_fraction > narrow.width_fraction * Decimal("2")


def test_the_interval_narrows_monotonically_as_path_count_rises() -> None:
    widths = [drawdown_confidence_interval(_drawdowns(n)).width_fraction for n in (50, 200, 800)]

    assert widths[0] > widths[1] > widths[2]


def test_the_mean_estimate_itself_is_stable_across_path_counts() -> None:
    """Width moves with path count; the point estimate it brackets should not, much.

    Distinguishes "the interval widens because path count matters" from "the interval
    widens because fewer paths produced a wildly different mean" -- the acceptance
    criterion is about precision, not about the estimate drifting.
    """
    small = drawdown_confidence_interval(_drawdowns(100))
    large = drawdown_confidence_interval(_drawdowns(1000))

    assert abs(small.mean_max_drawdown_fraction - large.mean_max_drawdown_fraction) < Decimal(
        "0.03"
    )


def test_fewer_than_two_paths_is_refused() -> None:
    with pytest.raises(MonteCarloConfigError, match=str(MIN_PATHS_FOR_CONFIDENCE_INTERVAL)):
        drawdown_confidence_interval((Decimal("0.10"),))


def test_confidence_outside_the_open_unit_interval_is_refused() -> None:
    with pytest.raises(MonteCarloConfigError, match="confidence"):
        drawdown_confidence_interval((Decimal("0.1"), Decimal("0.2")), confidence=Decimal("1.0"))
