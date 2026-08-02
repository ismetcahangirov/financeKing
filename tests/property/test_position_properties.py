"""Position arithmetic under Hypothesis.

Example-based tests confirm the cases somebody thought of. Position arithmetic fails on
the ones they did not: partial closes, direction flips, zero-crossings, dust
quantities. Every assertion here is a property that must hold for *any* sequence of
fills, which is the only shape of claim worth making about money arithmetic.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.domain import Direction, Fill, Position
from tests.support.domain_factory import BTCUSDT, fills

pytestmark = [pytest.mark.property, pytest.mark.unit]

_ZERO = Decimal("0")

fill_sequences = st.lists(fills(), min_size=1, max_size=12, unique_by=lambda fill: fill.fill_id)


@given(sequence=fill_sequences)
def test_position_arithmetic_invariants(sequence: list[Fill]) -> None:
    position = Position.flat(BTCUSDT)
    realised_running_total = _ZERO

    for fill in sequence:
        before = position
        transition = before.with_fill(fill)
        position = transition.after
        realised_running_total += transition.realised_pnl_quote

        # 1. Dust. A residual 1E-18 satisfies `== 0` for a float and is rejected by the
        #    venue as -1013 on the next order, which reads as an exchange fault.
        assert abs(position.signed_base_quantity) % BTCUSDT.lot_step == _ZERO

        # 2. Flat is exactly zero, and FLAT with a non-zero quantity is unrepresentable.
        assert (position.signed_base_quantity == _ZERO) == (position.direction is Direction.FLAT)

        # 3. A flip reports itself, and reports both halves. Netting a flip into one
        #    number loses the realised PnL of the side that closed.
        flipped = (
            before.direction is not Direction.FLAT
            and position.direction is not Direction.FLAT
            and before.direction is not position.direction
        )
        assert flipped == transition.crossed_flat
        if flipped:
            assert transition.closed_base_quantity == abs(before.signed_base_quantity)
            assert transition.opened_base_quantity == fill.base_quantity - abs(
                before.signed_base_quantity
            )
            assert position.average_entry_quote_price == fill.quote_price

        # 4. A partial close leaves the cost basis alone. Recomputing it on a close
        #    silently rewrites the basis of the remainder, so the next close realises a
        #    PnL no pair of trades ever produced.
        partially_closed = (
            transition.closed_base_quantity > _ZERO
            and not transition.crossed_flat
            and position.direction is before.direction
        )
        if partially_closed:
            assert position.average_entry_quote_price == before.average_entry_quote_price

        # 5. Fees never touch realised PnL, so the gross and net figures stay separable.
        assert position.fee_quote_paid == before.fee_quote_paid + fill.fee_quote

    # 6. Realised PnL is path independent: however the sequence decomposed into opens,
    #    partial closes and flips, the accumulator equals the running sum.
    assert position.realised_pnl_quote == realised_running_total
    assert position.net_realised_pnl_quote == realised_running_total - position.fee_quote_paid


@given(sequence=fill_sequences)
def test_applying_a_fill_leaves_the_original_untouched_and_is_idempotent(
    sequence: list[Fill],
) -> None:
    position = Position.flat(BTCUSDT)
    for fill in sequence:
        position = position.with_fill(fill).after

    snapshot = dataclasses.asdict(position)
    repeat = position.with_fill(sequence[-1])

    assert dataclasses.asdict(position) == snapshot
    assert repeat.after == position
    assert repeat.is_noop
    assert repeat.realised_pnl_quote == _ZERO
    # The fee is not charged twice either. A duplicated fee is the quiet version of a
    # duplicated fill: the position is right and the cost basis of the strategy is not.
    assert repeat.after.fee_quote_paid == position.fee_quote_paid


@given(sequence=fill_sequences)
def test_every_intermediate_position_is_a_legal_position(sequence: list[Fill]) -> None:
    """Reconstructing each state from its own fields must succeed.

    The constructor is where every invariant is enforced, so a state that `with_fill`
    can produce but the constructor would reject is a state that survives in memory and
    fails the moment it is loaded back from the database.
    """
    position = Position.flat(BTCUSDT)
    for fill in sequence:
        position = position.with_fill(fill).after
        rebuilt = Position(
            instrument=position.instrument,
            signed_base_quantity=position.signed_base_quantity,
            average_entry_quote_price=position.average_entry_quote_price,
            realised_pnl_quote=position.realised_pnl_quote,
            fee_quote_paid=position.fee_quote_paid,
            opened_at_utc=position.opened_at_utc,
            applied_fill_ids=position.applied_fill_ids,
        )
        assert rebuilt == position


@given(sequence=fill_sequences, mark_quote_price=st.sampled_from(["1", "64000.00", "150000"]))
def test_unrealised_pnl_has_the_sign_of_the_position(
    sequence: list[Fill], mark_quote_price: str
) -> None:
    position = Position.flat(BTCUSDT)
    for fill in sequence:
        position = position.with_fill(fill).after

    mark = Decimal(mark_quote_price)
    unrealised = position.unrealised_pnl_quote(mark)
    if position.direction is Direction.FLAT:
        assert unrealised == _ZERO
    elif mark > position.average_entry_quote_price:
        assert (unrealised > _ZERO) == (position.direction is Direction.LONG)
    elif mark < position.average_entry_quote_price:
        assert (unrealised > _ZERO) == (position.direction is Direction.SHORT)
    else:
        assert unrealised == _ZERO

    assert position.notional_quote(mark) == abs(position.signed_base_quantity) * mark
