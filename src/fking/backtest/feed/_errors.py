"""The feed's own refusals. Every one of them ends the run rather than repairing it.

The shape is the same argument the rest of the engine makes, applied to data rather than
to ordering: each condition below has a repair that produces a number, and the number is
indistinguishable from a correct one. A gap can be filled, an unresolvable epoch unit can
be divided by whichever divisor the neighbouring month used, and a warm-up bar can be
handed to a strategy "just to get it started". All three produce an equity curve and no
error, which is why all three raise here instead.

They are leaves of `BacktestError` rather than of `fking.platform.errors.DataIntegrityError`
even where the fault is in the corpus, because the decision being recorded is the engine's:
the corpus is what it is, and what raises is this module's refusal to serve a run from it.
`docs/rules/module-boundaries.md` puts the vocabulary in the layer that owns the
decision.
"""

from __future__ import annotations

from fking.backtest._errors import BacktestError


class FeedError(BacktestError):
    """Base for every refusal the market-data feed raises."""


class FeedRequestError(FeedError):
    """The window, interval, warm-up or symbol set asked for cannot be served as stated.

    Raised at construction of the request rather than at the first read, for the reason
    `RunConfigError` is: a partly-valid request produces coverage for a window nobody
    asked for, and the report then answers a different question from the one in the
    config file.
    """


class CoverageRefusedError(FeedError):
    """The window contains bars the corpus does not hold, so the run is refused.

    Carries the rendered coverage report -- per symbol, with the gap ranges -- because the
    caller's next action is either to narrow the window or to backfill, and both need the
    ranges rather than a count.

    The alternative is to interpolate, and the reason that is never done is worth stating
    where the refusal lives: a forward-filled or linearly interpolated bar is a price that
    existed nowhere, at a timestamp at which nobody could have traded. A breakout or gap
    strategy trades into it and is filled at it, and the phantom move is systematically
    *favourable*, because interpolation is smooth and real markets are not. A refused run
    costs an afternoon; a run built on 0.4% invented bars costs whatever is promoted from
    it, because it looks fine (`BACKTEST_ENGINE.md` section 9).
    """


class AmbiguousEpochUnitError(FeedError):
    """A partition's epoch unit cannot be resolved from a declared `(market, date)` format.

    Never defaulted. Binance spot archives became microsecond epochs on 2025-01-01 while
    USDⓈ-M futures stayed on milliseconds, so a divisor picked from the neighbouring month
    or the other corpus is wrong by a factor of a thousand -- which places a 2026 bar in
    1970 or in the year 56,000 for one leg of a mixed run while the other leg is correct.
    `fking.data.format_resolver` is the one table that answers this, and a combination it
    does not declare is an escalation rather than a guess.
    """


class CorpusIntegrityError(FeedError):
    """A partition holds something the canonical schema says it cannot.

    A duplicated open time, a bar off the interval lattice, or a column whose Python type
    is not the one `fking.data.parquet.schema` declares. Each of these reads back without
    complaint and changes a count, a rolling window or a price by an amount no downstream
    assertion is watching for.
    """


class WarmupLeakError(FeedError):
    """Something other than a bar was dispatched during the warm-up phase.

    Warm-up bars advance features and reach no strategy, so nothing during warm-up can
    emit a signal, and therefore nothing can acknowledge, fill or reject an order at a
    warm-up instant. An event that is not market data before the exposure boundary means
    a decision was taken from a partially-filled lookback -- values no live run would ever
    have had, landing in the sample as though they were real.
    """
