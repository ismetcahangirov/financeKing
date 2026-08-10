"""Properties of `fking.risk.metrics`: VaR/CVaR ordering, beta window selection,
concentration boundedness and its response to clustering, and order-invariance.

`docs/rules/testing-rules.md` clause 2: property tests are mandatory for every function
in `fking.risk`. Four guarantees are checked here, each stated in the issue this module
implements (#56):

1. `CVaR_alpha >= VaR_alpha` for every return series and every confidence level, including
   degenerate (constant) and all-positive series -- explicit `@example`s pin both, general
   fuzzing covers the rest.
2. Concentration's Herfindahl index is bounded in `[0, 1]` for *any* cluster-share vector,
   not just the ones a well-behaved portfolio produces -- the module docstring in
   `fking.risk.metrics` explains why the naive `|CTR_i| / sigma_p` share would not be.
3. Concentration rises (never falls) as previously separate clusters merge into one at
   constant total notional.
4. VaR/CVaR and beta are invariant to the order observations arrive in -- a return series
   is a set of trading days, not a sequence whose position carries meaning once every
   observation is in hand.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from fking.risk.contribution import PortfolioRisk, portfolio_risk
from fking.risk.covariance import (
    CLUSTER_CUT_CORRELATION,
    MAX_ESTIMATED_CORRELATION,
    CorrelationMatrix,
    RiskModel,
    cluster_by_correlation,
)
from fking.risk.metrics import (
    beta_to_market,
    concentration_herfindahl,
    historical_tail_risk,
)

pytestmark = [pytest.mark.property, pytest.mark.unit]

_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")

_RETURN = st.decimals(
    min_value=Decimal("-0.90"),
    max_value=Decimal("0.90"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)
_CONFIDENCE = st.decimals(
    min_value=Decimal("0.500"), max_value=Decimal("0.999"), places=3, allow_nan=False
)
_ALL_POSITIVE_EXAMPLE: Final = [Decimal("0.01"), Decimal("0.02"), Decimal("0.005")]
_DEGENERATE_EXAMPLE: Final = [Decimal("0")] * 30


@given(returns=st.lists(_RETURN, min_size=1, max_size=60), confidence_ratio=_CONFIDENCE)
@example(returns=_DEGENERATE_EXAMPLE, confidence_ratio=Decimal("0.99"))
@example(returns=_ALL_POSITIVE_EXAMPLE, confidence_ratio=Decimal("0.99"))
@settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_cvar_is_never_less_severe_than_var(
    returns: list[Decimal], confidence_ratio: Decimal
) -> None:
    """`CVaR >= VaR` always: the tail mean can only be at least as bad as its boundary."""
    estimate = historical_tail_risk(returns, confidence_ratio=confidence_ratio)
    assert estimate.cvar_loss_ratio >= estimate.var_loss_ratio
    assert estimate.sample_size == len(returns)
    assert 1 <= estimate.tail_sample_size <= estimate.sample_size


@given(returns=st.lists(_RETURN, min_size=1, max_size=40), confidence_ratio=_CONFIDENCE)
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_tail_risk_is_invariant_to_observation_order(
    returns: list[Decimal], confidence_ratio: Decimal
) -> None:
    """A return series is a set of trading days; VaR/CVaR do not depend on arrival order."""
    forward = historical_tail_risk(returns, confidence_ratio=confidence_ratio)
    reversed_series = list(reversed(returns))
    backward = historical_tail_risk(reversed_series, confidence_ratio=confidence_ratio)
    assert forward == backward


_PAIR = st.tuples(_RETURN, _RETURN)


@given(
    full=st.lists(_PAIR, min_size=2, max_size=30),
    stress=st.lists(_PAIR, min_size=2, max_size=30),
)
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_beta_used_is_whichever_window_carries_the_larger_magnitude(
    full: list[tuple[Decimal, Decimal]], stress: list[tuple[Decimal, Decimal]]
) -> None:
    """The reported beta is never smaller in magnitude than either window's own beta."""
    full_market = tuple(market for _, market in full)
    if all(market == _ZERO for market in full_market):
        return  # market variance is zero; beta_to_market refuses this input by design
    stress_market = tuple(market for _, market in stress)
    if all(market == _ZERO for market in stress_market):
        return

    estimate = beta_to_market(
        asset_daily_return_ratio_series=tuple(asset for asset, _ in full),
        market_daily_return_ratio_series=full_market,
        stress_asset_daily_return_ratio_series=tuple(asset for asset, _ in stress),
        stress_market_daily_return_ratio_series=stress_market,
    )
    assert abs(estimate.beta_used_ratio) >= abs(estimate.full_sample_beta_ratio)
    assert abs(estimate.beta_used_ratio) >= abs(estimate.stress_window_beta_ratio)
    if estimate.used_stress_window:
        assert estimate.beta_used_ratio == estimate.stress_window_beta_ratio
        assert abs(estimate.stress_window_beta_ratio) > abs(estimate.full_sample_beta_ratio)
    else:
        assert estimate.beta_used_ratio == estimate.full_sample_beta_ratio


