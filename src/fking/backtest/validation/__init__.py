"""The overfitting gate: a Sharpe discounted by the search, and a verdict on the search.

Two statistics, two different questions, and neither one substitutes for the other.

**The deflated Sharpe** turns an observed Sharpe into a statement that accounts for how
many configurations were tried to find it. The correction is easy to under-apply because
it grows so slowly: `SR*` scales as `sqrt(2 ln K)`, so a hundredfold increase in search
effort raises the noise threshold by only about 41%. That feels survivable, which is
precisely why the trial counter is allowed to drift -- and it never comes back down, and
it applies to every strategy in the population at once. Skew and kurtosis sit in the
denominator, so a strategy that earns steadily and loses catastrophically is discounted
by the statistics for the same reason it is objectionable economically.

**PBO** measures the search rather than the candidate: the fraction of CPCV path-splits
where the in-sample winner underperforms the out-of-sample median. Above 0.30 fails
regardless of how good the mean looks, and a failure escalates -- `ValidationReport`
exposes that as `indicts_the_search`, because the finding implicates every candidate the
same procedure produced.

The trial count is a required field on `SharpeEvidence` with no default, so no Sharpe in
this package can be expressed without it (`BACKTEST_ENGINE.md` section 6.5).

`float` is used for the statistics under the exception in
`.claude/rules/decimal-and-money.md`, bounded to two named conversion boundaries -- one in
`_deflated`, one in `_pbo`. Every value this package publishes is `Decimal`, and the
equity curve these statistics are computed from never becomes one.

Everything not in `__all__` is private and may change without notice.
"""

from fking.backtest.validation._deflated import (
    SharpeEvidence,
    deflated_sharpe_ratio,
    expected_max_sharpe,
)
from fking.backtest.validation._errors import (
    SharpeEvidenceUnusableError,
    TrialCountUnavailableError,
    ValidationGateError,
)
from fking.backtest.validation._gate import (
    MAX_PROBABILITY_OF_BACKTEST_OVERFITTING,
    MIN_DEFLATED_SHARPE,
    ValidationRefusal,
    ValidationReport,
    assess_validation,
)
from fking.backtest.validation._pbo import (
    OverfittingProbability,
    PathSplit,
    PathSplitMalformedError,
    probability_of_backtest_overfitting,
)

__all__: tuple[str, ...] = (
    "MAX_PROBABILITY_OF_BACKTEST_OVERFITTING",
    "MIN_DEFLATED_SHARPE",
    "OverfittingProbability",
    "PathSplit",
    "PathSplitMalformedError",
    "SharpeEvidence",
    "SharpeEvidenceUnusableError",
    "TrialCountUnavailableError",
    "ValidationGateError",
    "ValidationRefusal",
    "ValidationReport",
    "assess_validation",
    "deflated_sharpe_ratio",
    "expected_max_sharpe",
    "probability_of_backtest_overfitting",
)
