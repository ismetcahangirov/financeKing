"""Properties of portfolio exposure limits and pre-trade validation.

The guarantee under test is the one that matters: **whatever the risk engine approves,
adding it to the book leaves every limit satisfied.** Example-based tests confirm the
portfolio shapes someone thought of; the shapes that break exposure arithmetic are the
ones nobody enumerates -- a net-short book receiving a long, an asset held through two
instruments, an equity number inflated by a bad mark, a permitted notional that quantizes
to dust.

`.claude/rules/testing-rules.md` clause 2: property tests are mandatory for every function
in `fking.risk`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fking.domain import Direction, Instrument, Portfolio, Position, Signal, Venue
from fking.risk.exposure import (
    EXPOSURE_HARD_CEILINGS,
    EXPOSURE_HARD_FLOORS,
    ExposureLimits,
    PreTradeContext,
    ViolationTally,
    portfolio_exposure,
    validate_pre_trade,
)
from fking.risk.limits import RiskLimits

pytestmark = [pytest.mark.property, pytest.mark.unit]

_AS_OF: Final = datetime(2026, 8, 1, tzinfo=UTC)

# Filters as recorded from Binance spot testnet exchangeInfo. `lot_step` deliberately
# differs between the two instruments so a quantity valid on one is invalid on the other.
BTCUSDT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_notional_quote=Decimal("10"),
)
ETHUSDT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="ETHUSDT",
    base_asset="ETH",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.0001"),
    min_notional_quote=Decimal("10"),
)
# A second BTC instrument on a second venue: the per-asset limit must net across both,
# which a per-instrument limit alone would miss.
BTC_PERP: Final = Instrument(
    venue=Venue.BINANCE_FUTURES_TESTNET,
    symbol="BTCUSDT-PERP",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.1"),
    lot_step=Decimal("0.001"),
    min_notional_quote=Decimal("5"),
)
INSTRUMENTS: Final = (BTCUSDT, ETHUSDT, BTC_PERP)
TRADABLE: Final = frozenset(instrument.symbol for instrument in INSTRUMENTS)

MARKS_USD: Final = {
    BTCUSDT: Decimal("64000"),
    ETHUSDT: Decimal("3200"),
    BTC_PERP: Decimal("64100"),
}


def frozen_clock(moment: datetime = _AS_OF) -> Callable[[], datetime]:
    """A `Clock` that never moves. Purity in `risk` means the time is an input."""

    def _now() -> datetime:
        return moment

    return _now


def _held(instrument: Instrument, signed_base_quantity: Decimal) -> Position:
    return Position(
        instrument=instrument,
        signed_base_quantity=signed_base_quantity,
        average_entry_quote_price=MARKS_USD[instrument],
        realised_pnl_quote=Decimal("0"),
        fee_quote_paid=Decimal("0"),
        opened_at_utc=_AS_OF - timedelta(hours=1),
        applied_fill_ids=frozenset(),
    )


signed_quantities = st.decimals(
    min_value=Decimal("-2"), max_value=Decimal("2"), places=4, allow_nan=False, allow_infinity=False
)
equities_usd = st.decimals(
    min_value=Decimal("1000"),
    max_value=Decimal("500000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)


@st.composite
def portfolios(draw: st.DrawFn) -> Portfolio:
    """Books holding any subset of the three instruments, long, short or flat."""
    positions = tuple(
        _held(instrument, quantity)
        for instrument, quantity in (
            (instrument, draw(signed_quantities)) for instrument in INSTRUMENTS
        )
        if quantity != 0
    )
    return Portfolio(as_of_utc=_AS_OF, positions=positions, cash_balances={})


def _signal(instrument: Instrument, direction: Direction) -> Signal:
    return Signal(
        strategy_id="momentum-v1",
        instrument=instrument,
        direction=direction,
        conviction=Decimal("0.6"),
        horizon=timedelta(hours=8),
        # A flat signal asserts nothing to invalidate, so it carries no level.
        invalidation_quote_price=(
            None if direction is Direction.FLAT else MARKS_USD[instrument] * Decimal("0.98")
        ),
        rationale="property test",
        decided_at_utc=_AS_OF,
    )


@given(
    portfolio=portfolios(),
    equity_usd=equities_usd,
    instrument=st.sampled_from(INSTRUMENTS),
    direction=st.sampled_from((Direction.LONG, Direction.SHORT)),
)
@settings(max_examples=400, deadline=None)
def test_an_approved_order_leaves_every_exposure_bound_satisfied(
    portfolio: Portfolio,
    equity_usd: Decimal,
    instrument: Instrument,
    direction: Direction,
) -> None:
    """The whole guarantee: apply what was approved, and nothing is over a limit.

    Asserted against the *post-trade* book, recomputed from scratch, not against the
    headroom arithmetic that produced the number -- otherwise the test would only prove
    the implementation agrees with itself.
    """
    exposure_limits = ExposureLimits()
    absolute_limits = RiskLimits()
    assessment = validate_pre_trade(
        signal=_signal(instrument, direction),
        portfolio=portfolio,
        marks_usd=MARKS_USD,
        context=PreTradeContext(
            equity_usd=equity_usd,
            exposure_limits=exposure_limits,
            absolute_limits=absolute_limits,
            tradable_symbols=TRADABLE,
        ),
        clock=frozen_clock(),
    )
    if not assessment.is_approved:
        return

    before = portfolio_exposure(portfolio, MARKS_USD)
    added_notional_usd = assessment.permitted_base_quantity * MARKS_USD[instrument]
    directional_sign = Decimal("1") if direction is Direction.LONG else Decimal("-1")

    assert (
        before.held_notional_usd(instrument) + added_notional_usd
        <= exposure_limits.max_position_equity_ratio * equity_usd
    )
    assert (
        before.held_notional_usd(instrument) + added_notional_usd
        <= absolute_limits.max_position_notional_usd
    )
    assert (
        before.asset_notional_usd(instrument.base_asset) + added_notional_usd
        <= exposure_limits.max_asset_exposure_ratio * equity_usd
    )
    assert (
        before.gross_notional_usd + added_notional_usd
        <= exposure_limits.max_gross_exposure_ratio * equity_usd
    )
    assert (
        before.gross_notional_usd + added_notional_usd <= absolute_limits.max_portfolio_notional_usd
    )
    assert (
        before.net_notional_usd * directional_sign + added_notional_usd
        <= exposure_limits.max_net_exposure_ratio * equity_usd
    )
    free_margin_after_usd = (
        equity_usd - (before.gross_notional_usd + added_notional_usd) / absolute_limits.max_leverage
    )
    assert free_margin_after_usd >= exposure_limits.min_free_margin_ratio * equity_usd
    assert added_notional_usd <= absolute_limits.max_single_order_notional_usd


@given(
    portfolio=portfolios(),
    equity_usd=equities_usd,
    instrument=st.sampled_from(INSTRUMENTS),
    direction=st.sampled_from((Direction.LONG, Direction.SHORT)),
)
@settings(max_examples=300, deadline=None)
def test_every_emitted_quantity_satisfies_the_venue_filters(
    portfolio: Portfolio,
    equity_usd: Decimal,
    instrument: Instrument,
    direction: Direction,
) -> None:
    """Dust and near-minimum notionals included.

    A residual off the `lot_step` lattice is rejected by the venue with `-1013` on a
    value that prints as if it were correct, and a quantity under MIN_NOTIONAL is
    rejected with `-1013`'s quieter sibling. Both are approvals that can never fill.
    """
    assessment = validate_pre_trade(
        signal=_signal(instrument, direction),
        portfolio=portfolio,
        marks_usd=MARKS_USD,
        context=PreTradeContext(
            equity_usd=equity_usd,
            exposure_limits=ExposureLimits(),
            absolute_limits=RiskLimits(),
            tradable_symbols=TRADABLE,
        ),
        clock=frozen_clock(),
    )
    if not assessment.is_approved:
        assert assessment.permitted_base_quantity == 0
        return

    quantity = assessment.permitted_base_quantity
    assert quantity > 0
    assert quantity % instrument.lot_step == 0
    assert instrument.meets_min_notional(quantity, MARKS_USD[instrument])
    assert instrument.quantize_base_quantity(quantity) == quantity


@given(equity_multiple=st.integers(min_value=1, max_value=1000))
@settings(max_examples=100, deadline=None)
def test_an_inflated_equity_number_cannot_breach_the_absolute_notional_cap(
    equity_multiple: int,
) -> None:
    """The reason both a relative and an absolute cap enter the same `min()`.

    A duplicated fill, a bad mark on an illiquid symbol or a quote-conversion error all
    inflate equity, and a limit expressed as a fraction of the number that broke cannot
    see it. The absolute cap does not scale with anything, so it still binds.
    """
    absolute_limits = RiskLimits()
    assessment = validate_pre_trade(
        signal=_signal(BTCUSDT, Direction.LONG),
        portfolio=Portfolio(as_of_utc=_AS_OF, positions=(), cash_balances={}),
        marks_usd=MARKS_USD,
        context=PreTradeContext(
            equity_usd=Decimal("100000") * equity_multiple,
            exposure_limits=ExposureLimits(),
            absolute_limits=absolute_limits,
            tradable_symbols=TRADABLE,
        ),
        clock=frozen_clock(),
    )
    notional_usd = assessment.permitted_base_quantity * MARKS_USD[BTCUSDT]
    assert notional_usd <= absolute_limits.max_position_notional_usd
    assert notional_usd <= absolute_limits.max_single_order_notional_usd


@given(
    portfolio=portfolios(),
    equity_usd=equities_usd,
    instrument=st.sampled_from(INSTRUMENTS),
    direction=st.sampled_from((Direction.LONG, Direction.SHORT, Direction.FLAT)),
)
@settings(max_examples=200, deadline=None)
def test_every_assessment_records_a_verdict_and_a_headroom_for_each_limit_it_evaluated(
    portfolio: Portfolio,
    equity_usd: Decimal,
    instrument: Instrument,
    direction: Direction,
) -> None:
    """A rejection is an audited decision, so the payload is complete in both outcomes."""
    assessment = validate_pre_trade(
        signal=_signal(instrument, direction),
        portfolio=portfolio,
        marks_usd=MARKS_USD,
        context=PreTradeContext(
            equity_usd=equity_usd,
            exposure_limits=ExposureLimits(),
            absolute_limits=RiskLimits(),
            tradable_symbols=TRADABLE,
        ),
        clock=frozen_clock(),
    )
    payload = assessment.audit_payload()
    assert payload["verdict"] in ("approved", "rejected")
    assert payload["decided_at_utc"] == _AS_OF.isoformat()
    rows = payload["limits_evaluated"]
    assert isinstance(rows, tuple)
    assert len(rows) == len(assessment.evaluations)
    for row in rows:
        assert set(row) == {
            "limit_name",
            "bound_kind",
            "threshold_usd",
            "observed_usd",
            "headroom_usd",
            "is_breached",
        }
        # Strings, not Decimals: the payload lands in jsonb, and a JSON encoder that has
        # not been told otherwise turns a Decimal into a float on the way into a table
        # that can never be corrected.
        assert isinstance(row["threshold_usd"], str)
        assert isinstance(row["observed_usd"], str)


@given(
    portfolio=portfolios(),
    equity_usd=equities_usd,
    direction=st.sampled_from((Direction.LONG, Direction.SHORT)),
)
@settings(max_examples=200, deadline=None)
def test_a_refusal_increments_the_strategys_violation_tally_and_an_approval_does_not(
    portfolio: Portfolio,
    equity_usd: Decimal,
    direction: Direction,
) -> None:
    """Intent is scored, not only the exposure the risk engine permitted.

    A strategy routinely clipped by limits is being defined by the risk engine rather
    than by its own thesis, and counting only the trades it was allowed grades the risk
    engine instead (`SURVIVAL_PROTOCOL.md` section 9).
    """
    assessment = validate_pre_trade(
        signal=_signal(BTCUSDT, direction),
        portfolio=portfolio,
        marks_usd=MARKS_USD,
        context=PreTradeContext(
            equity_usd=equity_usd,
            exposure_limits=ExposureLimits(),
            absolute_limits=RiskLimits(),
            tradable_symbols=TRADABLE,
        ),
        clock=frozen_clock(),
    )
    tally = ViolationTally().with_assessment(assessment)
    expected = 0 if assessment.is_approved else 1
    assert tally.violation_count_for("momentum-v1") == expected
    assert ViolationTally().violation_count_for("momentum-v1") == 0


@given(
    ratio=st.decimals(
        min_value=Decimal("0.0001"),
        max_value=Decimal("5"),
        places=4,
        allow_nan=False,
        allow_infinity=False,
    )
)
@settings(max_examples=200, deadline=None)
def test_exposure_limits_are_accepted_exactly_when_inside_every_compiled_in_bound(
    ratio: Decimal,
) -> None:
    """The biconditional. Accepted implies stored unmodified -- never clamped."""
    within_every_bound = (
        ratio <= EXPOSURE_HARD_CEILINGS["max_position_equity_ratio"].bound
        and ratio <= EXPOSURE_HARD_CEILINGS["max_gross_exposure_ratio"].bound
        and ratio <= EXPOSURE_HARD_CEILINGS["max_net_exposure_ratio"].bound
        and ratio <= EXPOSURE_HARD_CEILINGS["max_asset_exposure_ratio"].bound
        and ratio >= EXPOSURE_HARD_FLOORS["min_free_margin_ratio"].bound
    )
    try:
        limits = ExposureLimits(
            max_position_equity_ratio=ratio,
            max_gross_exposure_ratio=ratio,
            max_net_exposure_ratio=ratio,
            max_asset_exposure_ratio=ratio,
            min_free_margin_ratio=ratio,
        )
    except ValueError:
        assert not within_every_bound
        return
    assert within_every_bound
    assert limits.max_position_equity_ratio == ratio
    assert limits.min_free_margin_ratio == ratio
