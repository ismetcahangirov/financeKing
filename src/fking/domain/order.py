"""The order path's two records: what was asked for, and what happened."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fking.domain._errors import DomainError
from fking.domain._guards import (
    require_non_negative_decimal,
    require_positive_decimal,
    require_text,
    require_utc,
)
from fking.domain.enums import OrderType, Side, TimeInForce
from fking.domain.instrument import Instrument


def _require_uuid(candidate: object, field_name: str) -> UUID:
    if not isinstance(candidate, UUID):
        raise DomainError(
            f"{field_name} must be a UUID, got {type(candidate).__name__} {candidate!r}"
        )
    return candidate


@dataclass(frozen=True, slots=True)
class Order:
    """An instruction to the venue. Constructed only by the risk engine.

    `client_order_id` is the idempotency key and is derived deterministically from the
    correlation id and the order's own content, never randomly and never from a
    counter. That is what makes a retried submission after a timeout recognisable to
    the venue as the same order rather than a second one
    (`.claude/rules/idempotency.md`). This type holds it; deriving it is `execution`'s
    job, because the derivation needs the venue's charset and length limits.

    `base_quantity` is unsigned. Direction lives in `side`, so there is no way to
    express a negative buy -- a combination that type-checks and means nothing.
    """

    order_id: UUID
    client_order_id: str
    correlation_id: UUID
    instrument: Instrument
    side: Side
    order_type: OrderType
    time_in_force: TimeInForce
    base_quantity: Decimal
    limit_quote_price: Decimal | None
    created_at_utc: datetime

    def __post_init__(self) -> None:
        _require_uuid(self.order_id, "order_id")
        _require_uuid(self.correlation_id, "correlation_id")
        require_text(self.client_order_id, "client_order_id")
        require_positive_decimal(self.base_quantity, "base_quantity")
        require_utc(self.created_at_utc, "created_at_utc")

        if self.order_type is OrderType.LIMIT:
            if self.limit_quote_price is None:
                raise DomainError(
                    f"limit order {self.client_order_id} carries no limit_quote_price"
                )
            require_positive_decimal(self.limit_quote_price, "limit_quote_price")
        elif self.limit_quote_price is not None:
            # Not merely redundant: a market order carrying a price is one somebody
            # meant to send as a limit, and the venue will silently ignore the field
            # and fill at whatever the book offers.
            raise DomainError(
                f"market order {self.client_order_id} carries limit_quote_price "
                f"{self.limit_quote_price}; the venue ignores it and fills at market"
            )

    @property
    def signed_base_quantity(self) -> Decimal:
        """The quantity as it will change a position: positive to buy, negative to sell."""
        return self.base_quantity * self.side.signed_multiplier


@dataclass(frozen=True, slots=True)
class Fill:
    """One execution reported by the venue.

    `fee_quote` is the fee *charged*, not a rate. The two differ by a factor of the
    notional, and the field name says which this is because `.claude/rules/naming.md`
    bans the bare `fee` that would leave it open.

    A venue may report the commission in an asset that is not the instrument's quote
    asset -- Binance charges in BNB when the account holds it. Normalising that is the
    venue adapter's job; this type records a quote-denominated charge and nothing else,
    so an unconverted BNB fee cannot enter the books wearing a USDT label.
    """

    fill_id: UUID
    order_id: UUID
    venue_trade_id: str
    instrument: Instrument
    side: Side
    event_time_utc: datetime
    quote_price: Decimal
    base_quantity: Decimal
    fee_quote: Decimal

    def __post_init__(self) -> None:
        _require_uuid(self.fill_id, "fill_id")
        _require_uuid(self.order_id, "order_id")
        require_text(self.venue_trade_id, "venue_trade_id")
        # The exchange's timestamp, never ours: our clock's relationship to the
        # venue's is an unknown offset plus network delay, and Binance rejects
        # requests outside recvWindow with -1021 precisely because that skew is real.
        require_utc(self.event_time_utc, "event_time_utc")
        require_positive_decimal(self.quote_price, "quote_price")
        require_positive_decimal(self.base_quantity, "base_quantity")
        require_non_negative_decimal(self.fee_quote, "fee_quote")

    @property
    def signed_base_quantity(self) -> Decimal:
        """How this fill moves a position: positive for a buy, negative for a sell."""
        return self.base_quantity * self.side.signed_multiplier

    @property
    def notional_quote(self) -> Decimal:
        """Executed value in the quote asset, gross of the fee."""
        return self.quote_price * self.base_quantity
