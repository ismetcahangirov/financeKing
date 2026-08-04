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

from fking.data.features._definition_digests import DEFINITION_DIGESTS
from fking.data.features.compute import trailing_realised_volatility, trailing_return_fraction
from fking.data.features.spec import FeatureObservation, FeaturePoint, FeatureSpec
from fking.platform.errors import FeatureContractError

__all__ = ["FEATURES", "evaluate", "registered", "registered_names"]

# Both features consume closed 1-minute bars and nothing else. Named as a set of
# dataset identifiers rather than as free text so #30's availability contract can check
# a declaration against what the corpus actually holds without parsing prose.
_CLOSED_BARS: Final[frozenset[str]] = frozenset({"klines"})

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
    _require_strictly_increasing(observations)
    return spec.compute(observations, spec.window())


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
