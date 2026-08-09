"""Properties of risk contribution, risk share and the cluster concentration limit.

The identity `sum_i CTR_i == sigma_p` is the one that has to hold numerically, because
every limit in this module is expressed as `CTR_i / sigma_p` and a decomposition that
does not sum back to the whole silently rescales every share. It is asserted here for
arbitrary weight vectors, including the ones nobody writes an example for: all zeros, a
single asset, exactly offsetting positions, and dust.

The second guarantee is that the division by `sigma_p` is always defined. That is not a
`try/except` -- it is a consequence of the estimator capping correlations strictly below
one, which makes `Sigma` positive definite and therefore `w' Sigma w > 0` for every
non-zero `w`. The property below is what keeps that consequence true.

`docs/rules/testing-rules.md` clause 2: property tests are mandatory for every function
in `fking.risk`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fking.domain import DomainError
from fking.risk.contribution import (
    CONCENTRATION_HARD_CEILINGS,
    RISK_CONTRIBUTION_TOLERANCE,
    ConcentrationLimits,
    assess_concentration,
    portfolio_risk,
    weight_ratios_from_notional,
)
from fking.risk.covariance import (
    CLUSTER_CUT_CORRELATION,
    MAX_ESTIMATED_CORRELATION,
    CorrelationMatrix,
    RiskModel,
    cluster_by_correlation,
)

pytestmark = [pytest.mark.property, pytest.mark.unit]

_AS_OF: Final = datetime(2026, 8, 1, tzinfo=UTC)
_SYMBOLS: Final[tuple[str, ...]] = ("ADAUSDT", "BNBUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT")
_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")


def _equicorrelated_model(
    symbols: tuple[str, ...],
    *,
    correlation: Decimal,
    daily_volatility_ratio: Decimal = Decimal("0.04"),
) -> RiskModel:
    """A model whose every pair sits at one correlation level.

    Equicorrelation is the shape the concentration limit is defined against: it has an
    exact closed form for `sigma_p`, so a property can check the implementation against
    arithmetic rather than against itself.
    """
    entries = {
        symbol_a: {
            symbol_b: (_ONE if symbol_a == symbol_b else correlation) for symbol_b in symbols
        }
        for symbol_a in symbols
    }
    matrix = CorrelationMatrix(symbols=symbols, entries=entries)
    return RiskModel(
        symbols=symbols,
        daily_volatility_ratio=dict.fromkeys(symbols, daily_volatility_ratio),
        correlations=matrix,
        shrinkage_intensity_ratio=_ZERO,
        psd_repair_ratio=_ZERO,
        clusters=cluster_by_correlation(matrix, cut_correlation=CLUSTER_CUT_CORRELATION),
    )


# Signed weights in units of equity. The range spans a flat book, dust, and a leveraged
# book well past the gross exposure ceiling -- the arithmetic must hold outside the range
# the exposure limits permit, because this function is also called on candidate books.
_WEIGHT = st.decimals(
    min_value=Decimal("-1.5"),
    max_value=Decimal("1.5"),
    places=10,
    allow_nan=False,
    allow_infinity=False,
)


def _weight_vectors(symbol_count: int) -> st.SearchStrategy[dict[str, Decimal]]:
    return st.fixed_dictionaries(dict.fromkeys(_SYMBOLS[:symbol_count], _WEIGHT))


@given(
    weight_ratio_by_symbol=st.integers(min_value=1, max_value=5).flatmap(_weight_vectors),
    correlation=st.decimals(
        min_value=Decimal("-0.2"), max_value=MAX_ESTIMATED_CORRELATION, places=4, allow_nan=False
    ),
)
@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_risk_contributions_sum_to_portfolio_volatility(
    weight_ratio_by_symbol: dict[str, Decimal], correlation: Decimal
) -> None:
    """`sum_i CTR_i == sigma_p` to the stated tolerance, for any weight vector."""
    model = _equicorrelated_model(tuple(sorted(weight_ratio_by_symbol)), correlation=correlation)
    risk = portfolio_risk(weight_ratio_by_symbol=weight_ratio_by_symbol, model=model)

    contribution_sum = sum(
        (entry.risk_contribution_ratio for entry in risk.contributions), start=_ZERO
    )
    tolerance = RISK_CONTRIBUTION_TOLERANCE * max(risk.portfolio_volatility_ratio, _ONE)
    assert abs(contribution_sum - risk.portfolio_volatility_ratio) <= tolerance


@given(
    weight_ratio_by_symbol=st.integers(min_value=1, max_value=5).flatmap(_weight_vectors),
    correlation=st.decimals(
        min_value=Decimal("-0.2"), max_value=MAX_ESTIMATED_CORRELATION, places=4, allow_nan=False
    ),
)
@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_portfolio_variance_is_never_negative_and_shares_sum_to_one(
    weight_ratio_by_symbol: dict[str, Decimal], correlation: Decimal
) -> None:
    """`w' Sigma w >= 0` always, and `sigma_p > 0` for every non-zero weight vector.

    The second half is the load-bearing one: it is what makes `CTR_i / sigma_p` defined
    without a guard, and it follows from the correlation cap rather than from luck.
    """
    model = _equicorrelated_model(tuple(sorted(weight_ratio_by_symbol)), correlation=correlation)
    risk = portfolio_risk(weight_ratio_by_symbol=weight_ratio_by_symbol, model=model)

    assert risk.portfolio_volatility_ratio >= _ZERO
    if any(weight != _ZERO for weight in weight_ratio_by_symbol.values()):
        assert risk.portfolio_volatility_ratio > _ZERO
        share_sum = sum((entry.risk_share_ratio for entry in risk.contributions), start=_ZERO)
        assert abs(share_sum - _ONE) <= RISK_CONTRIBUTION_TOLERANCE
    else:
        assert risk.portfolio_volatility_ratio == _ZERO
        assert all(entry.risk_share_ratio == _ZERO for entry in risk.contributions)


@given(
    weight_ratio_by_symbol=st.integers(min_value=1, max_value=5).flatmap(_weight_vectors),
    correlation=st.decimals(
        min_value=Decimal("-0.2"), max_value=MAX_ESTIMATED_CORRELATION, places=4, allow_nan=False
    ),
)
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_cluster_shares_partition_the_asset_shares(
    weight_ratio_by_symbol: dict[str, Decimal], correlation: Decimal
) -> None:
    """Every cluster's share is exactly the sum of its members', and every symbol is in one."""
    model = _equicorrelated_model(tuple(sorted(weight_ratio_by_symbol)), correlation=correlation)
    risk = portfolio_risk(weight_ratio_by_symbol=weight_ratio_by_symbol, model=model)

    assert sorted(risk.cluster_risk_share_ratio) == sorted(
        cluster.cluster_id for cluster in model.clusters
    )
    for cluster in model.clusters:
        member_sum = sum((risk.risk_share_of(symbol) for symbol in cluster.symbols), start=_ZERO)
        difference = abs(risk.cluster_risk_share_ratio[cluster.cluster_id] - member_sum)
        assert difference <= RISK_CONTRIBUTION_TOLERANCE


@given(
    weight=st.decimals(
        min_value=Decimal("-1.5"), max_value=Decimal("1.5"), places=10, allow_nan=False
    )
)
@settings(max_examples=200, deadline=None)
def test_a_single_asset_book_carries_the_entire_risk_share(weight: Decimal) -> None:
    """One position is 100% of the portfolio's risk, and `sigma_p` is `|w| * sigma`."""
    model = _equicorrelated_model(("BTCUSDT",), correlation=_ZERO)
    risk = portfolio_risk(weight_ratio_by_symbol={"BTCUSDT": weight}, model=model)

    expected = abs(weight) * model.daily_volatility_ratio["BTCUSDT"]
    assert abs(risk.portfolio_volatility_ratio - expected) <= RISK_CONTRIBUTION_TOLERANCE
    if weight != _ZERO:
        assert abs(risk.risk_share_of("BTCUSDT") - _ONE) <= RISK_CONTRIBUTION_TOLERANCE


