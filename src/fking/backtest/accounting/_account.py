"""The ledger: cash, positions, and the identity that ties them to equity.

Three decisions carry this module.

**Equity is `cash + Σ signed_quantity × mark`, and nothing else.** No separate unrealised
term is stored, because storing one creates a second number that must agree with the
first and will eventually not. The identity is linear in the signed quantity, so it holds
without a branch for a long, for a short, and across a flip -- which is exactly where a
hand-maintained unrealised accumulator goes wrong. A buy moves the same value out of cash
that it moves into exposure, so opening a position changes equity by the fee alone; that
is the invariant the property tests assert and it is what makes a fee visible in the
equity curve rather than in a footnote.

**Fees and realised PnL are derived from the positions, never stored beside them.**
`Position` already accumulates both. Mirroring them onto the account would put two
sources of truth one addition apart, and the drift between them would surface as a
reconciliation difference nobody could attribute.

**Idempotency is delegated for fills and owned for funding.** `Position.with_fill` is
already idempotent on `fill_id`, so a redelivered fill returns a no-op transition and the
account leaves cash alone on the strength of that. Funding has no such record anywhere
below, so the account keeps the applied keys itself. Delivery is at-least-once
(`CLAUDE.md` section 2) and a funding settlement applied twice is a charge the venue never
made, sitting permanently in the equity path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Final

from fking.backtest.accounting._errors import (
    AccountCurrencyError,
    AccountLedgerError,
    MarkUnavailableError,
)
from fking.backtest.accounting._funding import FundingKey, FundingSettlement
from fking.backtest.accounting._guards import require_finite_decimal
from fking.domain import Fill, Instrument, Position, PositionTransition

_ZERO: Final = Decimal("0")


@dataclass(frozen=True, slots=True)
class PortfolioAccount:
    """Cash and positions for one quote asset, as a value.

    One quote asset per account, enforced on every event. An account holding USDT cash
    cannot absorb an ETH-quoted fill without an exchange rate, and a rate carries an
    as-of time and a source that an addition cannot express. A run trading two quote
    assets runs two accounts and combines them where a rate is available and audited.

    `cash_quote` is signed and may go negative: that is a margin balance, and refusing it
    here would make the ledger unable to represent a leveraged position at all. What the
    ledger will not do is compute a *return* through non-positive total equity -- see
    `EquityPathRuinedError`.
    """

    quote_asset: str
    cash_quote: Decimal
    positions: tuple[Position, ...]
    settled_funding_quote: Decimal
    applied_funding_keys: frozenset[FundingKey]

    def __post_init__(self) -> None:
        require_finite_decimal(self.cash_quote, "cash_quote")
        require_finite_decimal(self.settled_funding_quote, "settled_funding_quote")
        seen: set[Instrument] = set()
        for position in self.positions:
            if position.instrument.quote_asset != self.quote_asset:
                raise AccountCurrencyError(
                    f"account is denominated in {self.quote_asset} but holds a "
                    f"{position.instrument.symbol} position quoted in "
                    f"{position.instrument.quote_asset}"
                )
            if position.instrument in seen:
                raise AccountLedgerError(
                    f"account holds two positions in {position.instrument.venue}:"
                    f"{position.instrument.symbol}; net them before constructing it"
                )
            seen.add(position.instrument)

    @classmethod
    def opened(cls, *, quote_asset: str, opening_cash_quote: Decimal) -> PortfolioAccount:
        """A fresh account with cash and no exposure."""
        return cls(
            quote_asset=quote_asset,
            cash_quote=require_finite_decimal(opening_cash_quote, "opening_cash_quote"),
            positions=(),
            settled_funding_quote=_ZERO,
            applied_funding_keys=frozenset(),
        )

    def position_for(self, instrument: Instrument) -> Position | None:
        """The position in `instrument`, or `None` when the account has never held one.

        `None` rather than a flat position: "we hold nothing" and "we have never traded
        this" are different facts, and only the first belongs in an exposure report.
        """
        for position in self.positions:
            if position.instrument == instrument:
                return position
        return None

    @property
    def open_positions(self) -> tuple[Position, ...]:
        """Only the positions carrying exposure right now."""
        return tuple(position for position in self.positions if position.signed_base_quantity != 0)

    @property
    def has_exposure(self) -> bool:
        """Whether any instrument carries a non-zero position."""
        return bool(self.open_positions)

    @property
    def fee_quote_paid(self) -> Decimal:
        """Every fee charged across every instrument, derived rather than mirrored."""
        return sum((position.fee_quote_paid for position in self.positions), _ZERO)

    @property
    def realised_pnl_quote(self) -> Decimal:
        """Realised trading PnL, gross of fees and gross of funding.

        Gross on both counts on purpose. A strategy whose gross edge is real and whose
        net result is negative is a capacity problem; one whose gross edge is zero is a
        dead strategy. Netting them into one number makes those indistinguishable, and
        they need different answers (`EVOLUTION_ENGINE.md`).
        """
        return sum((position.realised_pnl_quote for position in self.positions), _ZERO)

    @property
    def net_realised_pnl_quote(self) -> Decimal:
        """Realised PnL after the fees actually charged and the funding actually settled."""
        return self.realised_pnl_quote - self.fee_quote_paid + self.settled_funding_quote

    def exposure_quote(self, marks: Mapping[Instrument, Decimal]) -> Decimal:
        """Signed net exposure at `marks`, in the quote asset.

        Every open position must be priced. A missing mark raises rather than
        contributing zero: zero prices the instrument at nothing, which reads as a total
        loss on it and then as a full recovery at the next mark that does exist -- a
        drawdown the strategy never had, in the series the drawdown metrics come from.
        """
        exposure = _ZERO
        for position in self.open_positions:
            mark_quote_price = marks.get(position.instrument)
            if mark_quote_price is None:
                raise MarkUnavailableError(
                    f"no mark for {position.instrument.venue}:{position.instrument.symbol}, "
                    f"which carries {position.signed_base_quantity} "
                    f"{position.instrument.base_asset} of exposure"
                )
            require_finite_decimal(mark_quote_price, f"mark for {position.instrument.symbol}")
            if mark_quote_price <= _ZERO:
                raise MarkUnavailableError(
                    f"mark for {position.instrument.venue}:{position.instrument.symbol} is "
                    f"{mark_quote_price}; a non-positive mark is a missing observation "
                    f"wearing a number"
                )
            exposure += position.signed_base_quantity * mark_quote_price
        return exposure

    def equity_quote(self, marks: Mapping[Instrument, Decimal]) -> Decimal:
        """Cash plus signed exposure at `marks`.

        The one definition of equity in this package. Everything the metric suite
        computes is a function of this number sampled on the daily grid.
        """
        return self.cash_quote + self.exposure_quote(marks)

    def with_fill(self, fill: Fill) -> AccountTransition:
        """Apply a fill to cash and to the position it moves.

        Cash moves by the opposite of the exposure the fill adds, less the fee: a buy of
        one unit at 50,000 with a fee of 10 takes 50,010 out of cash and puts 50,000 of
        exposure in, so equity falls by exactly the fee. The same expression serves a
        sell without a sign branch, which is what keeps a flip correct.

        Idempotent on `fill.fill_id`, delegated to `Position`. A redelivered fill returns
        a transition that changed nothing rather than raising -- raising would force every
        at-least-once consumer to wrap the call in a `try` indistinguishable from
        swallowing a real error.
        """
        if fill.instrument.quote_asset != self.quote_asset:
            raise AccountCurrencyError(
                f"account is denominated in {self.quote_asset} but the fill on "
                f"{fill.instrument.symbol} is quoted in {fill.instrument.quote_asset}; "
                f"combining them needs a rate carrying its own as-of time"
            )

        before = self.position_for(fill.instrument) or Position.flat(fill.instrument)
        position_transition = before.with_fill(fill)
        if position_transition.is_noop:
            return AccountTransition(
                before=self,
                after=self,
                cash_change_quote=_ZERO,
                position_transition=position_transition,
                funding_settlement=None,
            )

        cash_change_quote = -fill.signed_base_quantity * fill.quote_price - fill.fee_quote
        after = replace(
            self,
            cash_quote=self.cash_quote + cash_change_quote,
            positions=self._replacing(position_transition.after),
        )
        return AccountTransition(
            before=self,
            after=after,
            cash_change_quote=cash_change_quote,
            position_transition=position_transition,
            funding_settlement=None,
        )

    def with_funding(self, settlement: FundingSettlement) -> AccountTransition:
        """Apply a funding settlement to cash.

        Idempotent on `(instrument, occurs_at_utc)`. Funding does not move a position, so
        `position_transition` stays `None` and the whole effect is on cash -- which is
        also why a duplicate is invisible in every position-level check and shows up only
        as an equity curve that drifts against the venue's.
        """
        if settlement.instrument.quote_asset != self.quote_asset:
            raise AccountCurrencyError(
                f"account is denominated in {self.quote_asset} but funding on "
                f"{settlement.instrument.symbol} settles in "
                f"{settlement.instrument.quote_asset}"
            )
        if settlement.key in self.applied_funding_keys:
            return AccountTransition(
                before=self,
                after=self,
                cash_change_quote=_ZERO,
                position_transition=None,
                funding_settlement=settlement,
            )

        cash_change_quote = settlement.signed_funding_quote
        after = replace(
            self,
            cash_quote=self.cash_quote + cash_change_quote,
            settled_funding_quote=self.settled_funding_quote + cash_change_quote,
            applied_funding_keys=self.applied_funding_keys | {settlement.key},
        )
        return AccountTransition(
            before=self,
            after=after,
            cash_change_quote=cash_change_quote,
            position_transition=None,
            funding_settlement=settlement,
        )

    def _replacing(self, position: Position) -> tuple[Position, ...]:
        """`positions` with `position` swapped in, or appended when it is new.

        Order is preserved rather than sorted, so the tuple is a stable function of the
        order instruments were first traded in. A re-sort on every fill would make the
        account's serialisation depend on symbol names, and the determinism check
        compares serialisations.
        """
        for index, held in enumerate(self.positions):
            if held.instrument == position.instrument:
                return (*self.positions[:index], position, *self.positions[index + 1 :])
        return (*self.positions, position)


@dataclass(frozen=True, slots=True)
class AccountTransition:
    """What one event did to the account.

    Both sides are carried, not only the result. The audit trail needs to answer "why did
    cash move by this much at this instant", and `cash_change_quote` alongside the
    position transition that produced it answers it without re-deriving anything -- which
    is the test `.claude/rules/append-only-audit.md` sets for whether a record is
    sufficient.
    """

    before: PortfolioAccount
    after: PortfolioAccount
    cash_change_quote: Decimal
    position_transition: PositionTransition | None
    funding_settlement: FundingSettlement | None

    @property
    def is_noop(self) -> bool:
        """Whether the event was a duplicate and changed nothing."""
        return self.before == self.after
