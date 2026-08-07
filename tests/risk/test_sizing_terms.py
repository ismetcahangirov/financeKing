"""The other four terms, the `min()` that combines them, and the bounds on the knobs.

The property suite proves the relations hold for every input it can generate; it cannot
tell a reader whether the Kelly haircut is a quarter or a half, or whether annualising
uses 365 or 252. These are the worked numbers for that, plus the two refusals that make
the parameters a policy rather than a suggestion.

The Kelly cases carry the most weight. `RISK_PHILOSOPHY.md` section 3.3 warns that
reading "returns 0 below 100 closed trades" literally puts a zero inside a minimum,
which is a position of zero forever for every strategy that has not traded yet -- and
every strategy starts there, so the symptom is a pipeline that runs cleanly and never
trades.

`RISK_PHILOSOPHY.md` sections 3.2-3.3.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Final

import pytest

from fking.domain import Direction, DomainError, Instrument, Venue
from fking.risk.sizing import (
    SIZING_CEILINGS,
    SIZING_FLOORS,
    SizingInputs,
    SizingParameters,
    size_position,
    volatility_used,
)

pytestmark = pytest.mark.unit

BTCUSDT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_notional_quote=Decimal("10"),
)

# A brand-new strategy: no closed trades, no realised distribution, and a stated stop.
# Every non-Kelly term is deliberately generous so that a spurious zero from Kelly would
# show up as a zero quantity rather than being masked by another term binding first.
NEW_STRATEGY: Final = SizingInputs(
    instrument=BTCUSDT,
    direction=Direction.LONG,
    equity_usd=Decimal("100000"),
    entry_quote_price=Decimal("60000"),
    invalidation_quote_price=Decimal("59000"),
    atr_14_quote=Decimal("400"),
    return_series=(Decimal("0.01"), Decimal("-0.01"), Decimal("0.02")),
    volatility_floor_annualised=Decimal("0.20"),
    closed_trade_count=0,
    realised_mean_return_fraction=Decimal("0"),
    realised_return_stdev_fraction=Decimal("0"),
    permitted_notional_usd=Decimal("1000000"),
)

# The same strategy after a hundred closed trades, with a realised distribution chosen so
# f* = mu / sigma^2 = 0.004 / 0.04^2 = 2.5 is checkable by hand.
WITH_RECORD: Final = replace(
    NEW_STRATEGY,
    closed_trade_count=100,
    realised_mean_return_fraction=Decimal("0.004"),
    realised_return_stdev_fraction=Decimal("0.04"),
)


def _terms_of(payload: Mapping[str, object]) -> Mapping[str, str]:
    """The nested `terms` mapping, narrowed.

    `audit_payload` returns `Mapping[str, object]` because the row is heterogeneous, so
    the nested shape is asserted here rather than declared twice -- a `TypedDict` on the
    production side would put the schema in two places and let them drift.
    """
    terms = payload["terms"]
    assert isinstance(terms, Mapping)
    return {str(name): str(rendered) for name, rendered in terms.items()}


def test_a_strategy_with_no_closed_trades_still_sizes_a_position() -> None:
    """The failure this guards against is silent: no exception, no trades, no signal."""
    sizing = size_position(NEW_STRATEGY, SizingParameters())

    assert sizing.kelly_base_quantity is None
    assert "kelly" not in sizing.terms
    assert sizing.base_quantity > Decimal("0")


def test_the_kelly_term_appears_only_once_the_record_reaches_the_floor() -> None:
    parameters = SizingParameters()
    one_short = replace(WITH_RECORD, closed_trade_count=parameters.min_trades_for_kelly - 1)

    assert size_position(one_short, parameters).kelly_base_quantity is None
    assert size_position(WITH_RECORD, parameters).kelly_base_quantity is not None


def test_the_kelly_term_is_a_quarter_of_full_kelly_when_full_kelly_is_small() -> None:
    """f* = 0.0004 / 0.04^2 = 0.25, so quarter Kelly is 0.0625 of equity.

    Read as a cap on `f` instead of a multiplier of `f*`, this case would size the
    position at full Kelly and apply no haircut at all -- and the estimation-error
    argument the haircut exists for does not care that `f*` happened to be small.
    """
    modest_edge = replace(WITH_RECORD, realised_mean_return_fraction=Decimal("0.0004"))

    sizing = size_position(modest_edge, SizingParameters())

    # 0.25 * 0.25 * 100,000 / 60,000 BTC
    assert sizing.kelly_base_quantity == Decimal("0.0625") * Decimal("100000") / Decimal("60000")


def test_the_kelly_term_stops_at_the_cap_when_full_kelly_is_large() -> None:
    """f* = 2.5, so c*f* = 0.625 of equity, above the 0.25 cap. The cap wins."""
    sizing = size_position(WITH_RECORD, SizingParameters())

    assert sizing.kelly_base_quantity == Decimal("0.25") * Decimal("100000") / Decimal("60000")


def test_a_losing_realised_record_sizes_kelly_at_zero_rather_than_omitting_it() -> None:
    """Zero here is a verdict, and it is a different fact from `None`.

    A hundred closed trades with a negative mean is Kelly saying the bet loses money.
    Reporting that as an absent term would let a losing strategy be sized by whichever
    other term happened to be next-smallest.
    """
    losing = replace(WITH_RECORD, realised_mean_return_fraction=Decimal("-0.01"))

    sizing = size_position(losing, SizingParameters())

    assert sizing.kelly_base_quantity == Decimal("0")
    assert sizing.base_quantity == Decimal("0")
    assert sizing.binding_term == "venue_filters"


def test_a_realised_record_with_no_dispersion_sizes_kelly_at_zero() -> None:
    """mu/sigma^2 with sigma = 0 is undefined, not infinite, and this many closed trades
    with zero dispersion is a data fault rather than a riskless edge."""
    degenerate = replace(WITH_RECORD, realised_return_stdev_fraction=Decimal("0"))

    assert size_position(degenerate, SizingParameters()).kelly_base_quantity == Decimal("0")


def test_the_volatility_target_term_scales_equity_by_target_over_realised() -> None:
    """q = (sigma_target / sigma_used) * E / P, with sigma_used pinned by the floor.

    The return series is flat, so both estimators report zero dispersion and the floor
    is what sizes the position. That is the 3 a.m. lull the floor exists for: without
    it, `sigma_target / sigma_used` on a quiet tape is a division by nearly nothing.
    """
    quiet = replace(NEW_STRATEGY, return_series=(Decimal("0"), Decimal("0"), Decimal("0")))

    sizing = size_position(quiet, SizingParameters())

    assert sizing.volatility.used_annualised == Decimal("0.20")
    expected = (Decimal("0.15") / Decimal("0.20")) * Decimal("100000") / Decimal("60000")
    assert sizing.volatility_target_base_quantity == expected


def test_annualisation_uses_every_day_of_the_year_because_crypto_has_no_weekend() -> None:
    """A single daily move of 1% annualises to 1% * sqrt(365), not sqrt(252).

    The equity convention understates crypto volatility by about 20%, and understating
    volatility is the expensive direction.
    """
    estimate = volatility_used((Decimal("0.01"),), floor_annualised=Decimal("0"))

    assert estimate.ewma_annualised == Decimal("0.01") * Decimal("365").sqrt()


def test_the_smallest_term_binds_and_is_named_on_the_record() -> None:
    """Only one term binds at a time; tuning any other one moves nothing."""
    exposure_bound = replace(NEW_STRATEGY, permitted_notional_usd=Decimal("600"))

    sizing = size_position(exposure_bound, SizingParameters())

    assert sizing.binding_term == "exposure"
    assert sizing.base_quantity == Decimal("0.01")
    assert sizing.terms["exposure"] == min(sizing.terms.values())


def test_a_parameter_above_its_ceiling_is_refused_with_both_numbers_named() -> None:
    """Never clamped. A silent clamp leaves the system safe, the operator wrong, and
    nobody informed -- and the next decision is made on the belief they still hold."""
    with pytest.raises(ValueError, match=r"max_kelly_fraction=0\.75 exceeds"):
        SizingParameters(max_kelly_fraction=Decimal("0.75"))


def test_a_parameter_below_its_floor_is_refused() -> None:
    """The direction a single ceilings-only validation loop gets backwards: a 1.0x ATR
    multiple is a tighter assumed stop and therefore a *larger* position."""
    with pytest.raises(ValueError, match="atr_invalidation_multiple=1 is below"):
        SizingParameters(atr_invalidation_multiple=Decimal("1"))


def test_the_unstated_invalidation_penalty_is_bounded_above_not_below() -> None:
    """1.0 means "no penalty at all", so the risky end of this parameter is the top.

    A floor here would refuse the settings that penalise an unstated stop harder, which
    is the exact inversion `fking.risk.ceilings` exists to make untypeable.
    """
    assert SizingParameters(unstated_invalidation_risk_multiplier=Decimal("0.1"))
    with pytest.raises(ValueError, match="unstated_invalidation_risk_multiplier"):
        SizingParameters(unstated_invalidation_risk_multiplier=Decimal("1"))


def test_every_shipped_default_sits_inside_its_own_compiled_bound() -> None:
    """A default outside its bound would make the no-argument constructor raise, which
    turns a missing configuration file into a boot failure rather than a safe baseline."""
    submitted = SizingParameters().bounded_values()

    assert not [
        name for name, ceiling in SIZING_CEILINGS.items() if ceiling.is_exceeded_by(submitted[name])
    ]
    assert not [
        name for name, floor in SIZING_FLOORS.items() if floor.is_undercut_by(submitted[name])
    ]


def test_the_audit_payload_renders_every_number_as_a_string() -> None:
    """A `Decimal` handed to a JSON encoder becomes a float, in a table that is
    append-only and therefore cannot be corrected afterwards."""
    payload = size_position(NEW_STRATEGY, SizingParameters()).audit_payload()

    assert payload["base_quantity"] == "0.50000"
    assert payload["binding_term"] == "fixed_fractional"
    assert payload["risk_fraction_used"] == "0.005"
    assert payload["kelly_excluded"] is True
    assert all(isinstance(rendered, str) for rendered in _terms_of(payload).values())


def test_the_audit_payload_distinguishes_an_excluded_kelly_from_a_zero_one() -> None:
    """Both size the same way and mean opposite things: "no record yet" against "the
    record says this bet loses money"."""
    excluded = size_position(NEW_STRATEGY, SizingParameters()).audit_payload()
    priced_at_zero = size_position(
        replace(WITH_RECORD, realised_mean_return_fraction=Decimal("-0.01")),
        SizingParameters(),
    ).audit_payload()

    assert (excluded["kelly_excluded"], priced_at_zero["kelly_excluded"]) == (True, False)
    assert "kelly" not in _terms_of(excluded)
    # Compared as a Decimal: the rendered exponent carries through from the arithmetic
    # ("0.00", not "0"), and an audit row asserting on a trailing zero is a test about
    # formatting wearing the clothes of a test about risk.
    assert Decimal(_terms_of(priced_at_zero)["kelly"]) == Decimal("0")


def test_an_entry_price_of_zero_is_refused_rather_than_divided_by() -> None:
    """Every term divides by the entry price. A zero there is a missing mark, and the
    only thing dividing by it can produce is an unbounded quantity."""
    with pytest.raises(DomainError, match="entry_quote_price must be above zero"):
        replace(NEW_STRATEGY, entry_quote_price=Decimal("0"))


def test_a_negative_closed_trade_record_is_refused() -> None:
    """A negative trade record is a counting bug upstream, and it reads as "well below
    the Kelly floor" -- which is the one branch where being wrong looks like caution."""
    with pytest.raises(DomainError, match="closed_trade_count"):
        replace(NEW_STRATEGY, closed_trade_count=-1)


def test_a_negative_equity_reading_is_refused_rather_than_sized() -> None:
    """Equity below zero is a broken accounting read, and sizing against it would
    produce a negative quantity that quantizes to a short nobody asked for."""
    with pytest.raises(DomainError, match="equity_usd"):
        replace(NEW_STRATEGY, equity_usd=Decimal("-1"))
