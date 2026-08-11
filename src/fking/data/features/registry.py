"""The registry: every feature the system is allowed to compute, and nothing else.

A feature reaches a strategy only by being here. That closes the route where a
computation lives next to the code that consumes it, is never declared, and therefore
never states a lookback, never states an availability lag, and is never covered by the
adversarial look-ahead probe -- which is parametrised over this mapping precisely so
that adding a feature adds a probe case with no test-file edit.

Keyed on `(name, version)` rather than on name. Two versions of a feature coexist by
design: values computed under the old definition remain in the store, tagged with the
version that produced them, and a reference that carried only a name would resolve to
"whatever definition is current" -- which is the read that makes a historical result
irreproducible.

`evaluate` is the boundary where an input series is validated. The `compute` functions
themselves trust their input, which is what lets each one stay self-contained and
therefore individually digestible (`compute`, module docstring).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from fking.data.alt.registry import BINANCE_FUNDING_RATE
from fking.data.features._definition_digests import DEFINITION_DIGESTS
from fking.data.features.compute import (
    bollinger_band_width_fraction,
    bollinger_z_score,
    donchian_channel_breakout_state,
    settled_funding_rate,
    trailing_mean_absolute_funding_rate,
    trailing_realised_volatility,
    trailing_return_fraction,
)
from fking.data.features.spec import (
    FeatureObservation,
    FeaturePoint,
    FeatureSpec,
    SettlementRateObservation,
)
from fking.platform.errors import FeatureContractError

__all__ = [
    "FEATURES",
    "evaluate",
    "evaluate_settlement_rates",
    "registered",
    "registered_names",
]

# Both features consume closed 1-minute bars and nothing else. Named as a set of
# dataset identifiers rather than as free text so #30's availability contract can check
# a declaration against what the corpus actually holds without parsing prose.
_CLOSED_BARS: Final[frozenset[str]] = frozenset({"klines"})

# Twenty periods of the fifteen-minute grain the baseline strategies decide on. Twenty is
# the conventional Donchian channel and the conventional Bollinger band length -- Donchian's
# own rule and Bollinger's original 1980s formulation both use it -- and it is written here
# as a duration because a feature's window is a duration and never a bar count: the corpus
# holds one-minute bars, so this same five hours is 300 closes in production and 20 closes
# in a fifteen-minute test series. The extremes and the dispersion are those of the same
# five hours either way, sampled more finely in production.
#
# Fixed a priori and deliberately never searched. These three features exist to serve the
# control group (#57), and a swept baseline is not a control: it charges trials against the
# global counter on behalf of a strategy nobody intends to promote, so every future
# deflated Sharpe pays for a search that was never meant to produce anything.
_TWENTY_PERIOD_WINDOW: Final[timedelta] = timedelta(hours=5)

# The perpetual funding series and nothing else. Spelled as the dataset identifier for the
# same reason `_CLOSED_BARS` is: `fking.data.features.availability` resolves it against
# `fking.data.format_resolver.Dataset` and refuses a feature whose input names a series the
# corpus does not hold, without parsing prose.
_FUNDING_SETTLEMENTS: Final[frozenset[str]] = frozenset({"fundingRate"})

# Twenty-one settlements at Binance's eight-hourly cadence -- one week. It is the shortest
# window over which a funding *regime* rather than a single settlement is observable, and
# the carry baseline (#57) holds for exactly this long, so the window that estimates the
# rate and the window the rate is earned over are the same seven days by construction
# rather than by two independent choices. Fixed a priori and never searched, for the reason
# `_TWENTY_PERIOD_WINDOW` states.
_FUNDING_REGIME_WINDOW: Final[timedelta] = timedelta(days=7)

# The same lag `fking.data.alt.registry` measured for this source, read from the
# declaration rather than restated. Two numbers for one publication behaviour drift, and
# the direction they drift in -- a feature claiming to know a rate earlier than the ingest
# path could deliver it -- is look-ahead that no test of either module alone would catch.
_FUNDING_PUBLICATION_LAG: Final[timedelta] = BINANCE_FUNDING_RATE.availability_lag

FEATURES: Final[Mapping[tuple[str, int], FeatureSpec]] = MappingProxyType(
    {
        spec.key(): spec
        for spec in (
            FeatureSpec(
                name="trailing_return_fraction",
                version=1,
                compute=trailing_return_fraction,
                inputs=_CLOSED_BARS,
                lookback=timedelta(hours=1),
                # Zero, and defensible: the inputs are closed bars, and a closed bar's
                # close is known at the instant it closes. Anything a venue publishes
                # after the instant it stamps -- funding, open interest, an index --
                # carries a positive lag, and #32 is where those arrive.
                availability_lag=timedelta(0),
                label_horizon=timedelta(hours=1),
                point_in_time_proof=(
                    "Window is half-open (t-1h, t]; the base is the newest closed bar at "
                    "or before t-1h and the head is the bar closing at t, so both "
                    "endpoints had already closed at t. No partial window is emitted."
                ),
                uses_trailing_statistics_only=True,
            ),
            FeatureSpec(
                name="trailing_realised_volatility",
                version=1,
                compute=trailing_realised_volatility,
                inputs=_CLOSED_BARS,
                lookback=timedelta(hours=1),
                availability_lag=timedelta(0),
                label_horizon=timedelta(hours=1),
                point_in_time_proof=(
                    "Sample standard deviation of successive returns between closed bars "
                    "inside (t-1h, t]; the oldest return's base is the bar at or before "
                    "t-1h, which also already existed at t. Fewer than two returns emits "
                    "nothing rather than a zero."
                ),
                uses_trailing_statistics_only=True,
            ),
            FeatureSpec(
                name="donchian_channel_breakout_state",
                version=1,
                compute=donchian_channel_breakout_state,
                inputs=_CLOSED_BARS,
                lookback=_TWENTY_PERIOD_WINDOW,
                availability_lag=timedelta(0),
                label_horizon=_TWENTY_PERIOD_WINDOW,
                point_in_time_proof=(
                    "The channel high and low are taken over the closes in (t-5h, t], the "
                    "newest of which is the observation being stamped; every one of them "
                    "had closed at t. The oldest observation used is the newest one at or "
                    "before t-5h, which also already existed. A window without such an "
                    "observation emits nothing rather than an extreme over a short window."
                ),
                uses_trailing_statistics_only=True,
            ),
            FeatureSpec(
                name="bollinger_z_score",
                version=1,
                compute=bollinger_z_score,
                inputs=_CLOSED_BARS,
                lookback=_TWENTY_PERIOD_WINDOW,
                availability_lag=timedelta(0),
                label_horizon=_TWENTY_PERIOD_WINDOW,
                point_in_time_proof=(
                    "Mean and sample standard deviation over the closes in (t-5h, t], all "
                    "of which had closed at t, with the stamped observation inside its own "
                    "window -- which is what a Bollinger band is. Nothing after t enters "
                    "either moment, and a window with zero dispersion emits no point."
                ),
                uses_trailing_statistics_only=True,
            ),
            FeatureSpec(
                name="bollinger_band_width_fraction",
                version=1,
                compute=bollinger_band_width_fraction,
                inputs=_CLOSED_BARS,
                lookback=_TWENTY_PERIOD_WINDOW,
                availability_lag=timedelta(0),
                label_horizon=_TWENTY_PERIOD_WINDOW,
                point_in_time_proof=(
                    "The same trailing (t-5h, t] window and the same sample standard "
                    "deviation bollinger_z_score divides by, over the mean of that window. "
                    "Both moments are taken over closed bars at or before t."
                ),
                uses_trailing_statistics_only=True,
            ),
            FeatureSpec(
                name="settled_funding_rate",
                version=1,
                settlement_rate_compute=settled_funding_rate,
                inputs=_FUNDING_SETTLEMENTS,
                lookback=_FUNDING_REGIME_WINDOW,
                availability_lag=_FUNDING_PUBLICATION_LAG,
                label_horizon=_FUNDING_REGIME_WINDOW,
                point_in_time_proof=(
                    "The value is the rate settled at t and nothing else enters it, so no "
                    "observation after t can. It is stamped available one minute later, "
                    "which is the measured envelope for the mark-price stream that "
                    "broadcasts it; a decision taken at the settlement instant itself sees "
                    "the previous settlement. A window holding no settlement at or before "
                    "t-7d emits nothing, so this series and the trailing mean over the "
                    "same window begin on the same date."
                ),
                uses_trailing_statistics_only=True,
            ),
            FeatureSpec(
                name="trailing_mean_absolute_funding_rate",
                version=1,
                settlement_rate_compute=trailing_mean_absolute_funding_rate,
                inputs=_FUNDING_SETTLEMENTS,
                lookback=_FUNDING_REGIME_WINDOW,
                availability_lag=_FUNDING_PUBLICATION_LAG,
                label_horizon=_FUNDING_REGIME_WINDOW,
                point_in_time_proof=(
                    "Mean of |rate| over the settlements in (t-7d, t], the newest of which "
                    "is the one being stamped and all of which had already settled at t. "
                    "The oldest used is the newest settlement at or before t-7d, which "
                    "also already existed. A window without one emits nothing rather than "
                    "a mean over however many settlements happened to be loaded."
                ),
                uses_trailing_statistics_only=True,
            ),
        )
    }
)


def registered(name: str, version: int) -> FeatureSpec:
    """The spec for one `(name, version)`.

    Raises rather than returning `None`: every caller of this function is about to
    compute or read a series, and there is no sensible thing to do with a feature that
    was never declared. The message names what was asked for and what exists, because
    the common cause is an LLM-authored strategy asking for a feature that exists in the
    literature it was trained on rather than in this corpus (`DATA_PIPELINE.md` 8).
    """
    try:
        return FEATURES[(name, version)]
    except KeyError as unregistered:
        raise FeatureContractError(
            f"no feature {name!r} at version {version} is registered; "
            f"registered features are {sorted(registered_names())}"
        ) from unregistered


def registered_names() -> frozenset[str]:
    """Every registered feature name, versions collapsed."""
    return frozenset(name for name, _ in FEATURES)


def locked_digest(name: str, version: int) -> str:
    """The digest this `(name, version)` was registered with, from the frozen lock."""
    try:
        return DEFINITION_DIGESTS[(name, version)]
    except KeyError as unlocked:
        raise FeatureContractError(
            f"{name} v{version} has no entry in DEFINITION_DIGESTS; a definition that is "
            f"not locked can change without anybody noticing which results it produced"
        ) from unlocked


def evaluate(
    spec: FeatureSpec, observations: Sequence[FeatureObservation]
) -> tuple[FeaturePoint, ...]:
    """Compute one feature over one series, validating the series first.

    The validation is here rather than inside each `compute` for two reasons: it is the
    same for every feature, and duplicating it into the functions would make their
    digests move whenever it changed -- which would read as a definition change in every
    feature at once.
    """
    if spec.compute is None:
        raise FeatureContractError(
            f"{spec.name} v{spec.version} is declared over settlement rates and was handed "
            f"closed bars; call evaluate_settlement_rates. Computing it from the wrong "
            f"series would produce a number under a name whose point-in-time proof "
            f"describes a different input"
        )
    _require_strictly_increasing(observations)
    return spec.compute(observations, spec.window())


def evaluate_settlement_rates(
    spec: FeatureSpec, observations: Sequence[SettlementRateObservation]
) -> tuple[FeaturePoint, ...]:
    """Compute one settlement-rate feature over one series, validating the series first.

    A second entry point rather than a union parameter on `evaluate`. The two series carry
    different validity conditions -- a price must be positive, a funding rate is signed and
    routinely negative -- so one function would have to branch on the observation type and
    would accept a mixed sequence that neither branch rejects.
    """
    if spec.settlement_rate_compute is None:
        raise FeatureContractError(
            f"{spec.name} v{spec.version} is declared over closed bars and was handed "
            f"settlement rates; call evaluate. Computing it from the wrong series would "
            f"produce a number under a name whose point-in-time proof describes a "
            f"different input"
        )
    _require_strictly_increasing_settlements(observations)
    return spec.settlement_rate_compute(observations, spec.window())


def _require_strictly_increasing(observations: Sequence[FeatureObservation]) -> None:
    """Ascending event times, no duplicates, positive prices.

    Duplicates are rejected rather than deduplicated. Two observations claiming one
    instant means the series was merged from two sources that disagree, and picking one
    here would record that disagreement as a fact nobody can see afterwards.
    """
    previous: FeatureObservation | None = None
    for observation in observations:
        for field_name, quoted in (
            ("close_quote_price", observation.close_quote_price),
            ("open_quote_price", observation.open_quote_price),
        ):
            if quoted <= Decimal("0"):
                raise FeatureContractError(
                    f"{field_name} at {observation.event_time_utc.isoformat()} is "
                    f"{quoted}; a non-positive price is corrupt input"
                )
        if previous is not None and observation.event_time_utc <= previous.event_time_utc:
            raise FeatureContractError(
                f"observations must ascend strictly by event_time_utc; "
                f"{observation.event_time_utc.isoformat()} does not follow "
                f"{previous.event_time_utc.isoformat()}"
            )
        previous = observation


def _require_strictly_increasing_settlements(
    observations: Sequence[SettlementRateObservation],
) -> None:
    """Ascending settlement instants, no duplicates.

    There is deliberately no bound on the rate itself. A funding rate is signed, and
    Binance's own cap moves per symbol and has been changed on live perpetuals, so a
    plausibility band written here would be a second declaration of the venue's rules that
    silently rejects real prints the day the venue widens one. Duplicates are still refused
    for the reason the bar series refuses them: two rates claiming one settlement means two
    sources disagree, and picking one buries the disagreement.
    """
    previous: SettlementRateObservation | None = None
    for observation in observations:
        if previous is not None and observation.event_time_utc <= previous.event_time_utc:
            raise FeatureContractError(
                f"settlements must ascend strictly by event_time_utc; "
                f"{observation.event_time_utc.isoformat()} does not follow "
                f"{previous.event_time_utc.isoformat()}"
            )
        previous = observation
