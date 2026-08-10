"""Parameter perturbation: is this a plateau or a spike?

The most informative Monte Carlo diagnostic and the one run least, because it is cheap in
wall-clock time and expensive in the currency the trial ledger measures. A strategy whose
performance collapses under a ten-percent jitter on one of its own parameters has found a
spike in the fitness landscape, which is the geometric signature of overfitting; a robust
strategy sits on a plateau (`BACKTEST_ENGINE.md` section 6.4).

**The grid is declared before any evaluation, and every declared point is charged.**
`perturbation_grid` nudges one parameter at a time, in both directions, holding every
other parameter at its baseline value -- `2 * n_parameters` configurations. Each is a
distinct configuration evaluated against the same context, and distinct configurations
charge the trial ledger exactly like any other run
(`docs/rules/overfitting-defences.md`): the charge happens per point, before that point is
evaluated, matching the contract `fking.backtest.cpcv.run_cpcv` and
`fking.backtest.walkforward.run_walk_forward` already use, so a perturbation sweep
abandoned after the first collapse still pays for the points it never ran.

**One collapse is a spike.** `PerturbationReport.is_spike` is true if *any* perturbed
point falls below `MIN_RETAINED_EDGE_FRACTION` of the baseline edge -- not the mean across
points. Averaging across axes would let a strategy that is fragile on one parameter and
flat on the rest read as robust on the mean, which is precisely the shape a spike takes
when a search has locked onto one dimension.

The one-axis-at-a-time grid, not a combinatorial jitter across every axis, is a deliberate
scope decision: a full combinatorial sweep answers a different, more expensive question
this issue does not ask for, and it would silently inflate the declared grid -- and
therefore the trial charge -- without a reason stated anywhere a reviewer would see it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from fking.backtest.montecarlo._errors import MonteCarloConfigError, PerturbationRefusedError

#: Scores one parameter set and returns the edge it produced (a Sharpe or similar).
#: Injected so the harness never special-cases a strategy's own evaluation code.
PerturbationEvaluator = Callable[[Mapping[str, Decimal]], Decimal]

#: Charges one trial for one declared grid point. Called before that point is evaluated,
#: once per point, always -- the same contract every other harness in this package uses.
TrialCharge = Callable[[int], None]

# `BACKTEST_ENGINE.md` section 6.4: jitter every parameter ±10%. Fixed here rather than
# left to the call site, because the decision rule is written down before the data is
# touched (`docs/rules/overfitting-defences.md`) -- widening it after seeing which
# strategies fail is the post-hoc adjustment that rule forbids.
DEFAULT_JITTER_FRACTION: Final = Decimal("0.10")

# A perturbed point below half the baseline edge counts as a collapse. Fixed once, here,
# so nobody tunes it per-strategy after seeing which strategies fail it.
MIN_RETAINED_EDGE_FRACTION: Final = Decimal("0.50")

_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")


@dataclass(frozen=True, slots=True)
class PerturbedPoint:
    """One declared grid point: one parameter nudged in one direction, and its edge."""

    parameter_name: str
    jitter_fraction: Decimal
    perturbed_value: Decimal
    edge: Decimal

    def is_collapsed(self, *, baseline_edge: Decimal) -> bool:
        """Whether this point fell below the retained-edge floor of the baseline."""
        return self.edge < baseline_edge * MIN_RETAINED_EDGE_FRACTION


@dataclass(frozen=True, slots=True)
class PerturbationReport:
    """The plateau/spike verdict for one baseline parameter set.

    `trials_charged` is asserted equal to `len(points)`, matching every other harness in
    this package: a declared grid and a charged grid drifting apart is the bug this
    report exists to make impossible.
    """

    baseline_edge: Decimal
    jitter_fraction: Decimal
    points: tuple[PerturbedPoint, ...]
    trials_charged: int

    def __post_init__(self) -> None:
        if self.trials_charged != len(self.points):
            raise MonteCarloConfigError(
                f"{self.trials_charged} trials charged against {len(self.points)} "
                f"declared grid points; every point is charged once"
            )

    @property
    def collapsed_points(self) -> tuple[PerturbedPoint, ...]:
        return tuple(
            point for point in self.points if point.is_collapsed(baseline_edge=self.baseline_edge)
        )

    @property
    def is_spike(self) -> bool:
        """Whether any single perturbed point collapsed -- the geometric signature of
        overfitting. See the module docstring for why this is `any`, not a mean."""
        return len(self.collapsed_points) > 0

    @property
    def is_plateau(self) -> bool:
        return not self.is_spike


def perturbation_grid(
    baseline: Mapping[str, Decimal], *, jitter_fraction: Decimal = DEFAULT_JITTER_FRACTION
) -> tuple[tuple[str, Decimal, Decimal], ...]:
    """Declare the full `2 * n_parameters` grid, before any evaluation.

    Each entry is `(parameter_name, jitter_fraction, perturbed_value)`. This is what gets
    charged -- `run_perturbation` charges every entry this function returns, whether or
    not the sweep is later abandoned.
    """
    if not baseline:
        raise PerturbationRefusedError(
            "a perturbation grid over zero parameters has nothing to jitter, and cannot "
            "distinguish a plateau from an untested strategy"
        )
    if jitter_fraction <= _ZERO:
        raise MonteCarloConfigError(f"jitter_fraction must be positive; got {jitter_fraction}")

    grid: list[tuple[str, Decimal, Decimal]] = []
    for parameter_name in sorted(baseline):
        baseline_value = baseline[parameter_name]
        for signed_jitter in (jitter_fraction, -jitter_fraction):
            perturbed_value = baseline_value * (_ONE + signed_jitter)
            grid.append((parameter_name, signed_jitter, perturbed_value))
    return tuple(grid)


def run_perturbation(
    baseline: Mapping[str, Decimal],
    *,
    evaluate: PerturbationEvaluator,
    charge: TrialCharge,
    jitter_fraction: Decimal = DEFAULT_JITTER_FRACTION,
) -> PerturbationReport:
    """Evaluate the baseline, then charge and evaluate every declared grid point in order.

    The baseline evaluation itself is not charged here: it is the run under test, already
    charged through its own registration -- this harness charges only the perturbed
    points it declares.
    """
    grid = perturbation_grid(baseline, jitter_fraction=jitter_fraction)
    baseline_edge = evaluate(baseline)
    if baseline_edge <= _ZERO:
        raise PerturbationRefusedError(
            f"baseline edge {baseline_edge} is non-positive; a plateau/spike verdict "
            f"needs a positive edge to retain a fraction of"
        )

    points: list[PerturbedPoint] = []
    for index, (parameter_name, signed_jitter, perturbed_value) in enumerate(grid):
        # Before the evaluator, unconditionally. A charge taken afterwards is a charge a
        # crash avoids, and a crash is the case this ordering exists for.
        charge(index)
        perturbed_params = dict(baseline)
        perturbed_params[parameter_name] = perturbed_value
        edge = evaluate(perturbed_params)
        points.append(
            PerturbedPoint(
                parameter_name=parameter_name,
                jitter_fraction=signed_jitter,
                perturbed_value=perturbed_value,
                edge=edge,
            )
        )

    return PerturbationReport(
        baseline_edge=baseline_edge,
        jitter_fraction=jitter_fraction,
        points=tuple(points),
        trials_charged=len(points),
    )
