"""Position sizing: three methods computed independently, combined by `min()`.

    q = min(q_fixed_fractional, q_vol_target, q_kelly, q_exposure, q_venue_filters)

`RISK_PHILOSOPHY.md` section 3 states the combination rule and the reason it is a
minimum rather than an average. The reason is worth restating because averaging is the
intuitive choice: an average lets an overconfident method be rescued by a conservative
one, so the overconfident method's errors survive into production at reduced amplitude
-- quieter, still there, and much harder to attribute afterwards. A minimum gives every
term veto power, so a bug in any single term can only make the system *smaller*.

The corollary costs people afternoons and so it is recorded in the output rather than
left to be rediscovered: **only one term binds at a time**, and improving a term that is
not binding has no effect whatsoever. `PositionSizing.binding_term` names it.

Everything here is pure. No I/O, no clock, no randomness -- nothing in this module needs
the current instant, so nothing takes a `Clock`; a parameter that is never read is a
parameter someone will eventually populate from `datetime.now()`. Every price, quantity
and notional is a `Decimal` constructed from `str`.

`RISK_PHILOSOPHY.md` sections 3.1-3.3 and 4, `CONFIGURATION.md` section 8.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from fking.domain import Direction, DomainError, Instrument
from fking.risk.ceilings import (
    HARD_FLOORS,
    Ceiling,
    Floor,
    assert_above_floors,
    assert_within_ceilings,
)

__all__ = [
    "SIZING_CEILINGS",
    "SIZING_FLOORS",
    "PositionSizing",
    "SizingInputs",
    "SizingParameters",
    "VolatilityEstimate",
    "size_position",
    "volatility_used",
]

# RiskMetrics daily decay. lambda = 0.94 gives an effective half-life of about 11
# observations and an effective sample of about 33, which is the parameter J.P. Morgan
# published for daily data and the one every comparison in RISK_PHILOSOPHY.md section
# 3.2 is stated against.
_EWMA_DECAY: Final = Decimal("0.94")

# The slow leg of `max(sigma_ewma, sigma_60d, sigma_floor)`. Sixty observations is the
# horizon over which a crypto major's quiet period stops dominating the estimate.
_SLOW_WINDOW_OBSERVATIONS: Final = 60

# Crypto trades continuously: no session boundary, no weekend gap. Annualising a daily
# volatility with the 252 trading days used for equities understates it by 20%, and
# understating volatility is the expensive direction (RISK_PHILOSOPHY.md section 3.2).
_PERIODS_PER_YEAR: Final = Decimal("365")

# Dispersion needs two observations to exist at all; one observation has a mean equal to
# itself and therefore a deviation of exactly zero, which is a statement about arithmetic
# rather than about the market.
_MIN_OBSERVATIONS_FOR_DISPERSION: Final = 2

_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")

# Term names, recorded on the decision so that a tuning attempt starts from which term
# actually bound rather than from which term the tuner finds most interesting.
_FIXED_FRACTIONAL: Final = "fixed_fractional"
_VOLATILITY_TARGET: Final = "volatility_target"
_KELLY: Final = "kelly"
_EXPOSURE: Final = "exposure"
_VENUE_FILTERS: Final = "venue_filters"


# Bounded in the compiled-in direction that is dangerous for each parameter, using the
# `Ceiling`/`Floor` types from `fking.risk.ceilings` rather than a second mechanism.
# These are sizing parameters rather than portfolio limits, so they are a separate
# mapping: `assert_within_ceilings` refuses a bound whose field the caller did not
# submit, and `RiskLimits` does not carry these fields.
SIZING_CEILINGS: Final[Mapping[str, Ceiling]] = MappingProxyType(
    {
        # RISK_PHILOSOPHY.md section 3.1 states the risk budget as a band: r_min =
        # 0.25%, r_max = 1.00% of equity per trade. The ceiling is r_max itself rather
        # than a rounder number above it, on the same reasoning as the
        # `min_trades_for_kelly` floor in `fking.risk.ceilings` -- the document states a
        # value, not a range to be extended, and the safe direction is downward.
        "risk_fraction_per_trade": Ceiling(Decimal("0.01")),
        # RISK_PHILOSOPHY.md section 3.2: 15% annualised per position, 12% at portfolio
        # level. Above 15% this is not a configuration of the documented policy, it is a
        # different policy; configuration may lower it as far as it likes.
        "target_volatility_annualised": Ceiling(Decimal("0.15")),
        # RISK_PHILOSOPHY.md section 3.3, the drawdown table: under fractional Kelly
        # `c`, P(ever down to fraction x) is x^(2/c - 1). At c = 0.50 that is a 12.5%
        # chance of ever halving the account with a *perfectly known* edge; at c = 1.00
        # it is a coin flip. 0.25 is the shipped default, 0.50 the compiled ceiling.
        "max_kelly_fraction": Ceiling(Decimal("0.50")),
        # A wider ATR multiple is a wider assumed stop, which is a *smaller* position,
        # so the dangerous direction here is downward and the bound that matters is the
        # floor below. This ceiling exists only to catch a transcription error -- a `25`
        # where `2.5` was meant produces a position 10x smaller than intended, which is
        # safe and silently wrong, and silently wrong is still worth refusing.
        "atr_invalidation_multiple": Ceiling(Decimal("10")),
        # A halving that does not halve is a fallback that charges nothing for refusing
        # to state a stop. The ceiling is the documented value itself; configuration may
        # penalise the estimate harder, never less. Note the direction: 1.0 means "no
        # penalty", so *larger* is the risky end and a floor here would forbid exactly
        # the conservative settings it looks like it is protecting
        # (`fking.risk.ceilings`, on why the two directions are separate mappings).
        "unstated_invalidation_risk_multiplier": Ceiling(Decimal("0.5")),
    }
)

SIZING_FLOORS: Final[Mapping[str, Floor]] = MappingProxyType(
    {
        # The same floor `fking.risk.ceilings` already declares, read from there rather
        # than restated: two compiled-in copies of a safety constant are two numbers
        # that can disagree, and the one that disagrees silently is whichever the
        # reader is not looking at.
        "min_trades_for_kelly": HARD_FLOORS["min_trades_for_kelly"],
        # RISK_PHILOSOPHY.md section 3.1: k = 2.5 on ATR_14 when no invalidation level
        # was stated. A narrower multiple is a tighter assumed stop and therefore a
        # larger position sized off a stop nobody ever committed to, so the documented
        # value is the floor.
        "atr_invalidation_multiple": Floor(Decimal("2.5")),
    }
)


def _require_non_negative(candidate: Decimal, field_name: str) -> None:
    if candidate < _ZERO:
        raise DomainError(f"{field_name} must be at or above zero; got {candidate}")


def _require_positive(candidate: Decimal, field_name: str) -> None:
    if candidate <= _ZERO:
        raise DomainError(f"{field_name} must be above zero; got {candidate}")


@dataclass(frozen=True, slots=True)
class SizingParameters:
    """The configurable half of sizing, refused at construction if a bound is crossed.

    Defaults are the shipped baseline from `RISK_PHILOSOPHY.md` section 3, every one of
    them inside its bound with room to tighten. A process constructed with no arguments
    runs at those, because a missing configuration file must never produce a less
    conservative system than a present one.
    """

    risk_fraction_per_trade: Decimal = Decimal("0.005")
    target_volatility_annualised: Decimal = Decimal("0.15")
    max_kelly_fraction: Decimal = Decimal("0.25")
    min_trades_for_kelly: int = 100
    atr_invalidation_multiple: Decimal = Decimal("2.5")
    # A strategy that will not name its stop pays for the estimate: `r_used` is halved
    # on top of the ATR denominator (RISK_PHILOSOPHY.md section 3.1).
    unstated_invalidation_risk_multiplier: Decimal = Decimal("0.5")

    def __post_init__(self) -> None:
        submitted = self.bounded_values()
        assert_within_ceilings(submitted, SIZING_CEILINGS, scope="sizing")
        assert_above_floors(submitted, SIZING_FLOORS, scope="sizing")
        _require_positive(self.risk_fraction_per_trade, "risk_fraction_per_trade")
        _require_positive(self.target_volatility_annualised, "target_volatility_annualised")
        _require_non_negative(self.max_kelly_fraction, "max_kelly_fraction")
        _require_positive(self.atr_invalidation_multiple, "atr_invalidation_multiple")

    def bounded_values(self) -> Mapping[str, Decimal]:
        """Every bounded parameter as a `Decimal`, keyed by field name.

        The integer-valued parameter is widened here rather than compared as an int, so
        one comparison implementation serves both and there is no second code path
        whose direction could be written backwards.
        """
        names = (*SIZING_CEILINGS, *SIZING_FLOORS)
        return MappingProxyType({name: Decimal(str(getattr(self, name))) for name in names})


@dataclass(frozen=True, slots=True)
class VolatilityEstimate:
    """The three candidate volatilities and the one that was used, all annualised.

    Kept as a record rather than collapsed to a single number because the maximum is the
    whole point: an investigation that finds a position was tiny needs to know whether
    the floor bound, which is a per-symbol calibration question, or the EWMA bound,
    which is a market question.
    """

    ewma_annualised: Decimal
    slow_window_annualised: Decimal
    floor_annualised: Decimal
    used_annualised: Decimal


@dataclass(frozen=True, slots=True)
class SizingInputs:
    """Everything sizing reads. Assembled by the caller; never fetched from here.

    `return_series` is trailing, most recent last, and is the caller's responsibility to
    have taken point-in-time -- a series that includes the bar being decided on is
    look-ahead, and this module cannot detect that (`.claude/rules/no-lookahead.md`).
    """

    instrument: Instrument
    direction: Direction
    equity_usd: Decimal
    entry_quote_price: Decimal
    invalidation_quote_price: Decimal | None
    atr_14_quote: Decimal
    return_series: tuple[Decimal, ...]
    # Per-symbol 10th percentile of three years of realised volatility. Crypto majors go
    # quiet for weeks and then do not; without the floor a 3 a.m. lull produces a 6x
    # position (RISK_PHILOSOPHY.md section 3.2).
    volatility_floor_annualised: Decimal
    # The strategy's own realised per-trade distribution, not the backtest's.
    closed_trade_count: int
    realised_mean_return_fraction: Decimal
    realised_return_stdev_fraction: Decimal
    # The exposure term, `q_exposure`. Supplied by the caller as
    # `ExposureAssessment.permitted_notional_usd`, which has already folded in the
    # per-position notional cap, the gross, net and per-asset ratios and the free-margin
    # requirement. Recomputing any of that here would give the portfolio two opinions
    # about the same limit, and the one that disagreed would be whichever the reader was
    # not looking at (`fking.risk.exposure`).
    permitted_notional_usd: Decimal

    def __post_init__(self) -> None:
        if self.direction is Direction.FLAT:
            raise DomainError(
                "a flat signal asks for no exposure; sizing it is a category error, not "
                "a request for zero"
            )
        _require_positive(self.entry_quote_price, "entry_quote_price")
        _require_non_negative(self.equity_usd, "equity_usd")
        _require_non_negative(self.atr_14_quote, "atr_14_quote")
        _require_non_negative(self.volatility_floor_annualised, "volatility_floor_annualised")
        _require_non_negative(self.realised_return_stdev_fraction, "realised_return_stdev_fraction")
        _require_non_negative(self.permitted_notional_usd, "permitted_notional_usd")
        if self.closed_trade_count < 0:
            raise DomainError(
                f"closed_trade_count must be at or above zero; got {self.closed_trade_count}"
            )
        if self.invalidation_quote_price is not None:
            _require_positive(self.invalidation_quote_price, "invalidation_quote_price")


@dataclass(frozen=True, slots=True)
class PositionSizing:
    """The sized quantity and every term that produced it.

    `ARCHITECTURE.md` section 11 requires a trade to be reconstructable from the audit
    log alone. That means the non-binding terms are part of the record: knowing the
    quantity was 0.004 BTC answers nothing without knowing that Kelly was absent, the
    volatility term was 0.02 and the fixed-fractional term bound at 0.004.
    """

    base_quantity: Decimal
    notional_usd: Decimal
    binding_term: str
    fixed_fractional_base_quantity: Decimal
    volatility_target_base_quantity: Decimal
    kelly_base_quantity: Decimal | None
    exposure_base_quantity: Decimal
    volatility: VolatilityEstimate
    used_invalidation_fallback: bool
    risk_fraction_used: Decimal

    @property
    def terms(self) -> Mapping[str, Decimal]:
        """Every term that entered the `min()`, by name. Kelly is absent when excluded.

        Absent rather than zero: a term of zero inside a minimum is a position of zero,
        and a caller reading `terms["kelly"] == 0` cannot tell "no edge" from "no
        record yet".
        """
        present: dict[str, Decimal] = {
            _FIXED_FRACTIONAL: self.fixed_fractional_base_quantity,
            _VOLATILITY_TARGET: self.volatility_target_base_quantity,
            _EXPOSURE: self.exposure_base_quantity,
        }
        if self.kelly_base_quantity is not None:
            present[_KELLY] = self.kelly_base_quantity
        return MappingProxyType(present)

    def audit_payload(self) -> Mapping[str, object]:
        """The row body, in the shape `fking.risk.exposure` already writes.

        Decimals are rendered as strings rather than left as `Decimal`: the payload lands
        in `jsonb`, and a JSON encoder that has not been told otherwise turns a `Decimal`
        into a float on the way into an append-only table that can never be corrected.

        `kelly` is `None` when the term was excluded, and that null is load-bearing --
        `"kelly": "0"` in a row six months from now cannot be told apart from a strategy
        Kelly priced at zero, and the two produce the same quantity for opposite reasons.
        """
        return MappingProxyType(
            {
                "base_quantity": str(self.base_quantity),
                "notional_usd": str(self.notional_usd),
                "binding_term": self.binding_term,
                "risk_fraction_used": str(self.risk_fraction_used),
                "used_invalidation_fallback": self.used_invalidation_fallback,
                "terms": MappingProxyType(
                    {name: str(quantity) for name, quantity in self.terms.items()}
                ),
                "kelly_excluded": self.kelly_base_quantity is None,
                "volatility_annualised": MappingProxyType(
                    {
                        "ewma": str(self.volatility.ewma_annualised),
                        "slow_window": str(self.volatility.slow_window_annualised),
                        "floor": str(self.volatility.floor_annualised),
                        "used": str(self.volatility.used_annualised),
                    }
                ),
            }
        )


def _population_stdev(observations: Sequence[Decimal]) -> Decimal:
    """Population standard deviation. Fewer than two observations is zero dispersion.

    Zero rather than an exception: an empty or single-observation history is the normal
    state of a newly listed symbol, and the floor is what covers it. Raising here would
    make a new symbol unsizeable rather than conservatively sized.
    """
    if len(observations) < _MIN_OBSERVATIONS_FOR_DISPERSION:
        return _ZERO
    observation_count = Decimal(len(observations))
    mean_return_fraction = sum(observations, _ZERO) / observation_count
    squared = sum(
        ((observation - mean_return_fraction) ** 2 for observation in observations), _ZERO
    )
    return (squared / observation_count).sqrt()


def volatility_used(
    return_series: Sequence[Decimal], *, floor_annualised: Decimal
) -> VolatilityEstimate:
    """`sigma_used = max(sigma_ewma(0.94), sigma_60d, sigma_floor)`, annualised.

    The maximum of a fast and a slow estimate, never a blend, and that asymmetry is not
    aesthetic: underestimating volatility is expensive and overestimating it is merely
    suboptimal. A fast estimator in a calm regime is exactly the configuration that
    maximises position size immediately before a volatility expansion, because the
    estimator's error and the market's next move are positively correlated in the
    direction that hurts (`RISK_PHILOSOPHY.md` section 3.2).
    """
    _require_non_negative(floor_annualised, "floor_annualised")
    annualisation = _PERIODS_PER_YEAR.sqrt()

    ewma_variance = _ZERO
    for index, return_fraction in enumerate(return_series):
        squared = return_fraction**2
        # Seeded with the first squared return rather than with zero: a zero seed decays
        # away over roughly 33 observations, so a short series would report a
        # volatility biased downward by exactly the length of the series -- smallest
        # sample, largest position.
        ewma_variance = (
            squared if index == 0 else _EWMA_DECAY * ewma_variance + (_ONE - _EWMA_DECAY) * squared
        )

    ewma_annualised = ewma_variance.sqrt() * annualisation
    slow_window_annualised = (
        _population_stdev(tuple(return_series)[-_SLOW_WINDOW_OBSERVATIONS:]) * annualisation
    )
    return VolatilityEstimate(
        ewma_annualised=ewma_annualised,
        slow_window_annualised=slow_window_annualised,
        floor_annualised=floor_annualised,
        used_annualised=max(ewma_annualised, slow_window_annualised, floor_annualised),
    )


def _risk_distance_quote(inputs: SizingInputs, parameters: SizingParameters) -> Decimal:
    """Distance from entry to the point that would prove the thesis wrong.

    This is the denominator that makes "risk 0.5% per trade" a computable statement at
    all. Without it you are risking 0.5% of equity *in notional*, which is a completely
    different and much weaker claim (`RISK_PHILOSOPHY.md` section 3.1).
    """
    if inputs.invalidation_quote_price is not None:
        return abs(inputs.entry_quote_price - inputs.invalidation_quote_price)
    return parameters.atr_invalidation_multiple * inputs.atr_14_quote


def size_position(inputs: SizingInputs, parameters: SizingParameters) -> PositionSizing:
    """Size one position: three methods, the exposure headroom, and venue filters.

    Note the consequence that surprises people: a high-conviction signal with a distant
    stop gets a *smaller* position than a low-conviction signal with a tight stop. The
    budget is risk, not exposure.
    """
    volatility = volatility_used(
        inputs.return_series, floor_annualised=inputs.volatility_floor_annualised
    )

    used_invalidation_fallback = inputs.invalidation_quote_price is None
    risk_fraction_used = parameters.risk_fraction_per_trade
    if used_invalidation_fallback:
        risk_fraction_used *= parameters.unstated_invalidation_risk_multiplier

    risk_distance_quote = _risk_distance_quote(inputs, parameters)
    # A zero denominator is an invalidation level sitting exactly on the entry, or an
    # ATR of zero on a symbol that has not moved. Both mean "the distance to being
    # wrong is unmeasurable", and the conservative reading of an unmeasurable risk
    # budget is no position -- not an unbounded one, which is what dividing would give.
    fixed_fractional_base_quantity = (
        (risk_fraction_used * inputs.equity_usd) / risk_distance_quote
        if risk_distance_quote > _ZERO
        else _ZERO
    )

    # sigma_used is bounded below by the floor, which the caller may legitimately set to
    # zero for a symbol with no calibration yet; the same reading applies as above.
    volatility_target_base_quantity = (
        (parameters.target_volatility_annualised / volatility.used_annualised)
        * inputs.equity_usd
        / inputs.entry_quote_price
        if volatility.used_annualised > _ZERO
        else _ZERO
    )

    kelly_base_quantity = _kelly_base_quantity(inputs, parameters)

    exposure_base_quantity = inputs.permitted_notional_usd / inputs.entry_quote_price

    candidates: dict[str, Decimal] = {
        _FIXED_FRACTIONAL: fixed_fractional_base_quantity,
        _VOLATILITY_TARGET: volatility_target_base_quantity,
        _EXPOSURE: exposure_base_quantity,
    }
    if kelly_base_quantity is not None:
        candidates[_KELLY] = kelly_base_quantity

    # min over a dict of (name, quantity): the binding term is recorded rather than
    # recomputed, because recomputing it is where the two answers drift apart.
    binding_term = min(candidates, key=lambda name: candidates[name])
    unfiltered_base_quantity = candidates[binding_term]

    base_quantity = inputs.instrument.quantize_base_quantity(unfiltered_base_quantity)
    if not inputs.instrument.meets_min_notional(base_quantity, inputs.entry_quote_price):
        # Below MIN_NOTIONAL the venue refuses the order outright, so the only honest
        # quantity is zero. Rounding *up* to the floor would be the risk engine
        # authorising a position larger than any of its own terms permitted.
        #
        # Snapping onto the lot lattice does not make the venue the binding term -- it
        # moves the quantity by less than one step and would otherwise be reported as
        # binding on almost every call, which would make the field useless for the one
        # question it exists to answer.
        base_quantity = _ZERO
        binding_term = _VENUE_FILTERS

    return PositionSizing(
        base_quantity=base_quantity,
        notional_usd=inputs.instrument.notional_quote(base_quantity, inputs.entry_quote_price),
        binding_term=binding_term,
        fixed_fractional_base_quantity=fixed_fractional_base_quantity,
        volatility_target_base_quantity=volatility_target_base_quantity,
        kelly_base_quantity=kelly_base_quantity,
        exposure_base_quantity=exposure_base_quantity,
        volatility=volatility,
        used_invalidation_fallback=used_invalidation_fallback,
        risk_fraction_used=risk_fraction_used,
    )


def _kelly_base_quantity(inputs: SizingInputs, parameters: SizingParameters) -> Decimal | None:
    """Capped Kelly, or `None` when the record is too short to estimate it.

    `None`, never zero, and the distinction is the whole implementation. `f* = mu/sigma^2`
    is growth-optimal and entirely conditional on knowing `mu`; the fractional standard
    error on `mu_hat` is `1/(SR*sqrt(T))`, which at SR = 1 with a year of data is 100%.
    Meanwhile `g(2f*) = 0` exactly -- a one-sigma overestimate takes you from maximum
    growth to zero growth, while a one-sigma underestimate costs 25%.

    So below `min_trades_for_kelly` the term is *omitted from the min()*. Reading "returns
    0 below 100 trades" literally and returning zero puts a zero inside a minimum, which
    is a position size of zero, forever, for every strategy that has not yet traded --
    which is every new strategy. The pipeline then shows no trades and no error.

    `max_kelly_fraction` is applied in both of the senses the source document can be read
    in, and the smaller answer wins. `RISK_PHILOSOPHY.md` section 3.3 introduces `c` as
    the multiplier in `f = c*f*` and tabulates the drawdown probability against it, then
    says "we cap at max_kelly_fraction = 0.25" -- which reads as a cap on `f` itself.
    Taken alone the cap reading applies no haircut whatever whenever `f*` is already
    below 0.25, and the estimation-error argument the haircut exists for does not depend
    on `f*` being large. Taken alone the multiplier reading leaves `f` unbounded when a
    quiet realised distribution makes `f*` enormous. `min(c*f*, c)` satisfies both, and
    resolving an ambiguity in a risk rule toward the smaller number is the same
    principle the surrounding `min()` is built on.
    """
    if inputs.closed_trade_count < parameters.min_trades_for_kelly:
        return None
    if inputs.realised_return_stdev_fraction <= _ZERO:
        # A record with no dispersion makes mu/sigma^2 undefined rather than infinite.
        # With enough closed trades to be here, that is a data fault, and the
        # conservative reading of a data fault in the numerator of a growth-optimal bet
        # is not to bet.
        return _ZERO
    # A negative realised mean with a hundred closed trades is Kelly saying the bet is
    # losing, and the correct size for a losing bet is zero. Zero here is a measured
    # verdict, unlike the None above, which is the absence of one.
    full_kelly_equity_fraction = max(
        inputs.realised_mean_return_fraction / (inputs.realised_return_stdev_fraction**2), _ZERO
    )
    equity_fraction = min(
        parameters.max_kelly_fraction * full_kelly_equity_fraction, parameters.max_kelly_fraction
    )
    return equity_fraction * inputs.equity_usd / inputs.entry_quote_price
