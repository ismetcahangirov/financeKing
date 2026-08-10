"""Properties of cross-strategy netting and its attribution.

The guarantee under test is the one issue #55 makes load-bearing: **attributed quantities
sum exactly to the net order quantity, and attributed notional sums to venue notional plus
`crossing_residual`.** If either identity slips, total attributed PnL stops equalling venue
PnL and every survival score computed downstream is measuring fiction -- consistently, and
with nothing to detect it, because both halves stay internally coherent.

Example-based tests confirm the batches somebody enumerated. The ones that break netting
arithmetic are the ones nobody does: a partial cross whose sum is off the lot lattice, a
perfect cross, a direction flip inside one batch, and dust that quantizes to nothing.

`docs/rules/testing-rules.md` clause 2: property tests are mandatory for every function in
`fking.risk`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from fking.domain import DomainError, Instrument, Venue
from fking.risk.netting import (
    CROSSING_RESIDUAL_ACCOUNT,
    BookingBasis,
    StrategyRequest,
    net_requests,
)

_ZERO: Final = Decimal("0")

# Real Binance spot testnet filters. A lot step of 1 makes every drawn sum trivially
# on-lattice, and the quantization property then passes without exercising anything.
BTCUSDT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_notional_quote=Decimal("10.00"),
)
COARSE: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="ETHUSDT",
    base_asset="ETH",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    # Deliberately coarse, and not a power of ten: this is the lattice on which a sum of
    # individually-legal quantities is most often illegal.
    lot_step=Decimal("0.005"),
    min_notional_quote=Decimal("10.00"),
)

MARK: Final = Decimal("64000.00")
MID: Final = Decimal("63999.50")


def signed_quantities() -> st.SearchStrategy[Decimal]:
    """Quantities across the full range that matters: dust, ordinary, and both signs."""
    return st.decimals(
        min_value=Decimal("-5"),
        max_value=Decimal("5"),
        places=8,
        allow_nan=False,
        allow_infinity=False,
    ).map(Decimal)


@st.composite
def request_batches(draw: st.DrawFn) -> tuple[Instrument, tuple[StrategyRequest, ...]]:
    instrument = draw(st.sampled_from((BTCUSDT, COARSE)))
    count = draw(st.integers(min_value=1, max_value=6))
    quantities = draw(st.lists(signed_quantities(), min_size=count, max_size=count))
    return instrument, tuple(
        StrategyRequest(strategy_id=f"strategy-{index}", signed_base_quantity=quantity)
        for index, quantity in enumerate(quantities)
    )


@st.composite
def crossing_batches(draw: st.DrawFn) -> tuple[Instrument, tuple[StrategyRequest, ...]]:
    """Batches guaranteed to contain both signs, so the cross is never vacuous."""
    instrument, requests = draw(request_batches())
    longs = draw(st.lists(signed_quantities(), min_size=1, max_size=3))
    shorts = draw(st.lists(signed_quantities(), min_size=1, max_size=3))
    extra = tuple(
        StrategyRequest(strategy_id=f"long-{index}", signed_base_quantity=abs(quantity))
        for index, quantity in enumerate(longs)
    ) + tuple(
        StrategyRequest(strategy_id=f"short-{index}", signed_base_quantity=-abs(quantity))
        for index, quantity in enumerate(shorts)
    )
    return instrument, requests + extra


@given(batch=request_batches())
@settings(max_examples=250)
def test_attributed_quantities_sum_exactly_to_the_net_order(
    batch: tuple[Instrument, tuple[StrategyRequest, ...]],
) -> None:
    """The identity the whole module exists for.

    Exactly, not approximately. A discrepancy of less than one lot step is the size that
    survives review and then accumulates across thousands of decisions until reconciliation
    reports a delta that looks like an exchange bug.
    """
    instrument, requests = batch
    plan = net_requests(
        instrument=instrument,
        requests=requests,
        reference_quote_price=MARK,
        decision_mid_quote_price=MID,
    )
    assert plan.attributed_signed_base_quantity == plan.net_signed_base_quantity


@given(batch=crossing_batches())
@settings(max_examples=250)
def test_attributed_notional_sums_to_venue_notional_plus_the_residual(
    batch: tuple[Instrument, tuple[StrategyRequest, ...]],
) -> None:
    """The crossed portion books at the venue portion's own price, so nothing is left over.

    `crossing_residual_at_decision_quote` is computed rather than asserted away. It is zero
    for every plan this module builds, and that zero is the machine-checkable form of
    "attribution sums to reality" -- the day somebody books a crossed slice at a synthetic
    price it stops being zero and this fails, instead of the discrepancy entering the
    scoring engine unnoticed.
    """
    instrument, requests = batch
    plan = net_requests(
        instrument=instrument,
        requests=requests,
        reference_quote_price=MARK,
        decision_mid_quote_price=MID,
    )
    assert plan.crossing_residual_at_decision_quote == _ZERO
    assert (
        plan.attributed_signed_notional_quote
        == plan.venue_signed_notional_quote + plan.crossing_residual_at_decision_quote
    )


@given(batch=request_batches())
@settings(max_examples=250)
def test_every_attribution_books_at_one_price(
    batch: tuple[Instrument, tuple[StrategyRequest, ...]],
) -> None:
    """One price per plan, and it is the mid only when nothing reaches the venue."""
    instrument, requests = batch
    plan = net_requests(
        instrument=instrument,
        requests=requests,
        reference_quote_price=MARK,
        decision_mid_quote_price=MID,
    )
    expected_price = MARK if plan.has_venue_portion else MID
    expected_basis = (
        BookingBasis.VENUE_VWAP if plan.has_venue_portion else BookingBasis.DECISION_MID
    )
    for attribution in plan.attributions:
        assert attribution.booked_quote_price == expected_price
        assert attribution.booking_basis is expected_basis


@given(batch=request_batches())
@settings(max_examples=250)
def test_the_net_quantity_never_exceeds_the_raw_sum(
    batch: tuple[Instrument, tuple[StrategyRequest, ...]],
) -> None:
    """Truncation only ever asks the venue for less, never more.

    `ROUND_DOWN` is toward zero, so a short's magnitude shrinks too. Rounding a net *up*
    through the lot lattice would be the risk engine sending more than any of its own terms
    authorised, and it would do it on the largest orders.
    """
    instrument, requests = batch
    raw = sum((request.signed_base_quantity for request in requests), start=_ZERO)
    plan = net_requests(
        instrument=instrument,
        requests=requests,
        reference_quote_price=MARK,
        decision_mid_quote_price=MID,
    )
    assert abs(plan.net_signed_base_quantity) <= abs(raw)
    assert plan.net_signed_base_quantity * raw >= _ZERO


@given(batch=request_batches())
@settings(max_examples=250)
def test_no_attribution_changes_sign_under_the_quantization_adjustment(
    batch: tuple[Instrument, tuple[StrategyRequest, ...]],
) -> None:
    """Absorbing the truncation never flips a strategy from long to short.

    A flipped attribution is the failure that is invisible in aggregate: the sum still
    matches, and one strategy has been booked for the opposite of what it asked for.
    """
    instrument, requests = batch
    plan = net_requests(
        instrument=instrument,
        requests=requests,
        reference_quote_price=MARK,
        decision_mid_quote_price=MID,
    )
    for request, attribution in zip(requests, plan.attributions, strict=True):
        assert attribution.strategy_id == request.strategy_id
        assert attribution.signed_base_quantity * request.signed_base_quantity >= _ZERO
        assert abs(attribution.signed_base_quantity) <= abs(request.signed_base_quantity)


@given(batch=crossing_batches())
@settings(max_examples=200)
def test_the_crossed_quantity_is_the_smaller_side(
    batch: tuple[Instrument, tuple[StrategyRequest, ...]],
) -> None:
    """Gross long plus gross short equals the venue quantity plus twice the cross."""
    instrument, requests = batch
    plan = net_requests(
        instrument=instrument,
        requests=requests,
        reference_quote_price=MARK,
        decision_mid_quote_price=MID,
    )
    gross_long = sum(
        (
            attribution.signed_base_quantity
            for attribution in plan.attributions
            if attribution.signed_base_quantity > _ZERO
        ),
        start=_ZERO,
    )
    gross_short = -sum(
        (
            attribution.signed_base_quantity
            for attribution in plan.attributions
            if attribution.signed_base_quantity < _ZERO
        ),
        start=_ZERO,
    )
    assert plan.crossed_base_quantity == min(gross_long, gross_short)
    assert gross_long + gross_short == abs(plan.net_signed_base_quantity) + 2 * (
        plan.crossed_base_quantity
    )


@given(
    long_quantity=st.decimals(
        min_value=Decimal("0.001"), max_value=Decimal("5"), places=5, allow_nan=False
    ),
    next_quote_price=st.decimals(
        min_value=Decimal("1000"), max_value=Decimal("120000"), places=2, allow_nan=False
    ),
)
@settings(max_examples=200)
def test_a_perfect_cross_charges_its_difference_to_the_residual_account(
    long_quantity: Decimal, next_quote_price: Decimal
) -> None:
    """No venue leg: booked at the mid, and the difference belongs to nobody's strategy.

    Charging it to a strategy would make one of them pay for the other's liquidity -- a
    transfer nobody authorised and which the strategy cannot see in its own record.
    """
    quantity = BTCUSDT.quantize_base_quantity(Decimal(long_quantity))
    assume(quantity > _ZERO)
    plan = net_requests(
        instrument=BTCUSDT,
        requests=(
            StrategyRequest(strategy_id="alpha", signed_base_quantity=quantity),
            StrategyRequest(strategy_id="beta", signed_base_quantity=-quantity),
        ),
        reference_quote_price=MARK,
        decision_mid_quote_price=MID,
    )
    assert plan.net_signed_base_quantity == _ZERO
    assert plan.crossed_base_quantity == quantity
    residual = plan.residual
    assert residual is not None
    assert residual.account == CROSSING_RESIDUAL_ACCOUNT
    assert residual.booking_basis is BookingBasis.DECISION_MID
    assert residual.settle_against(Decimal(next_quote_price)) == quantity * (
        Decimal(next_quote_price) - MID
    )
    # Neither strategy absorbs it: both are booked at the mid whatever the market does next.
    assert {attribution.booked_quote_price for attribution in plan.attributions} == {MID}


@given(batch=request_batches())
@settings(max_examples=100)
def test_a_venue_portion_leaves_nothing_to_settle(
    batch: tuple[Instrument, tuple[StrategyRequest, ...]],
) -> None:
    """With a venue leg the crossed portion is already at the venue's price."""
    instrument, requests = batch
    plan = net_requests(
        instrument=instrument,
        requests=requests,
        reference_quote_price=MARK,
        decision_mid_quote_price=MID,
    )
    if plan.residual is None or not plan.has_venue_portion:
        return
    assert plan.residual.settle_against(Decimal("70000.00")) == _ZERO