@given(full=st.lists(_PAIR, min_size=2, max_size=20))
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_beta_is_invariant_to_observation_order(full: list[tuple[Decimal, Decimal]]) -> None:
    """Permuting the paired observations together leaves the covariance ratio unchanged."""
    market = tuple(value for _, value in full)
    if all(value == _ZERO for value in market):
        return
    asset = tuple(value for value, _ in full)
    reversed_pairs = list(reversed(full))

    forward = beta_to_market(
        asset_daily_return_ratio_series=asset,
        market_daily_return_ratio_series=market,
        stress_asset_daily_return_ratio_series=asset,
        stress_market_daily_return_ratio_series=market,
    )
    backward = beta_to_market(
        asset_daily_return_ratio_series=tuple(a for a, _ in reversed_pairs),
        market_daily_return_ratio_series=tuple(m for _, m in reversed_pairs),
        stress_asset_daily_return_ratio_series=tuple(a for a, _ in reversed_pairs),
        stress_market_daily_return_ratio_series=tuple(m for _, m in reversed_pairs),
    )
    assert forward.full_sample_beta_ratio == backward.full_sample_beta_ratio


def _portfolio_risk_with_cluster_shares(shares: dict[str, Decimal]) -> PortfolioRisk:
    """A `PortfolioRisk` carrying exactly the supplied cluster shares.

    Constructed directly rather than through `portfolio_risk()`: the boundedness property
    below is a statement about `concentration_herfindahl`'s arithmetic for *any* share
    vector, including ones no correlation model would ever produce, so the input is built
    by hand against the public `PortfolioRisk` type rather than reverse-engineered from a
    model.
    """
    return PortfolioRisk(
        portfolio_volatility_ratio=_ONE,
        contributions=(),
        cluster_risk_share_ratio=shares,
    )


_SHARE = st.decimals(
    min_value=Decimal("-5"), max_value=Decimal("5"), places=6, allow_nan=False, allow_infinity=False
)


@given(
    shares=st.dictionaries(
        st.sampled_from(("c1", "c2", "c3", "c4", "c5")), _SHARE, min_size=1, max_size=5
    )
)
@settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_concentration_herfindahl_is_bounded_regardless_of_hedging(
    shares: dict[str, Decimal],
) -> None:
    """`herfindahl_ratio` sits in `[0, 1]` for *any* signed cluster-share vector.

    In particular for vectors a hedged book can produce, where the magnitudes do not sum
    to one -- the whole point of normalising by the sum of magnitudes rather than by
    `sigma_p` (`fking.risk.metrics.ConcentrationEstimate`'s docstring).
    """
    portfolio = _portfolio_risk_with_cluster_shares(shares)
    estimate = concentration_herfindahl(portfolio)
    assert _ZERO <= estimate.herfindahl_ratio <= _ONE
    held = sum(1 for share in shares.values() if share != _ZERO)
    assert estimate.sample_size == held
    if held > 0:
        assert estimate.herfindahl_ratio >= _ONE / Decimal(held)


def test_concentration_is_exactly_one_over_n_for_n_equal_uncorrelated_clusters() -> None:
    """The textbook Herfindahl floor: `n` equal shares give `1/n` exactly."""
    cluster_count = 4
    shares = {f"c{i}": Decimal("0.25") for i in range(cluster_count)}
    estimate = concentration_herfindahl(_portfolio_risk_with_cluster_shares(shares))
    assert estimate.herfindahl_ratio == Decimal("0.25")
    assert estimate.sample_size == cluster_count


def test_concentration_is_zero_for_an_entirely_flat_book() -> None:
    """A book with no cluster carrying weight reports zero concentration, not a refusal."""
    shares = {"c1": _ZERO, "c2": _ZERO}
    estimate = concentration_herfindahl(_portfolio_risk_with_cluster_shares(shares))
    assert estimate.herfindahl_ratio == _ZERO
    assert estimate.sample_size == 0


