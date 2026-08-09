"""Portfolio state, advanced by fills, funding settlements and risk-limit breaches.

Every transition returns a new object. Nothing here mutates `self`, and the mapping
fields are copied into a `MappingProxyType` at construction -- `frozen=True` protects
the binding, not the object bound, and a `dict` field on a frozen dataclass is the
immutability bug that passes review because the decorator is right there at the top of
the class (`docs/rules/immutability.md`).

**Cash carries the full notional, on both sides.** A buy debits `quantity * price` plus
the fee; a sell credits it minus the fee. Equity is then `cash + sum(signed quantity x
mark)` for every open position, and that one identity is correct for a spot holding and
for a linear perpetual alike -- which matters because the metric suite must not need to
know which of the two produced the path.

**Idempotence is part of the type, not of the caller.** `applied_fill_ids` and
`applied_funding_keys` are state, so replaying a fill or a funding settlement returns
the same portfolio rather than doubling the position. Redis Streams delivery is
at-least-once and the backtest replays folds repeatedly; either route produces the same
event twice, and a consumer that assumed exactly-once would report the duplicate as a
reconciliation discrepancy pointing at the venue adapter.

**Risk-limit breaches are counted here rather than derived later.** A breach is an event
that happened during the run; reconstructing it afterwards from the equity path is
guesswork, and issue #38 requires that a run carrying one cannot be reported as clean.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Final
from uuid import UUID

from fking.backtest._guards import require_finite_decimal, require_positive_int, require_utc
from fking.backtest.portfolio._errors import MarkPriceMissingError, PortfolioAccountingError
from fking.domain import Fill, Instrument, Position

_ZERO: Final = Decimal("0")

type FundingKey = tuple[Instrument, datetime]


def _frozen_positions(
    positions: Mapping[Instrument, Position],
) -> Mapping[Instrument, Position]:
    for instrument, position in positions.items():
        if not isinstance(position, Position):
            raise PortfolioAccountingError(
                f"positions[{instrument.symbol!r}] must be a Position, "
                f"got {type(position).__name__}"
            )
        if position.instrument != instrument:
            # A key that disagrees with its value is what a partial rename leaves
            # behind, and every lookup afterwards silently marks another symbol.
            raise PortfolioAccountingError(
                f"positions is keyed {instrument.symbol!r} but holds a "
                f"{position.instrument.symbol} position"
            )
    return MappingProxyType(dict(positions))


@dataclass(frozen=True, slots=True)
class PortfolioState:
    """Everything the books know at one instant.

    `realised_pnl_usd` and `fee_paid_usd` are derived from the positions rather than
    stored beside them. Two fields that must agree are two fields that eventually do
    not, and the disagreement surfaces as a PnL attribution that no pair of trades
    produced.

    The `_usd` suffix asserts that the quote asset is treated as one dollar. Binance
    USDT pairs are quoted in USDT, which is correct for reporting and wrong during a
    depeg; the assumption is stated here rather than implied by every field name
    downstream.
    """

    as_of_utc: datetime
    quote_cash_usd: Decimal
    positions: Mapping[Instrument, Position]
    applied_fill_ids: frozenset[UUID]
    applied_funding_keys: frozenset[FundingKey]
    funding_paid_usd: Decimal
    risk_limit_breach_count: int

    def __post_init__(self) -> None:
        require_utc(self.as_of_utc, "as_of_utc")
        require_finite_decimal(self.quote_cash_usd, "quote_cash_usd")
        require_finite_decimal(self.funding_paid_usd, "funding_paid_usd")
        if self.risk_limit_breach_count < 0:
            raise PortfolioAccountingError(
                f"risk_limit_breach_count must not be negative; got {self.risk_limit_breach_count}"
            )
        if not isinstance(self.applied_fill_ids, frozenset):
            raise PortfolioAccountingError(
                f"applied_fill_ids must be a frozenset, got {type(self.applied_fill_ids).__name__}"
            )
        if not isinstance(self.applied_funding_keys, frozenset):
            raise PortfolioAccountingError(
                f"applied_funding_keys must be a frozenset, got "
                f"{type(self.applied_funding_keys).__name__}"
            )
        object.__setattr__(self, "positions", _frozen_positions(self.positions))

    @classmethod
    def opening(cls, *, as_of_utc: datetime, starting_cash_usd: Decimal) -> PortfolioState:
        """A portfolio holding cash and nothing else."""
        require_finite_decimal(starting_cash_usd, "starting_cash_usd")
        if starting_cash_usd <= _ZERO:
            raise PortfolioAccountingError(
                f"starting_cash_usd must be positive; got {starting_cash_usd}. A run "
                f"opened at zero equity has no denominator for a return"
            )
        return cls(
            as_of_utc=as_of_utc,
            quote_cash_usd=starting_cash_usd,
            positions={},
            applied_fill_ids=frozenset(),
            applied_funding_keys=frozenset(),
            funding_paid_usd=_ZERO,
            risk_limit_breach_count=0,
        )

    @property
    def fill_count(self) -> int:
        """How many distinct fills have been applied.

        Reported for the record and deliberately absent from every path statistic: two
        strategies with identical daily equity curves and a tenfold difference in this
        number receive identical Sharpes (issue #38).
        """
        return len(self.applied_fill_ids)

    @property
    def realised_pnl_usd(self) -> Decimal:
        """Realised PnL across every instrument, gross of fees."""
        return sum(
            (position.realised_pnl_quote for position in self.positions.values()), start=_ZERO
        )

    @property
    def fee_paid_usd(self) -> Decimal:
        """Every fee actually charged, summed across instruments. A charge, not a rate."""
        return sum((position.fee_quote_paid for position in self.positions.values()), start=_ZERO)

    @property
    def open_positions(self) -> tuple[Position, ...]:
        """Only the positions carrying exposure right now."""
        return tuple(
            position
            for _, position in sorted(self.positions.items(), key=lambda entry: entry[0].symbol)
            if position.signed_base_quantity != _ZERO
        )

    @property
    def is_in_market(self) -> bool:
        """Whether any exposure is held at this instant."""
        return bool(self.open_positions)

    def equity_usd(self, mark_quote_prices: Mapping[Instrument, Decimal]) -> Decimal:
        """Cash plus every open position marked to the supplied prices.

        A missing mark for a held position is refused rather than defaulted, because
        both plausible defaults hide a drawdown for exactly as long as the gap lasts.
        """
        equity = self.quote_cash_usd
        for position in self.open_positions:
            mark = mark_quote_prices.get(position.instrument)
            if mark is None:
                raise MarkPriceMissingError(
                    f"no mark supplied for the open {position.instrument.symbol} position "
                    f"of {position.signed_base_quantity} at {self.as_of_utc.isoformat()}"
                )
            require_finite_decimal(mark, f"mark for {position.instrument.symbol}")
            if mark <= _ZERO:
                raise MarkPriceMissingError(
                    f"mark for {position.instrument.symbol} is {mark}; a non-positive "
                    f"mark is a feed fault, not a price"
                )
            equity += position.signed_base_quantity * mark
        return equity

    def _advanced_to(self, moment: datetime, what: str) -> datetime:
        require_utc(moment, f"{what} instant")
        if moment < self.as_of_utc:
            raise PortfolioAccountingError(
                f"{what} is stamped {moment.isoformat()}, before the portfolio's own "
                f"{self.as_of_utc.isoformat()}. The books cannot be advanced backwards"
            )
        return moment

    def with_fill(self, fill: Fill) -> PortfolioState:
        """Apply one execution. Idempotent on `fill.fill_id`.

        A repeat returns `self` rather than raising: raising would force every
        at-least-once consumer to wrap the call in a handler that is indistinguishable
        from swallowing a real error.
        """
        if fill.fill_id in self.applied_fill_ids:
            return self
        as_of_utc = self._advanced_to(fill.event_time_utc, f"fill {fill.venue_trade_id}")

        held = self.positions.get(fill.instrument) or Position.flat(fill.instrument)
        transition = held.with_fill(fill)
        # A buy debits the notional and the fee; a sell credits the notional and still
        # pays the fee. `signed_base_quantity` carries the direction, so one expression
        # covers both and no branch can disagree with the position's own arithmetic.
        cash_delta = -(fill.signed_base_quantity * fill.quote_price) - fill.fee_quote

        return replace(
            self,
            as_of_utc=as_of_utc,
            quote_cash_usd=self.quote_cash_usd + cash_delta,
            positions={**self.positions, fill.instrument: transition.after},
            applied_fill_ids=self.applied_fill_ids | {fill.fill_id},
        )

    def with_funding(
        self,
        *,
        instrument: Instrument,
        occurs_at_utc: datetime,
        funding_rate: Decimal,
        mark_quote_price: Decimal,
    ) -> PortfolioState:
        """Settle funding on the position held at this exact instant.

        Charged on the holding at the settlement instant and never on average exposure.
        The discreteness is real and exploitable: a strategy flat thirty seconds before
        settlement pays nothing and one holding through pays in full, so modelling it as
        an accrual would price away a cost a strategy can genuinely dodge.

        Idempotent on `(instrument, occurs_at_utc)` -- the venue's own identity for the
        settlement, not an id this process minted, which would be fresh on every replay.
        """
        require_finite_decimal(funding_rate, "funding_rate")
        require_finite_decimal(mark_quote_price, "mark_quote_price")
        if mark_quote_price <= _ZERO:
            raise MarkPriceMissingError(
                f"funding mark for {instrument.symbol} is {mark_quote_price}; a "
                f"non-positive mark is a feed fault, not a price"
            )
        key: FundingKey = (instrument, occurs_at_utc)
        if key in self.applied_funding_keys:
            return self
        as_of_utc = self._advanced_to(occurs_at_utc, f"funding on {instrument.symbol}")

        held = self.positions.get(instrument)
        signed_base_quantity = held.signed_base_quantity if held is not None else _ZERO
        # Signed, and dimensionless: longs pay the shorts when the rate is positive, so
        # a long holding produces a positive payment and a debit to cash.
        payment_usd = signed_base_quantity * mark_quote_price * funding_rate

        return replace(
            self,
            as_of_utc=as_of_utc,
            quote_cash_usd=self.quote_cash_usd - payment_usd,
            applied_funding_keys=self.applied_funding_keys | {key},
            funding_paid_usd=self.funding_paid_usd + payment_usd,
        )

    def with_risk_limit_breach(
        self, *, occurs_at_utc: datetime, breach_count: int = 1
    ) -> PortfolioState:
        """Record that a risk limit was breached during the run.

        Recorded rather than reconstructed. A breach is an event, and a run carrying one
        cannot afterwards be read as a clean result -- `PortfolioReport.is_clean` is
        derived from this counter and there is no field that overrides it.
        """
        require_positive_int(breach_count, "breach_count")
        return replace(
            self,
            as_of_utc=self._advanced_to(occurs_at_utc, "risk-limit breach"),
            risk_limit_breach_count=self.risk_limit_breach_count + breach_count,
        )
