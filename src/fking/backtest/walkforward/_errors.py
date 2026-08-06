"""The walk-forward harness's own failures, all of them terminal.

Every class here is a leaf of `BacktestError`, so a caller that wanted "any failure the
engine raises on purpose" catches these too. None of them is recoverable: a schedule
that could not be built, or a fold whose windows overlap, produces a validation result
that is indistinguishable from a correct one and would be passed to the promotion gate
as evidence.

`FoldEvaluationError` is the one exception to "terminal", and only in a precise sense:
it terminates the *fold*, not the run. `run_walk_forward` records it as a consumed trial
and continues, because a walk-forward that silently drops a crashed fold reports a
smaller, cleaner fold set than the one it actually attempted -- and a fold count that
only counts successes is a fold count designed to flatter, in exactly the way
`BACKTEST_ENGINE.md` section 8 says a trial count must not be.
"""

from __future__ import annotations

from fking.backtest._errors import BacktestError


class WalkForwardError(BacktestError):
    """Base for every failure the walk-forward harness raises deliberately."""


class WalkForwardConfigError(WalkForwardError):
    """A plan or a declaration is malformed, so no schedule can be built from it.

    Raised at construction rather than at the first fold. A plan is the thing a
    validation result cites; a partly-valid plan produces a fold table describing a
    validation that could never have run.
    """


class ScheduleRefusedError(WalkForwardError):
    """The window admits no walk-forward schedule worth reporting.

    Two shapes. The window is too short to hold even one training window plus one test
    window after purge and embargo -- in which case there is nothing to run. Or it holds
    exactly one fold, which is a single train/test split: one draw, with one boundary,
    chosen by somebody who had already seen the data. That is not evidence
    (`BACKTEST_ENGINE.md` section 6), and returning it under the name "walk-forward"
    would launder it into some.

    Not clamped, not padded, not softened into a warning. The correct response is a
    longer window, a shorter step, or an honest single-window backtest reported as one.
    """


class FoldEvaluationError(WalkForwardError):
    """One fold could not be evaluated.

    Raised by the caller's evaluator, caught by `run_walk_forward`, and recorded in
    `folds_failed` with its reason. The trial is charged either way: the configuration
    was specified and the data was reached for, and `.claude/rules/overfitting-defences.md`
    charges at specification time precisely so that abandoning a run cannot make it free.
    """


class DecayReportError(WalkForwardError):
    """Out-of-sample decay cannot be estimated from the observations supplied.

    Includes the case that matters most: every observation sits at the same distance
    from its training window, so the slope is a division by zero. Reporting zero there
    would read as "measured, and flat", which is the opposite of "not measured".
    """
