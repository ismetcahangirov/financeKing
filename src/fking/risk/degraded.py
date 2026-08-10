"""The five named degraded modes of `FAILSAFE.md` section 3, as a pure state machine.

A degraded mode is the tier below a full trip: behaviour differs from normal, and it
differs in a way that has a name. There is no unnamed degraded state in this system,
which is a stronger claim than it sounds -- the natural implementation of every one of
these is a boolean somewhere in the component that noticed, and five booleans in five
modules is five behaviours nobody can enumerate and none of which appear on a dashboard.

**Why this lives in `risk` and not in `platform`.** `platform` holds mechanism and is
forbidden trading vocabulary, and this table decides whether orders may be constructed,
which is policy of exactly the kind `RISK_PHILOSOPHY.md` gives to one owner. It sits
next to the kill switch because one of the modes trips it and the rest are the states
that sit below it; splitting them would put the halt ladder in two modules.

**Two hysteresis rules, in opposite directions, and both are deliberate.**
`DATA_STALE` exits on *two* consecutive fresh ticks, because one tick can be a stale
replay from a reconnecting feed and a mode that exits on it re-enters a second later.
`EXCHANGE_UNREACHABLE` enters on *two* consecutive failures, because a single failed
REST call is the ordinary texture of a testnet and a mode that trips on every one trains
people to ignore it.

**`LLM_QUOTA_EXHAUSTED` blocks nothing, and that is an assertion rather than an
oversight.** If exhausting the quota ever moves an order-flow metric, an LLM has reached
the order path, which `ARCHITECTURE.md` section 9 forbids. `ModeRule.affects_trading` is
false for exactly that one mode and
`tests/slow/test_quota_exhaustion_does_not_touch_trading.py` is the weekly check.

Everything except `DegradedModeGate` is pure and takes its instant as a field on the
observation. The gate is the narrow mutable holder the order path reads in one attribute
access, in the same way `KillSwitchGate` is, and it is the only thing here that emits a
metric -- a write-only side effect that no decision in this module reads back.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from uuid import UUID

from fking.platform.telemetry import counter, gauge
from fking.platform.telemetry._registry import (
    RISK_DEGRADED_MODE_ENGAGED,
    RISK_DEGRADED_MODE_TRANSITIONS,
)

__all__ = [
    "MODE_RULES",
    "DegradedMode",
    "DegradedModeError",
    "DegradedModeGate",
    "DegradedModeState",
    "ModeDirection",
    "ModeEntry",
    "ModeObservation",
    "ModeProgress",
    "ModeRule",
    "ModeTransition",
    "agent_work_paused",
    "blocked_symbols",
    "blocks_new_orders",
    "kill_switch_trip_required",
    "symbols_without_usable_data",
]


class DegradedModeError(ValueError):
    """A mode observation is malformed and is refused rather than guessed at."""


class DegradedMode(StrEnum):
    """The five named states. Adding a sixth means adding a row to `MODE_RULES`.

    The enum and the rule table are separate so that a mode with no declared behaviour
    cannot exist: `MODE_RULES` is checked for completeness at import.
    """

    DATA_STALE = "DATA_STALE"
    EXCHANGE_UNREACHABLE = "EXCHANGE_UNREACHABLE"
    LLM_QUOTA_EXHAUSTED = "LLM_QUOTA_EXHAUSTED"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    FEATURE_STORE_PARTIAL = "FEATURE_STORE_PARTIAL"


class ModeDirection(StrEnum):
    """Which way a transition went. Also the `outcome` label on the transition metric."""

    ENTERED = "entered"
    EXITED = "exited"


@dataclass(frozen=True, slots=True, kw_only=True)
class ModeRule:
    """What one mode does, as data rather than as branches in a supervisor.

    A table because the alternative -- an `if mode is DATA_STALE` chain at each call site
    -- is five copies of a policy that has to agree with itself, and the copy that
    disagrees is found by an incident rather than by a reader.
    """

    mode: DegradedMode
    consecutive_faults_to_enter: int
    consecutive_clears_to_exit: int
    requires_clean_reconciliation_to_exit: bool
    blocks_new_orders: bool
    restricts_affected_symbols: bool
    withholds_feature_values: bool
    trips_kill_switch: bool
    pauses_agent_work: bool

    def __post_init__(self) -> None:
        if self.consecutive_faults_to_enter < 1 or self.consecutive_clears_to_exit < 1:
            raise DegradedModeError(
                f"{self.mode} declares a threshold below one observation; a mode that "
                f"enters or exits on zero observations changes state on nothing"
            )

    @property
    def affects_trading(self) -> bool:
        """Whether being in this mode can change what the order path does.

        False for `LLM_QUOTA_EXHAUSTED` alone. That is the design assertion in
        `FAILSAFE.md` section 3.3 made mechanical: research pauses, trading does not
        notice, and a change here that flips it to true fails a test rather than
        quietly moving an order-flow metric.
        """
        return (
            self.blocks_new_orders
            or self.restricts_affected_symbols
            or self.withholds_feature_values
            or self.trips_kill_switch
        )


MODE_RULES: Final[Mapping[DegradedMode, ModeRule]] = MappingProxyType(
    {
        DegradedMode.DATA_STALE: ModeRule(
            mode=DegradedMode.DATA_STALE,
            consecutive_faults_to_enter=1,
            # Two, because one fresh tick can be a stale replay from a reconnecting feed
            # (FAILSAFE.md section 3.1).
            consecutive_clears_to_exit=2,
            requires_clean_reconciliation_to_exit=False,
            blocks_new_orders=False,
            restricts_affected_symbols=True,
            withholds_feature_values=True,
            trips_kill_switch=False,
            pauses_agent_work=False,
        ),
        DegradedMode.EXCHANGE_UNREACHABLE: ModeRule(
            mode=DegradedMode.EXCHANGE_UNREACHABLE,
            # Two consecutive failed REST calls (FAILSAFE.md section 3.2). A single
            # failure is the ordinary texture of the testnet.
            consecutive_faults_to_enter=2,
            consecutive_clears_to_exit=1,
            # Reconnection is not recovery. The gap is exactly the interval in which
            # positions can have changed without us -- stop-outs, liquidations, partial
            # fills of orders we believed were resting -- so reconciliation runs before
            # anything else resumes.
            requires_clean_reconciliation_to_exit=True,
            blocks_new_orders=True,
            restricts_affected_symbols=False,
            withholds_feature_values=False,
            trips_kill_switch=False,
            pauses_agent_work=False,
        ),
        DegradedMode.LLM_QUOTA_EXHAUSTED: ModeRule(
            mode=DegradedMode.LLM_QUOTA_EXHAUSTED,
            consecutive_faults_to_enter=1,
            consecutive_clears_to_exit=1,
            requires_clean_reconciliation_to_exit=False,
            blocks_new_orders=False,
            restricts_affected_symbols=False,
            withholds_feature_values=False,
            trips_kill_switch=False,
            pauses_agent_work=True,
        ),
        DegradedMode.DATABASE_UNAVAILABLE: ModeRule(
            mode=DegradedMode.DATABASE_UNAVAILABLE,
            # Immediately, on the first observation. The audit log is a precondition for
            # trading rather than a record of it: a trade taken while it is down is
            # permanently unreconstructable, not merely harder to reconstruct.
            consecutive_faults_to_enter=1,
            consecutive_clears_to_exit=1,
            requires_clean_reconciliation_to_exit=False,
            blocks_new_orders=True,
            restricts_affected_symbols=False,
            withholds_feature_values=False,
            trips_kill_switch=True,
            pauses_agent_work=False,
        ),
        DegradedMode.FEATURE_STORE_PARTIAL: ModeRule(
            mode=DegradedMode.FEATURE_STORE_PARTIAL,
            consecutive_faults_to_enter=1,
            consecutive_clears_to_exit=1,
            requires_clean_reconciliation_to_exit=False,
            blocks_new_orders=False,
            restricts_affected_symbols=True,
            # No substituted value, no imputation, no forward fill, no zero. Imputation
            # is the single most tempting shortcut in the data path: it always produces a
            # number, the number always looks reasonable, and the resulting strategy
            # behaviour is unfalsifiable.
            withholds_feature_values=True,
            trips_kill_switch=False,
            pauses_agent_work=False,
        ),
    }
)

_MISSING_RULES = sorted(mode for mode in DegradedMode if mode not in MODE_RULES)
if _MISSING_RULES:  # pragma: no cover - an import-time guard against an unnamed mode
    raise DegradedModeError(
        f"modes {_MISSING_RULES} have no rule; a mode whose behaviour is undeclared is "
        f"the unnamed degraded state this table exists to make impossible"
    )


def _require_utc(candidate: datetime, field_name: str) -> None:
    if candidate.tzinfo is None or candidate.utcoffset() != timedelta(0):
        raise DegradedModeError(f"{field_name} must be timezone-aware UTC; got {candidate!r}")


def _require_reason(reason: str) -> None:
    if not reason.strip():
        raise DegradedModeError(
            "a mode transition carries a reason; an audit row that says a mode changed "
            "and not why is a row nobody can act on"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ModeObservation:
    """One report about one mode, from the component that measured it.

    `observation_id` is the idempotency key. The bus delivers at least once, and a
    redelivered fault observation for `EXCHANGE_UNREACHABLE` -- which enters on two
    consecutive faults -- would otherwise enter the mode on one real failure counted
    twice. The key is the producer's identity for the *measurement*, so a genuine second
    failed call carries a second id and still counts twice.
    """

    observation_id: UUID
    correlation_id: UUID
    mode: DegradedMode
    is_faulted: bool
    observed_at_utc: datetime
    reason: str
    affected_symbols: tuple[str, ...] = ()
    reconciliation_is_clean: bool = False

    def __post_init__(self) -> None:
        _require_utc(self.observed_at_utc, "observed_at_utc")
        _require_reason(self.reason)
        for symbol in self.affected_symbols:
            if not symbol.strip():
                raise DegradedModeError("affected_symbols must not contain a blank symbol")


@dataclass(frozen=True, slots=True, kw_only=True)
class ModeEntry:
    """A mode that is currently entered, and what it was entered for."""

    mode: DegradedMode
    entered_at_utc: datetime
    reason: str
    correlation_id: UUID
    affected_symbols: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ModeTransition:
    """The audit event a mode change writes. One row per crossing, never per observation.

    Entering is recorded once even though the fault that caused it is observed
    continuously, which is what makes the row count a count of incidents rather than of
    heartbeats.
    """

    mode: DegradedMode
    direction: ModeDirection
    occurred_at_utc: datetime
    reason: str
    correlation_id: UUID
    affected_symbols: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ModeProgress:
    """One mode's counters and its entry, if it has one.

    The counters are here rather than in the supervisor because hysteresis is state: a
    supervisor that keeps them in a local is a supervisor whose restart silently resets
    the two-failure threshold to zero.
    """

    consecutive_faults: int = 0
    consecutive_clears: int = 0
    last_observation_id: UUID | None = None
    last_observed_at_utc: datetime | None = None
    entry: ModeEntry | None = None


@dataclass(frozen=True, slots=True)
class DegradedModeState:
    """Which modes are entered, and how close the rest are. Transitions return new states."""

    progress: Mapping[DegradedMode, ModeProgress] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # `frozen=True` protects the binding, not the mapping bound to it, and this
        # object is read by the order path while a supervisor is applying observations.
        object.__setattr__(self, "progress", MappingProxyType(dict(self.progress)))

    def entry_for(self, mode: DegradedMode) -> ModeEntry | None:
        """The entry for `mode`, or `None` when the mode is not entered."""
        current = self.progress.get(mode)
        return None if current is None else current.entry

    def is_active(self, mode: DegradedMode) -> bool:
        """Whether `mode` is currently entered."""
        return self.entry_for(mode) is not None

    @property
    def active_modes(self) -> tuple[DegradedMode, ...]:
        """Every entered mode, in declaration order so the tuple is stable to compare."""
        return tuple(mode for mode in DegradedMode if self.is_active(mode))

    def observe(
        self, observation: ModeObservation
    ) -> tuple[DegradedModeState, ModeTransition | None]:
        """Apply one observation. Returns the new state and the transition it caused.

        A redelivery produces the same state back and no transition, which is what makes
        a consumer of this safe under at-least-once delivery: the effect happens once
        however many times the message arrives.
        """
        return _observe(self, observation)


def _superseded(current: ModeProgress, observation: ModeObservation) -> bool:
    """Whether this observation carries nothing the state has not already applied.

    Two cases, both of which are normal rather than exceptional. A repeat of the last
    observation id is an at-least-once redelivery. An observation stamped before the one
    already applied is a reordered delivery, and applying it would let a stale fault
    resurrect a mode that a later measurement already cleared.
    """
    if current.last_observation_id == observation.observation_id:
        return True
    return (
        current.last_observed_at_utc is not None
        and observation.observed_at_utc < current.last_observed_at_utc
    )


def _entered(observation: ModeObservation, previous: ModeEntry | None) -> ModeEntry:
    """The entry after a fault, keeping the original instant and unioning the symbols.

    The instant is the first one, because that is when the incident started; the symbols
    accumulate, because a second symbol going stale while the mode is already entered is
    still a symbol that must not take a new position.
    """
    if previous is None:
        return ModeEntry(
            mode=observation.mode,
            entered_at_utc=observation.observed_at_utc,
            reason=observation.reason,
            correlation_id=observation.correlation_id,
            affected_symbols=observation.affected_symbols,
        )
    widened = tuple(sorted({*previous.affected_symbols, *observation.affected_symbols}))
    return replace(previous, affected_symbols=widened)


def _observe(
    state: DegradedModeState, observation: ModeObservation
) -> tuple[DegradedModeState, ModeTransition | None]:
    rule = MODE_RULES[observation.mode]
    current = state.progress.get(observation.mode, ModeProgress())
    if _superseded(current, observation):
        return state, None

    faults = current.consecutive_faults
    clears = current.consecutive_clears
    entry = current.entry
    transition: ModeTransition | None = None

    if observation.is_faulted:
        # Saturated rather than unbounded: once the entry threshold is met the count has
        # said everything it can, and an unbounded counter is a number that grows for as
        # long as an outage lasts and means nothing at either end of it.
        faults = min(faults + 1, rule.consecutive_faults_to_enter)
        clears = 0
        was_active = entry is not None
        if faults >= rule.consecutive_faults_to_enter:
            entry = _entered(observation, entry)
            if not was_active:
                transition = ModeTransition(
                    mode=observation.mode,
                    direction=ModeDirection.ENTERED,
                    occurred_at_utc=observation.observed_at_utc,
                    reason=observation.reason,
                    correlation_id=observation.correlation_id,
                    affected_symbols=entry.affected_symbols,
                )
    else:
        clears = min(clears + 1, rule.consecutive_clears_to_exit)
        faults = 0
        cleared_enough = clears >= rule.consecutive_clears_to_exit
        reconciled = (
            observation.reconciliation_is_clean or not rule.requires_clean_reconciliation_to_exit
        )
        if entry is not None and cleared_enough and reconciled:
            transition = ModeTransition(
                mode=observation.mode,
                direction=ModeDirection.EXITED,
                occurred_at_utc=observation.observed_at_utc,
                reason=observation.reason,
                correlation_id=observation.correlation_id,
                affected_symbols=entry.affected_symbols,
            )
            entry = None

    advanced = ModeProgress(
        consecutive_faults=faults,
        consecutive_clears=clears,
        last_observation_id=observation.observation_id,
        last_observed_at_utc=observation.observed_at_utc,
        entry=entry,
    )
    return (
        DegradedModeState({**state.progress, observation.mode: advanced}),
        transition,
    )


def blocks_new_orders(state: DegradedModeState) -> bool:
    """Whether any entered mode forbids constructing a new order at all."""
    return any(MODE_RULES[mode].blocks_new_orders for mode in state.active_modes)


def kill_switch_trip_required(state: DegradedModeState) -> bool:
    """Whether any entered mode requires the kill switch to be tripped."""
    return any(MODE_RULES[mode].trips_kill_switch for mode in state.active_modes)


def agent_work_paused(state: DegradedModeState) -> bool:
    """Whether agent-authored work queues rather than runs."""
    return any(MODE_RULES[mode].pauses_agent_work for mode in state.active_modes)


def blocked_symbols(state: DegradedModeState) -> frozenset[str]:
    """Symbols that may not take a new position while their mode is entered.

    Existing positions keep their resting invalidation orders at the venue; this blocks
    opening, not protecting.
    """
    return _symbols_where(state, lambda rule: rule.restricts_affected_symbols)


def symbols_without_usable_data(state: DegradedModeState) -> frozenset[str]:
    """Symbols whose feature values must be withheld rather than substituted.

    The caller hands these to `fking.strategy.step` as `unavailable_features`, which
    refuses to evaluate rather than passing a forward-filled value. Forward-filling a
    price during an outage produces a flat series, which every volatility estimator
    reads as calm and every mean-reversion strategy reads as an opportunity -- both
    exactly wrong, both looking entirely reasonable.
    """
    return _symbols_where(state, lambda rule: rule.withholds_feature_values)


def _symbols_where(
    state: DegradedModeState, predicate: Callable[[ModeRule], bool]
) -> frozenset[str]:
    affected: set[str] = set()
    for mode in state.active_modes:
        if not predicate(MODE_RULES[mode]):
            continue
        entry = state.entry_for(mode)
        if entry is not None:
            affected.update(entry.affected_symbols)
    return frozenset(affected)


class DegradedModeGate:
    """The in-process holder a supervisor writes and the order path reads.

    Mutable, deliberately and narrowly, for the same reason `KillSwitchGate` is: the
    order path must read the current mode set in one attribute access rather than
    awaiting a query, and a queue between the two is a window in which behaviour differs
    from what the modes say it is.

    It is also the one place a metric moves. Emission is write-only -- nothing here
    reads a metric back -- so the decisions this module makes stay a pure function of
    the observations, which is what keeps them replayable from the journal.
    """

    __slots__ = ("_state",)

    def __init__(self, state: DegradedModeState | None = None) -> None:
        self._state = DegradedModeState() if state is None else state

    @property
    def state(self) -> DegradedModeState:
        """The current state. A frozen object, so a reader cannot change it."""
        return self._state

    def adopt(self, state: DegradedModeState) -> None:
        """Replace the held state, as the boot sequence does after reading the journal."""
        self._state = state

    def observe(self, observation: ModeObservation) -> ModeTransition | None:
        """Apply one observation, emit the metrics for any crossing, return the audit row.

        The caller persists the returned transition. It is returned rather than written
        here because `risk` performs no I/O, and because the audit write and the state
        change belong in one transaction owned by the caller.
        """
        self._state, transition = _observe(self._state, observation)
        if transition is None:
            return None
        counter(RISK_DEGRADED_MODE_TRANSITIONS).increment(
            mode=transition.mode.value, outcome=transition.direction.value
        )
        gauge(RISK_DEGRADED_MODE_ENGAGED).set(
            1 if transition.direction is ModeDirection.ENTERED else 0,
            mode=transition.mode.value,
        )
        return transition
