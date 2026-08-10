"""The tearsheet's own failures.

Leaves of `BacktestError`, for the reason every sibling `_errors.py` in `fking.backtest`
gives: a caller that wanted "any failure this codebase raises on purpose" must catch
these too.

`TearsheetRegenerationError` is separate from `TearsheetInputError` because the two say
opposite things about where the fault is. A bad input is the caller's; a stored tearsheet
whose bytes disagree with what this code renders today is either a determinism failure in
the renderer or an attempt to regenerate an old run against current code -- both of which
issue #45 exists to make impossible, and neither of which is a field failing a bound.
"""

from __future__ import annotations

from fking.backtest._errors import BacktestError


class TearsheetError(BacktestError):
    """Base for every failure the tearsheet renderer raises deliberately."""


class TearsheetInputError(TearsheetError):
    """An input the tearsheet cannot honestly render.

    A blank engine SHA, a coverage list that names no series, a held-out window that
    ends before it starts. Every one of these would otherwise reach the document as an
    empty table cell, and an empty provenance cell reads as "nothing to report" rather
    than "nobody supplied it".
    """


class TearsheetRegenerationError(TearsheetError):
    """A tearsheet already exists for this run and its bytes differ from this render.

    The artefact is the record of the run (`issue #45`). Overwriting it would replace a
    document about that run with a document about today's metric definitions, today's
    cost model and today's regime labeller, silently applied to an old result.
    """
