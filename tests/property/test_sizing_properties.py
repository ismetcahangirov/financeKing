"""Properties of position sizing that no example-based test can establish.

The three that matter, in the order they would hurt if broken:

1. **The combined quantity never exceeds any term that entered the `min()`.** This is
   the guarantee that gives every method veto power, and it is the reason a bug in one
   term can only make the system smaller (`RISK_PHILOSOPHY.md` section 3).
2. **`sigma_used >= sigma_floor` and `sigma_used >= max(sigma_ewma, sigma_60d)`,
   including for the all-zero and single-observation series** that a newly listed symbol
   actually produces.
3. **The result is non-negative, an exact multiple of the lot step, and idempotent under
   re-quantization** -- dust never survives. A residual `1E-18` compares equal to zero
   under a float and is rejected by the venue with `-1013` on a value that prints as if
   it were correct.

`docs/rules/testing-rules.md` clause 2: property tests are mandatory for every
function in `fking.risk`.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.domain import Direction, Instrument, Venue
from fking.risk.sizing import (
    PositionSizing,
    SizingInputs,
    SizingParameters,
    size_position,
    volatility_used,
)

pytestmark = [pytest.mark.property, pytest.mark.unit]

# BTCUSDT spot filters as the venue publishes them.
BTCUSDT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_notional_quote=Decimal("10"),
)


def stepped(step: Decimal, *, min_value: str, max_value: str) -> st.SearchStrategy[Decimal]:
    """Decimals snapped onto a venue step lattice.

    `places` alone is insufficient: Binance step sizes are not always powers of ten --
    `0.005` occurs -- so a value quantized to three places is still off that lattice
    half the time, and the venue rejects it with `-1013`.

    Deliberately not `tests.support.domain_factory.stepped`, which filters the drawn
    value to strictly positive. Zero equity, a zero ATR and a zero exposure headroom are
    all reachable states that sizing has to answer for, and filtering them out would
    leave every division-guard in the module untested by the property suite.
    """
    exponent = step.as_tuple().exponent
    # A finite Decimal always has an int exponent; 'n', 'N' and 'F' are NaN and
    # infinity, and a step size that is one of those is a bug in this file.
    assert isinstance(exponent, int), f"step {step} is not finite"
    places = -exponent
    return st.decimals(
        min_value=Decimal(min_value),
        max_value=Decimal(max_value),
        places=places,
        allow_nan=False,
        allow_infinity=False,
    ).map(lambda drawn: (drawn / step).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN) * step)


return_fractions = st.decimals(
    min_value=Decimal("-0.5"),
    max_value=Decimal("0.5"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)
non_negative_fractions = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("2"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)

sizing_inputs = st.builds(
    SizingInputs,
    instrument=st.just(BTCUSDT),
    direction=st.sampled_from([Direction.LONG, Direction.SHORT]),
    equity_usd=stepped(Decimal("0.01"), min_value="0", max_value="1000000"),
    entry_quote_price=stepped(BTCUSDT.tick_size, min_value="1", max_value="150000"),
    invalidation_quote_price=st.one_of(
        st.none(), stepped(BTCUSDT.tick_size, min_value="1", max_value="150000")
    ),
    atr_14_quote=stepped(BTCUSDT.tick_size, min_value="0", max_value="5000"),
    return_series=st.lists(return_fractions, max_size=80).map(tuple),
    volatility_floor_annualised=non_negative_fractions,
    closed_trade_count=st.integers(min_value=0, max_value=500),
    realised_mean_return_fraction=return_fractions,
    realised_return_stdev_fraction=non_negative_fractions,
    permitted_notional_usd=stepped(Decimal("0.01"), min_value="0", max_value="25000"),
)

sizing_parameters = st.builds(
    SizingParameters,
    risk_fraction_per_trade=st.decimals(
        min_value=Decimal("0.0001"),
        max_value=Decimal("0.01"),
        places=4,
        allow_nan=False,
        allow_infinity=False,
    ),
    target_volatility_annualised=st.decimals(
        min_value=Decimal("0.01"),
        max_value=Decimal("0.15"),
        places=4,
        allow_nan=False,
        allow_infinity=False,
    ),
    max_kelly_fraction=st.decimals(
        min_value=Decimal("0"),
        max_value=Decimal("0.50"),
        places=4,
        allow_nan=False,
        allow_infinity=False,
    ),
    min_trades_for_kelly=st.integers(min_value=100, max_value=400),
    atr_invalidation_multiple=st.decimals(
        min_value=Decimal("2.5"),
        max_value=Decimal("10"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
    unstated_invalidation_risk_multiplier=st.decimals(
        min_value=Decimal("0.05"),
        max_value=Decimal("0.5"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
)


@given(inputs=sizing_inputs, parameters=sizing_parameters)
def test_combined_quantity_never_exceeds_any_term_that_entered_the_minimum(
    inputs: SizingInputs, parameters: SizingParameters
) -> None:
    """Every term has veto power, so a bug in one of them can only shrink the position.

    Asserted against the pre-quantization terms, because quantization is monotone
    downward: `<=` therefore holds against every term whether or not the lot lattice
    moved the answer.
    """
    sizing = size_position(inputs, parameters)
    for term_name, term_base_quantity in sizing.terms.items():
        assert sizing.base_quantity <= term_base_quantity, term_name


@given(inputs=sizing_inputs, parameters=sizing_parameters)
def test_quantity_is_non_negative_on_the_lot_lattice_and_survives_requantization(
    inputs: SizingInputs, parameters: SizingParameters
) -> None:
    sizing = size_position(inputs, parameters)
    assert sizing.base_quantity >= Decimal("0")
    assert sizing.base_quantity % BTCUSDT.lot_step == Decimal("0")
    assert BTCUSDT.quantize_base_quantity(sizing.base_quantity) == sizing.base_quantity


@given(inputs=sizing_inputs, parameters=sizing_parameters)
def test_a_nonzero_quantity_always_clears_the_venue_minimum_notional(
    inputs: SizingInputs, parameters: SizingParameters
) -> None:
    """A quantity the venue would reject is not a smaller position, it is no order.

    Sizing that emits one converts a risk decision into a `-1013` in the order path,
    where it looks like an exchange fault rather than a sizing bug.
    """
    sizing = size_position(inputs, parameters)
    if sizing.base_quantity > Decimal("0"):
        assert BTCUSDT.meets_min_notional(sizing.base_quantity, inputs.entry_quote_price)


@given(inputs=sizing_inputs, parameters=sizing_parameters)
def test_the_kelly_term_is_absent_rather_than_zero_below_the_trade_floor(
    inputs: SizingInputs, parameters: SizingParameters
) -> None:
    """Asserted on the term directly, not inferred from the output.

    A zero inside a minimum is a position of zero forever, for every strategy that has
    not yet traded -- which is every new strategy -- and the backtest simply shows no
    trades.
    """
    sizing = size_position(inputs, parameters)
    below_floor = inputs.closed_trade_count < parameters.min_trades_for_kelly
    assert (sizing.kelly_base_quantity is None) is below_floor
    assert ("kelly" in sizing.terms) is not below_floor


@given(inputs=sizing_inputs, parameters=sizing_parameters)
def test_the_kelly_term_never_risks_more_equity_than_the_kelly_cap(
    inputs: SizingInputs, parameters: SizingParameters
) -> None:
    """`min(c*f*, c)` -- both readings of `max_kelly_fraction` hold, not just the one.

    The multiplier reading alone leaves the term unbounded when a quiet realised
    distribution makes `f*` enormous, and that is precisely the distribution a strategy
    reports just before its regime ends.
    """
    sizing = size_position(inputs, parameters)
    if sizing.kelly_base_quantity is None:
        return
    # Compared as a quantity rather than by multiplying back up to a notional: the
    # divide-then-multiply round trip is not exact at 28 significant digits, and a test
    # that fails on the last digit of a volatility haircut trains people to rerun CI.
    capped_base_quantity = (
        parameters.max_kelly_fraction * inputs.equity_usd
    ) / inputs.entry_quote_price
    assert sizing.kelly_base_quantity <= capped_base_quantity


@given(inputs=sizing_inputs, parameters=sizing_parameters)
def test_the_binding_term_is_the_one_that_produced_the_quantity(
    inputs: SizingInputs, parameters: SizingParameters
) -> None:
    """Only one term binds at a time, and tuning a non-binding term does nothing.

    The record has to name the right one or the first hour of every tuning attempt is
    spent moving a number that cannot move the answer.
    """
    sizing = size_position(inputs, parameters)
    if sizing.binding_term == "venue_filters":
        assert sizing.base_quantity == Decimal("0")
        return
    assert sizing.terms[sizing.binding_term] == min(sizing.terms.values())


@given(inputs=sizing_inputs, parameters=sizing_parameters)
def test_the_unstated_invalidation_fallback_is_flagged_and_charged(
    inputs: SizingInputs, parameters: SizingParameters
) -> None:
    sizing: PositionSizing = size_position(inputs, parameters)
    assert sizing.used_invalidation_fallback is (inputs.invalidation_quote_price is None)
    expected = parameters.risk_fraction_per_trade * (
        parameters.unstated_invalidation_risk_multiplier
        if sizing.used_invalidation_fallback
        else Decimal("1")
    )
    assert sizing.risk_fraction_used == expected
    assert sizing.risk_fraction_used <= parameters.risk_fraction_per_trade


@given(
    return_series=st.lists(return_fractions, max_size=120).map(tuple),
    floor_annualised=non_negative_fractions,
)
def test_volatility_used_dominates_the_floor_and_both_estimators(
    return_series: tuple[Decimal, ...], floor_annualised: Decimal
) -> None:
    """Maximum, never a blend, including for empty and single-observation series.

    Underestimating volatility is expensive; overestimating it is merely suboptimal.
    The empty and single-observation cases are the ones a newly listed symbol actually
    produces, and they are exactly where a blend would report near-zero dispersion.
    """
    estimate = volatility_used(return_series, floor_annualised=floor_annualised)
    assert estimate.used_annualised >= estimate.floor_annualised
    assert estimate.used_annualised >= estimate.ewma_annualised
    assert estimate.used_annualised >= estimate.slow_window_annualised
    assert estimate.used_annualised == max(
        estimate.ewma_annualised, estimate.slow_window_annualised, estimate.floor_annualised
    )


@given(
    return_series=st.lists(return_fractions, min_size=1, max_size=60).map(tuple),
    floor_annualised=non_negative_fractions,
    extra_floor=non_negative_fractions,
)
def test_raising_the_volatility_floor_never_raises_the_estimate_used_below_it(
    return_series: tuple[Decimal, ...], floor_annualised: Decimal, extra_floor: Decimal
) -> None:
    """Monotone in the floor: a higher floor is a smaller position, never a larger one."""
    lower = volatility_used(return_series, floor_annualised=floor_annualised)
    higher = volatility_used(return_series, floor_annualised=floor_annualised + extra_floor)
    assert higher.used_annualised >= lower.used_annualised