def test_rebooking_at_the_realised_vwap_returns_a_new_attribution() -> None:
    """Immutability, and the reason the basis is a field rather than a comment."""
    plan = net_requests(
        instrument=BTCUSDT,
        requests=(StrategyRequest(strategy_id="alpha", signed_base_quantity=Decimal("0.5")),),
        reference_quote_price=MARK,
        decision_mid_quote_price=MID,
    )
    attribution = plan.attributions[0]
    rebooked = attribution.rebook(Decimal("64010.00"))
    assert rebooked is not attribution
    assert attribution.booked_quote_price == MARK
    assert rebooked.booked_quote_price == Decimal("64010.00")


def test_an_internally_crossed_slice_cannot_be_rebooked_at_a_venue_price() -> None:
    """There is no venue trade behind it, so there is no VWAP that belongs to it."""
    plan = net_requests(
        instrument=BTCUSDT,
        requests=(
            StrategyRequest(strategy_id="alpha", signed_base_quantity=Decimal("0.5")),
            StrategyRequest(strategy_id="beta", signed_base_quantity=Decimal("-0.5")),
        ),
        reference_quote_price=MARK,
        decision_mid_quote_price=MID,
    )
    with pytest.raises(DomainError, match="no venue trade behind that quantity"):
        plan.attributions[0].rebook(Decimal("64010.00"))


