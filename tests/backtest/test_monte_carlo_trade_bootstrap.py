"""Trade-sequence bootstrap: order varies, the drawdown distribution is the finding.

Charge-before-evaluate and the failure-mode tests mirror
`tests/backtest/test_cpcv_trials.py` and `tests/backtest/test_walk_forward_ledger_charges.py`
deliberately: the contract every harness in this package promises the ledger is the same
contract, and a reviewer checking one should recognise the others.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fking.backtest.montecarlo import (
    MIN_TRADES_FOR_BOOTSTRAP,
    MonteCarloConfigError,
    ResamplingRefusedError,
    run_trade_bootstrap,
)
from tests.backtest.montecarlo_support import pseudo_trade_returns

pytestmark = [pytest.mark.unit]

TRADE_TOTAL = 50
LARGER_TRADE_TOTAL = 120
PATH_TOTAL = 30
LARGE_PATH_TOTAL = 100
SMALL_PATH_TOTAL = 15
TINY_PATH_TOTAL = 5


def test_every_path_is_the_same_length_as_the_original_trade_sequence() -> None:
    trades = pseudo_trade_returns(TRADE_TOTAL)
    report = run_trade_bootstrap(
        trades, path_count=PATH_TOTAL, run_seed=7, charge=lambda _index: None
    )

    assert report.trade_count == TRADE_TOTAL
    assert report.path_total == PATH_TOTAL
    assert len(report.paths) == PATH_TOTAL


def test_paths_disagree_on_drawdown_because_order_was_reshuffled() -> None:
    """The whole diagnostic: paths built from the same trades still spread out.

    If every path reported the same drawdown, either the resampling is not actually
    randomising order or the trade sequence has no order-dependence to reveal -- and this
    fixture is built with enough sign variation that a real spread is expected.
    """
    trades = pseudo_trade_returns(LARGER_TRADE_TOTAL)
    report = run_trade_bootstrap(
        trades, path_count=LARGE_PATH_TOTAL, run_seed=11, charge=lambda _index: None
    )

    distinct_drawdowns = {path.max_drawdown_fraction for path in report.paths}
    assert len(distinct_drawdowns) > 1
    assert report.max_drawdown_p95 > report.max_drawdown_p05


def test_fewer_trades_than_the_floor_is_refused() -> None:
    trades = pseudo_trade_returns(MIN_TRADES_FOR_BOOTSTRAP - 1)
    with pytest.raises(ResamplingRefusedError, match="not evidence"):
        run_trade_bootstrap(trades, path_count=10, run_seed=1, charge=lambda _index: None)


def test_a_zero_path_count_is_refused_rather_than_producing_an_empty_report() -> None:
    trades = pseudo_trade_returns(TRADE_TOTAL)
    with pytest.raises(MonteCarloConfigError, match="path_count"):
        run_trade_bootstrap(trades, path_count=0, run_seed=1, charge=lambda _index: None)


class _ChargeLedger:
    def __init__(self) -> None:
        self.charged_paths: list[int] = []

    def __call__(self, path_index: int) -> None:
        self.charged_paths.append(path_index)


def test_every_path_charges_exactly_once_before_it_is_drawn() -> None:
    trades = pseudo_trade_returns(60)
    ledger = _ChargeLedger()

    report = run_trade_bootstrap(trades, path_count=SMALL_PATH_TOTAL, run_seed=3, charge=ledger)

    assert report.trials_charged == SMALL_PATH_TOTAL
    assert ledger.charged_paths == list(range(SMALL_PATH_TOTAL))


def test_the_report_refuses_a_charge_count_that_does_not_match_its_paths() -> None:
    trades = pseudo_trade_returns(60)
    report = run_trade_bootstrap(
        trades, path_count=TINY_PATH_TOTAL, run_seed=3, charge=lambda _index: None
    )

    with pytest.raises(MonteCarloConfigError, match="trials charged"):
        type(report)(
            trade_count=report.trade_count,
            path_total=report.path_total,
            trials_charged=1,
            paths=report.paths,
            max_drawdown_mean=report.max_drawdown_mean,
            max_drawdown_p05=report.max_drawdown_p05,
            max_drawdown_p95=report.max_drawdown_p95,
        )


def test_a_flat_trade_sequence_has_zero_drawdown_on_every_path() -> None:
    """A boundary case: every trade nets zero, so no ordering can create a drawdown."""
    flat_trades = tuple(Decimal("0") for _ in range(MIN_TRADES_FOR_BOOTSTRAP))
    report = run_trade_bootstrap(flat_trades, path_count=20, run_seed=5, charge=lambda _index: None)

    assert all(path.max_drawdown_fraction == Decimal("0") for path in report.paths)
