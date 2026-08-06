"""Funding, turned from a rate into a charge against the account.

`fking.backtest.FundingEvent` carries the venue's *rate*. The ledger needs a *charge*,
and the two are separated on purpose: the charge depends on the position held at the
settlement instant and on the mark at that instant, neither of which the event knows.
Computing it inside the account would bury that dependence in a method; computing it
here makes the three inputs visible in one signature and the arithmetic testable without
an account.

The settlement is idempotent on `(instrument, occurs_at_utc)` -- the venue's own identity
for the event -- rather than on a producer-minted id. Binance settles funding on a fixed
eight-hour schedule per symbol, so that pair names exactly one settlement and keeps naming
it across a replay, a reconnect and a backfill that re-emits the day. A producer id
changes on every one of those, and the second application charges the position twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from fking.backtest.accounting._errors import AccountLedgerError
from fking.backtest.accounting._guards import require_finite_decimal, require_utc
from fking.domain import Instrument, Position

_ZERO: Final = Decimal("0")


@dataclass(frozen=True, slots=True)
class FundingKey:
    """The venue's identity for one funding settlement."""

    instrument: Instrument
    occurs_at_utc: datetime


@dataclass(frozen=True, slots=True)
class FundingSettlement:
    """One funding payment, with everything it was derived from.

    The rate, the mark and the exposure are all kept rather than only the resulting
    charge. `ARCHITECTURE.md` section 11 requires a trade to be reconstructable from the
    record alone, and "why was this account charged 4.12 USDT at 08:00" is answerable
    from those three numbers and unanswerable from the charge.

    `signed_funding_quote` is positive when the account *received* funding and negative
    when it paid. A magnitude plus a direction flag would let a caller add where it
    should have subtracted, and the two spellings type-check identically.
    """

    instrument: Instrument
    occurs_at_utc: datetime
    funding_rate: Decimal
    mark_quote_price: Decimal
    signed_base_quantity: Decimal
    signed_funding_quote: Decimal

    def __post_init__(self) -> None:
        require_utc(self.occurs_at_utc, "occurs_at_utc")
        require_finite_decimal(self.funding_rate, "funding_rate")
        require_finite_decimal(self.mark_quote_price, "mark_quote_price")
        require_finite_decimal(self.signed_base_quantity, "signed_base_quantity")
        require_finite_decimal(self.signed_funding_quote, "signed_funding_quote")
        if self.mark_quote_price <= _ZERO:
            raise AccountLedgerError(
                f"funding on {self.instrument.symbol} at {self.occurs_at_utc.isoformat()} "
                f"marks the position at {self.mark_quote_price}; a non-positive mark "
                f"prices the notional the charge is a fraction of"
            )

    @property
    def key(self) -> FundingKey:
        """The pair the ledger dedupes on."""
        return FundingKey(instrument=self.instrument, occurs_at_utc=self.occurs_at_utc)


def settle_funding(
    *,
    position: Position,
    occurs_at_utc: datetime,
    funding_rate: Decimal,
    mark_quote_price: Decimal,
) -> FundingSettlement:
    """The charge a funding settlement lays on the position held at that exact instant.

    Charged on the exposure held *at* the settlement, never on average exposure over the
    interval. The discreteness is real and exploitable -- a strategy flat thirty seconds
    before settlement pays nothing and one holding through pays in full -- and smoothing
    it would make a funding-timing strategy look like a funding-carry strategy.

    Longs pay when the rate is positive, which is why the exposure enters negated: a long
    of 2 BTC at a rate of 0.0001 is charged, a short of 2 BTC at the same rate is paid.
    """
    require_utc(occurs_at_utc, "occurs_at_utc")
    require_finite_decimal(funding_rate, "funding_rate")
    require_finite_decimal(mark_quote_price, "mark_quote_price")
    return FundingSettlement(
        instrument=position.instrument,
        occurs_at_utc=occurs_at_utc,
        funding_rate=funding_rate,
        mark_quote_price=mark_quote_price,
        signed_base_quantity=position.signed_base_quantity,
        signed_funding_quote=-position.signed_base_quantity * mark_quote_price * funding_rate,
    )
