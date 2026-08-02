"""Money and Balance."""

from __future__ import annotations

from decimal import Decimal

import pytest

from fking.domain import Balance, DomainError, Money

pytestmark = pytest.mark.unit


def test_money_carries_a_signed_quantity() -> None:
    """An unrealised loss and a negative funding payment are both Money."""
    assert Money(asset="USDT", quantity=Decimal("-12.50")).quantity < 0


def test_adding_the_same_asset_is_exact() -> None:
    total = Money(asset="USDT", quantity=Decimal("0.1")).plus(
        Money(asset="USDT", quantity=Decimal("0.2"))
    )
    assert total == Money(asset="USDT", quantity=Decimal("0.3"))


def test_adding_different_assets_raises_rather_than_converting() -> None:
    """There is no exchange rate in `domain`, and there should not be.

    A rate has an as-of time, a source and a staleness. None of those fit in an
    addition, and an implicit conversion would fix them all at whatever the caller
    happened to have in scope.
    """
    with pytest.raises(DomainError, match="convert explicitly"):
        Money(asset="USDT", quantity=Decimal("1")).plus(Money(asset="BTC", quantity=Decimal("1")))


def test_money_defines_no_arithmetic_operators() -> None:
    """`+` on Money would have to lie to satisfy Python's data model.

    The model expects `__add__` to return `NotImplemented` for an operand it cannot
    handle, so the interpreter can try the reflected call and raise `TypeError`
    itself. Two Money values are always a handled operand *type*; a mismatched asset
    is a domain rule. Expressing that through `+` means either breaking the contract
    or discarding the explanation in favour of a bare TypeError.
    """
    usdt = Money(asset="USDT", quantity=Decimal("1"))
    for special in ("__add__", "__radd__", "__sub__", "__mul__"):
        assert not hasattr(Money, special), f"Money defines {special}"
    with pytest.raises(TypeError):
        usdt + usdt  # type: ignore[operator]  # the absence is the test


def test_money_rejects_a_float_quantity() -> None:
    with pytest.raises(DomainError, match="already rounded"):
        Money(asset="USDT", quantity=0.1)  # type: ignore[arg-type]  # the wrong type is the test


def test_money_rejects_a_lowercase_asset_code() -> None:
    with pytest.raises(DomainError, match="uppercase ASCII"):
        Money(asset="usdt", quantity=Decimal("1"))


def test_balance_totals_free_and_locked() -> None:
    balance = Balance(
        asset="USDT", free_quantity=Decimal("900.10"), locked_quantity=Decimal("99.90")
    )
    assert balance.total_quantity == Decimal("1000.00")


def test_balance_components_may_not_be_negative() -> None:
    with pytest.raises(DomainError, match="free_quantity must not be negative"):
        Balance(asset="USDT", free_quantity=Decimal("-1"), locked_quantity=Decimal("0"))
