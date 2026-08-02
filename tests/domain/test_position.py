"""Named position transitions, with the arithmetic worked out in the assertions.

The property tests cover the space; these cover the cases whose *numbers* a reader
should be able to check by hand, and the constructor invariants that no fill sequence
can reach.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from fking.domain import Direction, DomainError, Position, PositionTransition, Side
from tests.support.domain_factory import BTCUSDT, EPOCH, ETHUSDT, make_fill

pytestmark = pytest.mark.unit

ZERO = Decimal("0")


def test_opening_a_long_sets_the_entry_price_and_the_open_time() -> None:
    transition = Position.flat(BTCUSDT).with_fill(
        make_fill(side=Side.BUY, quote_price="64000.00", base_quantity="0.01", fee_quote="0.64")
    )
    position = transition.after

    assert position.direction is Direction.LONG
    assert position.signed_base_quantity == Decimal("0.01")
    assert position.average_entry_quote_price == Decimal("64000.00")
    assert position.fee_quote_paid == Decimal("0.64")
    assert position.opened_at_utc == EPOCH
    assert transition.opened_base_quantity == Decimal("0.01")
    assert transition.closed_base_quantity == ZERO
    assert transition.realised_pnl_quote == ZERO
    assert transition.crossed_flat is False


def test_adding_to_a_long_blends_the_cost_basis() -> None:
    position = Position.flat(BTCUSDT)
    position = position.with_fill(
        make_fill(side=Side.BUY, quote_price="64000.00", base_quantity="0.01")
    ).after
    position = position.with_fill(
        make_fill(side=Side.BUY, quote_price="66000.00", base_quantity="0.01")
    ).after

    assert position.signed_base_quantity == Decimal("0.02")
    assert position.average_entry_quote_price == Decimal("65000.00")
    assert position.realised_pnl_quote == ZERO
    # The open time is the time the exposure began, not the time it was last added to.
    assert position.opened_at_utc == EPOCH


def test_a_partial_close_realises_only_the_closed_portion() -> None:
    position = (
        Position.flat(BTCUSDT)
        .with_fill(make_fill(side=Side.BUY, quote_price="64000.00", base_quantity="0.02"))
        .after
    )
    transition = position.with_fill(
        make_fill(side=Side.SELL, quote_price="65000.00", base_quantity="0.01")
    )

    assert transition.closed_base_quantity == Decimal("0.01")
    assert transition.opened_base_quantity == ZERO
    assert transition.realised_pnl_quote == Decimal("10.00")
    assert transition.crossed_flat is False
    # Untouched. Recomputing it here is the classic error: it rewrites the cost of the
    # remainder, so the next close realises a PnL no pair of trades ever produced.
    assert transition.after.average_entry_quote_price == Decimal("64000.00")
    assert transition.after.signed_base_quantity == Decimal("0.01")


def test_a_full_close_returns_to_flat_and_drops_the_stale_basis() -> None:
    position = (
        Position.flat(BTCUSDT)
        .with_fill(make_fill(side=Side.BUY, quote_price="64000.00", base_quantity="0.01"))
        .after
    )
    transition = position.with_fill(
        make_fill(side=Side.SELL, quote_price="63000.00", base_quantity="0.01")
    )

    assert transition.after.direction is Direction.FLAT
    assert transition.after.signed_base_quantity == ZERO
    assert transition.after.average_entry_quote_price == ZERO
    assert transition.after.opened_at_utc is None
    assert transition.realised_pnl_quote == Decimal("-10.00")
    # A close to exactly zero is not a flip: it realises once and opens nothing.
    assert transition.crossed_flat is False


def test_a_short_realises_the_opposite_sign() -> None:
    position = (
        Position.flat(BTCUSDT)
        .with_fill(make_fill(side=Side.SELL, quote_price="64000.00", base_quantity="0.01"))
        .after
    )
    assert position.direction is Direction.SHORT
    assert position.signed_base_quantity == Decimal("-0.01")

    transition = position.with_fill(
        make_fill(side=Side.BUY, quote_price="63000.00", base_quantity="0.01")
    )
    assert transition.realised_pnl_quote == Decimal("10.00")


def test_a_flip_closes_the_old_side_and_opens_the_new_one_at_the_fill_price() -> None:
    """Two economic actions in one event, and the audit trail needs both.

    Carrying the old average across the flip would price the new position with the
    cost basis of the one that just closed.
    """
    position = (
        Position.flat(BTCUSDT)
        .with_fill(make_fill(side=Side.BUY, quote_price="64000.00", base_quantity="0.01"))
        .after
    )
    later = EPOCH + timedelta(minutes=5)
    transition = position.with_fill(
        make_fill(
            side=Side.SELL,
            quote_price="65000.00",
            base_quantity="0.03",
            event_time_utc=later,
        )
    )

    assert transition.crossed_flat is True
    assert transition.closed_base_quantity == Decimal("0.01")
    assert transition.opened_base_quantity == Decimal("0.02")
    assert transition.realised_pnl_quote == Decimal("10.00")
    assert transition.after.direction is Direction.SHORT
    assert transition.after.signed_base_quantity == Decimal("-0.02")
    assert transition.after.average_entry_quote_price == Decimal("65000.00")
    assert transition.after.opened_at_utc == later


def test_reapplying_a_fill_changes_nothing() -> None:
    """Redis Streams delivery is at-least-once, so this is the normal case."""
    fill = make_fill(side=Side.BUY, quote_price="64000.00", base_quantity="0.01", fee_quote="0.64")
    once = Position.flat(BTCUSDT).with_fill(fill).after
    repeat = once.with_fill(fill)

    assert repeat.is_noop
    assert repeat.after == once
    assert repeat.after.fee_quote_paid == Decimal("0.64")


def test_a_fill_for_another_instrument_is_refused() -> None:
    with pytest.raises(DomainError, match="cannot apply a ETHUSDT fill"):
        Position.flat(BTCUSDT).with_fill(
            make_fill(side=Side.BUY, quote_price="3000.00", base_quantity="0.1", instrument=ETHUSDT)
        )


class TestConstructorInvariants:
    """States `with_fill` cannot produce, but a database row or a hand edit can."""

    def test_a_flat_position_may_not_keep_an_entry_price(self) -> None:
        with pytest.raises(DomainError, match="stale entry"):
            Position(
                instrument=BTCUSDT,
                signed_base_quantity=ZERO,
                average_entry_quote_price=Decimal("64000"),
                realised_pnl_quote=ZERO,
                fee_quote_paid=ZERO,
                opened_at_utc=None,
                applied_fill_ids=frozenset(),
            )

    def test_a_flat_position_may_not_keep_an_open_time(self) -> None:
        with pytest.raises(DomainError, match="carries an open time"):
            Position(
                instrument=BTCUSDT,
                signed_base_quantity=ZERO,
                average_entry_quote_price=ZERO,
                realised_pnl_quote=ZERO,
                fee_quote_paid=ZERO,
                opened_at_utc=EPOCH,
                applied_fill_ids=frozenset(),
            )

    def test_an_open_position_needs_an_open_time(self) -> None:
        with pytest.raises(DomainError, match="carries no opened_at_utc"):
            Position(
                instrument=BTCUSDT,
                signed_base_quantity=Decimal("0.01"),
                average_entry_quote_price=Decimal("64000"),
                realised_pnl_quote=ZERO,
                fee_quote_paid=ZERO,
                opened_at_utc=None,
                applied_fill_ids=frozenset(),
            )

    def test_an_open_position_needs_a_positive_entry_price(self) -> None:
        with pytest.raises(DomainError, match="average_entry_quote_price must be positive"):
            Position(
                instrument=BTCUSDT,
                signed_base_quantity=Decimal("0.01"),
                average_entry_quote_price=ZERO,
                realised_pnl_quote=ZERO,
                fee_quote_paid=ZERO,
                opened_at_utc=EPOCH,
                applied_fill_ids=frozenset(),
            )

    def test_applied_fill_ids_must_be_a_frozenset(self) -> None:
        """A `set` here would be mutable through the reference despite `frozen=True`."""
        with pytest.raises(DomainError, match="must be a frozenset"):
            Position(
                instrument=BTCUSDT,
                signed_base_quantity=ZERO,
                average_entry_quote_price=ZERO,
                realised_pnl_quote=ZERO,
                fee_quote_paid=ZERO,
                opened_at_utc=None,
                applied_fill_ids={UUID(int=1)},  # type: ignore[arg-type]  # the wrong type is the test
            )


class TestTransitionInvariants:
    def test_a_transition_may_not_span_two_instruments(self) -> None:
        with pytest.raises(DomainError, match="spans two instruments"):
            PositionTransition(
                before=Position.flat(BTCUSDT),
                after=Position.flat(ETHUSDT),
                closed_base_quantity=ZERO,
                opened_base_quantity=ZERO,
                realised_pnl_quote=ZERO,
                crossed_flat=False,
            )

    def test_crossed_flat_must_both_close_and_open(self) -> None:
        with pytest.raises(DomainError, match="did not both close and open"):
            PositionTransition(
                before=Position.flat(BTCUSDT),
                after=Position.flat(BTCUSDT),
                closed_base_quantity=Decimal("0.01"),
                opened_base_quantity=ZERO,
                realised_pnl_quote=ZERO,
                crossed_flat=True,
            )


def test_mark_to_market_helpers_require_a_positive_mark() -> None:
    position = Position.flat(BTCUSDT)
    with pytest.raises(DomainError, match="mark_quote_price must be positive"):
        position.notional_quote(ZERO)
    with pytest.raises(DomainError, match="mark_quote_price must be positive"):
        position.unrealised_pnl_quote(Decimal("-1"))
