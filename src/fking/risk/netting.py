"""Cross-strategy netting, and the attribution that has to sum to reality.

Two strategies with opposing signals on the same symbol produce one net order, not two
offsetting ones. Sending both pays the spread twice for a zero net position, and on a
symbol with a 7.5bp testnet spread that is not a rounding error.

The part that is easy to get wrong is not the arithmetic, it is the booking. When
strategies cross internally, a strategy's "fill" corresponds to no venue trade. The naive
implementation invents a synthetic price for that side -- usually the strategy's own
signal price -- and from that moment total attributed PnL stops equalling venue PnL. Every
survival score computed downstream is then measuring fiction, and nothing in the system
notices, because both halves are internally consistent. `SCORING_ENGINE.md` is built on
attribution being real, so this is a precondition rather than bookkeeping hygiene.

The rule, from issue #55:

- The crossed portion books at **the same price as the venue portion of the same net
  order** -- at decision time the reference mark, replaced by the realised VWAP through
  `Attribution.rebook` once the fills are known. One price for every attribution is what
  makes `crossing_residual_at_decision_quote` identically zero, and that zero is the
  machine-checkable statement of "attribution sums to reality".
- With no venue portion at all -- a perfect internal cross -- there is no venue price to
  book at, so it books at the prevailing mid at decision time and the difference against
  the next observed trade is charged to a `crossing_residual` account rather than to
  either strategy. Charging it to a strategy would make one of them pay for the other's
  liquidity, which is a transfer nobody authorised and which the strategy cannot see.

Quantization is the second trap. Sizing produces per-strategy quantities already snapped
onto the venue lattice, but their *sum* need not be a multiple of `lot_step`, and the net
order must be. Truncating the net without touching the attributions leaves them summing to
something the venue never traded -- a discrepancy smaller than one lot step, which is
exactly the size that survives review and then accumulates across thousands of decisions.
So the truncation is absorbed from the surplus side, largest position first, and the
attributions sum to the net order exactly.

Pure: no clock, no I/O, no randomness. Every instant arrives as an argument.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Final

from fking.domain import DomainError, Instrument

__all__ = [
    "CROSSING_RESIDUAL_ACCOUNT",
    "Attribution",
    "BookingBasis",
    "CrossingResidual",
    "NetOrderPlan",
    "StrategyRequest",
    "net_requests",
]

_ZERO: Final = Decimal("0")

# The account the internalisation difference is charged to. A named constant rather than a
# string literal at the two call sites, because a typo in one of them produces a second
# account that balances by itself and reconciles against nothing.
CROSSING_RESIDUAL_ACCOUNT: Final = "crossing_residual"


class BookingBasis(StrEnum):
    """Which price an attribution is booked at, and therefore what it still owes.

    The distinction is not cosmetic. `VENUE_VWAP` is a promise that the price will be
    replaced by an observed one; `DECISION_MID` is an admission that no venue trade will
    ever exist for this quantity. A reader six months from now needs to know which of the
    two produced the number in front of them, and a single `Decimal` cannot say.
    """

    VENUE_VWAP = "venue_vwap"
    DECISION_MID = "decision_mid"


def _require_positive_price(candidate: object, field_name: str) -> None:
    """A finite, strictly positive `Decimal`.

    `object` rather than `Decimal` so the `float` branch is reachable. Typing the parameter
    as `Decimal` makes mypy prove the isinstance can never be true and report the guard as
    dead -- which is true of every *typed* caller and false of the untyped ones, and it is
    the untyped ones this exists for (`fking.risk.drawdown` takes the same shape).
    """
    if isinstance(candidate, float):
        raise DomainError(
            f"{field_name} must be a Decimal constructed from str, not a float; "
            f"got {candidate!r}, which is already rounded"
        )
    if not isinstance(candidate, Decimal):
        raise DomainError(
            f"{field_name} must be a Decimal, got {type(candidate).__name__} {candidate!r}"
        )
    if not candidate.is_finite() or candidate <= _ZERO:
        raise DomainError(f"{field_name} must be a finite positive price; got {candidate}")


def _require_finite_quantity(candidate: object, field_name: str) -> None:
    """A finite `Decimal` of any sign. `object` for the reason above."""
    if isinstance(candidate, float):
        raise DomainError(
            f"{field_name} must be a Decimal constructed from str, not a float; "
            f"got {candidate!r}, which is already rounded"
        )
    if not isinstance(candidate, Decimal):
        raise DomainError(
            f"{field_name} must be a Decimal, got {type(candidate).__name__} {candidate!r}"
        )
    if not candidate.is_finite():
        raise DomainError(f"{field_name} must be finite; got {candidate}")


@dataclass(frozen=True, slots=True)
class StrategyRequest:
    """One strategy's authorised quantity on one instrument, signed and already sized.

    Signed rather than a `(Direction, magnitude)` pair: netting adds these, and a pair
    forces every caller to reconstruct the sign, which is where somebody eventually
    reconstructs it backwards and a hedge is booked as a doubled position.

    Zero is legal and meaningful -- a flat signal whose position was already closed by an
    earlier signal in the same batch requested a real thing and got nothing, and dropping
    it would erase that from the audit row.
    """

    strategy_id: str
    signed_base_quantity: Decimal

    def __post_init__(self) -> None:
        if not self.strategy_id:
            raise DomainError("a netting request must name the strategy it came from")
        _require_finite_quantity(self.signed_base_quantity, "signed_base_quantity")


@dataclass(frozen=True, slots=True)
class Attribution:
    """What one strategy is booked for out of one net order.

    `booked_quote_price` is the price this strategy's slice is recorded at, and it is the
    same value for every attribution on the same plan. That equality is the invariant --
    the moment one slice is booked at a different price from another, attributed notional
    stops summing to venue notional and `crossing_residual_at_decision_quote` stops being
    zero.
    """

    strategy_id: str
    signed_base_quantity: Decimal
    booked_quote_price: Decimal
    booking_basis: BookingBasis

    def __post_init__(self) -> None:
        _require_positive_price(self.booked_quote_price, "booked_quote_price")

    @property
    def signed_notional_quote(self) -> Decimal:
        """Value booked to this strategy: positive when long, negative when short."""
        return self.signed_base_quantity * self.booked_quote_price

    def rebook(self, realised_vwap_quote_price: Decimal) -> Attribution:
        """This attribution at the venue's realised VWAP. Returns a new object.

        Refused on a `DECISION_MID` attribution, and the refusal is the point: that
        quantity crossed internally and produced no venue trade, so there is no VWAP that
        belongs to it. Accepting one would let a caller launder an unrelated venue price
        onto an internal cross, which is the synthetic-price bug wearing a plausible name.
        """
        if self.booking_basis is not BookingBasis.VENUE_VWAP:
            raise DomainError(
                f"{self.strategy_id} is booked on {self.booking_basis}; there is no venue "
                f"trade behind that quantity and no VWAP that belongs to it"
            )
        _require_positive_price(realised_vwap_quote_price, "realised_vwap_quote_price")
        return replace(self, booked_quote_price=realised_vwap_quote_price)


@dataclass(frozen=True, slots=True)
class CrossingResidual:
    """The internally crossed quantity, and where its valuation difference is charged.

    Emitted whenever any quantity crossed, including the ordinary partial cross where a
    venue portion exists. In that case `settle_against` is exactly zero, and recording the
    row anyway is what makes "how much crossed internally today" answerable from the audit
    log rather than inferable from the absence of rows.
    """

    symbol: str
    crossed_base_quantity: Decimal
    booked_quote_price: Decimal
    booking_basis: BookingBasis
    account: str = CROSSING_RESIDUAL_ACCOUNT

    def __post_init__(self) -> None:
        if self.crossed_base_quantity <= _ZERO:
            raise DomainError(
                f"crossed_base_quantity is {self.crossed_base_quantity}; a residual row "
                f"for nothing crossed is a row that reconciles against nothing"
            )
        _require_positive_price(self.booked_quote_price, "booked_quote_price")
        if self.account != CROSSING_RESIDUAL_ACCOUNT:
            raise DomainError(
                f"account is {self.account!r}; the internalisation difference belongs to "
                f"{CROSSING_RESIDUAL_ACCOUNT!r} and to no strategy"
            )

    def settle_against(self, next_observed_quote_price: Decimal) -> Decimal:
        """The charge to `crossing_residual` once the market prints again.

        Zero on a `VENUE_VWAP` basis: the crossed portion was booked at the same price the
        venue actually traded at, so there is nothing left over. Non-zero on a
        `DECISION_MID` basis, because a mid is not a price anybody traded at -- one side
        would have paid the ask and the other received the bid -- and the difference has to
        land somewhere that is not either strategy's record.
        """
        if self.booking_basis is not BookingBasis.DECISION_MID:
            return _ZERO
        _require_positive_price(next_observed_quote_price, "next_observed_quote_price")
        return self.crossed_base_quantity * (next_observed_quote_price - self.booked_quote_price)


@dataclass(frozen=True, slots=True)
class NetOrderPlan:
    """One instrument's netted result: the venue quantity, and who owns which part of it.

    `net_signed_base_quantity` of zero is a successful outcome, not a refusal. It means the
    batch crossed perfectly and the venue is owed nothing, which is the case the whole
    module exists for.
    """

    instrument: Instrument
    net_signed_base_quantity: Decimal
    reference_quote_price: Decimal
    attributions: tuple[Attribution, ...]
    crossed_base_quantity: Decimal
    residual: CrossingResidual | None

    @property
    def symbol(self) -> str:
        """The instrument's symbol. The instrument itself is the key -- the same symbol
        exists on two venues with different filters, and keying by string merges them."""
        return self.instrument.symbol

    @property
    def has_venue_portion(self) -> bool:
        """Whether any quantity at all reaches the venue."""
        return self.net_signed_base_quantity != _ZERO

    @property
    def venue_signed_notional_quote(self) -> Decimal:
        """What the venue leg is worth at the reference price. Zero on a perfect cross."""
        return self.net_signed_base_quantity * self.reference_quote_price

    @property
    def attributed_signed_base_quantity(self) -> Decimal:
        """Every strategy's slice, summed. Equal to the net quantity by construction."""
        return sum(
            (attribution.signed_base_quantity for attribution in self.attributions), start=_ZERO
        )

    @property
    def attributed_signed_notional_quote(self) -> Decimal:
        """Every strategy's booked value, summed."""
        return sum(
            (attribution.signed_notional_quote for attribution in self.attributions), start=_ZERO
        )

    @property
    def crossing_residual_at_decision_quote(self) -> Decimal:
        """Attributed notional minus venue notional, at decision time.

        Computed rather than asserted to be zero. It *is* zero for every plan this module
        builds, and `tests/property/test_netting_properties.py` proves it over partial
        crosses, perfect crosses, flips and dust -- but computing it means the day somebody
        books a crossed slice at a synthetic price, the number stops being zero and a test
        fails, instead of the discrepancy quietly entering the scoring engine.
        """
        return self.attributed_signed_notional_quote - self.venue_signed_notional_quote