@given(
    first_weight=st.decimals(
        min_value=Decimal("0.01"), max_value=Decimal("1"), places=8, allow_nan=False
    )
)
@settings(max_examples=200, deadline=None)
def test_perfectly_correlated_positions_share_risk_by_weight(first_weight: Decimal) -> None:
    """At the correlation cap, two long positions add rather than diversify.

    `sigma_p` for two equally volatile, perfectly correlated longs is `(w1 + w2) * sigma`,
    so a book that per-notional limits read as two positions is one position in risk
    terms. The cap keeps it just short of exactly one, which is what keeps `Sigma`
    invertible; the residual is asserted rather than assumed.
    """
    symbols = ("BTCUSDT", "ETHUSDT")
    model = _equicorrelated_model(symbols, correlation=MAX_ESTIMATED_CORRELATION)
    weights = {"BTCUSDT": first_weight, "ETHUSDT": Decimal("1") - first_weight + Decimal("0.5")}
    risk = portfolio_risk(weight_ratio_by_symbol=weights, model=model)

    volatility = model.daily_volatility_ratio["BTCUSDT"]
    perfectly_correlated = sum(weights.values(), start=_ZERO) * volatility
    # Within 0.1%: the only difference between the capped matrix and rho = 1 is the cap.
    assert abs(risk.portfolio_volatility_ratio - perfectly_correlated) <= (
        perfectly_correlated / Decimal("1000")
    )
    share_sum = sum((entry.risk_share_ratio for entry in risk.contributions), start=_ZERO)
    assert abs(share_sum - _ONE) <= RISK_CONTRIBUTION_TOLERANCE


