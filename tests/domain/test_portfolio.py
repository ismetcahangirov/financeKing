"""Account and Portfolio, including the shallow-freeze trap."""

from __future__ import annotations

from decimal import Decimal

import pytest

from fking.domain import Account, Balance, DomainError, Portfolio, Position, Side, Venue
from tests.support.domain_factory import BTCUSDT, EPOCH, ETHUSDT, make_fill

pytestmark = pytest.mark.unit

USDT = Balance(asset="USDT", free_quantity=Decimal("900"), locked_quantity=Decimal("100"))


def _long_btc() -> Position:
    return (
        Position.flat(BTCUSDT)
        .with_fill(make_fill(side=Side.BUY, quote_price="64000.00", base_quantity="0.01"))
        .after
    )


def test_account_mapping_is_frozen_against_the_caller_and_against_mutation() -> None:
    """`frozen=True` protects the binding, not the object bound.

    Without the copy, the caller's dict is still reachable through the field and every
    later write to it silently rewrites the account.
    """
    supplied = {"USDT": USDT}
    account = Account(
        venue=Venue.BINANCE_SPOT_TESTNET,
        account_id="demo-1",
        balances=supplied,
        updated_at_utc=EPOCH,
    )

    supplied["BTC"] = Balance(asset="BTC", free_quantity=Decimal("1"), locked_quantity=Decimal("0"))
    assert "BTC" not in account.balances

    with pytest.raises(TypeError):
        account.balances["BTC"] = supplied["BTC"]  # type: ignore[index]  # proving it is a proxy


def test_account_reports_a_missing_asset_as_zero() -> None:
    account = Account(
        venue=Venue.BINANCE_SPOT_TESTNET,
        account_id="demo-1",
        balances={"USDT": USDT},
        updated_at_utc=EPOCH,
    )
    assert account.free_quantity("USDT") == Decimal("900")
    assert account.free_quantity("BTC") == Decimal("0")


def test_a_balance_keyed_under_the_wrong_asset_is_refused() -> None:
    """The shape a partial rename leaves behind; every lookup afterwards lies."""
    with pytest.raises(DomainError, match="keyed 'BTC' but holds a USDT balance"):
        Account(
            venue=Venue.BINANCE_SPOT_TESTNET,
            account_id="demo-1",
            balances={"BTC": USDT},
            updated_at_utc=EPOCH,
        )


def test_balances_must_hold_balance_values() -> None:
    with pytest.raises(DomainError, match="must be a Balance"):
        Account(
            venue=Venue.BINANCE_SPOT_TESTNET,
            account_id="demo-1",
            balances={"USDT": Decimal("900")},  # type: ignore[dict-item]  # the wrong type is the test
            updated_at_utc=EPOCH,
        )


def test_portfolio_finds_a_position_by_the_whole_instrument() -> None:
    """Not by symbol: the same symbol on two venues has different filters."""
    portfolio = Portfolio(as_of_utc=EPOCH, positions=(_long_btc(),), cash_balances={"USDT": USDT})
    assert portfolio.position_for(BTCUSDT) is not None
    assert portfolio.position_for(ETHUSDT) is None


def test_portfolio_refuses_two_positions_in_one_instrument() -> None:
    with pytest.raises(DomainError, match="two positions"):
        Portfolio(
            as_of_utc=EPOCH,
            positions=(_long_btc(), _long_btc()),
            cash_balances={},
        )


def test_portfolio_positions_must_be_positions() -> None:
    with pytest.raises(DomainError, match="must hold Position values"):
        Portfolio(
            as_of_utc=EPOCH,
            positions=(BTCUSDT,),  # type: ignore[arg-type]  # the wrong type is the test
            cash_balances={},
        )


def test_with_position_replaces_rather_than_appends() -> None:
    portfolio = Portfolio(as_of_utc=EPOCH, positions=(_long_btc(),), cash_balances={})
    closed = Position.flat(BTCUSDT)
    updated = portfolio.with_position(closed)

    assert len(updated.positions) == 1
    assert updated.position_for(BTCUSDT) == closed
    assert portfolio.position_for(BTCUSDT) != closed  # the original is untouched


def test_open_positions_excludes_the_flat_ones() -> None:
    portfolio = Portfolio(
        as_of_utc=EPOCH,
        positions=(_long_btc(), Position.flat(ETHUSDT)),
        cash_balances={},
    )
    assert portfolio.open_positions == (portfolio.positions[0],)
