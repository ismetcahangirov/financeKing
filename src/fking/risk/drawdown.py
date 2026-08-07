"""Drawdown, daily-loss and rolling-24h-loss limits over state that is restored, never
recomputed.

The whole file exists because of one failure, and it is a failure that looks like
nothing. Equity peaked at 100, the drawdown budget is 20%, current equity is 85. The
process restarts. If the high-water mark initialises from *current* equity, the budget
is now 20% below 85 -- the system has quietly granted itself another 15% of drawdown at
exactly the moment the evidence says it should have less. No log line is wrong. The
dashboard reads `drawdown: 0.0%`. The limit does not bind until the account has lost
nearly a third of its peak.

So there is no constructor here that derives a peak from current equity. `restore`
takes the persisted peak, the persisted day anchor and the persisted rolling marks and
refuses each of them if absent; `open_first_time` exists separately and is the only
place where peak and current equity may coincide, because at genuine inception they do.
A restart cannot reach `open_first_time` without a caller stating in one word that this
subject has no history -- which is a reviewable line, not a silent default.

**Everything here is pure.** `as_of_utc` is a parameter on every transition, never a
clock read: `.claude/rules/time-and-timezones.md` makes clock access in `risk`
mechanically impossible (`tools/checks/clock_isolation.py`), and the reason is that a
risk decision which depends on the wall clock cannot be replayed and therefore cannot be
audited. The caller -- the OMS fill consumer, the mark-update consumer, the recovery
sequence -- owns the clock and the database; this module owns the arithmetic.

**Three limits, and which one actually binds.**

- The *fixed* daily limit is measured from equity at 00:00 UTC. Crypto has no session
  close, so that boundary is arbitrary, and an arbitrary boundary in a loss limit is an
  exploitable seam: a strategy 2.9% down at 23:50 has its whole budget back eleven
  minutes later. The real exposure is nearer 6% over twelve hours than 3% over a day.
- The *rolling* 24h limit is therefore the one that binds during a bad night, set at
  1.5x the fixed one. It is measured against the highest equity observed inside the
  trailing window rather than against the equity at the window's start: a rise followed
  by a fall of the same magnitude is the same loss to the account, and measuring from
  the window edge would report it as flat.
- The drawdown limits are measured from the all-time high-water mark, per scope.

`evaluate` is called on every fill and every mark update, never on a schedule. A limit
checked hourly is a limit with an hour of slack in it, and a strategy can lose its
entire budget inside that hour.

**De-risking before the cliff.** A hard limit at 15% means full size at 14.9% and death
at 15.0% -- maximum exposure at the moment of maximum evidence that something is wrong.
`derisk_scalar` scales size linearly across the last 40% of the budget instead, so the
limit is approached asymptotically. Most drawdowns that reach 12% never reach 15%, and a
strategy that survives one at reduced size is still available afterwards.

`RISK_PHILOSOPHY.md`, `FAILSAFE.md` section 4 (restore order at recovery step 6),
`SCORING_ENGINE.md` (a breach is a scored violation, not only an incident).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Literal

from fking.risk.ceilings import Ceiling, assert_within_ceilings

__all__ = [
    "DRAWDOWN_HARD_CEILINGS",
    "ROLLING_WINDOW",
    "BreachRecord",
    "DrawdownBudgets",
    "DrawdownState",
    "DrawdownStateError",
    "EquityMark",
    "LimitVerdict",
    "derisk_scalar",
    "evaluate",
    "from_row",
    "open_first_time",
    "restore",
    "to_row",
    "utc_day_start",
    "with_equity",
]

_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")
_UTC_OFFSET: Final = timedelta(0)

# The rolling window is 24 hours rather than a calendar day precisely so that it has no
# boundary to sit just before. Its length is not configurable: a window that can be
# shortened is a seam that can be re-created.
ROLLING_WINDOW: Final = timedelta(hours=24)

# The fraction of the budget consumed before de-risking begins, and the width of the
# taper. Issue #52: m = clip(1 - (DD/DD_max - 0.6) / 0.4, 0, 1). Both are compiled in
# rather than configured: they are the shape of the size schedule, and a schedule that
# can be flattened to a step function by configuration is a hard limit again.
_DERISK_ONSET_RATIO: Final = Decimal("0.6")
_DERISK_TAPER_WIDTH: Final = Decimal("0.4")

# Bounded above, in the direction that is dangerous for each. These are a separate
# mapping from `fking.risk.ceilings.HARD_CEILINGS` rather than three more entries in it:
# that mapping is the union of the fields `RiskLimits` submits, and every name in it
# must be submitted or `assert_within_ceilings` refuses the whole configuration. A
# per-scope drawdown budget is not a `RiskLimits` field, so adding it there would either
# break that model or make the "a bound on a field nobody reports constrains nothing"
# guard vacuous. The *machinery* is shared; only the mapping is local.
#
# Values from RISK_PHILOSOPHY.md section 4 and issue #52: strategy drawdown 15% with a
# 25% ceiling, portfolio drawdown 10% with a 20% ceiling, daily loss 3% of 00:00 UTC
# equity with a 5% ceiling.
#
# `rolling_loss_ratio` is stated at exactly 1.5x the daily ceiling, which means that
# while `_ROLLING_MULTIPLE` is 1.5 it cannot bind independently -- a daily budget inside
# its own ceiling always implies a rolling budget inside this one. It is written down
# anyway, because the multiple is the kind of constant a later change moves, and a bound
# that only starts working the day somebody raises the multiple is the bound you want to
# already be there. `test_the_rolling_budget_is_derived_...` pins the relationship.
DRAWDOWN_HARD_CEILINGS: Final[Mapping[str, Ceiling]] = MappingProxyType(
    {
        "strategy_drawdown_ratio": Ceiling(Decimal("0.25")),
        "portfolio_drawdown_ratio": Ceiling(Decimal("0.20")),
        "daily_loss_ratio": Ceiling(Decimal("0.05")),
        "rolling_loss_ratio": Ceiling(Decimal("0.075")),
    }
)

# The multiple applied to the fixed daily budget to obtain the rolling one. 1.5 rather
# than 2.0 because two consecutive fixed-window days at the full budget is the shape the
# rolling limit exists to refuse, and 2.0 would permit exactly that.
_ROLLING_MULTIPLE: Final = Decimal("1.5")

Scope = Literal["strategy", "portfolio"]
LimitName = Literal["drawdown", "daily_loss", "rolling_loss"]

# Decode tables rather than `in` checks against a tuple: a membership test against a
# tuple of literals does not narrow `object` to the Literal type, so the version that
# reads naturally is the version that needs a `cast` -- and a `cast` here would be the
# one place a malformed persisted row could enter as a well-typed one.
_SCOPES: Final[Mapping[str, Scope]] = MappingProxyType(
    {"strategy": "strategy", "portfolio": "portfolio"}
)
_LIMIT_NAMES: Final[Mapping[str, LimitName]] = MappingProxyType(
    {"drawdown": "drawdown", "daily_loss": "daily_loss", "rolling_loss": "rolling_loss"}
)

# What a breach at each scope authorises, and nothing beyond it. A strategy-scope breach
# is handled inside `risk` and `evolution`; a portfolio-scope breach is *requested* of
# the kill switch rather than acted on here, because flattening the whole book is one
# mechanism with one owner (`FAILSAFE.md`), and a second caller that closes positions
# directly is a second, untested halt path.
BreachResponse = Literal["suspend_strategy", "request_kill_switch_trip"]
_RESPONSE_BY_SCOPE: Final[Mapping[Scope, BreachResponse]] = MappingProxyType(
    {"strategy": "suspend_strategy", "portfolio": "request_kill_switch_trip"}
)


class DrawdownStateError(ValueError):
    """Persisted drawdown state is absent, malformed, or moving backwards in time."""


def _require_utc(candidate: object, field_name: str) -> datetime:
    """A timezone-aware datetime whose offset is exactly UTC.

    Rejects rather than converts, for the reason in `.claude/rules/time-and-timezones.md`:
    `astimezone(UTC)` launders a wrong guess made three modules ago into a confident
    value, and the resulting day boundary is then wrong by the guessed offset for the
    life of the account.
    """
    if not isinstance(candidate, datetime):
        raise DrawdownStateError(
            f"{field_name} must be a datetime, got {type(candidate).__name__} {candidate!r}"
        )
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise DrawdownStateError(f"{field_name} must be timezone-aware; got naive {candidate!r}")
    if candidate.utcoffset() != _UTC_OFFSET:
        raise DrawdownStateError(
            f"{field_name} must be UTC; got offset {candidate.utcoffset()!r} in {candidate!r}"
        )
    return candidate


def _require_positive_equity(candidate: object, field_name: str) -> Decimal:
    """A finite, strictly positive `Decimal` equity.

    `float` is named separately because it is the one wrong type that arrives by
    accident and whose error is already baked in before this call. Zero and negative
    equity are refused because every ratio here divides by an equity figure, and a
    drawdown expressed against zero is not a smaller number -- it is an undefined one.
    """
    if isinstance(candidate, float):
        raise DrawdownStateError(
            f"{field_name} must be a Decimal constructed from str, not a float; "
            f"got {candidate!r}, which is already rounded"
        )
    if not isinstance(candidate, Decimal):
        raise DrawdownStateError(
            f"{field_name} must be a Decimal, got {type(candidate).__name__} {candidate!r}"
        )
    if not candidate.is_finite():
        raise DrawdownStateError(f"{field_name} must be finite; got {candidate}")
    if candidate <= _ZERO:
        raise DrawdownStateError(f"{field_name} must be positive; got {candidate}")
    return candidate


def utc_day_start(moment_utc: datetime) -> datetime:
    """The 00:00 UTC boundary of the day containing `moment_utc`.

    Named and exported rather than inlined because it is the seam the rolling limit
    exists to cover, and a reader tracing a daily-budget reset needs one definition to
    read instead of three `.replace(...)` calls that might disagree.
    """
    aware = _require_utc(moment_utc, "moment_utc")
    return aware.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True, slots=True)
class EquityMark:
    """One observation of equity, as it stood at an instant."""

    observed_at_utc: datetime
    equity_usd: Decimal

    def __post_init__(self) -> None:
        _require_utc(self.observed_at_utc, "observed_at_utc")
        _require_positive_equity(self.equity_usd, "equity_usd")


@dataclass(frozen=True, slots=True)
class BreachRecord:
    """A limit that was crossed, with everything needed to reconstruct the decision.

    Carries the observed value *and* the threshold, because a breach message stating
    only that a limit was hit forces the reader to reconstruct the budget from a
    configuration file whose value may since have changed.
    """

    scope: Scope
    subject_id: str
    limit_name: LimitName
    observed_ratio: Decimal
    budget_ratio: Decimal
    breached_at_utc: datetime
    response: BreachResponse

    def __post_init__(self) -> None:
        _require_utc(self.breached_at_utc, "breached_at_utc")


@dataclass(frozen=True, slots=True)
class DrawdownBudgets:
    """The three budgets for one scope, refused at construction above their ceilings.

    The rolling budget is derived, not configured. Making it a free field would let a
    configuration set it *below* the fixed daily budget, which reads as conservative and
    is not: the fixed budget would then never bind, and the seam the rolling limit
    exists to cover would be back with a different name on it.
    """

    scope: Scope
    drawdown_ratio: Decimal
    daily_loss_ratio: Decimal

    def __post_init__(self) -> None:
        drawdown_field = f"{self.scope}_drawdown_ratio"
        submitted: Mapping[str, Decimal] = MappingProxyType(
            {
                drawdown_field: self.drawdown_ratio,
                "daily_loss_ratio": self.daily_loss_ratio,
                "rolling_loss_ratio": self.rolling_loss_ratio,
            }
        )
        ceilings: Mapping[str, Ceiling] = MappingProxyType(
            {name: bound for name, bound in DRAWDOWN_HARD_CEILINGS.items() if name in submitted}
        )
        assert_within_ceilings(submitted, ceilings, scope=f"risk.drawdown.{self.scope}")

    @property
    def rolling_loss_ratio(self) -> Decimal:
        """The trailing-24h budget: 1.5x the fixed daily one."""
        return self.daily_loss_ratio * _ROLLING_MULTIPLE

    def budget_for(self, limit_name: LimitName) -> Decimal:
        if limit_name == "drawdown":
            return self.drawdown_ratio
        if limit_name == "daily_loss":
            return self.daily_loss_ratio
        return self.rolling_loss_ratio


@dataclass(frozen=True, slots=True)
class DrawdownState:
    """Everything that must survive a restart, and nothing that may be re-derived.

    `peak_equity_usd`, `day_open_equity_usd` and `rolling_marks` are the three fields
    whose loss silently widens a limit. They are stored, restored, and never recomputed
    from an in-memory series -- see the module docstring for what recomputation costs.

    `latched_breach` is why a recovered drawdown does not resume trading. A strategy
    that breached at 15% and has since recovered to 9% has produced evidence about
    itself that recovery does not retract; resuming automatically would make the limit a
    speed bump. Clearing it is a lifecycle decision taken in `evolution`, deliberately
    not expressible as a transition here.
    """

    scope: Scope
    subject_id: str
    peak_equity_usd: Decimal
    current_equity_usd: Decimal
    day_start_utc: datetime
    day_open_equity_usd: Decimal
    rolling_marks: tuple[EquityMark, ...]
    observed_at_utc: datetime
    latched_breach: BreachRecord | None = None

    def __post_init__(self) -> None:
        _require_positive_equity(self.peak_equity_usd, "peak_equity_usd")
        _require_positive_equity(self.current_equity_usd, "current_equity_usd")
        _require_positive_equity(self.day_open_equity_usd, "day_open_equity_usd")
        _require_utc(self.observed_at_utc, "observed_at_utc")
        if self.day_start_utc != utc_day_start(self.day_start_utc):
            raise DrawdownStateError(
                f"day_start_utc {self.day_start_utc.isoformat()} is not a 00:00 UTC boundary; "
                f"an anchor off the boundary makes the daily budget reset at an "
                f"unrepeatable instant"
            )
        if self.peak_equity_usd < self.current_equity_usd:
            raise DrawdownStateError(
                f"peak_equity_usd {self.peak_equity_usd} is below current_equity_usd "
                f"{self.current_equity_usd}; a high-water mark that trails current equity "
                f"is the restart bug this type exists to prevent"
            )
        if not self.rolling_marks:
            raise DrawdownStateError(
                "rolling_marks is empty; the trailing-24h window has no reference equity "
                "and the rolling limit would silently never bind"
            )
        ordering = tuple(mark.observed_at_utc for mark in self.rolling_marks)
        if list(ordering) != sorted(ordering):
            raise DrawdownStateError("rolling_marks must be ordered by observed_at_utc")

    @property
    def drawdown_ratio(self) -> Decimal:
        """Peak-to-current drawdown as a fraction of the high-water mark."""
        return (self.peak_equity_usd - self.current_equity_usd) / self.peak_equity_usd

    @property
    def daily_loss_ratio(self) -> Decimal:
        """Loss since 00:00 UTC as a fraction of the equity at that boundary.

        Clipped at zero on the upside: a profitable day consumes none of the budget, and
        a negative "loss" carried into a comparison would read as headroom that the next
        loss does not actually have.
        """
        loss = (self.day_open_equity_usd - self.current_equity_usd) / self.day_open_equity_usd
        return max(_ZERO, loss)

    @property
    def rolling_loss_ratio(self) -> Decimal:
        """Loss from the highest equity in the trailing 24h, as a fraction of that high.

        Against the window *high*, not the window *start*: a rise then an equal fall is
        the same loss to the account, and measuring from the edge reports it as flat.
        """
        window_high = max(mark.equity_usd for mark in self.rolling_marks)
        window_high = max(window_high, self.current_equity_usd)
        loss = (window_high - self.current_equity_usd) / window_high
        return max(_ZERO, loss)

    def observed_ratio_for(self, limit_name: LimitName) -> Decimal:
        if limit_name == "drawdown":
            return self.drawdown_ratio
        if limit_name == "daily_loss":
            return self.daily_loss_ratio
        return self.rolling_loss_ratio


def open_first_time(
    *,
    scope: Scope,
    subject_id: str,
    opening_equity_usd: Decimal,
    as_of_utc: datetime,
) -> DrawdownState:
    """State for a subject with no history at all.

    The *only* place peak and current equity may coincide, and it says so in its name.
    A restart cannot arrive here by omission: `restore` requires the persisted peak
    explicitly, so reaching this function is a caller stating that this strategy or
    portfolio has never traded -- one reviewable word rather than a silent default.
    """
    equity = _require_positive_equity(opening_equity_usd, "opening_equity_usd")
    moment = _require_utc(as_of_utc, "as_of_utc")
    return DrawdownState(
        scope=scope,
        subject_id=subject_id,
        peak_equity_usd=equity,
        current_equity_usd=equity,
        day_start_utc=utc_day_start(moment),
        day_open_equity_usd=equity,
        rolling_marks=(EquityMark(observed_at_utc=moment, equity_usd=equity),),
        observed_at_utc=moment,
    )


def restore(  # noqa: PLR0913 - see the docstring: collapsing these into one object
    # would give the object a constructor, and a constructor is where a default for
    # peak_equity_usd gets added. Nine explicit, defaultless arguments is the point.
    *,
    scope: Scope,
    subject_id: str,
    peak_equity_usd: Decimal,
    current_equity_usd: Decimal,
    day_start_utc: datetime,
    day_open_equity_usd: Decimal,
    rolling_marks: Sequence[EquityMark],
    observed_at_utc: datetime,
    latched_breach: BreachRecord | None,
) -> DrawdownState:
    """Rebuild persisted state exactly, refusing anything that is missing.

    Every argument is keyword-only and none has a default. A default here is a value
    somebody forgets to pass, and the value they would forget is the high-water mark --
    which is the entire failure this module is about. `FAILSAFE.md` section 4 puts this
    call at recovery step 6, before market data and strategies come back, so that no
    decision is ever taken against a partially restored budget.
    """
    return DrawdownState(
        scope=scope,
        subject_id=subject_id,
        peak_equity_usd=peak_equity_usd,
        current_equity_usd=current_equity_usd,
        day_start_utc=day_start_utc,
        day_open_equity_usd=day_open_equity_usd,
        rolling_marks=tuple(rolling_marks),
        observed_at_utc=observed_at_utc,
        latched_breach=latched_breach,
    )


def to_row(state: DrawdownState) -> Mapping[str, object]:
    """The persisted form of `state`, as primitives a repository can bind directly.

    A codec rather than an ORM mapping, and it lives here rather than in
    `fking.platform.persistence` for a boundary reason: `risk` may not perform I/O
    (`CLAUDE.md` section 4) and `platform` may not import another `fking` module
    (`.claude/rules/module-boundaries.md`), so neither package can hold both the type and
    the SQL. The type and its exact serialisation live together; the statement that binds
    the result lives with whoever owns the recovery sequence.

    Every equity figure is encoded as a string, not as a number. A `Decimal` that passes
    through a JSON encoder becomes a float and comes back rounded, and the rounding is
    already present before anything here could notice it.
    """
    return MappingProxyType(
        {
            "scope": state.scope,
            "subject_id": state.subject_id,
            "peak_equity_usd": str(state.peak_equity_usd),
            "current_equity_usd": str(state.current_equity_usd),
            "day_start_utc": state.day_start_utc.isoformat(),
            "day_open_equity_usd": str(state.day_open_equity_usd),
            "observed_at_utc": state.observed_at_utc.isoformat(),
            "rolling_marks": tuple(
                {
                    "observed_at_utc": mark.observed_at_utc.isoformat(),
                    "equity_usd": str(mark.equity_usd),
                }
                for mark in state.rolling_marks
            ),
            "latched_breach": None
            if state.latched_breach is None
            else {
                "limit_name": state.latched_breach.limit_name,
                "observed_ratio": str(state.latched_breach.observed_ratio),
                "budget_ratio": str(state.latched_breach.budget_ratio),
                "breached_at_utc": state.latched_breach.breached_at_utc.isoformat(),
                "response": state.latched_breach.response,
            },
        }
    )


def _decode_moment(row: Mapping[str, object], field_name: str) -> datetime:
    raw = row.get(field_name)
    if not isinstance(raw, str):
        raise DrawdownStateError(f"persisted {field_name} must be an ISO-8601 string, got {raw!r}")
    return _require_utc(datetime.fromisoformat(raw), field_name)


def _decode_ratio(row: Mapping[str, object], field_name: str) -> Decimal:
    raw = row.get(field_name)
    if not isinstance(raw, str):
        raise DrawdownStateError(f"persisted {field_name} must be a decimal string, got {raw!r}")
    ratio = Decimal(raw)
    if not ratio.is_finite() or ratio < _ZERO:
        raise DrawdownStateError(f"persisted {field_name} must be a finite fraction, got {ratio}")
    return ratio


def _decode_equity(row: Mapping[str, object], field_name: str) -> Decimal:
    raw = row.get(field_name)
    if not isinstance(raw, str):
        raise DrawdownStateError(
            f"persisted {field_name} must be a decimal string, got {raw!r}; a number here "
            f"has already been through a float"
        )
    return _require_positive_equity(Decimal(raw), field_name)


def from_row(row: Mapping[str, object]) -> DrawdownState:
    """Rebuild state from its persisted form, refusing anything absent or malformed.

    Deliberately intolerant. A row missing `peak_equity_usd` is not a row to fill in with
    current equity -- that substitution is the failure this module exists to prevent, and
    it is the one a lenient decoder makes silently.
    """
    raw_scope = row.get("scope")
    if not isinstance(raw_scope, str) or raw_scope not in _SCOPES:
        raise DrawdownStateError(
            f"persisted scope must be strategy or portfolio, got {raw_scope!r}"
        )
    scope = _SCOPES[raw_scope]
    subject_id = row.get("subject_id")
    if not isinstance(subject_id, str) or not subject_id.strip():
        raise DrawdownStateError(f"persisted subject_id must be non-empty text, got {subject_id!r}")

    raw_marks = row.get("rolling_marks")
    if not isinstance(raw_marks, Sequence) or isinstance(raw_marks, str) or not raw_marks:
        raise DrawdownStateError(
            f"persisted rolling_marks must be a non-empty sequence, got {raw_marks!r}"
        )
    marks: list[EquityMark] = []
    for entry in raw_marks:
        if not isinstance(entry, Mapping):
            raise DrawdownStateError(f"persisted rolling mark must be a mapping, got {entry!r}")
        marks.append(
            EquityMark(
                observed_at_utc=_decode_moment(entry, "observed_at_utc"),
                equity_usd=_decode_equity(entry, "equity_usd"),
            )
        )

    raw_breach = row.get("latched_breach")
    breach: BreachRecord | None = None
    if raw_breach is not None:
        if not isinstance(raw_breach, Mapping):
            raise DrawdownStateError(
                f"persisted latched_breach must be a mapping or null, got {raw_breach!r}"
            )
        raw_limit_name = raw_breach.get("limit_name")
        if not isinstance(raw_limit_name, str) or raw_limit_name not in _LIMIT_NAMES:
            raise DrawdownStateError(f"persisted limit_name is unknown: {raw_limit_name!r}")
        breach = BreachRecord(
            scope=scope,
            subject_id=subject_id,
            limit_name=_LIMIT_NAMES[raw_limit_name],
            observed_ratio=_decode_ratio(raw_breach, "observed_ratio"),
            budget_ratio=_decode_ratio(raw_breach, "budget_ratio"),
            breached_at_utc=_decode_moment(raw_breach, "breached_at_utc"),
            response=_RESPONSE_BY_SCOPE[scope],
        )

    return restore(
        scope=scope,
        subject_id=subject_id,
        peak_equity_usd=_decode_equity(row, "peak_equity_usd"),
        current_equity_usd=_decode_equity(row, "current_equity_usd"),
        day_start_utc=_decode_moment(row, "day_start_utc"),
        day_open_equity_usd=_decode_equity(row, "day_open_equity_usd"),
        rolling_marks=tuple(marks),
        observed_at_utc=_decode_moment(row, "observed_at_utc"),
        latched_breach=breach,
    )


def _prune(marks: Sequence[EquityMark], *, as_of_utc: datetime) -> tuple[EquityMark, ...]:
    """Marks inside the trailing window, plus the last one at or before its start.

    Keeping the straddling mark matters: drop it and a quiet 24 hours leaves the window
    holding only the newest observation, so the rolling high equals current equity and
    the rolling limit reports zero loss no matter how far equity has fallen.
    """
    window_start = as_of_utc - ROLLING_WINDOW
    inside = tuple(mark for mark in marks if mark.observed_at_utc > window_start)
    before = tuple(mark for mark in marks if mark.observed_at_utc <= window_start)
    return (before[-1], *inside) if before else inside


def with_equity(state: DrawdownState, *, equity_usd: Decimal, as_of_utc: datetime) -> DrawdownState:
    """State after observing `equity_usd` at `as_of_utc`. Returns a new object.

    Rolls the daily anchor when `as_of_utc` lands in a later UTC day, raises the
    high-water mark when equity exceeds it, and prunes the rolling window. Time may not
    move backwards: an out-of-order mark would lower the day anchor or re-open a pruned
    window, and both widen a limit rather than raising an error somebody would see.
    """
    equity = _require_positive_equity(equity_usd, "equity_usd")
    moment = _require_utc(as_of_utc, "as_of_utc")
    if moment < state.observed_at_utc:
        raise DrawdownStateError(
            f"as_of_utc {moment.isoformat()} precedes the last observation "
            f"{state.observed_at_utc.isoformat()}; drawdown state is not reorderable"
        )

    day_start = utc_day_start(moment)
    rolled = day_start > state.day_start_utc
    marks = _prune((*state.rolling_marks, EquityMark(moment, equity)), as_of_utc=moment)
    return replace(
        state,
        peak_equity_usd=max(state.peak_equity_usd, equity),
        current_equity_usd=equity,
        day_start_utc=day_start,
        # The new day opens at the equity carried across the boundary, which is this
        # observation -- not the previous day's open, and not the peak.
        day_open_equity_usd=equity if rolled else state.day_open_equity_usd,
        rolling_marks=marks,
        observed_at_utc=moment,
    )


def derisk_scalar(*, observed_ratio: Decimal, budget_ratio: Decimal) -> Decimal:
    """The size multiplier at this level of budget consumption, in [0, 1].

        m = clip(1 - (observed/budget - 0.6) / 0.4, 0, 1)

    1 while at most 60% of the budget is consumed, tapering linearly to 0 at 100%. The
    alternative -- full size until the limit and nothing after -- puts maximum exposure
    at the moment of maximum evidence that something is wrong, which is the worst
    available size schedule.

    A non-positive budget returns 0: a subject whose budget has been configured to zero
    is halted, and halted is the conservative reading of an unusable number.
    """
    if budget_ratio <= _ZERO:
        return _ZERO
    utilisation_ratio = max(_ZERO, observed_ratio) / budget_ratio
    taper = _ONE - (utilisation_ratio - _DERISK_ONSET_RATIO) / _DERISK_TAPER_WIDTH
    return min(_ONE, max(_ZERO, taper))


@dataclass(frozen=True, slots=True)
class LimitVerdict:
    """The outcome of one evaluation: every limit's reading, not only the failing one.

    `breach` is the first limit crossed in the order drawdown, daily, rolling -- but
    `observed_ratios` carries all three, because a post-mortem that can see only the
    limit that fired cannot tell whether the others were also about to.
    """

    state: DrawdownState
    observed_ratios: Mapping[LimitName, Decimal]
    derisk_multiplier: Decimal
    breach: BreachRecord | None

    @property
    def is_halted(self) -> bool:
        """True when trading for this subject must not continue.

        Latched: a state carrying a prior breach stays halted after the drawdown
        recovers. Recovery is not evidence that the breach did not happen.
        """
        return self.breach is not None


_LIMIT_ORDER: Final[tuple[LimitName, ...]] = ("drawdown", "daily_loss", "rolling_loss")


def evaluate(state: DrawdownState, budgets: DrawdownBudgets) -> LimitVerdict:
    """Read every limit against `state` and return the verdict, latching any breach.

    Called on every fill and every mark update -- never on a schedule. A limit evaluated
    hourly carries an hour of slack, and a single large fill can consume a whole day's
    budget inside it; that is why this is a pure function of state rather than a task.

    The returned state carries the breach, so persisting the verdict's state is what
    makes the halt survive a restart. Persisting the *input* state instead would restore
    a subject that had breached into an un-breached posture, which is the same class of
    bug as recomputing the high-water mark.
    """
    if budgets.scope != state.scope:
        raise DrawdownStateError(
            f"budgets are scoped {budgets.scope!r} but state is scoped {state.scope!r}; "
            f"a portfolio budget applied to a strategy is a limit off by its own size"
        )

    observed: Mapping[LimitName, Decimal] = MappingProxyType(
        {name: state.observed_ratio_for(name) for name in _LIMIT_ORDER}
    )
    breach = state.latched_breach
    if breach is None:
        for name in _LIMIT_ORDER:
            budget = budgets.budget_for(name)
            if observed[name] >= budget:
                breach = BreachRecord(
                    scope=state.scope,
                    subject_id=state.subject_id,
                    limit_name=name,
                    observed_ratio=observed[name],
                    budget_ratio=budget,
                    breached_at_utc=state.observed_at_utc,
                    response=_RESPONSE_BY_SCOPE[state.scope],
                )
                break

    # The binding limit for sizing is the one closest to its budget, which is not
    # necessarily the one that will breach first in absolute terms.
    multiplier = min(
        derisk_scalar(observed_ratio=observed[name], budget_ratio=budgets.budget_for(name))
        for name in _LIMIT_ORDER
    )
    return LimitVerdict(
        state=replace(state, latched_breach=breach),
        observed_ratios=observed,
        derisk_multiplier=_ZERO if breach is not None else multiplier,
        breach=breach,
    )
