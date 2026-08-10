"""The block length is load-bearing, pinned as a regression fixture.

`resample_blocks(block_length=1)` degenerates to an i.i.d. bootstrap: every position in
the resampled series is drawn independently, so a momentum rule's signal -- the sign of
the prior day -- becomes statistically independent of the day it is applied to, and the
mean edge collapses toward zero. `resample_blocks(block_length=max_holding_horizon)`
keeps whole trending blocks intact, so the sign at day *t-1* still predicts day *t* most
of the time and the edge survives close to its value on the real series.

Values below are pinned from an actual run
(`tests/backtest/montecarlo_support.momentum_market_returns`, `run_seed=777`,
`path_count=200`) rather than derived from a formula, because the whole point of this
fixture is that block length is not free to "simplify" to 1 without the number moving --
`docs/rules/overfitting-defences.md`'s sibling concern in `BACKTEST_ENGINE.md` section
6.4. A change to the resampling algorithm that leaves this test green has not changed the
property the acceptance criterion cares about.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from decimal import Decimal

import pytest

from fking.backtest.montecarlo import (
    BlockBootstrapPlan,
    MonteCarloConfigError,
    resample_blocks,
    run_block_bootstrap,
)
from tests.backtest.montecarlo_support import (
    TREND_MAGNITUDE,
    momentum_edge,
    momentum_market_returns,
)

pytestmark = [pytest.mark.unit]

#: The fixture's own block length -- also `max_holding_horizon` in the sense
#: `BACKTEST_ENGINE.md` section 6.4 means it: the longest a position in this momentum
#: rule is meant to be held before the trend it is riding could plausibly have reversed.
MAX_HOLDING_HORIZON = 6
BLOCK_TOTAL = 60
RUN_SEED = 777
PATH_TOTAL = 200
SMALL_PATH_TOTAL = 10
TOY_SERIES_LENGTH = 10

# Near-zero: an eighth of the trend magnitude, well inside the noise band an i.i.d.
# bootstrap of a roughly balanced +/- series produces.
_NEAR_ZERO_EDGE_CEILING = TREND_MAGNITUDE / Decimal("8")
# Preserved: more than half the raw series' edge, and six times the near-zero ceiling --
# the two bands do not overlap by construction.
_PRESERVED_EDGE_FLOOR = TREND_MAGNITUDE / Decimal("2") * Decimal("0.75")
_DIVERGENCE_MULTIPLE = Decimal("5")


def _momentum_series() -> tuple[Decimal, ...]:
    return momentum_market_returns(block_total=BLOCK_TOTAL, block_length=MAX_HOLDING_HORIZON)


def _plan(*, block_length: int, path_count: int = PATH_TOTAL) -> BlockBootstrapPlan:
    return BlockBootstrapPlan(
        returns=_momentum_series(),
        block_length=block_length,
        path_count=path_count,
        run_seed=RUN_SEED,
    )


def test_the_raw_series_has_a_clear_momentum_edge() -> None:
    """The fixture actually contains what the regression test needs it to contain."""
    edge = momentum_edge(_momentum_series())
    assert edge > _PRESERVED_EDGE_FLOOR


def test_block_length_one_reports_near_zero_edge_for_the_momentum_strategy() -> None:
    report = run_block_bootstrap(
        _plan(block_length=1), evaluate=momentum_edge, charge=lambda _index: None
    )

    assert abs(report.edge_mean) < _NEAR_ZERO_EDGE_CEILING


def test_block_length_at_the_max_holding_horizon_preserves_the_edge() -> None:
    report = run_block_bootstrap(
        _plan(block_length=MAX_HOLDING_HORIZON),
        evaluate=momentum_edge,
        charge=lambda _index: None,
    )

    assert report.edge_mean > _PRESERVED_EDGE_FLOOR


def test_block_length_one_and_the_holding_horizon_diverge_on_the_same_series() -> None:
    """The comparison the acceptance criterion actually states, in one assertion."""
    iid_report = run_block_bootstrap(
        _plan(block_length=1), evaluate=momentum_edge, charge=lambda _index: None
    )
    structured_report = run_block_bootstrap(
        _plan(block_length=MAX_HOLDING_HORIZON),
        evaluate=momentum_edge,
        charge=lambda _index: None,
    )

    assert structured_report.edge_mean > iid_report.edge_mean * _DIVERGENCE_MULTIPLE


def test_resample_blocks_of_length_one_is_a_single_observation_iid_bootstrap() -> None:
    returns = tuple(Decimal(value) for value in range(TOY_SERIES_LENGTH))
    resampled = resample_blocks(returns, block_length=1, rng=random.Random(1))

    assert len(resampled) == len(returns)


def test_a_block_length_longer_than_the_series_is_refused() -> None:
    with pytest.raises(MonteCarloConfigError, match="exceeds"):
        BlockBootstrapPlan(
            returns=(Decimal("0.01"), Decimal("-0.01")),
            block_length=3,
            path_count=1,
            run_seed=1,
        )


def test_a_zero_path_count_plan_is_refused() -> None:
    with pytest.raises(MonteCarloConfigError, match="path_count"):
        BlockBootstrapPlan(
            returns=(Decimal("0.01"), Decimal("-0.01")), block_length=1, path_count=0, run_seed=1
        )


class _ChargeLedger:
    """Records charges in call order, matching `tests/backtest/test_cpcv_trials.py`."""

    def __init__(self) -> None:
        self.charged_paths: list[int] = []

    def __call__(self, path_index: int) -> None:
        self.charged_paths.append(path_index)


def test_every_path_charges_before_it_is_evaluated() -> None:
    ledger = _ChargeLedger()
    observed: list[tuple[int, int]] = []

    def evaluate(returns: Sequence[Decimal]) -> Decimal:
        observed.append((len(observed), len(ledger.charged_paths)))
        return momentum_edge(returns)

    report = run_block_bootstrap(
        _plan(block_length=MAX_HOLDING_HORIZON, path_count=SMALL_PATH_TOTAL),
        evaluate=evaluate,
        charge=ledger,
    )

    assert report.trials_charged == SMALL_PATH_TOTAL
    assert ledger.charged_paths == list(range(SMALL_PATH_TOTAL))
    assert observed == [(index, index + 1) for index in range(SMALL_PATH_TOTAL)]
