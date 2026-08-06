"""The cost model's own failures.

Three classes, and the split between them is the point. `CalibrationProvenanceError`
names a condition under which every number the model would produce is fiction;
`CostModelConfigError` names a model that was asked to charge something it was never
calibrated for. Both are terminal. Neither is a `ValueError`, and that is deliberate --
see the note on `CalibrationProvenanceError`.

They descend from `BacktestError` rather than from `fking.platform.errors` directly,
because a caller that wanted "any failure the backtest engine raises on purpose" must
catch these too, and because `platform` gets no trading vocabulary
(`.claude/rules/module-boundaries.md`).
"""

from __future__ import annotations

from fking.backtest._errors import BacktestError


class CostModelError(BacktestError):
    """Base for every failure the cost model raises deliberately."""


class CalibrationProvenanceError(CostModelError):
    """A cost parameter was sourced from somewhere that cannot describe production.

    Today that means testnet. Binance USDⓈ-M futures testnet showed a median BTCUSDT
    spread of 7.5 bp against production's 0.16 bp -- a factor of 47 -- with roughly 10x
    inflated volume (`.claude/contexts/binance-testnet.md`, fact 6).

    The instinct is that a 47x overstated spread is *conservative*, and that instinct is
    exactly what gets the rule relaxed. It is wrong twice over. Testnet is pessimistic on
    spread and simultaneously optimistic on fill probability and capacity, so the errors
    do not point the same way. And the failure that has actually occurred in this
    project's design notes is the inverted config -- `7.5` entered as `0.075` bp,
    producing a model roughly 2x *cheaper* than production and making every strategy look
    brilliant. Provenance is disqualifying regardless of direction, because direction is
    not a property you can read off the result.

    **Deliberately not a `ValueError`.** Pydantic v2 collects `ValueError` and
    `AssertionError` raised inside a validator into a `ValidationError`; every other
    exception propagates out of `Model(...)` unchanged. Keeping this class outside that
    pair means a testnet-sourced `CostModel` fails construction with the name of the
    thing that is wrong, rather than as one entry in a list of field errors that a caller
    may summarise into a log line.
    """


class CostModelConfigError(CostModelError):
    """The model cannot charge what it was asked to charge.

    Raised when a symbol has no calibrated spread or depth profile, or when a run
    produced no fillable trade at all. Never defaulted: substituting a neighbouring
    symbol's spread, or a portfolio-wide median, produces a number that looks like a cost
    and describes a different instrument.
    """
