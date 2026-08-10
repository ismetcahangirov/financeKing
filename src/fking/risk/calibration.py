"""Conviction calibration: what a strategy claims, mapped onto what it has earned.

    r_used = r_min + calibrated(conviction) * (r_max - r_min)      0.25% .. 1.00%

`RISK_PHILOSOPHY.md` section 2 states the map and the reason it exists. The reason is
worth restating, because taking the reported number at face value is the intuitive
implementation: if a strategy can emit `conviction = 1.0` and that number multiplies
notional, the risk engine has reinvented strategy-side sizing with extra steps and a
nicer name. The map is what closes that. A strategy cannot inflate its size by asserting
confidence; it has to earn the gradient, out of its own realised record.

`calibrated()` is a monotone non-decreasing fit: bucket the strategy's closed trades by
reported-conviction decile, measure the realised outcome per bucket, run an isotonic
regression over the bucket means, and normalise the result onto `[0, 1]`. Below
`min_trades_for_calibration` closed trades the map is the constant `0.5` -- every signal
sized identically regardless of what the strategy claims.

Five decisions here are load-bearing and none of them is recoverable by reading the code.

**The fit is point-in-time, and that is the whole difficulty.** Fitting on a strategy's
*full* trade record and then using the map to size a signal from the middle of that record
is look-ahead -- inside the risk engine rather than the feature store. It inflates a
backtest exactly the way a leaky feature does, and it evades every defence built in P1,
because nobody thinks of the risk engine as a data consumer (`docs/rules/no-lookahead.md`).
So `fit_calibration` takes an `as_of_utc` with no default and filters on it itself rather
than trusting the caller to have done so, the map carries the instant it became knowable
as `available_at_utc`, and `assess_conviction` refuses a map that became knowable after
the decision it is being applied to. Two guards, because the first stops a map seeing the
future and the second stops a legitimately-later map being applied to an earlier decision,
which is how the leak arrives once a caller starts caching maps.

**The bucket outcome that gets fitted is mean realised return, not hit rate.** Both are
measured and both are recorded, because `SURVIVAL_PROTOCOL.md` section 9 reads the pair
and because hit rate is the diagnostic that explains *why* a bucket's mean moved. But the
quantity `r_used` scales is risk per trade, and risking more per trade is justified by a
larger expected return per trade, not by a higher frequency of small ones. Ranking on hit
rate alone promotes a bucket of frequent tiny wins over one of rare large ones -- which is
the ordering that maximises drawdown per unit of return.

**The fitted curve is normalised against the strategy's own range, so a record with no
gradient produces exactly the constant the unfitted map produces.** A strategy that
reports the same conviction on everything gets one bucket; a strategy that spreads its
convictions but whose outcomes do not follow gets its buckets pooled by the isotonic step
into one level. Both collapse to `0.5` everywhere, which is the same number a strategy
with no record at all gets -- deliberately, because "no evidence yet" and "the evidence
says your number predicts nothing" are the same epistemic position, and paying them
differently would make the risk budget a function of trade count rather than of evidence.
The consequence to be aware of: this map carries only the *gradient*. It says nothing
about whether the strategy is any good in absolute terms, and it must not, because that
judgement already has two owners -- the Kelly term in `fking.risk.sizing`, which returns
zero for a negative realised mean, and the survival score.

**The lookup is a step function, not an interpolation.** Interpolating between decile
midpoints asserts a resolution a decile fit does not have, and every intermediate value it
produces is a division that has to be rounded. A step is monotone by construction and
exact.

**Every fraction the map carries is quantized to eighteen decimal places at fit time**,
because `NUMERIC(38, 18)` is the column that has to hold it. Quantizing at write time
instead leaves the in-memory map and the persisted map as two different maps, and a
restart is the only place that difference is ever observable.

Everything here is pure: no I/O, no clock read, no randomness. `as_of_utc` and
`decided_at_utc` are parameters, which is what makes a risk decision replayable and
therefore auditable (`CLAUDE.md` section 4).

`RISK_PHILOSOPHY.md` section 2, `docs/rules/no-lookahead.md`, `CONFIGURATION.md` section 8.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from types import MappingProxyType
from typing import Final

from fking.domain import Direction, Signal
from fking.risk.ceilings import (
    HARD_FLOORS,
    Ceiling,
    Floor,
    assert_above_floors,
    assert_within_ceilings,
)
from fking.risk.exposure import Rejection
from fking.risk.sizing import SIZING_CEILINGS

__all__ = [
    "CALIBRATION_HARD_CEILINGS",
    "CALIBRATION_HARD_FLOORS",
    "CONVICTION_FLOOR_LIMIT_NAME",
    "UNCALIBRATED_FRACTION",
    "CalibrationBucket",
    "CalibrationError",
    "CalibrationMap",
    "ClosedTrade",
    "ConvictionAssessment",
    "ConvictionParameters",
    "assess_conviction",
    "fit_calibration",
    "from_calibration_row",
    "risk_fraction_for",
    "to_calibration_row",
]

_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")

# The resolution `NUMERIC(38, 18)` holds, and therefore the resolution this map has. See
# the module docstring: quantizing here rather than at the point of writing is what keeps
# the running map and the restored map the same map.
_STORED_EXPONENT: Final = Decimal("1E-18")

# Deciles, as `RISK_PHILOSOPHY.md` section 2 specifies. Ten is also why
# `HARD_FLOORS["conviction_floor"]` is 0.10: a floor finer than one decile discriminates
# on a difference the map that reads it cannot resolve.
_DECILE_COUNT: Final = 10

# The name this refusal is recorded under, in `Rejection.binding_limit_name` and in the
# audit row. A single constant rather than a literal at each site: a rejection reason that
# is spelled two ways is a rejection reason that cannot be counted, and counting sub-floor
# convictions per strategy is the discipline signal `SURVIVAL_PROTOCOL.md` section 9 reads.
CONVICTION_FLOOR_LIMIT_NAME: Final[str] = "conviction_floor"


def _quantized(candidate: Decimal) -> Decimal:
    """Snap onto the eighteen-place lattice the persisted form uses.

    `ROUND_HALF_EVEN` because these are reported statistics rather than order quantities,
    and banker's rounding is unbiased over many roundings
    (`docs/rules/decimal-and-money.md`). Monotone, so quantizing an ordered series leaves
    it ordered -- which the isotonic guarantee depends on.
    """
    return candidate.quantize(_STORED_EXPONENT, rounding=ROUND_HALF_EVEN)


# Exactly one half, at the stored resolution. Returned by every map that has not earned a
# gradient: too few trades, one conviction bucket, or a record whose buckets all pool to
# the same level. Numerically equal to `Decimal("0.5")`, which is what
# `RISK_PHILOSOPHY.md` section 2 states; spelled at full scale so that the string form in
# an audit row matches every other fraction in the same row.
UNCALIBRATED_FRACTION: Final[Decimal] = _quantized(Decimal("0.5"))


CALIBRATION_HARD_CEILINGS: Final[Mapping[str, Ceiling]] = MappingProxyType(
    {
        # Both ends of the risk band are per-trade risk fractions, so both are bounded by
        # the same compiled-in ceiling the single-valued sizing parameter is -- read from
        # `fking.risk.sizing` rather than restated, because two compiled-in copies of a
        # safety constant are two numbers that can disagree, and the one that disagrees
        # silently is whichever the reader is not looking at.
        "risk_fraction_min_per_trade": SIZING_CEILINGS["risk_fraction_per_trade"],
        "risk_fraction_max_per_trade": SIZING_CEILINGS["risk_fraction_per_trade"],
    }
)

CALIBRATION_HARD_FLOORS: Final[Mapping[str, Floor]] = MappingProxyType(
    {
        # The same floor `fking.risk.ceilings` already declares, read from there for the
        # reason above.
        "conviction_floor": HARD_FLOORS["conviction_floor"],
        # `RISK_PHILOSOPHY.md` section 2: the map returns the constant 0.5 below 100
        # closed trades. The argument is the one section 3.3 makes for Kelly, applied to a
        # decile mean instead of to mu-hat -- at 100 trades a decile bucket holds ten
        # observations, and the standard error on a ten-observation mean is already large
        # enough to invert two adjacent buckets. Configuration may demand a longer record;
        # it may not accept a shorter one, so the floor is the documented value itself.
        "min_trades_for_calibration": Floor(Decimal("100")),
    }
)


class CalibrationError(ValueError):
    """A calibration input, or a persisted map, is absent, malformed, or out of time.

    Distinct from `DomainError` because every one of these is a statement about the risk
    engine's own state rather than about a domain object: a map from another strategy, a
    map fitted after the decision using it, a trade carrying more precision than the
    record can hold. All of them produce a plausible number if allowed through, and a
    plausible wrong number in the risk path is what this module exists to prevent.
    """


def _require_utc(candidate: datetime, field_name: str) -> datetime:
    """A timezone-aware datetime whose offset is exactly UTC.

    Rejects rather than converts. A naive instant compared against a tz-aware trade
    timestamp raises `TypeError` from somewhere far away, and one converted on the
    assumption it was local time is silently wrong by the size of an offset -- which for
    a 24/7 market has no session boundary to make it visible
    (`docs/rules/time-and-timezones.md`).
    """
    if candidate.tzinfo is None or candidate.utcoffset() != UTC.utcoffset(None):
        raise CalibrationError(
            f"{field_name} must be timezone-aware UTC, got {candidate!r}; a naive instant "
            f"in a 24/7 market is wrong by an offset nothing makes visible"
        )
    return candidate


def _require_storable_fraction(candidate: Decimal, field_name: str) -> Decimal:
    """A finite `Decimal` in `[0, 1]` that survives the column holding it unchanged.

    Refused rather than quantized. Quantizing a reported conviction moves it between
    decile buckets, which changes the fitted map -- silently, and only observably after a
    restart rebuilds the map from rows that no longer say what the fit saw.
    """
    if not candidate.is_finite() or not _ZERO <= candidate <= _ONE:
        raise CalibrationError(f"{field_name} must be a finite fraction in [0, 1], got {candidate}")
    if candidate != _quantized(candidate):
        raise CalibrationError(
            f"{field_name}={candidate} carries more than 18 decimal places, which is the "
            f"resolution the persisted record holds; quantizing it here would move the "
            f"trade between decile buckets without saying so"
        )
    return candidate


def _require_storable_return(candidate: Decimal, field_name: str) -> Decimal:
    """A finite realised return fraction at or above -100%, at the stored resolution."""
    if not candidate.is_finite() or candidate < -_ONE:
        raise CalibrationError(
            f"{field_name} must be a finite fraction at or above -1, got {candidate}; a "
            f"trade cannot lose more than the whole position"
        )
    if candidate != _quantized(candidate):
        raise CalibrationError(
            f"{field_name}={candidate} carries more than 18 decimal places, which is the "
            f"resolution the persisted record holds"
        )
    return candidate


def _require_text(candidate: str, field_name: str) -> str:
    if not candidate.strip():
        raise CalibrationError(f"{field_name} must be non-empty text, got {candidate!r}")
    return candidate


@dataclass(frozen=True, slots=True)
class ConvictionParameters:
    """The configurable half of calibration, refused at construction if a bound is crossed.

    Defaults are the shipped baseline from `RISK_PHILOSOPHY.md` section 2 and
    `CONFIGURATION.md` section 8. A process constructed with no arguments runs at those,
    because a missing configuration file must never produce a less conservative system
    than a present one.

    Note which direction each bound runs. `conviction_floor` and
    `min_trades_for_calibration` are bounded *below*: raising either discards more signals
    and demands more evidence, which is the conservative direction. The two risk fractions
    are bounded *above*. `fking.risk.ceilings` explains why those are separate mappings
    with separate types rather than one loop with a comparison operator in it.
    """

    conviction_floor: Decimal = Decimal("0.15")
    min_trades_for_calibration: int = 100
    risk_fraction_min_per_trade: Decimal = Decimal("0.0025")
    risk_fraction_max_per_trade: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        submitted = self.bounded_values()
        assert_within_ceilings(submitted, CALIBRATION_HARD_CEILINGS, scope="calibration")
        assert_above_floors(submitted, CALIBRATION_HARD_FLOORS, scope="calibration")
        if self.risk_fraction_min_per_trade > self.risk_fraction_max_per_trade:
            # `RiskLimits` deliberately carries no cross-field rules, because every
            # configuration it accepts is more conservative than the default and refusing
            # one would mean refusing safety. This band is the exception and it is not a
            # counterexample: an inverted band does not make the system smaller, it makes
            # `r_used` *decrease* as calibrated conviction rises, which inverts the one
            # channel a strategy is allowed to influence size through.
            raise ValueError(
                f"risk_fraction_min_per_trade={self.risk_fraction_min_per_trade} is above "
                f"risk_fraction_max_per_trade={self.risk_fraction_max_per_trade}; an "
                f"inverted band sizes a strategy's most confident signals smallest"
            )

    def bounded_values(self) -> Mapping[str, Decimal]:
        """Every bounded parameter as a `Decimal`, keyed by field name.

        The integer-valued parameter is widened here rather than compared as an int, so
        one comparison implementation serves both and there is no second code path whose
        direction could be written backwards.
        """
        names = (*CALIBRATION_HARD_CEILINGS, *CALIBRATION_HARD_FLOORS)
        return MappingProxyType({name: Decimal(str(getattr(self, name))) for name in names})


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """One closed trade, as the calibration reads it: what was claimed, what happened, when.

    `closed_at_utc` is the instant the outcome became knowable, and it is the only field
    that governs whether this trade may influence a decision. Not the entry instant: a
    trade opened before `t` and closed after it has an outcome nobody knew at `t`, and
    admitting it on the strength of its entry time is the `event_time` filter that
    `docs/rules/no-lookahead.md` names as the most common form of this bug.
    """

    strategy_id: str
    reported_conviction: Decimal
    realised_return_fraction: Decimal
    closed_at_utc: datetime

    def __post_init__(self) -> None:
        _require_text(self.strategy_id, "strategy_id")
        _require_storable_fraction(self.reported_conviction, "reported_conviction")
        _require_storable_return(self.realised_return_fraction, "realised_return_fraction")
        _require_utc(self.closed_at_utc, "closed_at_utc")

    @property
    def is_win(self) -> bool:
        """Whether the trade closed above break-even. Exactly zero is not a win.

        Zero is a round trip whose gross move exactly paid its costs; counting it as a win
        would make a strategy that scratches every trade look like it hits every trade.
        """
        return self.realised_return_fraction > _ZERO


@dataclass(frozen=True, slots=True)
class CalibrationBucket:
    """One conviction decile, everything measured in it, and what it calibrates to.

    Carried in full rather than collapsed to the calibrated fraction, because
    `ARCHITECTURE.md` section 11 requires a decision to be reconstructable from the audit
    log alone. Knowing that a signal calibrated to 0.7 answers nothing without knowing
    that the bucket held 14 trades at a 43% hit rate and a mean return of 1.1%, and that
    the isotonic step pooled it with the bucket above.
    """

    conviction_upper_bound: Decimal
    trade_count: int
    hit_rate_fraction: Decimal
    mean_return_fraction: Decimal
    fitted_return_fraction: Decimal
    calibrated_fraction: Decimal

    def __post_init__(self) -> None:
        _require_storable_fraction(self.conviction_upper_bound, "conviction_upper_bound")
        _require_storable_fraction(self.hit_rate_fraction, "hit_rate_fraction")
        _require_storable_fraction(self.calibrated_fraction, "calibrated_fraction")
        _require_storable_return(self.mean_return_fraction, "mean_return_fraction")
        _require_storable_return(self.fitted_return_fraction, "fitted_return_fraction")
        if self.trade_count <= 0:
            raise CalibrationError(
                f"a bucket must hold at least one trade, got trade_count={self.trade_count}; "
                f"an empty bucket contributes a mean computed from nothing"
            )


@dataclass(frozen=True, slots=True)
class CalibrationMap:
    """A strategy's fitted conviction map, and the instant it became knowable.

    `available_at_utc` rather than `fitted_at_utc`, though they are the same instant by
    construction: `docs/rules/no-lookahead.md` clause 1 makes `available_at` the field
    that governs visibility, and naming it for what it is used for rather than for how it
    was produced is what stops the next reader filtering on the wrong one.

    An empty `buckets` is not a degenerate map, it is the documented state below
    `min_trades_for_calibration` -- `calibrated()` returns the constant `0.5` and
    `is_fitted` says so. Empty rather than ten identical buckets, because ten identical
    buckets claim a fit that was never performed.
    """

    strategy_id: str
    available_at_utc: datetime
    observation_count: int
    buckets: tuple[CalibrationBucket, ...]

    def __post_init__(self) -> None:
        _require_text(self.strategy_id, "strategy_id")
        _require_utc(self.available_at_utc, "available_at_utc")
        if self.observation_count < 0:
            raise CalibrationError(
                f"observation_count must be at or above zero, got {self.observation_count}"
            )
        bounds = tuple(bucket.conviction_upper_bound for bucket in self.buckets)
        if list(bounds) != sorted(set(bounds)):
            raise CalibrationError(
                f"bucket upper bounds must be strictly increasing, got {bounds}; a repeated "
                f"bound makes the map ambiguous at exactly that conviction"
            )
        calibrated = tuple(bucket.calibrated_fraction for bucket in self.buckets)
        if list(calibrated) != sorted(calibrated):
            # The guarantee, enforced at construction rather than only at the point of
            # fitting. `from_calibration_row` runs through here too, so a row edited by
            # hand during an incident cannot produce a map that sizes a less confident
            # signal larger.
            raise CalibrationError(
                f"calibrated fractions must be non-decreasing, got {calibrated}; an "
                f"inversion sizes a strategy's less confident signals larger"
            )

    @property
    def is_fitted(self) -> bool:
        """Whether a gradient was fitted at all, as opposed to the constant being returned."""
        return bool(self.buckets)

    def calibrated(self, conviction: Decimal) -> Decimal:
        """The calibrated fraction in `[0, 1]` for a reported conviction.

        A step function over the fitted decile bounds, so the result is exact and monotone
        non-decreasing over the whole of `[0, 1]` by construction rather than by
        arithmetic. A conviction above the top bound reads the top bucket: a strategy that
        has never reported above 0.8 and now reports 0.95 has produced no evidence about
        0.95, and the honest reading of no evidence is the highest level it has earned --
        not an extrapolation past the end of its own record.
        """
        _require_storable_fraction(_quantized(conviction), "conviction")
        if not self.buckets:
            return UNCALIBRATED_FRACTION
        for bucket in self.buckets:
            if conviction <= bucket.conviction_upper_bound:
                return bucket.calibrated_fraction
        return self.buckets[-1].calibrated_fraction

    def audit_payload(self) -> Mapping[str, object]:
        """The map as it is recorded beside a decision, every number a string.

        Decimals are rendered as strings rather than left as `Decimal`: the payload lands
        in `jsonb`, and a JSON encoder that has not been told otherwise turns a `Decimal`
        into a float on the way into an append-only table that can never be corrected.
        """
        return MappingProxyType(
            {
                "strategy_id": self.strategy_id,
                "available_at_utc": self.available_at_utc.isoformat(),
                "observation_count": self.observation_count,
                "is_fitted": self.is_fitted,
                "buckets": tuple(
                    MappingProxyType(
                        {
                            "conviction_upper_bound": str(bucket.conviction_upper_bound),
                            "trade_count": bucket.trade_count,
                            "hit_rate_fraction": str(bucket.hit_rate_fraction),
                            "mean_return_fraction": str(bucket.mean_return_fraction),
                            "fitted_return_fraction": str(bucket.fitted_return_fraction),
                            "calibrated_fraction": str(bucket.calibrated_fraction),
                        }
                    )
                    for bucket in self.buckets
                ),
            }
        )


def _decile_upper_bounds(ordered_convictions: Sequence[Decimal]) -> tuple[Decimal, ...]:
    """The upper bound of each conviction decile, with tied deciles merged.

    Merging on ties is the part that is easy to get wrong. Splitting an equal-count decile
    by rank puts identical reported convictions into different buckets with different
    realised means, and the map is then ill-defined at exactly that conviction -- two
    answers for one input, decided by the sort's tie-break. Merging instead means a
    strategy reporting one conviction gets one bucket, which is also the flattening
    `RISK_PHILOSOPHY.md` section 2 requires.
    """
    observation_count = len(ordered_convictions)
    bounds: list[Decimal] = []
    for decile_index in range(1, _DECILE_COUNT + 1):
        last_position = (decile_index * observation_count) // _DECILE_COUNT - 1
        bound = ordered_convictions[last_position]
        if not bounds or bound > bounds[-1]:
            bounds.append(bound)
    return tuple(bounds)


def _bucket_index_for(conviction: Decimal, bounds: Sequence[Decimal]) -> int:
    """The first bucket whose upper bound is at or above `conviction`.

    `bisect_left` rather than a scan with a trailing fallback: the bounds are the sample's
    own maximum at the top, so the fallback branch is unreachable from `fit_calibration`
    and an unreachable branch in risk code is a branch nobody can test and everybody
    eventually trusts. The `min` handles the same case as an expression instead.
    """
    return min(bisect_left(bounds, conviction), len(bounds) - 1)


def _pool_adjacent_violators(
    means: Sequence[Decimal], weights: Sequence[int]
) -> tuple[Decimal, ...]:
    """Isotonic regression by PAVA: the closest non-decreasing series, weighted by count.

    Weighted by trade count rather than uniformly, because merged deciles do not hold
    equal numbers of trades and an unweighted pool lets a two-trade bucket drag a
    forty-trade one.

    The violation test is a cross-multiplication rather than a comparison of two
    quotients: both sides are exact under `Decimal` integer weights, so the decision to
    pool never depends on a rounding that happened inside the comparison.
    """
    # (weighted sum, total weight, how many original buckets this block covers)
    blocks: list[tuple[Decimal, int, int]] = []
    for mean_return_fraction, weight in zip(means, weights, strict=True):
        blocks.append((mean_return_fraction * weight, weight, 1))
        while len(blocks) > 1:
            previous_sum, previous_weight, previous_span = blocks[-2]
            current_sum, current_weight, current_span = blocks[-1]
            if previous_sum * current_weight <= current_sum * previous_weight:
                break
            blocks.pop()
            blocks.pop()
            blocks.append(
                (
                    previous_sum + current_sum,
                    previous_weight + current_weight,
                    previous_span + current_span,
                )
            )

    fitted: list[Decimal] = []
    for block_sum, block_weight, block_span in blocks:
        level = _quantized(block_sum / block_weight)
        fitted.extend([level] * block_span)
    return tuple(fitted)


def _normalised(fitted: Sequence[Decimal]) -> tuple[Decimal, ...]:
    """Map a non-decreasing series onto `[0, 1]` against its own range.

    A flat series -- one bucket, or every bucket pooled to one level -- has no range to
    normalise against and becomes the constant `0.5`, which is the same value an unfitted
    map returns. See the module docstring for why those two cases are deliberately paid
    the same.

    Normalising against the strategy's own range rather than against an absolute return
    scale is what makes this a calibration of the *gradient*. An absolute scale would
    require a compiled-in opinion about what return per trade deserves `r_max`, and that
    number would be a magic constant in risk code that nobody could source.
    """
    lowest, highest = fitted[0], fitted[-1]
    span = highest - lowest
    if span <= _ZERO:
        return tuple(UNCALIBRATED_FRACTION for _ in fitted)
    # Quantization is monotone, so the normalised series is still non-decreasing; and the
    # endpoints land exactly on 0 and 1 because the numerator is exactly 0 and exactly
    # `span` there.
    return tuple(_quantized((level - lowest) / span) for level in fitted)


def fit_calibration(
    trades: Sequence[ClosedTrade],
    *,
    strategy_id: str,
    as_of_utc: datetime,
    parameters: ConvictionParameters,
) -> CalibrationMap:
    """Fit `strategy_id`'s conviction map from the trades that had closed by `as_of_utc`.

    `as_of_utc` is keyword-only and has no default. A default is a value someone forgets
    to override, and the value they would forget is "now", which is the leak
    (`docs/rules/no-lookahead.md`). The filter is applied here rather than left to the
    caller for the same reason: a caller who has already filtered pays nothing, and a
    caller who has not is the entire failure mode.

    A trade belonging to another strategy raises rather than being skipped. A mixed
    history fits a map that describes nobody, and it fits it without complaint.
    """
    _require_text(strategy_id, "strategy_id")
    _require_utc(as_of_utc, "as_of_utc")

    eligible: list[ClosedTrade] = []
    for trade in trades:
        if trade.strategy_id != strategy_id:
            raise CalibrationError(
                f"history for {strategy_id!r} contains a trade from {trade.strategy_id!r}; "
                f"a mixed record fits a map that describes neither strategy"
            )
        if trade.closed_at_utc <= as_of_utc:
            eligible.append(trade)

    if len(eligible) < parameters.min_trades_for_calibration:
        return CalibrationMap(
            strategy_id=strategy_id,
            available_at_utc=as_of_utc,
            observation_count=len(eligible),
            buckets=(),
        )

    bounds = _decile_upper_bounds(sorted(trade.reported_conviction for trade in eligible))
    grouped: list[list[ClosedTrade]] = [[] for _ in bounds]
    for trade in eligible:
        grouped[_bucket_index_for(trade.reported_conviction, bounds)].append(trade)

    trade_counts = [len(members) for members in grouped]
    hit_rates = [
        _quantized(Decimal(sum(1 for trade in members if trade.is_win)) / Decimal(len(members)))
        for members in grouped
    ]
    mean_returns = [
        _quantized(
            sum((trade.realised_return_fraction for trade in members), _ZERO)
            / Decimal(len(members))
        )
        for members in grouped
    ]
    fitted = _pool_adjacent_violators(mean_returns, trade_counts)
    calibrated = _normalised(fitted)

    return CalibrationMap(
        strategy_id=strategy_id,
        available_at_utc=as_of_utc,
        observation_count=len(eligible),
        buckets=tuple(
            CalibrationBucket(
                conviction_upper_bound=bound,
                trade_count=trade_count,
                hit_rate_fraction=hit_rate_fraction,
                mean_return_fraction=mean_return_fraction,
                fitted_return_fraction=fitted_return_fraction,
                calibrated_fraction=calibrated_fraction,
            )
            for (
                bound,
                trade_count,
                hit_rate_fraction,
                mean_return_fraction,
                fitted_return_fraction,
                calibrated_fraction,
            ) in zip(bounds, trade_counts, hit_rates, mean_returns, fitted, calibrated, strict=True)
        ),
    )


def risk_fraction_for(calibrated_fraction: Decimal, *, parameters: ConvictionParameters) -> Decimal:
    """`r_used = r_min + calibrated * (r_max - r_min)`, at the stored resolution.

    Separated from `assess_conviction` because #55 needs the arithmetic on its own for the
    audit trail, and because it is the one line in this module a reviewer can check
    against `RISK_PHILOSOPHY.md` section 2 without reading anything else.

    The result is inside `[r_min, r_max]` for every input in `[0, 1]`, so it is always a
    fraction `SizingParameters` will accept. Quantization cannot push it out: at
    `calibrated = 1` the exact value is `r_max`, whose own scale is coarser than eighteen
    places, so it quantizes to itself.
    """
    _require_storable_fraction(_quantized(calibrated_fraction), "calibrated_fraction")
    band = parameters.risk_fraction_max_per_trade - parameters.risk_fraction_min_per_trade
    return _quantized(parameters.risk_fraction_min_per_trade + calibrated_fraction * band)


@dataclass(frozen=True, slots=True)
class ConvictionAssessment:
    """What the conviction channel yielded for one signal: a risk fraction, or a refusal.

    `risk_fraction_used` is `Decimal | None`, and the `None` is the point. A rejected
    signal has no risk fraction -- not a zero one. A zero would type-check its way into
    `SizingParameters` and be refused there, one layer away from the decision that
    actually rejected the signal and with an error message about a positive-value
    constraint rather than about a conviction floor. `None` makes `mypy --strict` refuse
    the call at the point where the branch was skipped.
    """

    strategy_id: str
    reported_conviction: Decimal
    calibrated_conviction: Decimal
    risk_fraction_used: Decimal | None
    conviction_floor: Decimal
    calibration_observation_count: int
    calibration_available_at_utc: datetime
    is_calibrated: bool
    decided_at_utc: datetime
    rejection: Rejection | None

    def __post_init__(self) -> None:
        if (self.rejection is None) != (self.risk_fraction_used is not None):
            raise CalibrationError(
                "an assessment carries either a rejection or a risk fraction, never both "
                "and never neither; the pair disagreeing is a decision nobody can act on"
            )

    @property
    def is_approved(self) -> bool:
        """Whether the conviction channel permits any position at all."""
        return self.rejection is None

    def audit_payload(self) -> Mapping[str, object]:
        """The row body, in the shape `fking.risk.sizing` and `fking.risk.exposure` write.

        Both readings of the decision are present: the calibrated conviction is recorded
        even when the signal was rejected, so a reader can tell a signal that would have
        been sized large from one that would not -- which is what distinguishes a strategy
        emitting timid signals from one emitting worthless ones.
        """
        return MappingProxyType(
            {
                "verdict": "approved" if self.is_approved else "rejected",
                "strategy_id": self.strategy_id,
                "decided_at_utc": self.decided_at_utc.isoformat(),
                "reported_conviction": str(self.reported_conviction),
                "calibrated_conviction": str(self.calibrated_conviction),
                "risk_fraction_used": None
                if self.risk_fraction_used is None
                else str(self.risk_fraction_used),
                "conviction_floor": str(self.conviction_floor),
                "is_calibrated": self.is_calibrated,
                "calibration_observation_count": self.calibration_observation_count,
                "calibration_available_at_utc": self.calibration_available_at_utc.isoformat(),
                "binding_limit_name": None
                if self.rejection is None
                else self.rejection.binding_limit_name,
                "rejection_reason": None if self.rejection is None else self.rejection.reason,
            }
        )


def assess_conviction(
    *,
    signal: Signal,
    calibration: CalibrationMap,
    parameters: ConvictionParameters,
    decided_at_utc: datetime,
) -> ConvictionAssessment:
    """Turn a reported conviction into the risk fraction that will size it, or refuse it.

    This is the entry point `RiskEngine.decide()` calls, once per directional signal,
    before sizing. It never raises for a sub-floor conviction: a refusal is an ordinary,
    expected, frequent outcome, and raising would put it on the same code path as a bug --
    the caller would then be obliged to write a handler indistinguishable from swallowing
    a real error (`docs/rules/error-handling.md`).

    It does raise for the three inputs that would otherwise produce a plausible wrong
    number: a map belonging to another strategy, a map that became knowable after this
    decision, and a flat signal. A flat signal is refused rather than sized at zero
    because flat means "close what is open", and routing a reduce-only instruction through
    the conviction floor would discard it -- trapping the portfolio in the position the
    signal was trying to leave, which is the limit working exactly backwards.
    """
    _require_utc(decided_at_utc, "decided_at_utc")

    if signal.direction is Direction.FLAT:
        raise CalibrationError(
            f"{signal.strategy_id} emitted a flat signal on {signal.instrument.symbol}; a "
            f"flat signal asks to close a position rather than for a risk budget, and "
            f"sizing it is a category error rather than a request for zero"
        )
    if calibration.strategy_id != signal.strategy_id:
        raise CalibrationError(
            f"the calibration map belongs to {calibration.strategy_id!r} and the signal to "
            f"{signal.strategy_id!r}; a map is a claim about one strategy's record and "
            f"means nothing applied to another"
        )
    if calibration.available_at_utc > decided_at_utc:
        raise CalibrationError(
            f"the calibration map was fitted at "
            f"{calibration.available_at_utc.isoformat()}, after the decision at "
            f"{decided_at_utc.isoformat()}; sizing a past decision with a later map is "
            f"look-ahead inside the risk engine"
        )

    calibrated_conviction = calibration.calibrated(signal.conviction)
    rejection: Rejection | None = None
    risk_fraction_used: Decimal | None = None

    if signal.conviction < parameters.conviction_floor:
        # Discarded, not sized down. A near-zero conviction is the absence of an opinion,
        # and the correct response to the absence of an opinion is no position -- not a
        # tiny one whose expected edge cannot cover its own round trip
        # (`RISK_PHILOSOPHY.md` section 2).
        rejection = Rejection(
            reason=(
                f"reported conviction {signal.conviction} is below the conviction floor "
                f"{parameters.conviction_floor}; the absence of an opinion is not a small "
                f"position"
            ),
            binding_limit_name=CONVICTION_FLOOR_LIMIT_NAME,
            rejected_at_utc=decided_at_utc,
        )
    else:
        risk_fraction_used = risk_fraction_for(calibrated_conviction, parameters=parameters)

    return ConvictionAssessment(
        strategy_id=signal.strategy_id,
        reported_conviction=signal.conviction,
        calibrated_conviction=calibrated_conviction,
        risk_fraction_used=risk_fraction_used,
        conviction_floor=parameters.conviction_floor,
        calibration_observation_count=calibration.observation_count,
        calibration_available_at_utc=calibration.available_at_utc,
        is_calibrated=calibration.is_fitted,
        decided_at_utc=decided_at_utc,
        rejection=rejection,
    )


def to_calibration_row(calibration: CalibrationMap) -> Mapping[str, object]:
    """The persisted form of `calibration`, as primitives a repository can bind directly.

    A codec rather than an ORM mapping, and it lives here rather than in
    `fking.platform.persistence` for a boundary reason: `risk` may not perform I/O
    (`CLAUDE.md` section 4) and `platform` may not import another `fking` module
    (`docs/rules/module-boundaries.md`), so neither package can hold both the type and the
    SQL. The type and its exact serialisation live together; the statement that binds the
    result lives with whoever owns the recovery sequence.

    Named `to_calibration_row` rather than `to_row` because `fking.risk` already exports a
    `to_row` for drawdown state, and two codecs sharing one name in one namespace is one
    import statement away from persisting the wrong object.

    Every fraction is encoded as a string, not as a number. A `Decimal` that passes
    through a JSON encoder becomes a float and comes back rounded, and the rounding is
    already present before anything here could notice it.
    """
    return MappingProxyType(
        {
            "strategy_id": calibration.strategy_id,
            "available_at_utc": calibration.available_at_utc.isoformat(),
            "observation_count": calibration.observation_count,
            "buckets": tuple(
                MappingProxyType(
                    {
                        "conviction_upper_bound": str(bucket.conviction_upper_bound),
                        "trade_count": bucket.trade_count,
                        "hit_rate_fraction": str(bucket.hit_rate_fraction),
                        "mean_return_fraction": str(bucket.mean_return_fraction),
                        "fitted_return_fraction": str(bucket.fitted_return_fraction),
                        "calibrated_fraction": str(bucket.calibrated_fraction),
                    }
                )
                for bucket in calibration.buckets
            ),
        }
    )


def _decode_decimal(row: Mapping[str, object], field_name: str) -> Decimal:
    raw = row.get(field_name)
    if not isinstance(raw, str):
        raise CalibrationError(
            f"persisted {field_name} must be a decimal string, got {raw!r}; a number here "
            f"has already been through a float"
        )
    return Decimal(raw)


def _decode_trade_count(row: Mapping[str, object], field_name: str) -> int:
    raw = row.get(field_name)
    # `bool` is an `int` in Python, and `True` would decode as a bucket holding one trade.
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise CalibrationError(f"persisted {field_name} must be an integer, got {raw!r}")
    return raw


def from_calibration_row(row: Mapping[str, object]) -> CalibrationMap:
    """Rebuild a map from its persisted form, refusing anything absent or malformed.

    Deliberately intolerant. A row missing a bucket's `calibrated_fraction` is not a row to
    fill in with `0.5` -- that substitution silently flattens one strategy's map, and the
    only symptom is that its signals are all sized identically, which is exactly what a
    correctly flattened map looks like.

    Monotonicity is re-checked on the way in by `CalibrationMap.__post_init__`, so a row
    edited by hand during an incident cannot restore a map that sizes a less confident
    signal larger.
    """
    strategy_id = row.get("strategy_id")
    if not isinstance(strategy_id, str):
        raise CalibrationError(f"persisted strategy_id must be text, got {strategy_id!r}")

    raw_available_at = row.get("available_at_utc")
    if not isinstance(raw_available_at, str):
        raise CalibrationError(
            f"persisted available_at_utc must be an ISO-8601 string, got {raw_available_at!r}"
        )

    raw_buckets = row.get("buckets")
    if not isinstance(raw_buckets, Sequence) or isinstance(raw_buckets, str):
        raise CalibrationError(f"persisted buckets must be a sequence, got {raw_buckets!r}")

    buckets: list[CalibrationBucket] = []
    for entry in raw_buckets:
        if not isinstance(entry, Mapping):
            raise CalibrationError(f"persisted bucket must be a mapping, got {entry!r}")
        buckets.append(
            CalibrationBucket(
                conviction_upper_bound=_decode_decimal(entry, "conviction_upper_bound"),
                trade_count=_decode_trade_count(entry, "trade_count"),
                hit_rate_fraction=_decode_decimal(entry, "hit_rate_fraction"),
                mean_return_fraction=_decode_decimal(entry, "mean_return_fraction"),
                fitted_return_fraction=_decode_decimal(entry, "fitted_return_fraction"),
                calibrated_fraction=_decode_decimal(entry, "calibrated_fraction"),
            )
        )

    return CalibrationMap(
        strategy_id=strategy_id,
        available_at_utc=_require_utc(datetime.fromisoformat(raw_available_at), "available_at_utc"),
        observation_count=_decode_trade_count(row, "observation_count"),
        buckets=tuple(buckets),
    )