@given(
    weight_ratio_by_symbol=st.integers(min_value=1, max_value=5).flatmap(_weight_vectors),
    correlation=st.decimals(
        min_value=Decimal("-0.2"), max_value=MAX_ESTIMATED_CORRELATION, places=4, allow_nan=False
    ),
)
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_an_assessment_records_every_limit_and_never_raises(
    weight_ratio_by_symbol: dict[str, Decimal], correlation: Decimal
) -> None:
    """A breach is a refusal carrying every evaluation, never an exception."""
    model = _equicorrelated_model(tuple(sorted(weight_ratio_by_symbol)), correlation=correlation)
    assessment = assess_concentration(
        weight_ratio_by_symbol=weight_ratio_by_symbol,
        model=model,
        limits=ConcentrationLimits(),
        clock=lambda: _AS_OF,
    )

    evaluated = {evaluation.limit_name for evaluation in assessment.evaluations}
    if any(weight != _ZERO for weight in weight_ratio_by_symbol.values()):
        assert evaluated == {"max_asset_risk_share_ratio", "max_cluster_risk_share_ratio"}
    assert assessment.is_approved == all(
        not evaluation.is_breached for evaluation in assessment.evaluations
    )
    if not assessment.is_approved:
        assert assessment.rejection is not None
        assert assessment.rejection.binding_limit_name in evaluated


def test_five_uncorrelated_looking_books_in_one_cluster_are_refused() -> None:
    """The case per-strategy notional limits pass and this limit must not.

    Five symbols, each at 4% of equity -- inside every per-position notional limit in
    `RISK_PHILOSOPHY.md` section 4 -- all sitting in one cluster above rho = 0.9. Per
    notional the book reads as diversified; per risk share it is one 20% position.
    """
    model = _equicorrelated_model(_SYMBOLS, correlation=Decimal("0.95"))
    weights = dict.fromkeys(_SYMBOLS, Decimal("0.04"))

    assessment = assess_concentration(
        weight_ratio_by_symbol=weights,
        model=model,
        limits=ConcentrationLimits(),
        clock=lambda: _AS_OF,
    )

    assert len(model.clusters) == 1
    assert not assessment.is_approved
    assert assessment.rejection is not None
    assert assessment.rejection.binding_limit_name == "max_cluster_risk_share_ratio"
    # No single asset breaches: each carries a fifth of the risk, inside the per-asset
    # ceiling. The cluster limit is the only thing standing between this book and a 20%
    # single-position equivalent.
    breached = {
        evaluation.limit_name for evaluation in assessment.evaluations if evaluation.is_breached
    }
    assert breached == {"max_cluster_risk_share_ratio"}


def test_limits_refuse_construction_above_the_hard_ceiling() -> None:
    """Configuration is bounded by a compiled-in ceiling, never clamped to it."""
    ceiling = CONCENTRATION_HARD_CEILINGS["max_cluster_risk_share_ratio"].bound
    with pytest.raises(ValueError, match="hard ceiling"):
        ConcentrationLimits(max_cluster_risk_share_ratio=ceiling + Decimal("0.01"))


@given(
    notional=st.decimals(
        min_value=Decimal("-50000"), max_value=Decimal("50000"), places=8, allow_nan=False
    )
)
@settings(max_examples=200, deadline=None)
def test_weight_ratios_are_signed_notional_over_equity(notional: Decimal) -> None:
    """The one place USD becomes a weight, so the sign convention is decided once."""
    equity_usd = Decimal("100000")
    weights: Mapping[str, Decimal] = weight_ratios_from_notional(
        signed_notional_usd_by_symbol={"BTCUSDT": notional}, equity_usd=equity_usd
    )
    assert weights["BTCUSDT"] == notional / equity_usd


def test_weight_ratios_refuse_non_positive_equity() -> None:
    """A wiped account cannot be expressed as a weight vector, and must not be guessed at."""
    with pytest.raises(DomainError, match="equity"):
        weight_ratios_from_notional(
            signed_notional_usd_by_symbol={"BTCUSDT": Decimal("100")}, equity_usd=_ZERO
        )
