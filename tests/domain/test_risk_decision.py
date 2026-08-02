"""RiskDecision: a rejection is a decision, not an absence."""

from __future__ import annotations

from uuid import UUID

import pytest

from fking.domain import DomainError, RiskDecision, RiskVerdict
from tests.support.domain_factory import EPOCH, make_order

pytestmark = pytest.mark.unit


def _decision(**overrides: object) -> RiskDecision:
    arguments: dict[str, object] = {
        "decision_id": UUID(int=1),
        "correlation_id": UUID(int=2),
        "decided_at_utc": EPOCH,
        "verdict": RiskVerdict.APPROVED,
        "order": make_order(),
        "rejection_reason": None,
    }
    arguments.update(overrides)
    return RiskDecision(**arguments)  # type: ignore[arg-type]  # a test factory over a closed field set


def test_an_approval_carries_the_order_it_approved() -> None:
    decision = _decision()
    assert decision.is_approved is True
    assert decision.order is not None


def test_a_rejection_carries_a_reason_and_no_order() -> None:
    decision = _decision(
        verdict=RiskVerdict.REJECTED,
        order=None,
        rejection_reason="portfolio exposure limit of 10% would be breached at 11.4%",
    )
    assert decision.is_approved is False
    assert decision.order is None


def test_an_approval_without_an_order_is_refused() -> None:
    with pytest.raises(DomainError, match="must carry the order it approved"):
        _decision(order=None)


def test_an_approval_carrying_a_rejection_reason_is_refused() -> None:
    with pytest.raises(DomainError, match="carries a rejection reason"):
        _decision(rejection_reason="but also approved")


def test_a_rejection_carrying_an_order_is_refused() -> None:
    """The order would be right there, and somebody downstream would submit it."""
    with pytest.raises(DomainError, match="a rejection produces no order"):
        _decision(verdict=RiskVerdict.REJECTED, rejection_reason="too large")


@pytest.mark.parametrize("rejection_reason", [None, "", "   "])
def test_a_rejection_needs_a_non_blank_reason(rejection_reason: str | None) -> None:
    """ "rejected: " with nothing after it is the row an investigation lands on."""
    with pytest.raises(DomainError, match="rejection_reason"):
        _decision(verdict=RiskVerdict.REJECTED, order=None, rejection_reason=rejection_reason)


def test_identifiers_must_be_uuids() -> None:
    with pytest.raises(DomainError, match="correlation_id must be a UUID"):
        _decision(correlation_id="c-1")
