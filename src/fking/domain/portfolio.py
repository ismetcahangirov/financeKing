"""Aggregates: everything held on one venue, and everything held everywhere.

Both types wrap their mapping fields in `MappingProxyType` inside `__post_init__`.
`frozen=True` protects the *binding*, not the object bound -- a `dict` field on a
frozen dataclass is still mutable through the reference, and that is the immutability
bug that passes review because `frozen=True` is right there at the top of the class
and the reviewer stops reading. The copy plus the proxy is what makes the guarantee
real (`.claude/rules/immutability.md`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType

from fking.domain._errors import DomainError
from fking.domain._guards import require_text, require_utc
from fking.domain.enums import Venue
from fking.domain.instrument import Instrument
from fking.domain.money import Balance
from fking.domain.position import Position


def _frozen_balances(balances: Mapping[str, Balance], field_name: str) -> Mapping[str, Balance]:
    for asset, balance in balances.items():
        if not isinstance(balance, Balance):
            raise DomainError(
                f"{field_name}[{asset!r}] must be a Balance, got {type(balance).__name__}"
            )
        if balance.asset != asset:
            # A key that disagrees with its value is the shape a partial rename leaves
            # behind, and every lookup afterwards silently returns another asset's
            # number.
            raise DomainError(
                f"{field_name} is keyed {asset!r} but holds a {balance.asset} balance"
            )
    return MappingProxyType(dict(balances))


@dataclass(frozen=True, slots=True)
class Account:
    """What one venue reports about one set of credentials.

    `updated_at_utc` is the venue's own timestamp for the snapshot, not our receipt
    time. Reconciliation compares this against local state and needs to know which of
    the two is stale; a locally stamped time answers that question with our clock,
    which is the thing under suspicion.
    """

    venue: Venue
    account_id: str
    balances: Mapping[str, Balance]
    updated_at_utc: datetime

    def __post_init__(self) -> None:
        require_text(self.account_id, "account_id")
        require_utc(self.updated_at_utc, "updated_at_utc")
        object.__setattr__(self, "balances", _frozen_balances(self.balances, "balances"))

    def free_quantity(self, asset: str) -> Decimal:
        """Free balance of `asset`, or zero if the venue reports none.

        Zero rather than `None` is safe here and only here: the venue enumerates every
        asset it holds, so an absent key means a balance of nothing rather than an
        unknown balance. That is not true of a partial response, which is why the venue
        adapter parses rather than indexes.
        """
        held = self.balances.get(asset)
        return held.free_quantity if held is not None else Decimal("0")


@dataclass(frozen=True, slots=True)
class Portfolio:
    """Every position and cash balance, at one instant.

    Positions are a tuple rather than a mapping keyed by symbol, because the same
    symbol can exist on two venues with different filters and a symbol-keyed map
    silently merges them. `position_for` takes the whole `Instrument` for the same
    reason.
    """

    as_of_utc: datetime
    positions: tuple[Position, ...]
    cash_balances: Mapping[str, Balance]

    def __post_init__(self) -> None:
        require_utc(self.as_of_utc, "as_of_utc")
        object.__setattr__(
            self, "cash_balances", _frozen_balances(self.cash_balances, "cash_balances")
        )
        seen: set[Instrument] = set()
        for position in self.positions:
            if not isinstance(position, Position):
                raise DomainError(
                    f"positions must hold Position values, got {type(position).__name__}"
                )
            if position.instrument in seen:
                raise DomainError(
                    f"portfolio holds two positions in "
                    f"{position.instrument.venue}:{position.instrument.symbol}; net them "
                    f"before constructing it"
                )
            seen.add(position.instrument)

    def position_for(self, instrument: Instrument) -> Position | None:
        """The position in `instrument`, or `None` when there is none held.

        `None` rather than a flat `Position`: "we hold nothing" and "we have never
        traded this" are different facts, and only the first should contribute a row
        to an exposure report.
        """
        for position in self.positions:
            if position.instrument == instrument:
                return position
        return None

    def with_position(self, position: Position) -> Portfolio:
        """Replace or add one position, leaving the rest untouched."""
        others = tuple(held for held in self.positions if held.instrument != position.instrument)
        return replace(self, positions=(*others, position))

    @property
    def open_positions(self) -> tuple[Position, ...]:
        """Only the positions carrying exposure right now."""
        return tuple(position for position in self.positions if position.signed_base_quantity != 0)
