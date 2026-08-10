"""The result schema's own failures.

Both classes are leaves of `BacktestError`, for the reason every sibling `_errors.py` in
`fking.backtest` gives: a caller that wanted "any failure this codebase raises on
purpose" must catch these too, and `platform` gets no trading vocabulary
(`docs/rules/module-boundaries.md`).

`CredibilityInvariantError` is deliberately not a `ValueError` raised from inside a
`@model_validator`, even though pydantic would happily collect one into the surrounding
`ValidationError`. The distinction `fking.backtest.costs.CalibrationProvenanceError`
draws applies here with the same force: a `credibility` field that disagrees with what
the audit battery computes is not one field failing a bound among several independent
ones, it is the whole result failing to be what it claims to be, and it should not read
as one line in a list of unrelated field errors.
"""

from __future__ import annotations

from fking.backtest._errors import BacktestError


class BacktestResultError(BacktestError):
    """Base for every failure the result schema and audit battery raise deliberately."""


class CredibilityInvariantError(BacktestResultError):
    """A `BacktestResult` claims a `credibility` the audit battery does not support.

    Raised whether the claim is too generous (`credible` when a check failed, the sample
    is thin, or the cost model names testnet) or too stingy (`not_credible` claimed while
    the battery is actually incomplete, which is `unaudited`, a different and weaker
    statement). Both are refused, because a caller allowed to under-claim would use it as
    a way to avoid running `assess_credibility` at all.
    """
