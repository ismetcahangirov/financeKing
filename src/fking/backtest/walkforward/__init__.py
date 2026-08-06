"""Rolling re-fit validation, in anchored and rolling form, reporting decay not a mean.

A single-window backtest is one draw with one boundary, and the boundary was chosen by
somebody who had already seen the data. This package is where "a single train/test split
is not evidence" stops being rhetoric: `build_schedule` refuses to emit a one-fold
schedule under the name walk-forward, and `WalkForwardReport` keeps the fold structure
attached to the numbers it produced.

Three things are worth knowing before using it.

**The scheme is a consequence of the strategy's re-fit story, not a preference.**
`step` must mirror the cadence production would actually re-fit at. A harness that
re-fits monthly against a strategy that would re-fit quarterly has produced a number that
is not so much wrong as about a different system. Strategies with fixed parameters want
combinatorial purged cross-validation instead, which uses the data more efficiently.

**Purge and embargo come from `WalkForwardDeclaration`, which restates horizons the
feature and the strategy already declared.** They are not arguments to `build_schedule`
and there is no way to shorten them from a config file. The direction they would be
tuned is shorter, and a shorter embargo raises every fold's Sharpe by letting adjacent
folds share information -- an improvement in the number and not in the strategy.

**The output that matters is the slope, not the mean.** `DecayReport.sharpe_per_30_days`
is performance as a function of distance from the fit window. A strategy at -0.09 there
is short-lived rather than broken: its usable life is weeks, and either its re-fit cadence
says so or it is not promoted. Measuring it requires scoring sub-windows of each test
period -- every fold sits at one identical distance from its own fit -- which is what
`segment_windows` partitions and `decay_report` consumes.

Everything not in `__all__` is private and may change without notice.
"""

from fking.backtest.walkforward._decay import (
    DECAY_REPORT_DAYS,
    DecayPoint,
    DecayReport,
    OutOfSampleObservation,
    decay_points,
    decay_report,
    fit_decay,
)
from fking.backtest.walkforward._errors import (
    DecayReportError,
    FoldEvaluationError,
    ScheduleRefusedError,
    WalkForwardConfigError,
    WalkForwardError,
)
from fking.backtest.walkforward._harness import (
    FoldEvaluator,
    FoldFailure,
    TrialCharge,
    WalkForwardReport,
    run_walk_forward,
)
from fking.backtest.walkforward._schedule import (
    MINIMUM_FOLDS,
    Fold,
    WalkForwardDeclaration,
    WalkForwardPlan,
    WalkForwardSchedule,
    WalkForwardScheme,
    build_schedule,
    fold_by_index,
    schedule_table,
    segment_windows,
)

__all__ = [
    "DECAY_REPORT_DAYS",
    "MINIMUM_FOLDS",
    "DecayPoint",
    "DecayReport",
    "DecayReportError",
    "Fold",
    "FoldEvaluationError",
    "FoldEvaluator",
    "FoldFailure",
    "OutOfSampleObservation",
    "ScheduleRefusedError",
    "TrialCharge",
    "WalkForwardConfigError",
    "WalkForwardDeclaration",
    "WalkForwardError",
    "WalkForwardPlan",
    "WalkForwardReport",
    "WalkForwardSchedule",
    "WalkForwardScheme",
    "build_schedule",
    "decay_points",
    "decay_report",
    "fit_decay",
    "fold_by_index",
    "run_walk_forward",
    "schedule_table",
    "segment_windows",
]
