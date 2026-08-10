"""Combinatorial purged cross-validation: `C(N, k)` paths, two gaps, one distribution.

Standard k-fold on a financial time series is not conservative-but-fine. It **leaks by
default**, and it produces the cleanest-looking wrong answers in the project -- which is
the worst available failure profile, because a clean-looking answer is the one nobody
investigates.

Four things are worth knowing before using this package.

**Purging and the embargo are not two halves of one symmetric gap.** Purging removes
training samples whose label horizon overlaps the test window: a label that resolved
inside the test window was partly determined by test-period information, and training on
it is training on the answer. The embargo removes training samples that *begin after* the
test window, which is the half people skip, and the reasoning is different -- serial
correlation leaks backwards. A sample starting shortly after the test period is computed
from features whose lookback still reaches into it, so without an embargo the model
trains on the test period filtered through a feature lookback.

**The embargo floor is `max_feature_lookback + max_holding_horizon`, and it is checked
rather than clamped.** A four-hour lookback and a six-hour maximum hold need ten hours;
`CpcvPlan` refuses anything shorter at construction. The uncomfortable part is that
`FeatureSpec.lookback` is taken on trust -- this layer cannot verify it -- so an
understated lookback silently shortens the embargo, lets adjacent paths share information,
and produces a stable edge that is partly the same data seen twice. That is why the two
gaps appear in three places: the plan, the result schema, and `cpcv_log_line`.

**The distribution is the finding.** A mean Sharpe of 1.1 assembled from paths ranging
-0.9 to 3.0 is a strategy with no stable edge. `PathDistribution` reports the tails, the
spread and the fraction of paths that were positive, keeps every path's trade count, and
flags a suspiciously *small* spread as a defect signal rather than a triumph.

**Every path is a trial.** `N=8, k=2` is 28 paths and 28 permanent charges against the
global counter, registered as each path is reached rather than batched at the end.

Everything not in `__all__` is private and may change without notice.
"""

from fking.backtest.cpcv._distribution import (
    MINIMUM_PATH_TRADES,
    SUSPICIOUS_SHARPE_SPREAD,
    DistributionRefusedError,
    PathDistribution,
    PathPerformance,
    path_distribution,
)
from fking.backtest.cpcv._errors import (
    CpcvConfigError,
    CpcvError,
    CpcvPartitionError,
    CpcvPathEvaluationError,
)
from fking.backtest.cpcv._harness import (
    CpcvReport,
    PathEvaluator,
    PathFailure,
    TrialCharge,
    run_cpcv,
)
from fking.backtest.cpcv._partition import (
    MINIMUM_GROUPS,
    CpcvPartition,
    CpcvPlan,
    CpcvSplit,
    Group,
    TimeInterval,
    build_groups,
    build_splits,
    merge_adjacent,
    purged_train_intervals,
)
from fking.backtest.cpcv._report import cpcv_log_line, cpcv_path_table

__all__: tuple[str, ...] = (
    "MINIMUM_GROUPS",
    "MINIMUM_PATH_TRADES",
    "SUSPICIOUS_SHARPE_SPREAD",
    "CpcvConfigError",
    "CpcvError",
    "CpcvPartition",
    "CpcvPartitionError",
    "CpcvPathEvaluationError",
    "CpcvPlan",
    "CpcvReport",
    "CpcvSplit",
    "DistributionRefusedError",
    "Group",
    "PathDistribution",
    "PathEvaluator",
    "PathFailure",
    "PathPerformance",
    "TimeInterval",
    "TrialCharge",
    "build_groups",
    "build_splits",
    "cpcv_log_line",
    "cpcv_path_table",
    "merge_adjacent",
    "path_distribution",
    "purged_train_intervals",
    "run_cpcv",
)
