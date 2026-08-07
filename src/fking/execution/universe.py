"""The tradable universe, resolved at startup as an intersection and never assumed.

`venue_symbols ∩ archive.symbols_with_history()`. Both directions of the difference are
real and neither is containment: spot testnet was missing 79 symbols present on spot
production and carried 81 testnet-only stale ones, and futures testnet was missing 189
(VF-006, measured 2026-08-01). So both of the natural assumptions are wrong, and each
fails in a way that is invisible from the outcome:

- Assuming a backtested symbol is tradable here produces **zero fills**, and zero fills
  score as "no edge". A good strategy is retired for an infrastructure reason and
  nothing downstream can tell the difference.
- Assuming a testnet symbol has production history produces a backtest over data that
  does not exist, or -- worse, because it looks like data -- over the stale testnet-only
  listings.

A requested symbol outside the intersection is therefore a fatal startup error naming
both differences, not a warning and not a silent drop.

**This is today's tradable set, and it is not a backtest universe.** Nothing here takes
an `as_of`, and that absence is deliberate: selecting a historical universe from today's
intersection is survivorship bias wearing a safety check as a disguise. Historical
membership is the point-in-time `universe_as_of(venue, as_of)` question, answered
against listing and delisting timestamps (`.claude/rules/no-lookahead.md`).

Non-tradable symbols are **quarantined with a reason**, never dropped. Binance testnet
serves non-ASCII symbols on purpose, and `str.isalnum()` returns `True` for some of them
and `False` for others -- so a filter built on it drops some silently and the universe is
quietly wrong with no line naming what went missing. The reason names the offending code
points in ASCII (`U+8FD9(...)`), which is also what keeps the diagnostic renderable when
the console codepage is not UTF-8: a `UnicodeEncodeError` raised *inside* the log call
that reports the problem is a startup crash whose diagnostic channel is the thing that
failed.

Filters -- `tickSize`, `stepSize`, `minNotional` -- are loaded here, from the venue's own
response text as `Decimal`, and cached with the `exchangeInfo` fetch time. They are the
tolerance reconciliation compares against, not an epsilon: step sizes span eight orders
of magnitude across symbols, so a global `1e-8` is simultaneously too tight for one
symbol and too loose for another, and the too-loose direction hides real divergence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final

import structlog

from fking.execution._errors import UniverseUnavailableError
from fking.execution.models import SymbolFilters, VenueExchangeInfo, VenueSymbol
from fking.execution.symbols import SymbolClassification, classify_symbol

__all__ = [
    "IntersectionBaseline",
    "IntersectionDrift",
    "SymbolUniverse",
    "check_intersection_drift",
    "resolve_universe",
]

_LOG: Final = structlog.get_logger(__name__)

# How many symbols a difference names before the message truncates. A startup failure
# message is read in a terminal, and a list of 189 symbols scrolls the actionable part
# of it off the screen; the counts stay exact and are what the reader acts on.
_SAMPLE_LIMIT: Final[int] = 8

# Only `TRADING` symbols enter the universe. A `BREAK` or `HALT` symbol is listed and
# untradable right now, and admitting it produces a rejection in the order path rather
# than a refusal at startup.
_TRADING_STATUS: Final[str] = "TRADING"


def _sample(symbols: Iterable[str]) -> str:
    ordered = sorted(symbols)
    shown = ", ".join(repr(symbol) for symbol in ordered[:_SAMPLE_LIMIT])
    remainder = len(ordered) - _SAMPLE_LIMIT
    return shown if remainder <= 0 else f"{shown}, ... (+{remainder} more)"


@dataclass(frozen=True, slots=True)
class SymbolUniverse:
    """What may be traded on one venue right now, and what may not, with reasons."""

    venue_id: str
    # The venue's own `serverTime` rather than a local clock: the filters are only as
    # fresh as the payload they came from, and an order rounded against a stale filter
    # is rejected with -1013 in a hot path.
    resolved_at_utc: datetime
    filters: Mapping[str, SymbolFilters]
    quarantined: tuple[SymbolClassification, ...]
    # Listed by the venue, no verified archive history. Backtests cannot cover these.
    venue_only: frozenset[str]
    # Archive history exists, the venue does not list it. Strategies cannot trade these.
    archive_only: frozenset[str]

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(self.filters)

    def filters_for(self, symbol: str) -> SymbolFilters:
        """The symbol's filters, or a fatal error naming the resolved universe.

        Never a default. A default tick size is a number nobody chose being used to
        round a price the venue will reject.
        """
        found = self.filters.get(symbol)
        if found is None:
            raise UniverseUnavailableError(
                f"{symbol!r} is not in the resolved universe for {self.venue_id}; "
                f"tradable={_sample(self.filters)}"
            )
        return found


@dataclass(frozen=True, slots=True)
class IntersectionBaseline:
    """A previously measured difference, with where it came from.

    The counts live in the caller's recorded baseline rather than in this module on
    purpose: VF-006 records 79 and 189 as a *snapshot*, and the durable fact is that the
    sets differ in both directions and must be intersected. A constant here would turn a
    measurement into an assumption, and the next re-measurement would look like a bug.
    """

    venue_id: str
    archive_only_count: int
    venue_only_count: int
    # Free text naming the measurement -- a verified-fact id, a recording path.
    source: str
    # Absolute tolerance. Listings and delistings move these counts by a handful in
    # ordinary operation; a step change means the venue changed and the assumption needs
    # re-verifying by a human.
    tolerance: int = 10


@dataclass(frozen=True, slots=True)
class IntersectionDrift:
    """How far today's difference is from the baseline. `is_drifted` is the trigger."""

    venue_id: str
    baseline_source: str
    observed_archive_only: int
    observed_venue_only: int
    baseline_archive_only: int
    baseline_venue_only: int
    tolerance: int

    @property
    def is_drifted(self) -> bool:
        return (
            abs(self.observed_archive_only - self.baseline_archive_only) > self.tolerance
            or abs(self.observed_venue_only - self.baseline_venue_only) > self.tolerance
        )

    def describe(self) -> str:
        return (
            f"{self.venue_id}: archive-only {self.observed_archive_only} vs baseline "
            f"{self.baseline_archive_only}, venue-only {self.observed_venue_only} vs "
            f"baseline {self.baseline_venue_only}, tolerance {self.tolerance} "
            f"({self.baseline_source})"
        )


