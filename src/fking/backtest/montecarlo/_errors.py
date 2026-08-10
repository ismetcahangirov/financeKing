"""What the Monte Carlo package refuses, and why each refusal is terminal.

Every class here is a leaf of `BacktestError`, so a caller that wanted "any failure this
codebase raises on purpose" catches these too. None is recoverable in-process: a
resampling that continued past a malformed input would still print a distribution, and
the distribution would be indistinguishable from one computed over real data.
"""

from __future__ import annotations

from fking.backtest._errors import BacktestError


class MonteCarloError(BacktestError):
    """Base for every error this package raises deliberately."""


class MonteCarloConfigError(MonteCarloError):
    """A resampling was configured with inputs that cannot produce a result.

    Raised at construction or at the first call that needs the malformed input, never
    downstream of a silent coercion -- a block length longer than the series it resamples
    or a path count of zero are refusals, not values clamped to something plausible.
    """


class ResamplingRefusedError(MonteCarloError):
    """Too few observations survived to support a resampled distribution.

    A bootstrap over fewer trades than `MIN_TRADES_FOR_BOOTSTRAP` is not weak evidence --
    it is not evidence, in either direction, for the same reason `BACKTEST_ENGINE.md`
    section 6.7 refuses a strategy below the minimum credible trade count.
    """


class PerturbationRefusedError(MonteCarloError):
    """A perturbation grid could not be evaluated as configured.

    Covers a baseline whose own edge is non-positive -- "retains half its edge" is not a
    statement a non-positive baseline can make -- and an empty parameter grid, which would
    otherwise report a strategy with zero free parameters as a plateau by default rather
    than as untested.
    """
