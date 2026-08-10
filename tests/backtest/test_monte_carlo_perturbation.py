"""A spike is rejected, a plateau survives, and every declared point charges the ledger.

The plateau and spike evaluators in `tests/backtest/montecarlo_support.py` are shaped so
the two verdicts do not depend on a numeric coincidence: the plateau curve retains
~99.98% of its baseline edge under a single-axis ±10% jitter and the spike curve retains
exactly 5%, against a 50% floor that sits nowhere near either number.

`test_a_perturbation_run_charges_the_ledger_like_any_other_run` is the direct statement
of issue #43's fifth acceptance criterion: `run_perturbation` charges through the same
injected `charge`-before-`evaluate` callable every other harness in this package uses --
`fking.backtest.cpcv.run_cpcv`, `fking.backtest.walkforward.run_walk_forward` -- so a
`spec_hash` registered against a declared grid is charged the same way regardless of
which harness produced the run. The durability of that charge against a real,
append-only `trial_ledger` is proven once, for the walk-forward harness, in
`tests/backtest/test_walk_forward_ledger_charges.py`; this file proves the contract this
harness offers is the identical one that test exercises against Postgres.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

import pytest

from fking.backtest.montecarlo import (
    DEFAULT_JITTER_FRACTION,
    MIN_RETAINED_EDGE_FRACTION,
    MonteCarloConfigError,
    PerturbationRefusedError,
    perturbation_grid,
    run_perturbation,
)
from tests.backtest.montecarlo_support import (
    PERTURBATION_BASELINE,
    plateau_evaluator,
    spike_evaluator,
)

pytestmark = [pytest.mark.unit]


def test_the_declared_grid_is_two_points_per_parameter() -> None:
    grid = perturbation_grid(PERTURBATION_BASELINE)

    assert len(grid) == 2 * len(PERTURBATION_BASELINE)
    jitters = {jitter for _name, jitter, _value in grid}
    assert jitters == {DEFAULT_JITTER_FRACTION, -DEFAULT_JITTER_FRACTION}


def test_an_empty_baseline_has_nothing_to_jitter() -> None:
    with pytest.raises(PerturbationRefusedError, match="nothing to jitter"):
        perturbation_grid({})


def test_a_plateau_strategy_survives_the_ten_percent_jitter() -> None:
    report = run_perturbation(
        PERTURBATION_BASELINE, evaluate=plateau_evaluator, charge=lambda _index: None
    )

    assert report.is_plateau
    assert not report.is_spike
    assert report.collapsed_points == ()
    for point in report.points:
        assert point.edge >= report.baseline_edge * MIN_RETAINED_EDGE_FRACTION


def test_a_spike_strategy_is_rejected_by_the_same_jitter() -> None:
    report = run_perturbation(
        PERTURBATION_BASELINE, evaluate=spike_evaluator, charge=lambda _index: None
    )

    assert report.is_spike
    assert not report.is_plateau
    # Every point collapses here, not merely one -- the spike evaluator is symmetric --
    # but `is_spike` only requires one, which the block-length assertion in the module
    # docstring's "any" clause is what the property test below isolates.
    assert len(report.collapsed_points) == len(report.points)


def test_one_collapsed_axis_is_enough_to_call_it_a_spike() -> None:
    """`is_spike` is `any`, not a mean -- a strategy fragile on one axis is a spike even
    if every other axis is a perfect plateau."""

    def mostly_flat_but_fragile_on_one_axis(params: Mapping[str, Decimal]) -> Decimal:
        if params["fast_period"] != PERTURBATION_BASELINE["fast_period"]:
            return report_baseline * Decimal("0.01")
        return report_baseline

    report_baseline = Decimal("1.00")
    report = run_perturbation(
        PERTURBATION_BASELINE,
        evaluate=mostly_flat_but_fragile_on_one_axis,
        charge=lambda _index: None,
    )

    assert report.is_spike
    collapsed_names = {point.parameter_name for point in report.collapsed_points}
    assert collapsed_names == {"fast_period"}


def test_a_non_positive_baseline_edge_is_refused_rather_than_scored() -> None:
    def losing_evaluator(_params: Mapping[str, Decimal]) -> Decimal:
        return Decimal("-0.5")

    with pytest.raises(PerturbationRefusedError, match="non-positive"):
        run_perturbation(
            PERTURBATION_BASELINE, evaluate=losing_evaluator, charge=lambda _index: None
        )


def test_a_negative_jitter_fraction_is_refused() -> None:
    with pytest.raises(MonteCarloConfigError, match="jitter_fraction"):
        run_perturbation(
            PERTURBATION_BASELINE,
            evaluate=plateau_evaluator,
            charge=lambda _index: None,
            jitter_fraction=Decimal("0"),
        )


class _ChargeLedger:
    """Records charges in call order -- the same recorder shape
    `tests/backtest/test_cpcv_trials.py` and `tests/backtest/test_monte_carlo_block_bootstrap.py`
    use, so the contract reads as the same contract everywhere it appears."""

    def __init__(self) -> None:
        self.charged_indices: list[int] = []

    def __call__(self, index: int) -> None:
        self.charged_indices.append(index)


def test_a_perturbation_run_charges_the_ledger_like_any_other_run() -> None:
    ledger = _ChargeLedger()

    report = run_perturbation(PERTURBATION_BASELINE, evaluate=plateau_evaluator, charge=ledger)

    expected_grid_size = 2 * len(PERTURBATION_BASELINE)
    assert report.trials_charged == expected_grid_size
    assert ledger.charged_indices == list(range(expected_grid_size))


def test_the_charge_fires_before_the_point_is_evaluated() -> None:
    ledger = _ChargeLedger()
    observed: list[tuple[int, int]] = []

    def evaluate(params: Mapping[str, Decimal]) -> Decimal:
        if params == PERTURBATION_BASELINE:
            return Decimal("1.00")
        observed.append((len(observed), len(ledger.charged_indices)))
        return plateau_evaluator(params)

    run_perturbation(PERTURBATION_BASELINE, evaluate=evaluate, charge=ledger)

    assert observed == [(index, index + 1) for index in range(len(observed))]


def test_a_point_that_crashes_keeps_its_charge_because_nothing_refunds_it() -> None:
    """A defect partway through a sweep does not un-charge the points already reached --
    the same property `tests/backtest/test_cpcv_trials.py` pins for CPCV paths, here for
    perturbation grid points."""
    ledger = _ChargeLedger()
    call_count = 0

    def evaluate(params: Mapping[str, Decimal]) -> Decimal:
        nonlocal call_count
        if params == PERTURBATION_BASELINE:
            return Decimal("1.00")
        call_count += 1
        if call_count == 1:
            raise RuntimeError("a defect in the evaluator, not a modelled failure")
        return plateau_evaluator(params)

    with pytest.raises(RuntimeError):
        run_perturbation(PERTURBATION_BASELINE, evaluate=evaluate, charge=ledger)

    # The point that crashed was already charged, and nothing refunds it.
    assert len(ledger.charged_indices) == 1
