"""Sizing, exposure limits, netting and the kill switch. Knows about signals,
exposure and capital.

This module holds sole authority to construct an `Order`. Everything upstream
proposes; this decides. It is pure for the same reason `strategy` is -- a risk
decision that depends on the wall clock cannot be replayed, and a risk decision that
cannot be replayed cannot be audited.

Every limit here is configuration bounded by a compiled-in constant, in whichever
direction is dangerous for that particular limit: a ceiling where larger is riskier, a
floor where smaller is. Tightening is free; loosening past a bound requires a source
edit and a pull request labelled `safety:critical`.

Everything not listed in `__all__` is private and may change without notice.
"""

from fking.risk.ceilings import (
    HARD_CEILINGS,
    HARD_FLOORS,
    Ceiling,
    Floor,
    assert_above_floors,
    assert_within_ceilings,
)
from fking.risk.drawdown import (
    DRAWDOWN_HARD_CEILINGS,
    ROLLING_WINDOW,
    BreachRecord,
    DrawdownBudgets,
    DrawdownState,
    DrawdownStateError,
    EquityMark,
    LimitVerdict,
    derisk_scalar,
    evaluate,
    from_row,
    open_first_time,
    restore,
    to_row,
    utc_day_start,
    with_equity,
)
from fking.risk.exposure import (
    EXPOSURE_HARD_CEILINGS,
    EXPOSURE_HARD_FLOORS,
    ExposureAssessment,
    ExposureLimits,
    LimitEvaluation,
    PortfolioExposure,
    PreTradeContext,
    Rejection,
    ViolationTally,
    portfolio_exposure,
    validate_pre_trade,
)
from fking.risk.limits import GATE_FIELDS, RiskLimits

__all__: tuple[str, ...] = (
    "DRAWDOWN_HARD_CEILINGS",
    "EXPOSURE_HARD_CEILINGS",
    "EXPOSURE_HARD_FLOORS",
    "GATE_FIELDS",
    "HARD_CEILINGS",
    "HARD_FLOORS",
    "ROLLING_WINDOW",
    "BreachRecord",
    "Ceiling",
    "DrawdownBudgets",
    "DrawdownState",
    "DrawdownStateError",
    "EquityMark",
    "ExposureAssessment",
    "ExposureLimits",
    "Floor",
    "LimitEvaluation",
    "LimitVerdict",
    "PortfolioExposure",
    "PreTradeContext",
    "Rejection",
    "RiskLimits",
    "ViolationTally",
    "assert_above_floors",
    "assert_within_ceilings",
    "derisk_scalar",
    "evaluate",
    "from_row",
    "open_first_time",
    "portfolio_exposure",
    "restore",
    "to_row",
    "utc_day_start",
    "validate_pre_trade",
    "with_equity",
)