def test_an_empty_batch_is_refused_rather_than_read_as_a_perfect_cross() -> None:
    with pytest.raises(DomainError, match="no requests"):
        net_requests(
            instrument=BTCUSDT,
            requests=(),
            reference_quote_price=MARK,
            decision_mid_quote_price=MID,
        )


def test_one_strategy_cannot_hold_two_slices_of_one_net_order() -> None:
    with pytest.raises(DomainError, match="appears twice"):
        net_requests(
            instrument=BTCUSDT,
            requests=(
                StrategyRequest(strategy_id="alpha", signed_base_quantity=Decimal("1")),
                StrategyRequest(strategy_id="alpha", signed_base_quantity=Decimal("-1")),
            ),
            reference_quote_price=MARK,
            decision_mid_quote_price=MID,
        )


@pytest.mark.parametrize(
    ("field_name", "kwargs"),
    [
        ("reference_quote_price", {"reference_quote_price": 64000.0}),
        ("decision_mid_quote_price", {"decision_mid_quote_price": 63999.5}),
    ],
)
def test_a_float_price_is_refused_by_name(field_name: str, kwargs: dict[str, object]) -> None:
    """`Decimal(0.1)` is already wrong before the code runs; the message says so."""
    call: dict[str, object] = {
        "reference_quote_price": MARK,
        "decision_mid_quote_price": MID,
        **kwargs,
    }
    with pytest.raises(DomainError, match=f"{field_name} must be a Decimal"):
        net_requests(
            instrument=BTCUSDT,
            requests=(StrategyRequest(strategy_id="alpha", signed_base_quantity=Decimal("1")),),
            **call,  # type: ignore[arg-type]  # deliberately wrong types, refused at runtime
        )


def test_a_float_quantity_is_refused_by_name() -> None:
    with pytest.raises(DomainError, match="signed_base_quantity must be a Decimal"):
        StrategyRequest(strategy_id="alpha", signed_base_quantity=1.5)  # type: ignore[arg-type]


def test_a_request_without_a_strategy_is_refused() -> None:
    with pytest.raises(DomainError, match="must name the strategy"):
        StrategyRequest(strategy_id="", signed_base_quantity=Decimal("1"))
