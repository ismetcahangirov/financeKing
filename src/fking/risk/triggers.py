"""The twelve kill-switch triggers of `FAILSAFE.md` section 2.1, as pure evaluators.

Every function here is a pure function of an observation record and a threshold record.
Nothing reads a clock, opens a socket or touches a database: the instant is a field on
the observations and the measurements were taken by the caller that owns the I/O. That
is what makes a trip replayable -- given the row the trip was written from, the same
evaluation months later produces the same verdict, which is the difference between an
audit trail and a story about one.

Three shapes in here are deliberate and easy to get wrong the other way.

**Triggers 1-3 do not carry their own thresholds.** They read `DrawdownBudgets`, the same
object `fking.risk.drawdown.evaluate` reads. A second copy of "10% / 3% / 4.5%" living
here would be a second configuration that can disagree with the first, and the way that
disagreement surfaces is a limit that binds in one mechanism and not in the other while
both report themselves healthy. `FAILSAFE.md`'s 4.5% rolling threshold is not a third
number either: it is `1.5 x` the 3% daily budget, which is what `DrawdownBudgets`
already derives.

**Trigger 4 does carry its own threshold, and must.** Loss velocity is the trigger that
catches what the daily limit cannot -- 2.9% over twenty hours is a bad day, 2.9% in four
minutes is a broken system, and one threshold cannot distinguish them. Its ceiling is
set *below* the default daily budget on purpose: at or above it, velocity could never
fire before the daily limit did, and the trigger would exist without being reachable.

**Trigger 7 takes a per-symbol measured threshold, not a constant.** Crypto trades
continuously so any gap is anomalous, but "anomalous" differs by four orders of
magnitude between BTCUSDT and a thin alt. `derive_p99_inter_tick_gap_seconds` computes
that per-symbol number from a recorded tick series; the nightly job that feeds it lives
in `data`, because recomputing it is I/O and this module has none.

What is *not* here: the trip itself. `evaluate_triggers` returns the `TripTrigger` rows a
trip would be written from, and `fking.risk.trip` writes them. Keeping the evaluation
separate from the act is what lets a supervisor evaluate continuously and trip once
(issue #54).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from types import MappingProxyType
from typing import Final, Literal
from uuid import UUID

from fking.risk._state import TripTrigger
from fking.risk.ceilings import HARD_CEILINGS, Ceiling, assert_within_ceilings
from fking.risk.drawdown import DrawdownBudgets, DrawdownState, LimitName

__all__ = [
    "DEFAULT_PORTFOLIO_BUDGETS",
    "LOSS_VELOCITY_WINDOW",
    "MINIMUM_GAP_OBSERVATIONS",
    "RECONCILIATION_ATTEMPTS_BEFORE_TRIP",
    "REJECTION_SAMPLE_ORDERS",
    "STALENESS_CALIBRATION_WINDOW",
    "TRIGGER_HARD_CEILINGS",
    "TRIGGER_ORDINALS",
    "OrderOutcome",
    "SymbolStaleness",
    "TriggerEvaluation",
    "TriggerId",
    "TriggerObservationError",
    "TriggerObservations",
    "TriggerThresholds",
    "derive_p99_inter_tick_gap_seconds",
    "evaluate_triggers",
    "loss_velocity_ratio",
]

_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")
_MICROSECONDS_PER_SECOND: Final = Decimal("1000000")


class TriggerObservationError(ValueError):
    """An observation record is malformed, and is refused rather than interpreted.

    A measurement this module cannot read is not a measurement of "nothing happening".
    Refusing here means the supervisor's own error path handles it -- which trips on an
    unhandled exception inside `risk` (trigger 9) -- rather than this module reporting a
    clean evaluation from an unreadable input.
    """


class TriggerId(StrEnum):
    """The trigger identities that reach an audit row.

    Strings rather than the table's integers, because `1` in a `kill_switch_events` row
    six months from now is only meaningful next to a copy of the version of
    `FAILSAFE.md` that was current when it was written. The ordinal is preserved
    separately in `TRIGGER_ORDINALS` so the row can still be matched to the table.
    """

    PORTFOLIO_DRAWDOWN = "portfolio_drawdown"
    DAILY_LOSS = "daily_loss"
    ROLLING_LOSS = "rolling_loss"
    LOSS_VELOCITY = "loss_velocity"
    RECONCILIATION_DIVERGENCE = "reconciliation_divergence"
    ORDER_REJECTION_RATE = "order_rejection_rate"
    MARKET_DATA_STALENESS = "market_data_staleness"
    CLOCK_SKEW = "clock_skew"
    UNHANDLED_EXCEPTION = "unhandled_exception"
    HARD_CEILING_BREACH = "hard_ceiling_breach"
    AUDIT_WRITE_FAILURE = "audit_write_failure"
    MANUAL = "manual"


# The row numbers in `FAILSAFE.md` section 2.1, kept so an operator reading a trip row
# can find the paragraph that explains it. Evaluation order follows these numbers.
TRIGGER_ORDINALS: Final[Mapping[TriggerId, int]] = MappingProxyType(
    {
        TriggerId.PORTFOLIO_DRAWDOWN: 1,
        TriggerId.DAILY_LOSS: 2,
        TriggerId.ROLLING_LOSS: 3,
        TriggerId.LOSS_VELOCITY: 4,
        TriggerId.RECONCILIATION_DIVERGENCE: 5,
        TriggerId.ORDER_REJECTION_RATE: 6,
        TriggerId.MARKET_DATA_STALENESS: 7,
        TriggerId.CLOCK_SKEW: 8,
        TriggerId.UNHANDLED_EXCEPTION: 9,
        TriggerId.HARD_CEILING_BREACH: 10,
        TriggerId.AUDIT_WRITE_FAILURE: 11,
        TriggerId.MANUAL: 12,
    }
)

# `FAILSAFE.md` section 2.1 trigger 4: 1.5% of equity inside any five-minute window. Not
# configurable, and that is the whole point of the trigger -- a window that can be
# lengthened is the daily limit again with a shorter name, and the pressure to lengthen
# it arrives the first time it fires during a legitimate market event.
LOSS_VELOCITY_WINDOW: Final = timedelta(minutes=5)

# Trigger 6 reads exactly the last twenty orders. Fixed rather than configured because
# the rate and the sample size trade off against each other: a caller free to set the
# sample to three can hold the rate at 20% and still fire on one rejection.
REJECTION_SAMPLE_ORDERS: Final = 20

# Trigger 5: a divergence that survives two reconciliation attempts. One attempt catches
# the ordinary race -- a fill that landed between the snapshot and the query -- and the
# second is what distinguishes that from a genuine disagreement about the book.
RECONCILIATION_ATTEMPTS_BEFORE_TRIP: Final = 2

# Trigger 7's calibration window: the trailing 30 days, recomputed nightly. Long enough
# that a weekend of thin trading does not dominate the percentile, short enough that a
# symbol whose liquidity profile has genuinely changed is re-measured within a month.
STALENESS_CALIBRATION_WINDOW: Final = timedelta(days=30)

# A 99th percentile drawn from fewer observations than this is the maximum with a
# decorative name. 100 gaps over 30 days is 3.3 ticks a day, which even a nearly dead
# alt clears; below it the symbol has no measurable inter-tick behaviour and the caller
# is told so rather than handed a number.
MINIMUM_GAP_OBSERVATIONS: Final = 100

# The portfolio-scope budgets `FAILSAFE.md` section 2.1 states for triggers 1-3: 10%
# drawdown from the persisted high-water mark and 3% daily loss against 00:00 UTC
# equity. The 4.5% rolling threshold in that table is not configured here because it is
# derived -- `DrawdownBudgets.rolling_loss_ratio` is 1.5x the daily budget, and 1.5 x 3%
# is exactly the documented 4.5%.
DEFAULT_PORTFOLIO_BUDGETS: Final = DrawdownBudgets(
    scope="portfolio",
    drawdown_ratio=Decimal("0.10"),
    daily_loss_ratio=Decimal("0.03"),
)

# Compiled ceilings for the thresholds that are not already bounded by
# `DRAWDOWN_HARD_CEILINGS`. Config may tighten any of them; loosening past these needs a
# source edit and a pull request labelled `safety:critical`.
TRIGGER_HARD_CEILINGS: Final[Mapping[str, Ceiling]] = MappingProxyType(
    {
        # Below the 3% default daily budget, not at it. At 3% the velocity trigger can
        # never fire before the daily limit does, which deletes the distinction the
        # trigger exists to draw; 2.5% keeps a configuration that maxes it out still
        # able to catch a fast loss inside an intact daily budget.
        "loss_velocity_ratio": Ceiling(Decimal("0.025")),
        # Half the sample. Ten rejections in the last twenty orders is past any reading
        # under which our model of the venue's filters is approximately right.
        "order_rejection_ratio": Ceiling(Decimal("0.50")),
        # Twice the documented 10x. Beyond this a symbol carrying an open position is
        # unobserved for longer than the escalation in section 3.1 contemplates, and the
        # position is being held against a price nobody has seen.
        "market_data_staleness_multiple": Ceiling(Decimal("20")),
        # Binance's default `recvWindow` is 5000 ms: past that the venue rejects signed
        # requests outright, so a skew ceiling above it bounds nothing that the venue has
        # not already bounded more harshly.
        "clock_skew_seconds": Ceiling(Decimal("5")),
    }
)

# Which `TriggerId` reports each drawdown-family limit. One mapping rather than three
# branches so that adding a limit to `drawdown.py` without a trigger id here fails at
# the lookup instead of silently evaluating two of three.
_DRAWDOWN_TRIGGERS: Final[Mapping[LimitName, TriggerId]] = MappingProxyType(
    {
        "drawdown": TriggerId.PORTFOLIO_DRAWDOWN,
        "daily_loss": TriggerId.DAILY_LOSS,
        "rolling_loss": TriggerId.ROLLING_LOSS,
    }
)

OrderOutcome = Literal["accepted", "rejected"]

# The two hard ceilings trigger 10 watches. Both are read from the compiled
# `HARD_CEILINGS`, never from the configured limits: the trigger exists to detect that
# the enforcement mechanism failed, and enforcement is what reads the configuration.
_GROSS_NOTIONAL_CEILING_NAME: Final = "max_portfolio_notional_usd"
_POSITION_COUNT_CEILING_NAME: Final = "max_open_positions"


def _require_utc(candidate: datetime, field_name: str) -> None:
    if candidate.tzinfo is None or candidate.utcoffset() != timedelta(0):
        raise TriggerObservationError(
            f"{field_name} must be timezone-aware UTC; got {candidate!r}. A naive "
            f"instant compared against an exchange timestamp is a silent offset"
        )


def _require_non_negative(candidate: int, field_name: str) -> None:
    if candidate < 0:
        raise TriggerObservationError(f"{field_name} must not be negative; got {candidate}")


def _require_decimal(candidate: Decimal, field_name: str) -> None:
    # `isinstance` rather than trust: these records are built at the process edge from
    # exchange responses and `ccxt` unified structures, and both hand back floats.
    if not isinstance(candidate, Decimal):
        raise TriggerObservationError(
            f"{field_name} must be a Decimal constructed from a string; got "
            f"{type(candidate).__name__}"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TriggerThresholds:
    """What each trigger compares against. Every field bounded in the dangerous direction.

    `portfolio_budgets` is the same type `fking.risk.drawdown` evaluates against, so
    triggers 1-3 and the drawdown limits cannot drift apart by configuration.
    """

    portfolio_budgets: DrawdownBudgets = DEFAULT_PORTFOLIO_BUDGETS
    loss_velocity_ratio: Decimal = Decimal("0.015")
    order_rejection_ratio: Decimal = Decimal("0.20")
    market_data_staleness_multiple: Decimal = Decimal("10")
    clock_skew_seconds: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if self.portfolio_budgets.scope != "portfolio":
            raise TriggerObservationError(
                f"the kill switch trips on portfolio-scope limits; got budgets scoped "
                f"{self.portfolio_budgets.scope!r}. A strategy-scope breach suspends "
                f"one strategy and is handled in `evolution`"
            )
        submitted: Mapping[str, Decimal] = MappingProxyType(
            {
                "loss_velocity_ratio": self.loss_velocity_ratio,
                "order_rejection_ratio": self.order_rejection_ratio,
                "market_data_staleness_multiple": self.market_data_staleness_multiple,
                "clock_skew_seconds": self.clock_skew_seconds,
            }
        )
        for name, value in submitted.items():
            _require_decimal(value, name)
            if value <= _ZERO:
                raise TriggerObservationError(
                    f"{name} must be positive; got {value}. A threshold of zero fires "
                    f"continuously, which is indistinguishable from a broken trigger"
                )
        assert_within_ceilings(submitted, TRIGGER_HARD_CEILINGS, scope="risk.triggers")


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolStaleness:
    """One symbol's freshness, against its own measured inter-tick behaviour.

    `p99_inter_tick_gap_seconds` is a measurement, supplied by the caller from
    `derive_p99_inter_tick_gap_seconds` over the trailing 30 days. It is a field rather
    than a constant because a constant is either useless on BTCUSDT or fires
    continuously on everything thinner.
    """

    symbol: str
    last_tick_at_utc: datetime
    p99_inter_tick_gap_seconds: Decimal
    has_open_position: bool

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise TriggerObservationError("symbol must not be blank")
        _require_utc(self.last_tick_at_utc, "last_tick_at_utc")
        _require_decimal(self.p99_inter_tick_gap_seconds, "p99_inter_tick_gap_seconds")
        if self.p99_inter_tick_gap_seconds <= _ZERO:
            raise TriggerObservationError(
                f"{self.symbol} reports a p99 inter-tick gap of "
                f"{self.p99_inter_tick_gap_seconds}s; a non-positive gap makes every "
                f"staleness comparison fire"
            )

    def age_seconds(self, as_of_utc: datetime) -> Decimal:
        """How long since the last tick, as an exact decimal number of seconds."""
        return _seconds_between(self.last_tick_at_utc, as_of_utc)


@dataclass(frozen=True, slots=True, kw_only=True)
class TriggerObservations:
    """Everything the twelve triggers are evaluated against, measured by the caller.

    One record rather than twelve arguments, because a trip row has to state what every
    trigger read at the moment one of them fired -- a post-mortem that can see only the
    trigger that fired cannot tell whether the others were also about to.

    `correlation_id` is the causing event's, propagated unchanged. It is what joins the
    trip row to the market-data event, the signal and the order that preceded it.
    """

    correlation_id: UUID
    observed_at_utc: datetime
    drawdown_state: DrawdownState
    reconciliation_divergence_attempts: int = 0
    reconciliation_divergence_is_beyond_dust: bool = False
    recent_order_outcomes: tuple[OrderOutcome, ...] = ()
    symbol_staleness: tuple[SymbolStaleness, ...] = ()
    clock_skew_seconds: Decimal = _ZERO
    unhandled_exception_module: str | None = None
    gross_exposure_usd: Decimal = _ZERO
    open_position_count: int = 0
    audit_write_failed: bool = False
    manual_trip_reason: str | None = None

    def __post_init__(self) -> None:
        _require_utc(self.observed_at_utc, "observed_at_utc")
        if self.drawdown_state.scope != "portfolio":
            raise TriggerObservationError(
                f"the kill switch trips on portfolio-scope state; got {self.drawdown_state.scope!r}"
            )
        if self.drawdown_state.observed_at_utc > self.observed_at_utc:
            raise TriggerObservationError(
                f"drawdown state is stamped {self.drawdown_state.observed_at_utc.isoformat()}, "
                f"after the observation instant {self.observed_at_utc.isoformat()}; a "
                f"trigger evaluated against a future state is not replayable"
            )
        _require_non_negative(
            self.reconciliation_divergence_attempts, "reconciliation_divergence_attempts"
        )
        _require_non_negative(self.open_position_count, "open_position_count")
        _require_decimal(self.clock_skew_seconds, "clock_skew_seconds")
        _require_decimal(self.gross_exposure_usd, "gross_exposure_usd")
        if self.gross_exposure_usd < _ZERO:
            raise TriggerObservationError(
                f"gross_exposure_usd is the absolute notional across the book and must "
                f"not be negative; got {self.gross_exposure_usd}"
            )
        for staleness in self.symbol_staleness:
            if staleness.last_tick_at_utc > self.observed_at_utc:
                raise TriggerObservationError(
                    f"{staleness.symbol} reports a tick at "
                    f"{staleness.last_tick_at_utc.isoformat()}, after the observation "
                    f"instant; a negative age reads as perfectly fresh"
                )
        if self.unhandled_exception_module is not None and not self.unhandled_exception_module:
            raise TriggerObservationError("unhandled_exception_module must name a module")
        if self.manual_trip_reason is not None and not self.manual_trip_reason.strip():
            raise TriggerObservationError(
                "a manual trip carries a reason; an empty one is a trip nobody can "
                "review afterwards"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class TriggerEvaluation:
    """Every trigger that fired at one instant, in `FAILSAFE.md` table order.

    All of them, not the first. A trip caused by three simultaneous triggers and a trip
    caused by one look identical in a row that records only the first, and they are not
    the same incident.
    """

    correlation_id: UUID
    observed_at_utc: datetime
    firing: tuple[TripTrigger, ...]

    @property
    def should_trip(self) -> bool:
        """True when the kill switch must close."""
        return bool(self.firing)

    @property
    def primary(self) -> TripTrigger | None:
        """The lowest-numbered firing trigger, which is the one the incident is named for."""
        return self.firing[0] if self.firing else None


def _seconds_between(earlier: datetime, later: datetime) -> Decimal:
    """The exact interval in seconds.

    Via integer microseconds rather than `timedelta.total_seconds()`, which returns a
    float: a 1000 ms clock-skew threshold compared against a binary float is a
    comparison that is wrong in the last place at exactly the boundary the threshold
    was chosen to sit on.
    """
    return Decimal((later - earlier) // timedelta(microseconds=1)) / _MICROSECONDS_PER_SECOND


def derive_p99_inter_tick_gap_seconds(
    tick_times_utc: Sequence[datetime],
    *,
    as_of_utc: datetime,
    window: timedelta = STALENESS_CALIBRATION_WINDOW,
) -> Decimal:
    """The 99th-percentile inter-tick gap for one symbol over the trailing `window`.

    Nearest-rank (`ceil(0.99n)`) rather than an interpolated percentile: interpolation
    invents a gap that was never observed, and the number is about to be multiplied by
    ten and used to decide whether a position is being held against a stale price.

    Refuses a sample too small to have a 99th percentile rather than returning the
    maximum under a percentile's name.
    """
    _require_utc(as_of_utc, "as_of_utc")
    if window <= timedelta(0):
        raise TriggerObservationError(f"window must be positive; got {window}")

    for index, tick in enumerate(tick_times_utc):
        # Validated before the window filter, because comparing a naive instant against
        # an aware horizon raises `TypeError` from inside a comprehension, which reports
        # the wrong thing about the wrong line.
        _require_utc(tick, f"tick_times_utc[{index}]")
        if index > 0 and tick <= tick_times_utc[index - 1]:
            raise TriggerObservationError(
                f"tick_times_utc must ascend strictly; {tick.isoformat()} does not "
                f"follow {tick_times_utc[index - 1].isoformat()}"
            )

    horizon = as_of_utc - window
    considered = [tick for tick in tick_times_utc if tick >= horizon]
    gaps = sorted(_seconds_between(earlier, later) for earlier, later in pairwise(considered))
    if len(gaps) < MINIMUM_GAP_OBSERVATIONS:
        raise TriggerObservationError(
            f"a 99th percentile needs at least {MINIMUM_GAP_OBSERVATIONS} gaps in the "
            f"trailing {window.days} days; got {len(gaps)}. A percentile from a short "
            f"sample is the sample maximum wearing a percentile's name"
        )
    # Nearest rank: the smallest observed gap at or above which 99% of the sample sits.
    rank = -(-99 * len(gaps) // 100)
    return gaps[rank - 1]


def loss_velocity_ratio(
    state: DrawdownState, *, window: timedelta = LOSS_VELOCITY_WINDOW
) -> Decimal:
    """Loss from the highest equity inside `window`, as a fraction of that high.

    Measured against the window *high* rather than the window *start*, for the same
    reason `DrawdownState.rolling_loss_ratio` is: a rise and an equal fall is the same
    money to the account, and measuring from the edge reports it as flat.

    Marks older than the window are ignored, so this number says nothing about a slow
    loss -- which is the point. Triggers 1-3 are what watch that.
    """
    if window <= timedelta(0):
        raise TriggerObservationError(f"window must be positive; got {window}")
    horizon = state.observed_at_utc - window
    inside = [mark.equity_usd for mark in state.rolling_marks if mark.observed_at_utc >= horizon]
    window_high = max((*inside, state.current_equity_usd))
    return max(_ZERO, (window_high - state.current_equity_usd) / window_high)


def _drawdown_triggers(
    observations: TriggerObservations, thresholds: TriggerThresholds
) -> list[TripTrigger]:
    """Triggers 1-3, read off the same state the drawdown limits are evaluated against."""
    firing: list[TripTrigger] = []
    budgets = thresholds.portfolio_budgets
    for limit_name, trigger_id in _DRAWDOWN_TRIGGERS.items():
        observed = observations.drawdown_state.observed_ratio_for(limit_name)
        budget = budgets.budget_for(limit_name)
        # `>=` rather than `>`, matching `drawdown.evaluate`: a limit stated as "10%"
        # that permits exactly 10% is a limit whose stated value is never reached.
        if observed >= budget:
            firing.append(
                TripTrigger(
                    trigger_id=trigger_id,
                    unit="fraction",
                    observed_value=observed,
                    threshold_value=budget,
                    detail=(
                        f"portfolio {limit_name} reached {observed} against a budget of "
                        f"{budget} at {observations.drawdown_state.observed_at_utc.isoformat()}"
                    ),
                )
            )
    return firing


def _loss_velocity_trigger(
    observations: TriggerObservations, thresholds: TriggerThresholds
) -> TripTrigger | None:
    """Trigger 4. Independent of the daily budget, and that independence is the design."""
    observed = loss_velocity_ratio(observations.drawdown_state)
    if observed < thresholds.loss_velocity_ratio:
        return None
    minutes = LOSS_VELOCITY_WINDOW // timedelta(minutes=1)
    return TripTrigger(
        trigger_id=TriggerId.LOSS_VELOCITY,
        unit="fraction",
        observed_value=observed,
        threshold_value=thresholds.loss_velocity_ratio,
        detail=(
            f"equity fell {observed} of its {minutes}-minute high against a velocity "
            f"threshold of {thresholds.loss_velocity_ratio}, independently of the daily "
            f"budget"
        ),
    )


def _reconciliation_trigger(observations: TriggerObservations) -> TripTrigger | None:
    """Trigger 5. A divergence that survived the retries is a disagreement, not a race."""
    if not observations.reconciliation_divergence_is_beyond_dust:
        return None
    attempts = observations.reconciliation_divergence_attempts
    if attempts < RECONCILIATION_ATTEMPTS_BEFORE_TRIP:
        return None
    return TripTrigger(
        trigger_id=TriggerId.RECONCILIATION_DIVERGENCE,
        unit="attempts",
        observed_value=Decimal(attempts),
        threshold_value=Decimal(RECONCILIATION_ATTEMPTS_BEFORE_TRIP),
        detail=(
            f"a reconciliation divergence beyond dust tolerance persisted through "
            f"{attempts} attempts; the local position record is not what the venue holds"
        ),
    )


def _rejection_rate_trigger(
    observations: TriggerObservations, thresholds: TriggerThresholds
) -> TripTrigger | None:
    """Trigger 6. A proxy for "our model of the venue is wrong".

    Refuses to fire on a short sample. One rejection out of one is a 100% rejection rate
    and says nothing at all; waiting for the full twenty is what makes the ratio mean
    what its name says.
    """
    sample = observations.recent_order_outcomes[-REJECTION_SAMPLE_ORDERS:]
    if len(sample) < REJECTION_SAMPLE_ORDERS:
        return None
    rejected = sum(1 for outcome in sample if outcome == "rejected")
    observed = Decimal(rejected) / Decimal(len(sample))
    # `>` rather than `>=`: FAILSAFE.md section 2.1 states "> 20%", and at exactly four
    # rejections in twenty the sample is one order away from either reading.
    if observed <= thresholds.order_rejection_ratio:
        return None
    return TripTrigger(
        trigger_id=TriggerId.ORDER_REJECTION_RATE,
        unit="fraction",
        observed_value=observed,
        threshold_value=thresholds.order_rejection_ratio,
        detail=(
            f"{rejected} of the last {len(sample)} orders were rejected; the venue's "
            f"filters, minimum notionals or margin requirements are not what sizing "
            f"believes them to be"
        ),
    )


def _staleness_triggers(
    observations: TriggerObservations, thresholds: TriggerThresholds
) -> list[TripTrigger]:
    """Trigger 7, once per stale symbol that carries an open position.

    A symbol with no open position that goes stale is `DATA_STALE` and blocks new
    positions in it; it does not trip the switch, because there is nothing being held
    against the price nobody can see.
    """
    firing: list[TripTrigger] = []
    for staleness in observations.symbol_staleness:
        if not staleness.has_open_position:
            continue
        threshold = staleness.p99_inter_tick_gap_seconds * thresholds.market_data_staleness_multiple
        age = staleness.age_seconds(observations.observed_at_utc)
        if age <= threshold:
            continue
        firing.append(
            TripTrigger(
                trigger_id=TriggerId.MARKET_DATA_STALENESS,
                unit="seconds",
                observed_value=age,
                threshold_value=threshold,
                detail=(
                    f"{staleness.symbol} has an open position and its last tick is {age}s "
                    f"old, past {thresholds.market_data_staleness_multiple}x its measured "
                    f"p99 inter-tick gap of {staleness.p99_inter_tick_gap_seconds}s"
                ),
            )
        )
    return firing


def _clock_skew_trigger(
    observations: TriggerObservations, thresholds: TriggerThresholds
) -> TripTrigger | None:
    """Trigger 8. Signed skew, compared on magnitude: running early is as wrong as late."""
    observed = abs(observations.clock_skew_seconds)
    if observed <= thresholds.clock_skew_seconds:
        return None
    return TripTrigger(
        trigger_id=TriggerId.CLOCK_SKEW,
        unit="seconds",
        observed_value=observed,
        threshold_value=thresholds.clock_skew_seconds,
        detail=(
            f"local time differs from exchange server time by {observed}s; every "
            f"timestamp this process stamps on an order, a fill or an audit row is "
            f"wrong by that much"
        ),
    )


def _unhandled_exception_trigger(observations: TriggerObservations) -> TripTrigger | None:
    """Trigger 9. Any unhandled exception inside `risk` or `execution`."""
    module = observations.unhandled_exception_module
    if module is None:
        return None
    return TripTrigger(
        trigger_id=TriggerId.UNHANDLED_EXCEPTION,
        unit="count",
        observed_value=_ONE,
        threshold_value=_ONE,
        detail=(
            f"an unhandled exception escaped {module}; the process is in the least "
            f"understood state it can reach and its position record cannot be trusted"
        ),
    )


def _hard_ceiling_triggers(observations: TriggerObservations) -> list[TripTrigger]:
    """Trigger 10, which should be impossible.

    Ceilings are enforced at order construction, so exceeding one means the enforcement
    failed. Both breaches are reported rather than the first, because "the mechanism is
    broken" is the finding and the count of ways it is broken is evidence about how.
    """
    firing: list[TripTrigger] = []
    notional_ceiling = HARD_CEILINGS[_GROSS_NOTIONAL_CEILING_NAME]
    if notional_ceiling.is_exceeded_by(observations.gross_exposure_usd):
        firing.append(
            TripTrigger(
                trigger_id=TriggerId.HARD_CEILING_BREACH,
                unit="usd",
                observed_value=observations.gross_exposure_usd,
                threshold_value=notional_ceiling.bound,
                detail=(
                    f"gross exposure {observations.gross_exposure_usd} USD is above the "
                    f"compiled hard ceiling {notional_ceiling.bound}; order construction "
                    f"is supposed to make this unreachable"
                ),
            )
        )
    count_ceiling = HARD_CEILINGS[_POSITION_COUNT_CEILING_NAME]
    observed_count = Decimal(observations.open_position_count)
    if count_ceiling.is_exceeded_by(observed_count):
        firing.append(
            TripTrigger(
                trigger_id=TriggerId.HARD_CEILING_BREACH,
                unit="count",
                observed_value=observed_count,
                threshold_value=count_ceiling.bound,
                detail=(
                    f"{observations.open_position_count} open positions is above the "
                    f"compiled hard ceiling {count_ceiling.bound}; order construction is "
                    f"supposed to make this unreachable"
                ),
            )
        )
    return firing


def _audit_write_trigger(observations: TriggerObservations) -> TripTrigger | None:
    """Trigger 11. The audit log is a precondition for trading, not a record of it."""
    if not observations.audit_write_failed:
        return None
    return TripTrigger(
        trigger_id=TriggerId.AUDIT_WRITE_FAILURE,
        unit="count",
        observed_value=_ONE,
        threshold_value=_ONE,
        detail=(
            "an audit write failed; a trade taken from here is permanently "
            "unreconstructable, not merely harder to reconstruct"
        ),
    )


def _manual_trigger(observations: TriggerObservations) -> TripTrigger | None:
    """Trigger 12. A person decided, and the row says why."""
    reason = observations.manual_trip_reason
    if reason is None:
        return None
    return TripTrigger(
        trigger_id=TriggerId.MANUAL,
        unit="count",
        observed_value=_ONE,
        threshold_value=_ONE,
        detail=f"manual trip requested: {reason.strip()}",
    )


def evaluate_triggers(
    observations: TriggerObservations, thresholds: TriggerThresholds | None = None
) -> TriggerEvaluation:
    """Every trigger that fires against `observations`, in `FAILSAFE.md` table order.

    Called on every equity mark, every fill, every reconciliation result and every
    market-data heartbeat -- continuously, not on a schedule. A trigger evaluated once a
    minute carries a minute of slack, and the loss-velocity trigger exists precisely
    because a minute is long enough for the thing it watches for to complete.
    """
    applied = TriggerThresholds() if thresholds is None else thresholds
    firing: list[TripTrigger] = [
        *_drawdown_triggers(observations, applied),
        *_optional(_loss_velocity_trigger(observations, applied)),
        *_optional(_reconciliation_trigger(observations)),
        *_optional(_rejection_rate_trigger(observations, applied)),
        *_staleness_triggers(observations, applied),
        *_optional(_clock_skew_trigger(observations, applied)),
        *_optional(_unhandled_exception_trigger(observations)),
        *_hard_ceiling_triggers(observations),
        *_optional(_audit_write_trigger(observations)),
        *_optional(_manual_trigger(observations)),
    ]
    return TriggerEvaluation(
        correlation_id=observations.correlation_id,
        observed_at_utc=observations.observed_at_utc,
        firing=tuple(firing),
    )


def _optional(trigger: TripTrigger | None) -> tuple[TripTrigger, ...]:
    """`()` or a one-tuple, so the evaluation list above reads as one sequence."""
    return () if trigger is None else (trigger,)
