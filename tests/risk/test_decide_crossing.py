"""Netting through `decide()`: what reaches the venue, and who is booked for what.

The arithmetic of netting is proved in `tests/property/test_netting_properties.py`. What is
tested here is the wiring: that two opposing signals produce one order rather than two, that
a perfect cross produces none, and that the crossing row exists with the price it was
actually booked at.
"""

from __future__ import annotations

from decimal import Decimal

from fking.domain import Direction, RiskVerdict, Side
from fking.risk import CROSSING_RESIDUAL_ACCOUNT, BookingBasis
from tests.support.risk_engine import (
    BTCUSDT,
    CORRELATION_ID,
    SEED,
    frozen_clock,
    make_engine,
    make_market_state,
    make_policy,
    make_portfolio_state,
    make_position,
    make_signal,
)

# The mark is 64000 and the exposure term binds at the 2000 USD single-order cap, so a
# signal with a stated invalidation sizes to 2000/64000. A signal whose invalidation sits
# far away is bound by the fixed-fractional term instead and sizes smaller, which is how a
# *partial* cross is produced without touching any limit.
_FULL_BASE_QUANTITY = Decimal("0.03125")
# Two strategies signalling on one instrument: the batch under test throughout.
_STRATEGY_COUNT = 2


def test_two_opposing_signals_of_equal_size_emit_no_venue_order() -> None:
    """A perfect internal cross. Zero orders is the correct outcome, not a refusal."""
    batch = make_engine().decide(
        signals=[
            make_signal("alpha", direction=Direction.LONG),
            make_signal("beta", direction=Direction.SHORT, invalidation_quote_price="65000.00"),
        ],
        portfolio_state=make_portfolio_state(),
        market_state=make_market_state(mid_offset_quote="-0.50"),
        clock=frozen_clock,
        correlation_id=CORRELATION_ID,
        seed=SEED,
    )
    assert batch.orders == ()
    assert batch.rejections == ()

    plan = batch.plans[0]
    assert plan.net_signed_base_quantity == Decimal("0")
    assert plan.crossed_base_quantity == _FULL_BASE_QUANTITY

    residual = batch.residuals[0]
    assert residual.account == CROSSING_RESIDUAL_ACCOUNT
    assert residual.crossed_base_quantity == _FULL_BASE_QUANTITY
    assert residual.booking_basis is BookingBasis.DECISION_MID
    # Booked at the mid, which is not the mark: with no venue leg there is no venue price.
    assert residual.booked_quote_price == Decimal("63999.50")


def test_neither_strategy_absorbs_the_crossing_difference() -> None:
    """Both slices are booked at the mid whatever the market prints next; the difference is
    charged to `crossing_residual` and to nothing else."""
    batch = make_engine().decide(
        signals=[
            make_signal("alpha", direction=Direction.LONG),
            make_signal("beta", direction=Direction.SHORT, invalidation_quote_price="65000.00"),
        ],
        portfolio_state=make_portfolio_state(),
        market_state=make_market_state(mid_offset_quote="-0.50"),
        clock=frozen_clock,
        correlation_id=CORRELATION_ID,
        seed=SEED,
    )
    attributions = batch.attributions_for(BTCUSDT)
    assert {attribution.booked_quote_price for attribution in attributions} == {Decimal("63999.50")}
    assert sum(attribution.signed_base_quantity for attribution in attributions) == Decimal("0")

    residual = batch.residuals[0]
    charge = residual.settle_against(Decimal("64100.00"))
    assert charge == _FULL_BASE_QUANTITY * Decimal("100.50")
    assert charge != Decimal("0")


def test_a_partial_cross_emits_one_net_order_rather_than_two() -> None:
    """One order, not two offsetting ones: otherwise the spread is paid twice for a
    position that is mostly zero."""
    batch = make_engine().decide(
        signals=[
            make_signal("alpha", direction=Direction.LONG),
            # A distant invalidation makes the fixed-fractional term bind, so beta asks for
            # less than alpha and the batch crosses only partially.
            make_signal("beta", direction=Direction.SHORT, invalidation_quote_price="96000.00"),
        ],
        portfolio_state=make_portfolio_state(),
        market_state=make_market_state(),
        clock=frozen_clock,
        correlation_id=CORRELATION_ID,
        seed=SEED,
    )
    assert len(batch.orders) == 1
    order = batch.orders[0]
    assert order.side is Side.BUY

    plan = batch.plans[0]
    assert plan.net_signed_base_quantity == order.base_quantity
    assert plan.crossed_base_quantity > Decimal("0")
    assert plan.crossed_base_quantity < _FULL_BASE_QUANTITY

    attributions = batch.attributions_for(BTCUSDT)
    assert sum(a.signed_base_quantity for a in attributions) == order.signed_base_quantity
    # Every slice, crossed and venue-bound alike, books at the venue portion's price.
    assert {a.booking_basis for a in attributions} == {BookingBasis.VENUE_VWAP}
    assert plan.crossing_residual_at_decision_quote == Decimal("0")


