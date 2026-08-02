"""The risk engine's verdict on a signal.

A rejection is a decision, not an absence. If the only record of a refused signal were
the missing order, the audit log could not distinguish "the risk engine said no" from
"the consumer crashed before it ran", and `ARCHITECTURE.md` section 11 requires that a
trade -- including the trade that did not happen -- be reconstructable months later
from these rows alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fking.domain._errors import DomainError
from fking.domain._guards import require_text, require_utc
from fking.domain.enums import RiskVerdict
from fking.domain.order import Order


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """What the risk engine decided about one signal, and why.

    The two fields the verdict governs are mutually exclusive and the constructor
    enforces it in both directions. An `APPROVED` decision carrying no order is a
    decision nothing can act on; a `REJECTED` decision carrying an order is one that
    somebody downstream will act on anyway, because the order is right there and the
    verdict is a string.
    """

    decision_id: UUID
    correlation_id: UUID
    decided_at_utc: datetime
    verdict: RiskVerdict
    order: Order | None
    rejection_reason: str | None

    def __post_init__(self) -> None:
        for field_name, candidate in (
            ("decision_id", self.decision_id),
            ("correlation_id", self.correlation_id),
        ):
            if not isinstance(candidate, UUID):
                raise DomainError(
                    f"{field_name} must be a UUID, got {type(candidate).__name__} {candidate!r}"
                )
        require_utc(self.decided_at_utc, "decided_at_utc")

        if self.verdict is RiskVerdict.APPROVED:
            if self.order is None:
                raise DomainError("an approved risk decision must carry the order it approved")
            if self.rejection_reason is not None:
                raise DomainError(
                    f"an approved risk decision carries a rejection reason: "
                    f"{self.rejection_reason!r}"
                )
        else:
            if self.order is not None:
                raise DomainError(
                    f"a rejected risk decision carries order "
                    f"{self.order.client_order_id}; a rejection produces no order"
                )
            # Non-empty, because "rejected: " with nothing after it is the row an
            # investigation lands on six months later.
            require_text(self.rejection_reason, "rejection_reason")

    @property
    def is_approved(self) -> bool:
        """Whether an order exists to submit."""
        return self.verdict is RiskVerdict.APPROVED
