"""Signal invariants, and the guarantee that a Signal cannot become an Order."""

from __future__ import annotations

import inspect
from datetime import timedelta
from decimal import Decimal
from typing import get_type_hints

import pytest

import fking.domain.signal as signal_module
from fking.domain import Direction, DomainError, Order, Signal
from tests.support.domain_factory import make_signal

pytestmark = pytest.mark.unit


def test_a_directional_signal_requires_an_invalidation_level() -> None:
    """A strategy that cannot say what would prove it wrong has a hope, not a thesis.

    Without this, a strategy can never be retired for being wrong -- there is no level
    whose breach constitutes wrongness, so every losing run reads as noise.
    """
    with pytest.raises(DomainError, match="no invalidation level"):
        make_signal(direction=Direction.SHORT, invalidation_quote_price=None)


def test_a_flat_signal_may_not_carry_an_invalidation_level() -> None:
    with pytest.raises(DomainError, match="flat asserts nothing to invalidate"):
        make_signal(direction=Direction.FLAT, invalidation_quote_price="63000")


def test_a_flat_signal_is_a_real_instruction() -> None:
    """Flat means "no position"; an absent signal means "no opinion".

    The risk engine nets across strategies, so collapsing the two would let one
    strategy's silence read as another's instruction to close.
    """
    signal = make_signal(direction=Direction.FLAT, invalidation_quote_price=None)
    assert signal.is_actionable is False
    assert make_signal().is_actionable is True


@pytest.mark.parametrize("conviction", ["-0.01", "1.01", "60"])
def test_conviction_is_a_fraction_not_a_percent(conviction: str) -> None:
    with pytest.raises(DomainError, match=r"\[0, 1\]"):
        make_signal(conviction=conviction)


def test_horizon_must_be_positive() -> None:
    with pytest.raises(DomainError, match="horizon must be positive"):
        Signal(
            strategy_id="breakout-4h",
            instrument=make_signal().instrument,
            direction=Direction.LONG,
            conviction=Decimal("0.6"),
            horizon=timedelta(0),
            invalidation_quote_price=Decimal("63000"),
            rationale="x",
            decided_at_utc=make_signal().decided_at_utc,
        )


def test_rationale_may_not_be_blank() -> None:
    """A blank rationale is a field somebody meant to fill in.

    Accepting it produces an audit row that satisfies the schema and answers nothing,
    at exactly the point an investigation needs the answer.
    """
    with pytest.raises(DomainError, match="rationale must not be blank"):
        Signal(
            strategy_id="breakout-4h",
            instrument=make_signal().instrument,
            direction=Direction.LONG,
            conviction=Decimal("0.6"),
            horizon=timedelta(hours=1),
            invalidation_quote_price=Decimal("63000"),
            rationale="   ",
            decided_at_utc=make_signal().decided_at_utc,
        )


def test_invalidation_level_must_be_a_positive_price() -> None:
    with pytest.raises(DomainError, match="invalidation_quote_price must be positive"):
        make_signal(invalidation_quote_price="0")


class TestSignalCannotBecomeAnOrder:
    """The domain-level half of the guarantee `import-linter` enforces structurally.

    Both halves are needed. The import contract stops `strategy` reaching `execution`;
    these stop somebody adding `Signal.to_order()` inside `domain`, where no contract
    is watching and where every module is already allowed to look.
    """

    def test_the_signal_module_does_not_reference_order(self) -> None:
        assert not any(member is Order for member in vars(signal_module).values())

    def test_no_field_is_typed_as_an_order(self) -> None:
        assert Order not in get_type_hints(Signal).values()

    def test_no_method_or_property_returns_an_order(self) -> None:
        """Introspected rather than listed, so a helper added later is covered too."""
        for name, member in inspect.getmembers(Signal):
            if name.startswith("__"):
                continue
            target = member.fget if isinstance(member, property) else member
            if not callable(target):
                continue
            try:
                annotations = get_type_hints(target)
            except (NameError, TypeError):  # pragma: no cover - no such member today
                continue
            assert Order not in annotations.values(), f"Signal.{name} can produce an Order"