def _absorb_quantization(
    requests: tuple[StrategyRequest, ...], *, adjustment: Decimal
) -> tuple[Decimal, ...]:
    """Push `adjustment` onto the surplus side, largest holding first.

    The surplus side is the one the net quantity points at, because that is the only side
    with enough magnitude to give up: the sum of its magnitudes is at least the absolute
    raw net, which is at least the adjustment. Taking it from the other side would enlarge
    the net rather than shrink it.

    Largest first, with the strategy id breaking ties, so the allocation is a pure function
    of the inputs. Anything order-dependent here would make two replays of the same batch
    attribute the same trade differently.
    """
    quantities = [request.signed_base_quantity for request in requests]
    if adjustment == _ZERO:
        return tuple(quantities)

    # `adjustment` moves the net toward zero, so it always opposes the surplus side's sign.
    surplus_is_long = adjustment < _ZERO
    on_surplus_side = [
        index
        for index, quantity in enumerate(quantities)
        if (quantity > _ZERO if surplus_is_long else quantity < _ZERO)
    ]
    on_surplus_side.sort(key=lambda index: (-abs(quantities[index]), requests[index].strategy_id))

    remaining = abs(adjustment)
    for index in on_surplus_side:
        if remaining <= _ZERO:
            break
        taken = min(abs(quantities[index]), remaining)
        quantities[index] += -taken if surplus_is_long else taken
        remaining -= taken

    if remaining > _ZERO:  # pragma: no cover - unreachable: see the docstring above
        raise DomainError(
            f"{remaining} of lot-step truncation could not be attributed; the surplus side "
            f"is smaller than the net it produced, which is arithmetically impossible"
        )
    return tuple(quantities)


