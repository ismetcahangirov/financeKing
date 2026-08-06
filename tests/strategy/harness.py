"""Bars, stand-in feature values, and a digest sharp enough to mean "byte-identical".

Three pieces, each with an obvious wrong version.

**`feature_values_for` derives its window from the feature catalogue**, not from a
constant written here. The registry refuses a strategy whose warm-up does not cover its
deepest declared lookback, so a hard-coded window in the harness would eventually disagree
with the declaration the test is supposed to be exercising -- and the disagreement would
read as a strategy bug.

**`poison_after` is gross, not subtle.** Closes after the cut are tripled and thirded in
alternation, which multiplies every return's magnitude by nine and inverts its sign. A
small perturbation can be absorbed by rounding and produce a *false pass*, which is the
worst possible outcome for the one test guarding the most dangerous defect class in the
project.

**`signal_digest` compares exact decimal text.** `format(value, "f")` never falls back to
scientific notation and preserves trailing zeros, so `Decimal("0.10")` and
`Decimal("0.1")` digest differently and a `1e-15` difference fails. A comparison that
tolerates a small difference will pass on the leak that only moves the fifteenth digit
today and the third digit on a different fold.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from fking.data.features.registry import FEATURES
from fking.domain import Bar, Instrument, Signal, Venue
from fking.strategy import Clock, FeatureRequirement, StrategySpec

__all__ = [
    "BTCUSDT",
    "ETHUSDT",
    "SERIES_START_UTC",
    "bars_from_closes",
    "clock_at",
    "feature_values_for",
    "poison_after",
    "rising_closes",
    "signal_digest",
]

# The real Binance spot testnet filters for these symbols. A tick size of 1 would make
# every invalidation level land on the lattice by accident, and the snapping direction --
# which is the half that decides position size -- would never be exercised.
BTCUSDT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_notional_quote=Decimal("10.00"),
)
ETHUSDT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="ETHUSDT",
    base_asset="ETH",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.0001"),
    min_notional_quote=Decimal("10.00"),
)

SERIES_START_UTC: Final = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

_POISON_FACTOR: Final[Decimal] = Decimal("3")


def clock_at(as_of: datetime) -> Clock:
    """A clock frozen at `as_of`, which is all a strategy is ever handed.

    A named factory rather than an inline lambda at each call site: a lambda closing over
    a loop variable is the spelling that quietly reads the *last* instant of the loop for
    every iteration, and that is a look-ahead bug wearing a syntax preference.
    """
    return lambda: as_of


def bars_from_closes(
    closes: Sequence[Decimal],
    *,
    instrument: Instrument = BTCUSDT,
    interval: timedelta = timedelta(minutes=15),
    start_utc: datetime = SERIES_START_UTC,
) -> tuple[Bar, ...]:
    """A regular series of closed bars whose opens chain from the previous close.

    Chained opens are what a continuously traded market produces, and they make the
    label-alignment question checkable elsewhere: a decision taken on `close[i]` could
    only ever have transacted at `open[i+1]`, which is a different number here only
    because the poison moves one of them.
    """
    built: list[Bar] = []
    for index, close_quote_price in enumerate(closes):
        open_quote_price = closes[index - 1] if index else close_quote_price
        high = max(open_quote_price, close_quote_price) * Decimal("1.001")
        low = min(open_quote_price, close_quote_price) * Decimal("0.999")
        built.append(
            Bar(
                instrument=instrument,
                open_time_utc=start_utc + interval * index,
                close_time_utc=start_utc + interval * (index + 1),
                open_quote_price=open_quote_price,
                high_quote_price=high,
                low_quote_price=low,
                close_quote_price=close_quote_price,
                base_volume=Decimal("12.5"),
                trade_count=4210,
            )
        )
    return tuple(built)


def rising_closes(
    bar_count: int, *, base: str = "64000", step: str = "0.006"
) -> tuple[Decimal, ...]:
    """Closes that compound upward fast enough to clear any declared entry threshold.

    Deterministic and monotone on purpose: the warm-up property needs a series on which a
    signal *would* be emitted the instant suppression ends, otherwise "no signal before
    the warm-up" is satisfied by a strategy that never signals at all.
    """
    growth = Decimal("1") + Decimal(step)
    closes: list[Decimal] = [Decimal(base)]
    for _ in range(1, bar_count):
        closes.append(closes[-1] * growth)
    return tuple(closes[:bar_count])


def feature_values_for(
    spec: StrategySpec, bars: Sequence[Bar]
) -> Mapping[datetime, Mapping[FeatureRequirement, Decimal]]:
    """A stand-in feature store: trailing return over each requirement's declared lookback.

    Trailing by construction -- the value at bar *i* reads `close[i]` and
    `close[i - window]` and nothing after either -- so a future perturbation cannot reach
    a pre-cut value through the harness. If it could, the look-ahead probe would fail for
    a reason that has nothing to do with the strategy, and the probe would get deleted.
    """
    by_instant: dict[datetime, dict[FeatureRequirement, Decimal]] = {
        observed.close_time_utc: {} for observed in bars
    }
    for requirement in spec.required_features:
        window = FEATURES[requirement.key].lookback // spec.shortest_bar_interval
        for index, observed in enumerate(bars):
            if index < window:
                continue
            base_quote_price = bars[index - window].close_quote_price
            by_instant[observed.close_time_utc][requirement] = (
                observed.close_quote_price / base_quote_price - Decimal("1")
            )
    return by_instant


def poison_after(bars: Sequence[Bar], *, cut_utc: datetime) -> tuple[Bar, ...]:
    """Replace every bar closing strictly after `cut_utc` with something unrecognisable."""
    poisoned: list[Bar] = []
    for index, observed in enumerate(bars):
        if observed.close_time_utc <= cut_utc:
            poisoned.append(observed)
            continue
        factor = _POISON_FACTOR if index % 2 == 0 else Decimal("1") / _POISON_FACTOR
        poisoned.append(
            Bar(
                instrument=observed.instrument,
                open_time_utc=observed.open_time_utc,
                close_time_utc=observed.close_time_utc,
                open_quote_price=observed.open_quote_price * factor,
                high_quote_price=observed.high_quote_price * factor,
                low_quote_price=observed.low_quote_price * factor,
                close_quote_price=observed.close_quote_price * factor,
                base_volume=observed.base_volume,
                trade_count=observed.trade_count,
            )
        )
    return tuple(poisoned)


def signal_digest(signals: Sequence[Signal]) -> str:
    """A digest over the exact text of every field of every signal, in emission order."""
    material = "\n".join(
        "|".join(
            (
                signal.strategy_id,
                signal.instrument.symbol,
                signal.direction.value,
                format(signal.conviction, "f"),
                str(signal.horizon),
                (
                    ""
                    if signal.invalidation_quote_price is None
                    else format(signal.invalidation_quote_price, "f")
                ),
                signal.decided_at_utc.isoformat(),
                signal.rationale,
            )
        )
        for signal in signals
    )
    return hashlib.blake2b(material.encode("utf-8"), digest_size=32).hexdigest()
