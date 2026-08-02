"""What a strategy is allowed to say.

This module is the domain-level half of the guarantee that `import-linter` enforces
structurally: a `Signal` expresses conviction and invalidation, and there is no method,
classmethod, property or field anywhere on it that yields an `Order`. A strategy that
could size its own positions can bankrupt the portfolio regardless of signal quality,
and this system writes its own strategies -- so the absence has to be a property of the
type, not a rule someone read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from fking.domain._errors import DomainError
from fking.domain._guards import (
    require_fraction,
    require_positive_decimal,
    require_positive_duration,
    require_text,
    require_utc,
)
from fking.domain.enums import Direction
from fking.domain.instrument import Instrument


@dataclass(frozen=True, slots=True)
class Signal:
    """A strategy's belief about one instrument at one instant.

    `conviction` is the strategy's own confidence, not a position size and not a
    weight. The risk engine is free to map it onto zero. Calibrating reported
    conviction against realised outcome is a separate concern that lives in `risk`,
    because a strategy grading its own confidence is the definition of unfalsifiable.
    """

    strategy_id: str
    instrument: Instrument
    direction: Direction
    conviction: Decimal
    horizon: timedelta
    invalidation_quote_price: Decimal | None
    rationale: str
    decided_at_utc: datetime

    def __post_init__(self) -> None:
        require_text(self.strategy_id, "strategy_id")
        require_fraction(self.conviction, "conviction")
        require_positive_duration(self.horizon, "horizon")
        require_text(self.rationale, "rationale")
        require_utc(self.decided_at_utc, "decided_at_utc")

        # A directional signal with no invalidation level is a hope, not a thesis: the
        # strategy has not said what would prove it wrong, so nothing can ever retire
        # it for being wrong. A flat signal carries none for the symmetric reason --
        # there is no thesis to invalidate.
        if self.direction is Direction.FLAT:
            if self.invalidation_quote_price is not None:
                raise DomainError(
                    f"{self.strategy_id} emitted a flat signal carrying an invalidation "
                    f"level of {self.invalidation_quote_price}; flat asserts nothing to invalidate"
                )
        elif self.invalidation_quote_price is None:
            raise DomainError(
                f"{self.strategy_id} emitted a {self.direction} signal on "
                f"{self.instrument.symbol} with no invalidation level; a strategy that "
                f"cannot state what would prove it wrong has no thesis"
            )
        else:
            require_positive_decimal(self.invalidation_quote_price, "invalidation_quote_price")

    @property
    def is_actionable(self) -> bool:
        """Whether this signal asks for exposure at all.

        A flat signal is a real instruction -- close what is open -- and is not the
        same as no signal. The distinction matters to the risk engine, which nets
        across strategies: an absent signal means "no opinion", flat means "no
        position".
        """
        return self.direction is not Direction.FLAT
