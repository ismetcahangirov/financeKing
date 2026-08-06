"""The gate: two independent refusals, both applied literally.

The two statistics answer different questions and neither substitutes for the other. The
deflated Sharpe asks whether *this* result exceeds what the search alone would have
produced. PBO asks whether the search's ranking carries any information at all. A
candidate can pass one and fail the other, so both are evaluated and every refusal is
reported -- stopping at the first would hide half the diagnosis, and the PBO half is the
one that implicates work beyond this candidate.

Thresholds are applied literally. A deflated Sharpe of 0.9499 failed. A PBO of 0.3001
failed. `.claude/rules/overfitting-defences.md` states the decision rule is written down
before the data is touched and applied exactly; a gate that rounds in the candidate's
favour is a gate with one configurable digit, and that digit is adjusted by whoever is in
a hurry.

What is deliberately absent: the parameter-count tightening. Each free parameter past the
second halves the residual noise budget, and that scaling lives in `evolution.promotion`
where the parameter count is known and where capital is actually committed. Duplicating
it here would mean two floors that drift apart, and the looser one would win by being the
one the engine printed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from fking.backtest.validation._deflated import (
    SharpeEvidence,
    deflated_sharpe_ratio,
    expected_max_sharpe,
)
from fking.backtest.validation._pbo import (
    OverfittingProbability,
    PathSplit,
    probability_of_backtest_overfitting,
)

# `.claude/rules/overfitting-defences.md`, promotion thresholds: 0.95 is the base floor
# before the parameter-count penalty. Not a tunable -- lowering it is the failure this
# gate exists to prevent, and raising it here would silently diverge from the promotion
# gate that reads the same number.
MIN_DEFLATED_SHARPE: Final = Decimal("0.95")

# `BACKTEST_ENGINE.md` section 6.6: PBO above 0.30 fails validation regardless of how
# good the mean looks.
MAX_PROBABILITY_OF_BACKTEST_OVERFITTING: Final = Decimal("0.30")


class ValidationRefusal(StrEnum):
    """Why the gate said no. Both may be present at once."""

    DEFLATED_SHARPE_BELOW_FLOOR = "deflated_sharpe_below_floor"
    OVERFITTING_PROBABILITY_ABOVE_CEILING = "overfitting_probability_above_ceiling"


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """One candidate's overfitting verdict, with the inputs that produced it.

    `selection_benchmark_sharpe` is carried alongside the deflated figure on purpose: it
    is the number that says *how much* of the observed Sharpe the search already explains,
    and a reader who sees only the deflated probability cannot recover it.
    """

    trials_at_time_of_run: int
    observed_sharpe: Decimal
    selection_benchmark_sharpe: Decimal
    deflated_sharpe: Decimal
    overfitting: OverfittingProbability
    refusals: tuple[ValidationRefusal, ...]

    @property
    def is_evidence(self) -> bool:
        """Whether this run may be used to argue anything about the strategy."""
        return not self.refusals

    @property
    def indicts_the_search(self) -> bool:
        """Whether the finding is about the search rather than about this candidate.

        A PBO refusal says the selection procedure ranked noise, which implicates every
        other candidate that procedure produced -- including ones already promoted. That
        escalates; a deflated-Sharpe refusal alone does not.
        """
        return ValidationRefusal.OVERFITTING_PROBABILITY_ABOVE_CEILING in self.refusals


def assess_validation(evidence: SharpeEvidence, splits: Sequence[PathSplit]) -> ValidationReport:
    """Deflate the Sharpe, measure the search, and refuse on either."""
    overfitting = probability_of_backtest_overfitting(splits)
    deflated_sharpe = deflated_sharpe_ratio(evidence)
    benchmark = expected_max_sharpe(
        evidence.trials_at_time_of_run, evidence.sharpe_variance_across_trials
    )

    refusals: list[ValidationRefusal] = []
    if deflated_sharpe < MIN_DEFLATED_SHARPE:
        refusals.append(ValidationRefusal.DEFLATED_SHARPE_BELOW_FLOOR)
    if overfitting.probability_of_backtest_overfitting > MAX_PROBABILITY_OF_BACKTEST_OVERFITTING:
        refusals.append(ValidationRefusal.OVERFITTING_PROBABILITY_ABOVE_CEILING)

    return ValidationReport(
        trials_at_time_of_run=evidence.trials_at_time_of_run,
        observed_sharpe=evidence.observed_sharpe,
        selection_benchmark_sharpe=benchmark,
        deflated_sharpe=deflated_sharpe,
        overfitting=overfitting,
        refusals=tuple(refusals),
    )
