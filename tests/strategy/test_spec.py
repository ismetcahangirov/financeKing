"""A specification that cannot be run is refused at declaration, not at the first bar.

Every clause here is a field somebody would have written in a docstring instead. The test
of whether that matters is mechanical: could the evolution engine mutate it, could the
walk-forward embargo be sized from it, and could a reviewer see it in a diff of the spec
alone? A number that fails all three is a number the lineage cannot reconstruct.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fking.strategy import (
    DecimalParameter,
    FeatureRequirement,
    InvalidationRule,
    ParameterSpace,
    StrategyContractError,
    StrategySpec,
)
from fking.strategy._guards import require_utc
from tests.strategy.harness import BTCUSDT, ETHUSDT

pytestmark = pytest.mark.unit

_THESIS = (
    "The declared edge persists over the declared horizon, and the thesis is wrong once "
    "the declared adverse move has happened."
)
_TRAILING_RETURN = FeatureRequirement(feature_name="trailing_return_fraction", feature_version=1)


def a_spec(**overrides: object) -> StrategySpec:
    fields: dict[str, object] = {
        "strategy_id": "declared",
        "strategy_version": 1,
        "thesis": _THESIS,
        "instruments": (BTCUSDT,),
        "bar_intervals": (timedelta(minutes=15),),
        "required_features": (_TRAILING_RETURN,),
        "warm_up_bars": 8,
        "parameters": ParameterSpace(),
        "invalidation": InvalidationRule(adverse_move_fraction=Decimal("0.01")),
        "signal_horizon": timedelta(hours=1),
    }
    fields.update(overrides)
    return StrategySpec(**fields)  # type: ignore[arg-type]  # a test factory over a frozen shape


def test_a_thesis_too_short_to_be_falsifiable_is_refused() -> None:
    """ "momentum" is a label, not a statement anything could contradict."""
    with pytest.raises(StrategyContractError, match="falsifiable"):
        a_spec(thesis="momentum")


def test_a_blank_strategy_id_is_refused() -> None:
    with pytest.raises(StrategyContractError, match="strategy_id"):
        a_spec(strategy_id="   ")


def test_a_strategy_declaring_no_instruments_is_refused() -> None:
    """The engine subscribes from this field, so naming none means reading bars nothing
    gated."""
    with pytest.raises(StrategyContractError, match="no instruments"):
        a_spec(instruments=())


def test_a_duplicate_instrument_is_refused() -> None:
    with pytest.raises(StrategyContractError, match="duplicate instrument"):
        a_spec(instruments=(BTCUSDT, BTCUSDT))


def test_two_distinct_instruments_are_admitted() -> None:
    spec = a_spec(instruments=(BTCUSDT, ETHUSDT))

    assert spec.declares(ETHUSDT)
    assert spec.declares(BTCUSDT)


def test_a_strategy_declaring_no_bar_interval_is_refused() -> None:
    """The interval is what turns `warm_up_bars` into a duration the embargo can use."""
    with pytest.raises(StrategyContractError, match="no bar intervals"):
        a_spec(bar_intervals=())


def test_a_duplicate_bar_interval_is_refused() -> None:
    with pytest.raises(StrategyContractError, match="duplicate bar interval"):
        a_spec(bar_intervals=(timedelta(minutes=15), timedelta(minutes=15)))


def test_a_non_positive_bar_interval_is_refused() -> None:
    with pytest.raises(StrategyContractError, match="bar_interval must be positive"):
        a_spec(bar_intervals=(timedelta(0),))


def test_requiring_the_same_feature_twice_is_refused() -> None:
    with pytest.raises(StrategyContractError, match="twice"):
        a_spec(required_features=(_TRAILING_RETURN, _TRAILING_RETURN))


def test_a_zero_bar_warm_up_is_refused() -> None:
    """A strategy claiming its first bar is meaningful has not declared a warm-up."""
    with pytest.raises(StrategyContractError, match="warm_up_bars"):
        a_spec(warm_up_bars=0)


def test_a_boolean_warm_up_is_refused() -> None:
    """`bool` is an `int` subclass, so `warm_up_bars=True` would declare one bar and read
    as a flag somebody meant to set elsewhere."""
    with pytest.raises(StrategyContractError, match="must be an int"):
        a_spec(warm_up_bars=True)


def test_a_non_positive_signal_horizon_is_refused() -> None:
    with pytest.raises(StrategyContractError, match="signal_horizon"):
        a_spec(signal_horizon=timedelta(0))


def test_a_version_below_one_is_refused() -> None:
    with pytest.raises(StrategyContractError, match="strategy_version"):
        a_spec(strategy_version=0)


def test_warm_up_duration_is_measured_at_the_shortest_declared_interval() -> None:
    """The conservative direction: a warm-up that clears a lookback at the shortest
    interval clears it at every longer one."""
    spec = a_spec(bar_intervals=(timedelta(hours=1), timedelta(minutes=15)), warm_up_bars=8)

    assert spec.shortest_bar_interval == timedelta(minutes=15)
    assert spec.warm_up_duration == timedelta(hours=2)


def test_an_undeclared_interval_is_reported_as_undeclared() -> None:
    spec = a_spec()

    assert spec.declares_interval(timedelta(minutes=15))
    assert not spec.declares_interval(timedelta(minutes=5))


def test_the_key_is_the_id_and_the_version_together() -> None:
    assert a_spec(strategy_version=3).key == ("declared", 3)
    assert a_spec().describe() == "declared v1"


def test_a_feature_requirement_carries_its_version_everywhere() -> None:
    assert _TRAILING_RETURN.key == ("trailing_return_fraction", 1)
    assert _TRAILING_RETURN.describe() == "trailing_return_fraction v1"


def test_a_blank_feature_name_is_refused() -> None:
    with pytest.raises(StrategyContractError, match="feature_name"):
        FeatureRequirement(feature_name="  ", feature_version=1)


def test_a_feature_version_below_one_is_refused() -> None:
    with pytest.raises(StrategyContractError, match="feature_version"):
        FeatureRequirement(feature_name="trailing_return_fraction", feature_version=0)


def test_the_specification_is_frozen() -> None:
    """The evolution engine produces a child by constructing a new spec, never by editing
    its parent -- and a mutable spec would let a run change the declaration it was
    validated against."""
    spec = a_spec()

    with pytest.raises(AttributeError):
        spec.warm_up_bars = 1  # type: ignore[misc]  # the point of the test


def test_a_spec_whose_parameter_defaults_are_out_of_bounds_is_refused_at_declaration() -> None:
    """Bound once at declaration, so the failure names the space rather than the first
    strategy somebody constructs from it."""
    with pytest.raises(StrategyContractError, match="outside its declared bounds"):
        ParameterSpace(
            (
                DecimalParameter(
                    name="entry_return_fraction",
                    default=Decimal("9"),
                    minimum=Decimal("0.001"),
                    maximum=Decimal("0.05"),
                ),
            )
        )


def test_a_naive_decision_instant_is_refused_by_this_package_s_own_guard() -> None:
    """`require_utc` is this package's, not `fking.domain`'s private one; reaching across
    that boundary would make a domain refactor break strategies silently."""
    with pytest.raises(StrategyContractError, match="timezone-aware"):
        require_utc(datetime(2026, 3, 1, 12, 0), "decided_at_utc")  # noqa: DTZ001


def test_an_aware_but_non_utc_instant_is_refused_rather_than_converted() -> None:
    """`astimezone(UTC)` would silently accept a value whose offset was guessed wrong
    upstream; raising forces the guess to be made where the data enters."""
    baku = timezone(timedelta(hours=4))

    with pytest.raises(StrategyContractError, match="must be UTC"):
        require_utc(datetime(2026, 3, 1, 16, 0, tzinfo=baku), "decided_at_utc")
