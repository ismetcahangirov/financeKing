"""The one identity `fking.risk.metrics` promises: `RiskMetricsSnapshot.risk_model` is
the exact object passed in, never a recomputed copy carrying the same values.

Issue #56's second acceptance criterion, verbatim: "A test asserts the metrics module
holds the *same* covariance object as the sizing path (`is`, not `==`), and fails when a
recomputed copy is substituted." Guarding against value-equality standing in for identity
matters because `RiskModel` is a plain frozen dataclass: two independently built models
with identical parameters compare `==` while remaining two different objects, and that
gap is exactly where the drift `fking.risk.metrics`'s module docstring warns about would
hide.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

import pytest

from fking.risk.covariance import (
    CLUSTER_CUT_CORRELATION,
    CorrelationMatrix,
    RiskModel,
    cluster_by_correlation,
)
from fking.risk.metrics import RiskMetricsSnapshot, compute_risk_metrics

pytestmark = pytest.mark.unit

_AS_OF: Final = datetime(2026, 8, 1, tzinfo=UTC)
_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")
_SYMBOLS: Final[tuple[str, ...]] = ("BTCUSDT", "ETHUSDT")


def _model(*, correlation: Decimal = Decimal("0.4")) -> RiskModel:
    entries = {
        symbol_a: {
            symbol_b: (_ONE if symbol_a == symbol_b else correlation) for symbol_b in _SYMBOLS
        }
        for symbol_a in _SYMBOLS
    }
    matrix = CorrelationMatrix(symbols=_SYMBOLS, entries=entries)
    return RiskModel(
        symbols=_SYMBOLS,
        daily_volatility_ratio={"BTCUSDT": Decimal("0.03"), "ETHUSDT": Decimal("0.045")},
        correlations=matrix,
        shrinkage_intensity_ratio=_ZERO,
        psd_repair_ratio=_ZERO,
        clusters=cluster_by_correlation(matrix, cut_correlation=CLUSTER_CUT_CORRELATION),
    )


def _oscillating(amplitude: str, *, phase: int, length: int = 30) -> tuple[Decimal, ...]:
    magnitude = Decimal(amplitude)
    return tuple(magnitude if (step + phase) % 2 == 0 else -magnitude for step in range(length))


def _snapshot_for(model: RiskModel) -> RiskMetricsSnapshot:
    returns = _oscillating("0.01", phase=0)
    btc_returns = _oscillating("0.02", phase=0)
    clock: Callable[[], datetime] = lambda: _AS_OF  # noqa: E731 - a fixed injected Clock
    return compute_risk_metrics(
        weight_ratio_by_symbol={"BTCUSDT": Decimal("0.3"), "ETHUSDT": Decimal("0.2")},
        model=model,
        portfolio_daily_return_ratio_series=returns,
        confidence_ratio=Decimal("0.99"),
        asset_daily_return_ratio_series=returns,
        market_daily_return_ratio_series=btc_returns,
        stress_asset_daily_return_ratio_series=returns[-10:],
        stress_market_daily_return_ratio_series=btc_returns[-10:],
        volatility_floor_annualised_ratio=Decimal("0.10"),
        target_volatility_annualised_ratio=Decimal("0.12"),
        clock=clock,
    )


def test_the_snapshot_holds_the_exact_model_object_the_sizing_path_used() -> None:
    model = _model()
    snapshot = _snapshot_for(model)
    assert snapshot.risk_model is model


def test_a_recomputed_copy_with_equal_values_is_not_the_same_object() -> None:
    """The failure mode the acceptance criterion guards against, made concrete.

    `model_copy` is built from the same parameters as `model` and compares equal to it,
    but it is a distinct object. If `fking.risk.metrics` ever started recomputing or
    copying the model instead of threading the caller's object straight through, this is
    the assertion that would catch it: `is` would fail while `==` kept passing silently.
    """
    model = _model()
    model_copy = _model()
    assert model_copy == model
    assert model_copy is not model

    snapshot = _snapshot_for(model)
    assert snapshot.risk_model is model
    assert snapshot.risk_model is not model_copy
    assert snapshot.risk_model == model_copy  # equal by value; identity is the point, not this