def test_both_strategies_are_audited_as_approved_against_the_one_order() -> None:
    batch = make_engine().decide(
        signals=[
            make_signal("alpha", direction=Direction.LONG),
            make_signal("beta", direction=Direction.SHORT, invalidation_quote_price="96000.00"),
        ],
        portfolio_state=make_portfolio_state(),
        market_state=make_market_state(),
        clock=frozen_clock,
        correlation_id=CORRELATION_ID,
        seed=SEED,
    )
    client_order_id = batch.orders[0].client_order_id
    assert len(batch.audits) == _STRATEGY_COUNT
    for audit in batch.audits:
        assert audit.verdict is RiskVerdict.APPROVED
        assert audit.client_order_id == client_order_id
        assert audit.attributed_signed_base_quantity is not None
    signs = {
        audit.strategy_id: (audit.attributed_signed_base_quantity or Decimal("0")).copy_sign(
            Decimal("1")
        )
        for audit in batch.audits
    }
    assert signs["alpha"] > Decimal("0") or signs["beta"] > Decimal("0")


def test_a_flat_signal_closes_the_position_once_however_many_ask() -> None:
    """Two strategies flattening the same instrument do not close it twice. The second is
    attributed zero -- a real outcome, not a refusal: it asked for flat and flat is what the
    book ends up at."""
    held = make_portfolio_state()
    portfolio_with_btc = make_portfolio_state(
        positions=(
            *held.portfolio.positions,
            make_position("BTCUSDT", notional_usd=Decimal("2000")),
        )
    )
    batch = make_engine().decide(
        signals=[
            make_signal("alpha", direction=Direction.FLAT),
            make_signal("beta", direction=Direction.FLAT),
        ],
        portfolio_state=portfolio_with_btc,
        market_state=make_market_state(),
        clock=frozen_clock,
        correlation_id=CORRELATION_ID,
        seed=SEED,
    )
    assert len(batch.orders) == 1
    order = batch.orders[0]
    assert order.side is Side.SELL
    assert order.base_quantity == _FULL_BASE_QUANTITY

    attributions = {a.strategy_id: a.signed_base_quantity for a in batch.attributions_for(BTCUSDT)}
    assert attributions["alpha"] == -_FULL_BASE_QUANTITY
    assert attributions["beta"] == Decimal("0")


def test_a_net_below_the_venue_floor_books_no_cross_either() -> None:
    """The remainder cannot be sent, so the crossed portion is not booked at a mid against a
    venue that traded nothing. The whole instrument is refused, and the refusal says why."""
    held = make_portfolio_state()
    dust = make_portfolio_state(
        positions=(
            *held.portfolio.positions,
            # 0.0001 BTC is 6.40 USD at the fixture mark, below the venue's 10 USD floor.
            make_position("BTCUSDT", notional_usd=Decimal("6.40")),
        )
    )
    batch = make_engine().decide(
        signals=[make_signal("alpha", direction=Direction.FLAT)],
        portfolio_state=dust,
        market_state=make_market_state(),
        clock=frozen_clock,
        correlation_id=CORRELATION_ID,
        seed=SEED,
    )
    assert batch.orders == ()
    assert batch.plans == ()
    assert batch.residuals == ()
    rejection = batch.rejections[0]
    assert rejection.binding_limit_name == "net_min_notional_quote"
    assert batch.audits[0].stage == "netting"
    assert batch.audits[0].verdict is RiskVerdict.REJECTED


def test_the_policy_in_force_is_readable_beside_the_decision_it_produced() -> None:
    policy = make_policy()
    assert make_engine(policy=policy).policy is policy
