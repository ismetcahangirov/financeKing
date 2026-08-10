"""The cross-validation harness's own failures, all of them terminal.

Leaves of `BacktestError`, for the reason `fking.backtest._errors` gives: a caller that
wanted "any failure the engine raises on purpose" must catch these too.

They are not `WalkForwardError` subclasses even though the two harnesses share
`WalkForwardDeclaration`. A caller catching `WalkForwardError` is catching "the rolling
re-fit schedule refused", and a combinatorial partition refusing to emit a split is a
different condition about a different object. Reusing the class would make the one
sentence a reader gets -- the exception's name -- describe a harness that never ran.

`CpcvPathEvaluationError` is the only one that does not terminate the run, and only in
the precise sense that it terminates the *path*: `run_cpcv` records it, keeps its trial
charge, and continues. A path silently dropped would shrink the path count to the paths
that worked, and every statistic computed over that count would inherit the conditioning
(`BACKTEST_ENGINE.md` section 9).
"""

from __future__ import annotations

from fking.backtest._errors import BacktestError


class CpcvError(BacktestError):
    """Base for every failure the combinatorial cross-validation harness raises."""


class CpcvConfigError(CpcvError):
    """A plan, a group boundary or a path result is malformed.

    Raised at construction rather than at the first path. The embargo below its floor is
    the case this exists for: clamping it up to the floor would produce a validation that
    ran with a gap nobody chose, and reporting the requested value alongside would make
    the plan and the run disagree about the number most often silently wrong.
    """


class CpcvPartitionError(CpcvError):
    """A split's training ranges are not separated from its test ranges.

    A hard failure, never a warning. A warning about an overlapping train and test range
    is a warning attached to a result that still gets reported, and the result reads as a
    clean edge because the model was fitted on the answer. There is no degraded mode in
    which a leaking split is worth scoring.
    """


class CpcvPathEvaluationError(CpcvError):
    """One path could not be evaluated.

    Raised by the caller's evaluator, caught by `run_cpcv`, recorded with its reason. The
    trial is charged either way: the split was specified and the data was reached for, and
    `docs/rules/overfitting-defences.md` charges at specification time precisely so that
    abandoning a path cannot make it free.
    """