def _classify_all(
    symbols: Iterable[VenueSymbol],
) -> tuple[dict[str, VenueSymbol], list[SymbolClassification]]:
    """Split listed symbols into the tradable-shaped ones and the quarantine."""
    tradable: dict[str, VenueSymbol] = {}
    quarantined: list[SymbolClassification] = []
    for entry in symbols:
        classification = classify_symbol(entry.symbol)
        if not classification.is_tradable:
            quarantined.append(classification)
            continue
        if entry.status != _TRADING_STATUS:
            quarantined.append(
                SymbolClassification(
                    symbol=entry.symbol,
                    is_tradable=False,
                    reason=f"venue status {entry.status!r} is not {_TRADING_STATUS!r}",
                )
            )
            continue
        tradable[entry.symbol] = entry
    return tradable, quarantined


def resolve_universe(
    *,
    venue_id: str,
    exchange_info: VenueExchangeInfo,
    archive_symbols: frozenset[str],
    requested: frozenset[str],
) -> SymbolUniverse:
    """Resolve the tradable universe and load every intersected symbol's filters.

    Raises:
        UniverseUnavailableError: a requested symbol is outside the intersection. The
            message names both differences, because "not tradable" and "no history" are
            different problems with different fixes and the counts say which.
        PermanentExchangeError: an intersected symbol's `exchangeInfo` entry carries no
            usable `PRICE_FILTER` or `LOT_SIZE`. Fatal at startup rather than defaulted.
    """
    listed, quarantined = _classify_all(exchange_info.symbols)
    listed_symbols = frozenset(listed)
    intersected = listed_symbols & archive_symbols
    venue_only = listed_symbols - archive_symbols
    archive_only = archive_symbols - listed_symbols

    missing = requested - intersected
    if missing:
        raise UniverseUnavailableError(
            f"{_sample(missing)} requested but outside the tradable universe for "
            f"{venue_id}: {len(intersected)} symbols are listed with archive history. "
            f"{len(venue_only)} listed with no archive history ({_sample(venue_only)}); "
            f"{len(archive_only)} with archive history the venue does not list "
            f"({_sample(archive_only)}); {len(quarantined)} quarantined "
            f"({_sample(entry.symbol for entry in quarantined)})"
        )

    universe = SymbolUniverse(
        venue_id=venue_id,
        resolved_at_utc=exchange_info.server_time_utc,
        # A read-only view rather than the dict: `frozen=True` protects the binding and
        # not the object bound, so a caller holding this could otherwise add a symbol to
        # the universe after it was resolved.
        filters=MappingProxyType(
            {symbol: listed[symbol].order_filters() for symbol in sorted(intersected)}
        ),
        quarantined=tuple(quarantined),
        venue_only=venue_only,
        archive_only=archive_only,
    )
    _LOG.info(
        "execution.universe.resolved",
        venue=venue_id,
        tradable_count=len(universe.filters),
        quarantined_count=len(universe.quarantined),
        venue_only_count=len(venue_only),
        archive_only_count=len(archive_only),
        # The reasons, not the raw symbols: every reason names its offending code points
        # in ASCII, so this line renders on a console whose codepage is not UTF-8.
        quarantined=[entry.reason for entry in universe.quarantined],
    )
    return universe


def check_intersection_drift(
    universe: SymbolUniverse, baseline: IntersectionBaseline
) -> IntersectionDrift:
    """Compare today's difference against a recorded baseline and report, never adjust.

    A drifted result is a `needs-human` escalation rather than an automatic correction:
    the counts moving by more than a handful means the venue changed, and silently
    adopting the new number would discard the only evidence that it did.
    """
    if universe.venue_id != baseline.venue_id:
        raise ValueError(
            f"baseline is for {baseline.venue_id!r} and the universe is for "
            f"{universe.venue_id!r}; comparing them would report drift that is only a "
            f"venue mismatch"
        )
    drift = IntersectionDrift(
        venue_id=universe.venue_id,
        baseline_source=baseline.source,
        observed_archive_only=len(universe.archive_only),
        observed_venue_only=len(universe.venue_only),
        baseline_archive_only=baseline.archive_only_count,
        baseline_venue_only=baseline.venue_only_count,
        tolerance=baseline.tolerance,
    )
    if drift.is_drifted:
        _LOG.error(
            "execution.universe.baseline_drift",
            venue=universe.venue_id,
            reason=drift.describe(),
            venue_only_count=drift.observed_venue_only,
            archive_only_count=drift.observed_archive_only,
        )
    return drift
