"""The same `run_seed` produces identical draws whether or not the two runs share a
process.

`fking.backtest.montecarlo.path_rng` derives every draw from `run_seed` through
`fking.backtest._config.derive_seed`, the same function every other seeded source in the
engine uses. Because `derive_seed` is a pure hash and `random.Random(seed)` is CPython's
portable Mersenne Twister, two independent interpreter processes given the same
`run_seed` must reach the same per-path seed and therefore the same draws -- there is no
process-local state anywhere in the derivation. This file proves that across an actual
subprocess boundary rather than merely calling the function twice in one process, which
would still pass if some hidden module-level counter had leaked into the seed.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.backtest.montecarlo import (
    BlockBootstrapPlan,
    path_rng,
    resample_blocks,
    run_block_bootstrap,
    run_perturbation,
    run_trade_bootstrap,
)
from tests.backtest.montecarlo_support import (
    PERTURBATION_BASELINE,
    momentum_edge,
    momentum_market_returns,
    plateau_evaluator,
    pseudo_trade_returns,
)

pytestmark = [pytest.mark.unit]

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SUBPROCESS_SCRIPT = """
from tests.backtest.montecarlo_support import pseudo_trade_returns
from fking.backtest.montecarlo import run_trade_bootstrap

trades = pseudo_trade_returns(60)
report = run_trade_bootstrap(trades, path_count=5, run_seed=123456, charge=lambda _index: None)
for path in report.paths:
    print(path.path_index, path.max_drawdown_fraction, path.total_return_fraction)
"""


def _run_subprocess() -> str:
    completed = subprocess.run(  # noqa: S603 - fixed interpreter, fixed inline script
        [sys.executable, "-c", _SUBPROCESS_SCRIPT],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


@pytest.mark.slow
def test_the_same_run_seed_produces_identical_trade_bootstrap_draws_across_processes() -> None:
    first = _run_subprocess()
    second = _run_subprocess()

    assert first == second
    assert first.strip() != ""


def test_path_rng_is_a_pure_function_of_run_seed_label_and_path_index() -> None:
    first = path_rng(42, label="trade_bootstrap", path_index=3)
    second = path_rng(42, label="trade_bootstrap", path_index=3)

    assert first.getstate() == second.getstate()
    assert [first.random() for _ in range(10)] == [second.random() for _ in range(10)]


def test_different_labels_at_the_same_run_seed_and_path_index_do_not_collide() -> None:
    trade_stream = path_rng(42, label="trade_bootstrap", path_index=0)
    block_stream = path_rng(42, label="block_bootstrap:6", path_index=0)

    assert [trade_stream.random() for _ in range(5)] != [block_stream.random() for _ in range(5)]


def test_the_same_run_seed_reproduces_a_full_trade_bootstrap_report() -> None:
    trades = pseudo_trade_returns(80)

    first = run_trade_bootstrap(trades, path_count=20, run_seed=999, charge=lambda _index: None)
    second = run_trade_bootstrap(trades, path_count=20, run_seed=999, charge=lambda _index: None)

    assert first.paths == second.paths
    assert first.max_drawdown_p05 == second.max_drawdown_p05
    assert first.max_drawdown_p95 == second.max_drawdown_p95


def test_the_same_run_seed_reproduces_a_full_block_bootstrap_report() -> None:
    plan = BlockBootstrapPlan(
        returns=momentum_market_returns(block_total=30, block_length=6),
        block_length=6,
        path_count=20,
        run_seed=555,
    )

    first = run_block_bootstrap(plan, evaluate=momentum_edge, charge=lambda _index: None)
    second = run_block_bootstrap(plan, evaluate=momentum_edge, charge=lambda _index: None)

    assert first.paths == second.paths


def test_the_same_run_seed_reproduces_a_full_perturbation_report() -> None:
    first = run_perturbation(
        PERTURBATION_BASELINE, evaluate=plateau_evaluator, charge=lambda _index: None
    )
    second = run_perturbation(
        PERTURBATION_BASELINE, evaluate=plateau_evaluator, charge=lambda _index: None
    )

    assert first.points == second.points


@pytest.mark.property
@given(seed=st.integers(min_value=0, max_value=2**63 - 1), block_length=st.integers(1, 10))
def test_resample_blocks_always_returns_the_original_length(seed: int, block_length: int) -> None:
    returns = tuple(Decimal(value) / Decimal(100) for value in range(20))
    rng_first = path_rng(seed, label="property", path_index=0)
    rng_second = path_rng(seed, label="property", path_index=0)

    first = resample_blocks(returns, block_length=block_length, rng=rng_first)
    second = resample_blocks(returns, block_length=block_length, rng=rng_second)

    assert len(first) == len(returns)
    assert first == second
    assert all(value in returns for value in first)
