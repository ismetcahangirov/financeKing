"""Position arithmetic: the part of this package that is easy to get subtly wrong.

Three decisions here are load-bearing and none of them is obvious from the code alone.

**Quantity is signed, and `direction` is derived.** Storing a magnitude alongside a
`Direction` gives two sources of truth that can disagree, and the disagreement is
unrepresentable-but-constructible: `Direction.FLAT` with a quantity of 0.01. With a
signed number, flat *is* zero, and `direction` cannot lie about it.

**A transition is a value, not a side effect.** `with_fill` returns a
`PositionTransition` carrying both the before and after states plus what the fill
actually did -- how much closed, how much opened, what was realised, whether the
position crossed through flat. The audit log needs every one of those numbers
(`.claude/rules/append-only-audit.md`: "given only these rows, can I answer why this
order had this size at this moment?"), and a method returning only the new `Position`
throws them away at the one point where they are cheap to compute.

**`realised_pnl_quote` is gross of fees.** Fees accumulate separately in
`fee_quote_paid` and `net_realised_pnl_quote` combines them. Netting them into one
field makes the execution-cost decomposition in `EVOLUTION_ENGINE.md` unrecoverable:
a strategy whose gross edge is real and whose net result is negative is a capacity
problem, and one whose gross edge is zero is a dead strategy. They need different
answers and a single number cannot tell them apart.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from fking.domain._errors import DomainError
from fking.domain._guards import (
    require_decimal,
    require_non_negative_decimal,
    require_positive_decimal,
    require_utc,
)
from fking.domain.enums import Direction
from fking.domain.instrument import Instrument
from fking.domain.order import Fill

_ZERO: Final = Decimal("0")
_LONG_SIGN: Final = Decimal("1")
_SHORT_SIGN: Final = Decimal("-1")


@dataclass(frozen=True, slots=True)
class Position:
    """Exposure to one instrument, as a value.

    Idempotent on `fill_id` by construction: `applied_fill_ids` is part of the state,
    so re-applying a fill returns the same position rather than doubling it. Redis
    Streams delivery is at-least-once, so a consumer *will* see the same `FillApplied`
    event twice; making the check part of the type means it cannot be forgotten in one
    of the several consumers that apply fills.
    """

    instrument: Instrument
    signed_base_quantity: Decimal
    average_entry_quote_price: Decimal
    realised_pnl_quote: Decimal
    fee_quote_paid: Decimal
    opened_at_utc: datetime | None
    applied_fill_ids: frozenset[UUID]

    def __post_init__(self) -> None:
        require_decimal(self.signed_base_quantity, "signed_base_quantity")
        require_non_negative_decimal(self.average_entry_quote_price, "average_entry_quote_price")
        require_decimal(self.realised_pnl_quote, "realised_pnl_quote")
        require_non_negative_decimal(self.fee_quote_paid, "fee_quote_paid")
        if not isinstance(self.applied_fill_ids, frozenset):
            raise DomainError(
                f"applied_fill_ids must be a frozenset, got {type(self.applied_fill_ids).__name__}"
            )

        if self.signed_base_quantity == _ZERO:
            # Flat is exactly zero and carries no entry price and no open time. An
            # average entry left behind on a closed position is a number that looks
            # like a cost basis and is not one; the next partial open would blend
            # against it.
            if self.average_entry_quote_price != _ZERO:
                raise DomainError(
                    f"flat position in {self.instrument.symbol} carries a stale entry "
                    f"price of {self.average_entry_quote_price}"
                )
            if self.opened_at_utc is not None:
                raise DomainError(
                    f"flat position in {self.instrument.symbol} carries an open time of "
                    f"{self.opened_at_utc.isoformat()}"
                )
        else:
            require_positive_decimal(self.average_entry_quote_price, "average_entry_quote_price")
            if self.opened_at_utc is None:
                raise DomainError(
                    f"open position of {self.signed_base_quantity} "
                    f"{self.instrument.base_asset} carries no opened_at_utc"
                )
            require_utc(self.opened_at_utc, "opened_at_utc")

    @classmethod
    def flat(cls, instrument: Instrument) -> Position:
        """A position with no exposure and no history."""
        return cls(
            instrument=instrument,
            signed_base_quantity=_ZERO,
            average_entry_quote_price=_ZERO,
            realised_pnl_quote=_ZERO,
            fee_quote_paid=_ZERO,
            opened_at_utc=None,
            applied_fill_ids=frozenset(),
        )

    @property
    def direction(self) -> Direction:
        """Derived, never stored, so it cannot contradict the quantity."""
        if self.signed_base_quantity > _ZERO:
            return Direction.LONG
        if self.signed_base_quantity < _ZERO:
            return Direction.SHORT
        return Direction.FLAT

    @property
    def net_realised_pnl_quote(self) -> Decimal:
        """Realised PnL after the fees actually charged."""
        return self.realised_pnl_quote - self.fee_quote_paid

    def notional_quote(self, mark_quote_price: Decimal) -> Decimal:
        """Absolute exposure at a mark price, in the quote asset."""
        require_positive_decimal(mark_quote_price, "mark_quote_price")
        return abs(self.signed_base_quantity) * mark_quote_price

    def unrealised_pnl_quote(self, mark_quote_price: Decimal) -> Decimal:
        """Open PnL at a mark price, gross of the fee that closing it would cost."""
        require_positive_decimal(mark_quote_price, "mark_quote_price")
        return self.signed_base_quantity * (mark_quote_price - self.average_entry_quote_price)

    def with_fill(self, fill: Fill) -> PositionTransition:
        """Apply a fill and report what it did.

        Idempotent on `fill.fill_id`: a repeat returns a transition that changed
        nothing, rather than raising. Raising would force every at-least-once consumer
        to wrap the call in a try/except that is indistinguishable from swallowing a
        real error.
        """
        if fill.instrument != self.instrument:
            raise DomainError(
                f"cannot apply a {fill.instrument.symbol} fill to a "
                f"{self.instrument.symbol} position"
            )
        if fill.fill_id in self.applied_fill_ids:
            return PositionTransition(
                before=self,
                after=self,
                closed_base_quantity=_ZERO,
                opened_base_quantity=_ZERO,
                realised_pnl_quote=_ZERO,
                crossed_flat=False,
            )

        opening = self.signed_base_quantity == _ZERO or (
            (self.signed_base_quantity > _ZERO) == (fill.signed_base_quantity > _ZERO)
        )
        return self._opened(fill) if opening else self._reduced(fill)

    def _opened(self, fill: Fill) -> PositionTransition:
        """Open a new position or add to an existing one on the same side.

        The cost basis blends on magnitudes, so the same expression serves a long and
        a short without a sign branch.
        """
        after_base_quantity = self.signed_base_quantity + fill.signed_base_quantity
        prior_cost_quote = abs(self.signed_base_quantity) * self.average_entry_quote_price
        added_cost_quote = fill.base_quantity * fill.quote_price
        after = replace(
            self,
            signed_base_quantity=after_base_quantity,
            average_entry_quote_price=(prior_cost_quote + added_cost_quote)
            / abs(after_base_quantity),
            fee_quote_paid=self.fee_quote_paid + fill.fee_quote,
            opened_at_utc=self.opened_at_utc or fill.event_time_utc,
            applied_fill_ids=self.applied_fill_ids | {fill.fill_id},
        )
        return PositionTransition(
            before=self,
            after=after,
            closed_base_quantity=_ZERO,
            opened_base_quantity=fill.base_quantity,
            realised_pnl_quote=_ZERO,
            crossed_flat=False,
        )

    def _reduced(self, fill: Fill) -> PositionTransition:
        """Close some or all of the position, and open the other side if it overshoots."""
        held_base_quantity = abs(self.signed_base_quantity)
        closed_base_quantity = min(fill.base_quantity, held_base_quantity)
        held_sign = _LONG_SIGN if self.signed_base_quantity > _ZERO else _SHORT_SIGN
        # Signed once, at the end: a long realises (exit - entry) and a short realises
        # (entry - exit), which is the same expression through the held sign.
        realised_pnl_quote = (
            closed_base_quantity * (fill.quote_price - self.average_entry_quote_price) * held_sign
        )
        after_base_quantity = self.signed_base_quantity + fill.signed_base_quantity
        opened_base_quantity = fill.base_quantity - closed_base_quantity
        crossed_flat = opened_base_quantity > _ZERO

        if crossed_flat:
            # The remainder opens the opposite side at this fill's price. Carrying the
            # old average across the flip would price the new position with the cost
            # basis of the one that just closed.
            average_entry_quote_price = fill.quote_price
            opened_at_utc: datetime | None = fill.event_time_utc
        elif after_base_quantity == _ZERO:
            average_entry_quote_price = _ZERO
            opened_at_utc = None
        else:
            # A partial close does not touch the basis. Recomputing it here is the
            # classic error: it silently rewrites the cost of the remainder, so the
            # next close realises a PnL that no pair of trades ever produced.
            average_entry_quote_price = self.average_entry_quote_price
            opened_at_utc = self.opened_at_utc

        after = replace(
            self,
            signed_base_quantity=after_base_quantity,
            average_entry_quote_price=average_entry_quote_price,
            realised_pnl_quote=self.realised_pnl_quote + realised_pnl_quote,
            fee_quote_paid=self.fee_quote_paid + fill.fee_quote,
            opened_at_utc=opened_at_utc,
            applied_fill_ids=self.applied_fill_ids | {fill.fill_id},
        )
        return PositionTransition(
            before=self,
            after=after,
            closed_base_quantity=closed_base_quantity,
            opened_base_quantity=opened_base_quantity,
            realised_pnl_quote=realised_pnl_quote,
            crossed_flat=crossed_flat,
        )


@dataclass(frozen=True, slots=True)
class PositionTransition:
    """What one fill did to one position.

    `crossed_flat` is true only for a genuine flip -- the position was on one side,
    the fill overshot, and it is now on the other. A close to exactly zero is not a
    flip: it realises PnL once, opens nothing, and the distinction matters because a
    flip realises *and* opens in one event and must be audited as two economic
    actions.
    """

    before: Position
    after: Position
    closed_base_quantity: Decimal
    opened_base_quantity: Decimal
    realised_pnl_quote: Decimal
    crossed_flat: bool

    def __post_init__(self) -> None:
        require_non_negative_decimal(self.closed_base_quantity, "closed_base_quantity")
        require_non_negative_decimal(self.opened_base_quantity, "opened_base_quantity")
        require_decimal(self.realised_pnl_quote, "realised_pnl_quote")
        if self.before.instrument != self.after.instrument:
            raise DomainError(
                f"transition spans two instruments: {self.before.instrument.symbol} "
                f"and {self.after.instrument.symbol}"
            )
        if self.crossed_flat and not (
            self.closed_base_quantity > _ZERO and self.opened_base_quantity > _ZERO
        ):
            raise DomainError(
                "crossed_flat claims a flip, but the transition did not both close and open"
            )

    @property
    def is_noop(self) -> bool:
        """Whether the fill was a duplicate and changed nothing."""
        return self.before == self.after