# -- monotonic response to merging clusters -----------------------------------------------

_INTER_BLOCK = st.decimals(
    min_value=Decimal("-0.20"), max_value=Decimal("0.99"), places=4, allow_nan=False
)


def _two_block_model(*, inter_block_correlation: Decimal) -> RiskModel:
    """Two blocks of two symbols, each internally at the correlation cap, joined at
    `inter_block_correlation`.

    Below the cluster cut (0.70) the blocks stay two separate clusters; at or above it they
    merge into one. Symmetric within each block, so the only thing that can move the
    Herfindahl index is the cluster count -- exactly the effect issue #56 asks to be
    demonstrated. Intra-block correlation is pinned at the estimator's cap (rather than a
    lower value) because that is what keeps the matrix positive definite across the whole
    `inter_block_correlation` range this test sweeps: a block structure is only PD while
    the inter-block term stays at or below the intra-block one.
    """
    symbols = ("A1", "A2", "B1", "B2")
    intra = MAX_ESTIMATED_CORRELATION

    def correlation(symbol_a: str, symbol_b: str) -> Decimal:
        if symbol_a == symbol_b:
            return _ONE
        same_block = symbol_a[0] == symbol_b[0]
        return intra if same_block else inter_block_correlation

    entries = {
        symbol_a: {symbol_b: correlation(symbol_a, symbol_b) for symbol_b in symbols}
        for symbol_a in symbols
    }
    matrix = CorrelationMatrix(symbols=symbols, entries=entries)
    return RiskModel(
        symbols=symbols,
        daily_volatility_ratio=dict.fromkeys(symbols, Decimal("0.04")),
        correlations=matrix,
        shrinkage_intensity_ratio=_ZERO,
        psd_repair_ratio=_ZERO,
        clusters=cluster_by_correlation(matrix, cut_correlation=CLUSTER_CUT_CORRELATION),
    )


_EQUAL_BLOCK_WEIGHTS: Final[dict[str, Decimal]] = {
    "A1": Decimal("0.1"),
    "A2": Decimal("0.1"),
    "B1": Decimal("0.1"),
    "B2": Decimal("0.1"),
}


@given(lower=_INTER_BLOCK, higher=_INTER_BLOCK)
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_concentration_rises_as_clusters_merge_at_constant_notional(
    lower: Decimal, higher: Decimal
) -> None:
    """Raising the correlation between two previously separate clusters never lowers HHI.

    Weights are held fixed -- constant total notional, per the issue's acceptance
    criterion -- and only the correlation structure changes. Two equal-notional,
    equal-volatility blocks read as two roughly-equal clusters below the cut and as one
    cluster at or above it, and merging is a textbook Herfindahl increase.
    """
    if lower > higher:
        lower, higher = higher, lower

    lower_risk = portfolio_risk(
        weight_ratio_by_symbol=_EQUAL_BLOCK_WEIGHTS,
        model=_two_block_model(inter_block_correlation=lower),
    )
    higher_risk = portfolio_risk(
        weight_ratio_by_symbol=_EQUAL_BLOCK_WEIGHTS,
        model=_two_block_model(inter_block_correlation=higher),
    )

    lower_estimate = concentration_herfindahl(lower_risk)
    higher_estimate = concentration_herfindahl(higher_risk)
    assert higher_estimate.herfindahl_ratio >= lower_estimate.herfindahl_ratio


def test_concentration_jumps_from_two_clusters_to_one_across_the_cut() -> None:
    """A concrete instance of the property above, with the cluster count asserted directly."""
    two_separate_clusters = 2
    one_merged_cluster = 1

    below = _two_block_model(inter_block_correlation=Decimal("0.10"))
    above = _two_block_model(inter_block_correlation=Decimal("0.95"))
    assert len(below.clusters) == two_separate_clusters
    assert len(above.clusters) == one_merged_cluster

    below_estimate = concentration_herfindahl(
        portfolio_risk(weight_ratio_by_symbol=_EQUAL_BLOCK_WEIGHTS, model=below)
    )
    above_estimate = concentration_herfindahl(
        portfolio_risk(weight_ratio_by_symbol=_EQUAL_BLOCK_WEIGHTS, model=above)
    )
    assert above_estimate.herfindahl_ratio > below_estimate.herfindahl_ratio
    assert above_estimate.herfindahl_ratio == _ONE
