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

The kill switch is the same idea applied to the whole order path rather than to one
limit: it is closed by default, its state is derived from an append-only journal, and
reopening it requires a person rather than a value.

Everything not listed in `__all__` is private and may change without notice.
"""

from fking.risk._killswitch import (
    RECONCILIATION_FRESHNESS,
    REQUIRED_RECOVERY_STEP,
    KillSwitchError,
    KillSwitchGate,
    KillSwitchTrippedError,
    ResumePreconditions,
    ResumeRefusedError,
    TripPolicy,
    TripStep,
    arm,
    boot_halted_state,
    derive_state,
    derive_state_from,
    orders_to_cancel,
    resume,
    resume_refusals,
    trip,
    trip_sequence,
)
from fking.risk._state import (
    ARM_VALIDITY,
    HUMAN_OPERATOR_PREFIX,
    MIN_ROOT_CAUSE_LENGTH,
    ArmEvent,
    BookSnapshot,
    JournalRead,
    JournalReadOutcome,
    JournalUnreadable,
    KillSwitchEvent,
    KillSwitchState,
    KillSwitchStatus,
    ResumeEvent,
    TripEvent,
    TripTrigger,
)
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
    "ARM_VALIDITY",
    "DRAWDOWN_HARD_CEILINGS",
    "EXPOSURE_HARD_CEILINGS",
    "EXPOSURE_HARD_FLOORS",
    "GATE_FIELDS",
    "HARD_CEILINGS",
    "HARD_FLOORS",
    "HUMAN_OPERATOR_PREFIX",
    "MIN_ROOT_CAUSE_LENGTH",
    "RECONCILIATION_FRESHNESS",
    "REQUIRED_RECOVERY_STEP",
    "ROLLING_WINDOW",
    "ArmEvent",
    "BookSnapshot",
    "BreachRecord",
    "Ceiling",
    "DrawdownBudgets",
    "DrawdownState",
    "DrawdownStateError",
    "EquityMark",
    "ExposureAssessment",
    "ExposureLimits",
    "Floor",
    "JournalRead",
    "JournalReadOutcome",
    "JournalUnreadable",
    "KillSwitchError",
    "KillSwitchEvent",
    "KillSwitchGate",
    "KillSwitchState",
    "KillSwitchStatus",
    "KillSwitchTrippedError",
    "LimitEvaluation",
    "LimitVerdict",
    "PortfolioExposure",
    "PreTradeContext",
    "Rejection",
    "ResumeEvent",
    "ResumePreconditions",
    "ResumeRefusedError",
    "RiskLimits",
    "TripEvent",
    "TripPolicy",
    "TripStep",
    "TripTrigger",
    "ViolationTally",
    "arm",
    "assert_above_floors",
    "assert_within_ceilings",
    "boot_halted_state",
    "derisk_scalar",
    "derive_state",
    "derive_state_from",
    "evaluate",
    "from_row",
    "open_first_time",
    "orders_to_cancel",
    "portfolio_exposure",
    "restore",
    "resume",
    "resume_refusals",
    "to_row",
    "trip",
    "trip_sequence",
    "utc_day_start",
    "validate_pre_trade",
    "with_equity",
)
