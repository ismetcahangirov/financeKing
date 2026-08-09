"""Properties of the correlation estimator, the floor, the PSD repair and the clustering.

Four guarantees are under test, and each one exists because its failure is silent:

**The estimator never emits a matrix that is not positive definite.** A near-singular
`Sigma` makes `w' Sigma w` come back slightly negative from rounding, `sqrt` of that
raises `InvalidOperation` inside the order path, and a size computed by dividing by
`sigma_p` is either a crash or an enormous number. Neither is discovered against a 2x2
matrix, which is why the generated matrices here are rank-deficient by construction --
more assets than the observation window can support.

**The floor is never undone.** Shrinkage pulls correlations toward their average, which
lowers the high ones; the repair raises them. Both run after the floor, so "the floored
value survived" is a property about the whole pipeline rather than about one step.

**The distance is a metric.** `sqrt(0.5 * (1 - rho))` obeys the triangle inequality and
`1 - rho` does not. Hierarchical clustering on a non-metric produces assignments that
depend on linkage order, which is how a nightly job stops being reproducible.

**Clustering is reproducible from the data alone.** There is no seed here, because there
is no randomness -- the assertion is that the digest is stable across runs.

`docs/rules/testing-rules.md` clause 2: property tests are mandatory for every function
in `fking.risk`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fking.domain import DomainError
from fking.risk.covariance import (
    CLUSTER_CUT_CORRELATION,
    MAX_ESTIMATED_CORRELATION,
    MINIMUM_OBSERVATIONS,
    CorrelationMatrix,
    RiskModel,
    RiskModelError,
    cluster_by_correlation,
    correlation_distance,
    estimate_risk_model,
    is_positive_definite,
)

pytestmark = [pytest.mark.property, pytest.mark.unit]

_SYMBOLS: Final[tuple[str, ...]] = ("ADAUSDT", "BNBUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT")

# Daily log-return magnitudes for large-cap crypto sit inside +/-25%; the bound keeps
# Hypothesis away from returns that no venue has ever printed while leaving the tails
# that break a covariance estimator well inside the generated range.
_DAILY_RETURN = st.decimals(
    min_value=Decimal("-0.25"),
    max_value=Decimal("0.25"),
    places=8,
    allow_nan=False,
    allow_infinity=False,
)


def _return_series(symbol_count: int) -> st.SearchStrategy[dict[str, tuple[Decimal, ...]]]:
    """Aligned return histories at exactly the minimum observation window.

    Deliberately at the minimum rather than above it: `MINIMUM_OBSERVATIONS` observations
    across five assets is where the sample covariance is closest to singular, and a
    covariance estimator only fails interestingly when it is short of data.
    """
    chosen = _SYMBOLS[:symbol_count]
    # At least one non-zero observation per series: a window in which a symbol did not
    # move at all is refused by the estimator on purpose (a halted symbol is not a
    # riskless one), and that refusal has its own example-based test.
    window = (
        st.lists(_DAILY_RETURN, min_size=MINIMUM_OBSERVATIONS, max_size=MINIMUM_OBSERVATIONS)
        .filter(lambda series: any(observation != Decimal("0") for observation in series))
        .map(tuple)
    )
    return st.fixed_dictionaries(dict.fromkeys(chosen, window))


def _flat_stress(symbols: tuple[str, ...], level: Decimal) -> dict[str, dict[str, Decimal]]:
    """A stress-correlation floor at one level for every pair."""
    return {
        symbol_a: {symbol_b: level for symbol_b in symbols if symbol_b != symbol_a}
        for symbol_a in symbols
    }


def _model_for(
    returns: dict[str, tuple[Decimal, ...]], *, stress_level: Decimal = Decimal("0.30")
) -> RiskModel:
    symbols = tuple(sorted(returns))
    return estimate_risk_model(
        daily_return_ratio_by_symbol=returns,
        stress_correlation_by_pair=_flat_stress(symbols, stress_level),
    )


@given(returns=st.integers(min_value=1, max_value=5).flatmap(_return_series))
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_estimated_matrix_is_positive_definite(returns: dict[str, tuple[Decimal, ...]]) -> None:
    """Cholesky succeeds, which for a symmetric matrix is exactly lambda_min > 0.

    Stated as Cholesky rather than as an eigenvalue because the two are equivalent and
    only one of them is implementable in `Decimal` without an iterative eigensolver.
    """
    model = _model_for(returns)
    assert is_positive_definite(model.correlations.rows())
    # Strictly positive, not merely non-negative: the smallest pivot is bounded below by
    # 1 - MAX_ESTIMATED_CORRELATION by construction of the repair target.
    for symbol in model.symbols:
        assert model.daily_volatility_ratio[symbol] > Decimal("0")


@given(
    returns=st.integers(min_value=2, max_value=5).flatmap(_return_series),
    stress_level=st.decimals(
        min_value=Decimal("-0.5"), max_value=Decimal("0.98"), places=4, allow_nan=False
    ),
)
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_used_correlation_is_never_below_the_stress_floor(
    returns: dict[str, tuple[Decimal, ...]], stress_level: Decimal
) -> None:
    """`rho_used >= rho_p95` for every pair, after shrinkage and after the PSD repair.

    Both later steps move correlations: shrinkage pulls them toward their mean, the
    repair pushes them toward the common level. Only the pipeline's output is a
    meaningful place to assert the floor.
    """
    model = _model_for(returns, stress_level=stress_level)
    floor = min(stress_level, MAX_ESTIMATED_CORRELATION)
    for symbol_a in model.symbols:
        for symbol_b in model.symbols:
            if symbol_a == symbol_b:
                continue
            assert model.correlations.between(symbol_a, symbol_b) >= floor


@given(returns=st.integers(min_value=1, max_value=5).flatmap(_return_series))
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_shrinkage_intensity_is_a_proportion(returns: dict[str, tuple[Decimal, ...]]) -> None:
    """The Ledoit-Wolf intensity is clamped into [0, 1].

    The unclamped ratio is a quotient of two noisy estimates and goes outside [0, 1]
    routinely on short windows. An intensity above 1 would extrapolate past the target
    and can leave the diagonal negative.
    """
    model = _model_for(returns)
    assert Decimal("0") <= model.shrinkage_intensity_ratio <= Decimal("1")
    assert Decimal("0") <= model.psd_repair_ratio <= Decimal("1")


@given(returns=st.integers(min_value=2, max_value=5).flatmap(_return_series))
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_correlation_distance_obeys_the_triangle_inequality(
    returns: dict[str, tuple[Decimal, ...]],
) -> None:
    """`d = sqrt(0.5 * (1 - rho))` is a proper distance; `1 - rho` is not.

    The inequality holds exactly in real arithmetic for any positive semi-definite
    correlation matrix, so the tolerance covers `Decimal` rounding only.
    """
    model = _model_for(returns)
    tolerance = Decimal("1e-25")
    for symbol_a in model.symbols:
        for symbol_b in model.symbols:
            for symbol_c in model.symbols:
                direct = correlation_distance(model.correlations.between(symbol_a, symbol_c))
                first = correlation_distance(model.correlations.between(symbol_a, symbol_b))
                second = correlation_distance(model.correlations.between(symbol_b, symbol_c))
                assert direct <= first + second + tolerance


@given(returns=st.integers(min_value=1, max_value=5).flatmap(_return_series))
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_clustering_partitions_the_universe_and_is_reproducible(
    returns: dict[str, tuple[Decimal, ...]],
) -> None:
    """Every symbol lands in exactly one cluster, and the digest is stable across runs."""
    first = _model_for(returns)
    second = _model_for(returns)

    assigned = sorted(symbol for cluster in first.clusters for symbol in cluster.symbols)
    assert assigned == sorted(first.symbols)
    assert first.cluster_digest == second.cluster_digest
    for cluster in first.clusters:
        assert cluster.cluster_id == min(cluster.symbols)


@given(
    stress_level=st.decimals(
        min_value=Decimal("0.91"), max_value=Decimal("0.98"), places=4, allow_nan=False
    )
)
@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_a_universe_floored_above_the_cut_collapses_to_one_cluster(
    stress_level: Decimal,
) -> None:
    """A floor above the cut level makes the whole universe one cluster, whatever the data.

    This is the case per-strategy limits pass and correlation-aware limits must not: five
    symbols, five separate books, one risk position.
    """
    returns = {
        symbol: tuple(
            Decimal(str(observation)) / Decimal("1000")
            for observation in range(index + 1, index + 1 + MINIMUM_OBSERVATIONS)
        )
        for index, symbol in enumerate(_SYMBOLS)
    }
    model = _model_for(returns, stress_level=stress_level)
    assert stress_level > CLUSTER_CUT_CORRELATION
    assert len(model.clusters) == 1
    assert model.clusters[0].symbols == _SYMBOLS


def test_risk_model_refuses_a_singular_correlation_matrix() -> None:
    """A degenerate matrix is refused at construction, not carried into the order path.

    The three-asset matrix below is a textbook indefinite one: two pairs strongly
    positive, the third strongly negative. `w' Sigma w` is negative for some `w`, so
    `sigma_p` would be the square root of a negative number inside `decide()`.
    """
    symbols = ("AAAUSDT", "BBBUSDT", "CCCUSDT")
    entries = {
        "AAAUSDT": {
            "AAAUSDT": Decimal("1"),
            "BBBUSDT": Decimal("0.95"),
            "CCCUSDT": Decimal("0.95"),
        },
        "BBBUSDT": {
            "AAAUSDT": Decimal("0.95"),
            "BBBUSDT": Decimal("1"),
            "CCCUSDT": Decimal("-0.95"),
        },
        "CCCUSDT": {
            "AAAUSDT": Decimal("0.95"),
            "BBBUSDT": Decimal("-0.95"),
            "CCCUSDT": Decimal("1"),
        },
    }
    matrix = CorrelationMatrix(symbols=symbols, entries=entries)
    assert not is_positive_definite(matrix.rows())

    with pytest.raises(RiskModelError, match="positive definite"):
        RiskModel(
            symbols=symbols,
            daily_volatility_ratio=dict.fromkeys(symbols, Decimal("0.02")),
            correlations=matrix,
            shrinkage_intensity_ratio=Decimal("0"),
            psd_repair_ratio=Decimal("0"),
            clusters=cluster_by_correlation(matrix, cut_correlation=CLUSTER_CUT_CORRELATION),
        )


def test_estimator_refuses_a_symbol_with_no_stress_correlation() -> None:
    """A missing floor is refused rather than defaulted.

    Defaulting to the calm estimate is exactly the failure the floor exists to prevent,
    and it would arrive silently on the newest listing -- the one with the least history
    and the most correlated behaviour.
    """
    returns = {
        symbol: tuple(Decimal(str(index + 1)) / Decimal("1000") for index in range(30))
        for symbol in ("BTCUSDT", "ETHUSDT")
    }
    with pytest.raises(RiskModelError, match="stress correlation"):
        estimate_risk_model(
            daily_return_ratio_by_symbol=returns,
            stress_correlation_by_pair={"BTCUSDT": {}, "ETHUSDT": {}},
        )


def test_estimator_refuses_a_window_shorter_than_the_minimum() -> None:
    """Fewer observations than the floor is a refusal, not a wider confidence interval."""
    returns = {
        "BTCUSDT": tuple(Decimal(str(index + 1)) / Decimal("1000") for index in range(5)),
    }
    with pytest.raises(RiskModelError, match="observations"):
        estimate_risk_model(
            daily_return_ratio_by_symbol=returns,
            stress_correlation_by_pair={"BTCUSDT": {}},
        )


def test_correlation_matrix_refuses_an_asymmetric_entry() -> None:
    """Asymmetry is a construction error; every downstream identity assumes it away."""
    entries = {
        "AAAUSDT": {"AAAUSDT": Decimal("1"), "BBBUSDT": Decimal("0.5")},
        "BBBUSDT": {"AAAUSDT": Decimal("0.4"), "BBBUSDT": Decimal("1")},
    }
    with pytest.raises(DomainError, match="symmetric"):
        CorrelationMatrix(symbols=("AAAUSDT", "BBBUSDT"), entries=entries)
