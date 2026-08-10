"""Mutating any cost, slippage or fill assumption changes `frozen_assumptions_hash`.

`docs/rules/overfitting-defences.md`: "Assumptions are fixed before the run and recorded
with the run. Post-hoc adjustment is the purest form of the overfitting this system
exists to prevent." A caller who tweaks a fee after seeing a disappointing result cannot
overwrite the existing record -- the tweak produces a different hash, which is a new
trial, never a revision of the old one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fking.backtest.costs import CostModel
from fking.backtest.results import cost_model_digest, frozen_assumptions_hash
from tests.backtest.test_cost_fixtures import (
    cost_model,
    depth_profile,
    latency_profile,
    spread_profile,
)
from tests.support.run_config import config_for

pytestmark = pytest.mark.unit


def test_the_same_run_config_and_cost_model_hash_the_same_twice() -> None:
    run_config = config_for()
    model = cost_model()
    first = frozen_assumptions_hash(run_config=run_config, cost_model=model)
    second = frozen_assumptions_hash(run_config=run_config, cost_model=model)
    assert first == second


def test_cost_model_digest_is_stable_across_two_constructions() -> None:
    assert cost_model_digest(cost_model()) == cost_model_digest(cost_model())


_DEFAULT_FEES = cost_model().fees
_HIGHER_MAKER_FEE = _DEFAULT_FEES.model_copy(update={"maker_fee_bps": Decimal("3.0")})
_HIGHER_TAKER_FEE = _DEFAULT_FEES.model_copy(update={"taker_fee_bps": Decimal("7.0")})

CHANGES: tuple[tuple[str, object], ...] = (
    ("maker_fee_bps", cost_model(fees=_HIGHER_MAKER_FEE)),
    ("taker_fee_bps", cost_model(fees=_HIGHER_TAKER_FEE)),
    ("spread_p50_bps", cost_model(spreads={"BTCUSDT": spread_profile(p50_bps=Decimal("0.30"))})),
    ("depth_touch_base", cost_model(depth={"BTCUSDT": depth_profile(touch_base=Decimal("4"))})),
    ("latency_adverse_drift", cost_model(latency=latency_profile(Decimal("1.0")))),
    ("calibration_source", cost_model(calibration_source="binance_um_production_2026-06..2026-08")),
)


@pytest.mark.parametrize(("field_name", "changed_model"), CHANGES, ids=[c[0] for c in CHANGES])
def test_mutating_a_cost_assumption_changes_the_digest(
    field_name: str, changed_model: object
) -> None:
    assert isinstance(changed_model, CostModel)
    baseline = cost_model_digest(cost_model())
    mutated = cost_model_digest(changed_model)
    assert baseline != mutated, f"changing {field_name} left cost_model_digest unchanged"


@pytest.mark.parametrize(("field_name", "changed_model"), CHANGES, ids=[c[0] for c in CHANGES])
def test_mutating_a_cost_assumption_changes_the_frozen_assumptions_hash(
    field_name: str, changed_model: object
) -> None:
    assert isinstance(changed_model, CostModel)
    run_config = config_for()
    baseline = frozen_assumptions_hash(run_config=run_config, cost_model=cost_model())
    mutated = frozen_assumptions_hash(run_config=run_config, cost_model=changed_model)
    assert baseline != mutated, f"changing {field_name} left frozen_assumptions_hash unchanged"


def test_changing_only_the_run_config_also_changes_the_frozen_assumptions_hash() -> None:
    model = cost_model()
    baseline = frozen_assumptions_hash(run_config=config_for(), cost_model=model)
    changed = frozen_assumptions_hash(
        run_config=config_for(strategy_version="1.3.1"), cost_model=model
    )
    assert baseline != changed


def test_a_post_hoc_cost_adjustment_cannot_reuse_the_original_hash() -> None:
    """The scenario the acceptance criterion names directly: tweak a fee, get a new hash."""
    run_config = config_for()
    original_model = cost_model()
    original_hash = frozen_assumptions_hash(run_config=run_config, cost_model=original_model)

    adjusted_model = cost_model(
        fees=original_model.fees.model_copy(update={"taker_fee_bps": Decimal("0.01")})
    )
    adjusted_hash = frozen_assumptions_hash(run_config=run_config, cost_model=adjusted_model)

    assert adjusted_hash != original_hash