def net_requests(
    *,
    instrument: Instrument,
    requests: tuple[StrategyRequest, ...],
    reference_quote_price: Decimal,
    decision_mid_quote_price: Decimal,
) -> NetOrderPlan:
    """Net one instrument's authorised quantities into a single plan.

    `reference_quote_price` is the mark the venue portion is priced at for the audit row;
    it is superseded by the realised VWAP through `Attribution.rebook`.
    `decision_mid_quote_price` is used only when nothing reaches the venue, and it is a
    separate parameter rather than a default for the first because a mid and a mark are
    different numbers with different provenance, and silently substituting one for the
    other is how a booking price ends up being whichever was in scope.
    """
    if not requests:
        raise DomainError(
            f"netting {instrument.symbol} with no requests; an empty batch produces no "
            f"plan, and a plan for nothing would be indistinguishable from a perfect cross"
        )
    seen: set[str] = set()
    for request in requests:
        if request.strategy_id in seen:
            raise DomainError(
                f"{request.strategy_id} appears twice in the netting batch for "
                f"{instrument.symbol}; two slices for one strategy cannot be attributed"
            )
        seen.add(request.strategy_id)
    _require_positive_price(reference_quote_price, "reference_quote_price")
    _require_positive_price(decision_mid_quote_price, "decision_mid_quote_price")

    raw_net = sum((request.signed_base_quantity for request in requests), start=_ZERO)
    net_signed_base_quantity = instrument.quantize_base_quantity(raw_net)
    adjusted = _absorb_quantization(requests, adjustment=net_signed_base_quantity - raw_net)

    has_venue_portion = net_signed_base_quantity != _ZERO
    basis = BookingBasis.VENUE_VWAP if has_venue_portion else BookingBasis.DECISION_MID
    booked_quote_price = reference_quote_price if has_venue_portion else decision_mid_quote_price

    attributions = tuple(
        Attribution(
            strategy_id=request.strategy_id,
            signed_base_quantity=quantity,
            booked_quote_price=booked_quote_price,
            booking_basis=basis,
        )
        for request, quantity in zip(requests, adjusted, strict=True)
    )

    gross_long = sum((quantity for quantity in adjusted if quantity > _ZERO), start=_ZERO)
    gross_short = -sum((quantity for quantity in adjusted if quantity < _ZERO), start=_ZERO)
    crossed_base_quantity = min(gross_long, gross_short)

    residual = (
        CrossingResidual(
            symbol=instrument.symbol,
            crossed_base_quantity=crossed_base_quantity,
            booked_quote_price=booked_quote_price,
            booking_basis=basis,
        )
        if crossed_base_quantity > _ZERO
        else None
    )

    return NetOrderPlan(
        instrument=instrument,
        net_signed_base_quantity=net_signed_base_quantity,
        reference_quote_price=booked_quote_price,
        attributions=attributions,
        crossed_base_quantity=crossed_base_quantity,
        residual=residual,
    )
