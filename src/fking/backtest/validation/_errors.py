"""What the overfitting gate refuses, and why each refusal is terminal.

Both classes are leaves of `BacktestError`, so a caller that wanted "any failure this
engine raises on purpose" catches them too. Neither is recoverable in-process: a gate
that continued past one of them would still print a deflated Sharpe, and that number
would be indistinguishable from a correct one.
"""

from __future__ import annotations

from fking.backtest._errors import BacktestError


class ValidationGateError(BacktestError):
    """Base for the overfitting gate's own failures."""


class TrialCountUnavailableError(ValidationGateError):
    """The number of trials behind an observed Sharpe is unknown or non-positive.

    Zero is the dangerous value and is why this class exists rather than a clamp. A
    ledger that is empty, unreachable, or misread returns zero, and zero read as "no
    selection took place" deflates by nothing at all -- which reports a search over ten
    thousand configurations as though it had survived a coin flip
    (`docs/rules/overfitting-defences.md`). An unreadable ledger is a refusal, never
    a benchmark of zero.
    """


class SharpeEvidenceUnusableError(ValidationGateError):
    """The moment inputs cannot produce a deflated Sharpe at all.

    The deflation denominator is ``1 - skewness*SR + (kurtosis-1)/4 * SR^2``. Skew and
    kurtosis are sample estimates, so nothing stops a caller supplying a combination
    that drives it to zero or below -- at which point the statistic is not a weak
    result but an undefined one. Reporting a number derived from a non-positive
    variance would place a value on the same axis as a valid deflated Sharpe and let a
    reader compare them.
    """
