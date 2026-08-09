"""The refusal paths of the correlation estimator, and the cases with a known answer.

Property tests cover the invariants; these cover the specific inputs whose *correct*
behaviour is a refusal. A refusal path that is never exercised is a refusal path that
raises `AttributeError` the first time it fires, in the middle of a rebalance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

import pytest

from fking.domain import DomainError
from fking.risk.contribution import (
    ConcentrationLimits,
    assess_concentration,
    portfolio_risk,
    weight_ratios_from_notional,
)
from fking.risk.covariance import (
    CLUSTER_CUT_CORRELATION,
    MAX_ESTIMATED_CORRELATION,
    MINIMUM_OBSERVATIONS,
    CorrelationCluster,
    CorrelationMatrix,
    RiskModel,
    RiskModelError,
    cluster_by_correlation,
    correlation_distance,
    estimate_risk_model,
    is_positive_definite,
)

pytestmark = pytest.mark.unit

_AS_OF: Final = datetime(2026, 8, 1, tzinfo=UTC)
_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")


def _oscillating(*, amplitude: str, phase: int) -> tuple[Decimal, ...]:
    """A deterministic return series with non-zero variance and a controllable phase.

    Two series with the same phase are perfectly correlated; two with opposite phase are
    perfectly anti-correlated. That is what lets a cluster assignment be asserted against
    an expected answer rather than against whatever the estimator produced.
    """
    magnitude = Decimal(amplitude)
    return tuple(
        magnitude if (step + phase) % 2 == 0 else -magnitude for step in range(MINIMUM_OBSERVATIONS)
    )


def _flat_stress(symbols: tuple[str, ...], level: str) -> dict[str, dict[str, Decimal]]:
    return {
        symbol_a: {symbol_b: Decimal(level) for symbol_b in symbols if symbol_b != symbol_a}
        for symbol_a in symbols
    }


def _equicorrelated(symbols: tuple[str, ...], correlation: Decimal) -> RiskModel:
    entries = {
        symbol_a: {
            symbol_b: (_ONE if symbol_a == symbol_b else correlation) for symbol_b in symbols
        }
        for symbol_a in symbols
    }
    matrix = CorrelationMatrix(symbols=symbols, entries=entries)
    return RiskModel(
        symbols=symbols,
        daily_volatility_ratio=dict.fromkeys(symbols, Decimal("0.03")),
        correlations=matrix,
        shrinkage_intensity_ratio=_ZERO,
        psd_repair_ratio=_ZERO,
        clusters=cluster_by_correlation(matrix, cut_correlation=CLUSTER_CUT_CORRELATION),
    )


def test_a_halted_symbol_is_refused_rather_than_priced_as_riskless() -> None:
    """A 60-day window with no movement is a halt, and a halt is not zero volatility.

    Treating it as riskless is the most dangerous available reading: it would divide by
    zero to get the symbol's correlations and then report it as free risk capacity.
    """
    with pytest.raises(RiskModelError, match="zero estimated variance"):
        estimate_risk_model(
            daily_return_ratio_by_symbol={"BTCUSDT": (_ZERO,) * MINIMUM_OBSERVATIONS},
            stress_correlation_by_pair={"BTCUSDT": {}},
        )


def test_unaligned_histories_are_refused() -> None:
    """Series of differing length cannot be a covariance; it would pair different days."""
    with pytest.raises(RiskModelError, match="differing lengths"):
        estimate_risk_model(
            daily_return_ratio_by_symbol={
                "BTCUSDT": _oscillating(amplitude="0.02", phase=0),
                "ETHUSDT": _oscillating(amplitude="0.02", phase=0)[:-1],
            },
            stress_correlation_by_pair=_flat_stress(("BTCUSDT", "ETHUSDT"), "0.5"),
        )


def test_an_empty_universe_is_refused() -> None:
    with pytest.raises(RiskModelError, match="no return series"):
        estimate_risk_model(daily_return_ratio_by_symbol={}, stress_correlation_by_pair={})


def test_a_non_finite_observation_is_refused() -> None:
    """`NaN` reaching the estimator would propagate into every correlation silently."""
    series = (Decimal("NaN"), *_oscillating(amplitude="0.02", phase=0)[1:])
    with pytest.raises(RiskModelError, match="non-finite"):
        estimate_risk_model(
            daily_return_ratio_by_symbol={"BTCUSDT": series},
            stress_correlation_by_pair={"BTCUSDT": {}},
        )


def test_a_stress_correlation_that_disagrees_with_itself_is_refused() -> None:
    """`rho_p95` is symmetric by construction; a disagreement means it was built wrongly."""
    with pytest.raises(RiskModelError, match="disagrees with itself"):
        estimate_risk_model(
            daily_return_ratio_by_symbol={
                "BTCUSDT": _oscillating(amplitude="0.02", phase=0),
                "ETHUSDT": _oscillating(amplitude="0.03", phase=1),
            },
            stress_correlation_by_pair={
                "BTCUSDT": {"ETHUSDT": Decimal("0.6")},
                "ETHUSDT": {"BTCUSDT": Decimal("0.7")},
            },
        )


def test_a_stress_correlation_outside_the_unit_interval_is_refused() -> None:
    with pytest.raises(RiskModelError, match="outside"):
        estimate_risk_model(
            daily_return_ratio_by_symbol={
                "BTCUSDT": _oscillating(amplitude="0.02", phase=0),
                "ETHUSDT": _oscillating(amplitude="0.03", phase=1),
            },
            stress_correlation_by_pair={"BTCUSDT": {"ETHUSDT": Decimal("1.4")}},
        )


def test_a_rank_deficient_window_still_produces_a_usable_model() -> None:
    """More assets than the window can support is the ordinary case, not the exotic one.

    Twelve symbols over the minimum window gives a singular sample covariance. The
    estimator must still hand back something `decide()` can divide by.
    """
    symbols = tuple(f"SYM{index:02d}USDT" for index in range(12))
    returns = {
        symbol: _oscillating(amplitude=f"0.0{index + 1}", phase=index % 3)
        for index, symbol in enumerate(symbols)
    }
    model = estimate_risk_model(
        daily_return_ratio_by_symbol=returns,
        stress_correlation_by_pair=_flat_stress(symbols, "0.35"),
    )

    assert is_positive_definite(model.correlations.rows())
    for symbol_a in symbols:
        for symbol_b in symbols:
            if symbol_a != symbol_b:
                assert model.correlations.between(symbol_a, symbol_b) >= Decimal("0.35")


def test_a_floor_that_breaks_definiteness_is_repaired_upward() -> None:
    """The repair raises correlations; it never lowers one back through its floor.

    A uniform floor rarely breaks definiteness -- it pulls the matrix toward
    equicorrelation, which is where the PSD cone is. The case that breaks it is an uneven
    one: two pairs floored high while the third keeps a strongly negative sample value,
    which is not a consistent set of correlations for any three assets. The repair has to
    fix that without undoing either floor, so it can only move upward.
    """
    symbols = ("AAAUSDT", "BBBUSDT", "CCCUSDT")
    returns = {
        "AAAUSDT": _oscillating(amplitude="0.02", phase=0),
        "BBBUSDT": _oscillating(amplitude="0.03", phase=0),
        "CCCUSDT": _oscillating(amplitude="0.04", phase=1),
    }
    floors = {
        "AAAUSDT": {"BBBUSDT": Decimal("0.95"), "CCCUSDT": Decimal("0.95")},
        "BBBUSDT": {"AAAUSDT": Decimal("0.95"), "CCCUSDT": Decimal("-0.99")},
        "CCCUSDT": {"AAAUSDT": Decimal("0.95"), "BBBUSDT": Decimal("-0.99")},
    }
    model = estimate_risk_model(
        daily_return_ratio_by_symbol=returns, stress_correlation_by_pair=floors
    )

    assert model.psd_repair_ratio > _ZERO
    assert is_positive_definite(model.correlations.rows())
    for symbol_a in symbols:
        for symbol_b in symbols:
            if symbol_a != symbol_b:
                assert model.correlations.between(symbol_a, symbol_b) >= floors[symbol_a][symbol_b]


def test_anti_correlated_symbols_land_in_separate_clusters() -> None:
    """Clustering has to be able to say 'these are different', or the limit is a tax.

    Two pairs, each internally perfectly correlated and mutually anti-correlated, with a
    stress floor below the cut level so the floor does not decide the answer by itself.
    """
    symbols = ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT")
    returns = {
        "AAAUSDT": _oscillating(amplitude="0.02", phase=0),
        "BBBUSDT": _oscillating(amplitude="0.03", phase=0),
        "CCCUSDT": _oscillating(amplitude="0.02", phase=1),
        "DDDUSDT": _oscillating(amplitude="0.03", phase=1),
    }
    model = estimate_risk_model(
        daily_return_ratio_by_symbol=returns,
        stress_correlation_by_pair=_flat_stress(symbols, "-0.2"),
    )

    expected_cluster_count = 2
    assert len(model.clusters) == expected_cluster_count
    assert {cluster.symbols for cluster in model.clusters} == {
        ("AAAUSDT", "BBBUSDT"),
        ("CCCUSDT", "DDDUSDT"),
    }
    assert model.cluster_of("DDDUSDT") == "CCCUSDT"


def test_shrinkage_pulls_a_dispersed_sample_toward_its_mean() -> None:
    """The Ledoit-Wolf term is live, not a constant zero wired in by accident."""
    symbols = ("AAAUSDT", "BBBUSDT", "CCCUSDT")
    returns = {
        "AAAUSDT": _oscillating(amplitude="0.02", phase=0),
        "BBBUSDT": _oscillating(amplitude="0.03", phase=0),
        "CCCUSDT": _oscillating(amplitude="0.05", phase=1),
    }
    model = estimate_risk_model(
        daily_return_ratio_by_symbol=returns,
        stress_correlation_by_pair=_flat_stress(symbols, "-0.9"),
    )
    assert model.shrinkage_intensity_ratio > _ZERO


def test_the_correlation_cap_bounds_a_perfectly_correlated_pair() -> None:
    """Identical histories are modelled at the cap, which is what keeps Sigma invertible."""
    symbols = ("AAAUSDT", "BBBUSDT")
    series = _oscillating(amplitude="0.02", phase=0)
    model = estimate_risk_model(
        daily_return_ratio_by_symbol=dict.fromkeys(symbols, series),
        stress_correlation_by_pair=_flat_stress(symbols, "0.5"),
    )
    assert model.correlations.between("AAAUSDT", "BBBUSDT") == MAX_ESTIMATED_CORRELATION
    assert is_positive_definite(model.correlations.rows())


def test_correlation_matrix_refuses_malformed_input() -> None:
    """Every structural invariant is checked at construction, not assumed downstream."""
    with pytest.raises(DomainError, match="not a matrix"):
        CorrelationMatrix(symbols=(), entries={})
    with pytest.raises(DomainError, match="sorted"):
        CorrelationMatrix(
            symbols=("BBBUSDT", "AAAUSDT"),
            entries={
                "AAAUSDT": {"AAAUSDT": _ONE, "BBBUSDT": _ZERO},
                "BBBUSDT": {"AAAUSDT": _ZERO, "BBBUSDT": _ONE},
            },
        )
    with pytest.raises(DomainError, match="unique"):
        CorrelationMatrix(symbols=("AAAUSDT", "AAAUSDT"), entries={"AAAUSDT": {"AAAUSDT": _ONE}})
    with pytest.raises(DomainError, match="no correlation row"):
        CorrelationMatrix(symbols=("AAAUSDT",), entries={})
    with pytest.raises(DomainError, match="no correlation between"):
        CorrelationMatrix(
            symbols=("AAAUSDT", "BBBUSDT"),
            entries={"AAAUSDT": {"AAAUSDT": _ONE}, "BBBUSDT": {"BBBUSDT": _ONE}},
        )
    with pytest.raises(DomainError, match="outside"):
        CorrelationMatrix(symbols=("AAAUSDT",), entries={"AAAUSDT": {"AAAUSDT": Decimal("1.2")}})
    with pytest.raises(DomainError, match="diagonal"):
        CorrelationMatrix(symbols=("AAAUSDT",), entries={"AAAUSDT": {"AAAUSDT": Decimal("0.9")}})


def test_correlation_matrix_refuses_a_symbol_it_does_not_hold() -> None:
    matrix = _equicorrelated(("AAAUSDT", "BBBUSDT"), Decimal("0.5")).correlations
    with pytest.raises(DomainError, match="outside the estimated universe"):
        matrix.between("AAAUSDT", "ZZZUSDT")


def test_correlation_distance_refuses_an_impossible_correlation() -> None:
    assert correlation_distance(_ONE) == _ZERO
    with pytest.raises(DomainError, match="outside"):
        correlation_distance(Decimal("1.5"))


def test_cluster_refuses_a_malformed_membership() -> None:
    with pytest.raises(DomainError, match="empty cluster"):
        CorrelationCluster(cluster_id="AAAUSDT", symbols=())
    with pytest.raises(DomainError, match="sorted"):
        CorrelationCluster(cluster_id="AAAUSDT", symbols=("BBBUSDT", "AAAUSDT"))
    with pytest.raises(DomainError, match="smallest member"):
        CorrelationCluster(cluster_id="BBBUSDT", symbols=("AAAUSDT", "BBBUSDT"))


def test_risk_model_refuses_an_inconsistent_assembly() -> None:
    """The model is the object `decide()` trusts; it validates rather than assumes."""
    matrix = _equicorrelated(("AAAUSDT", "BBBUSDT"), Decimal("0.5")).correlations
    clusters = cluster_by_correlation(matrix, cut_correlation=CLUSTER_CUT_CORRELATION)
    volatilities = dict.fromkeys(("AAAUSDT", "BBBUSDT"), Decimal("0.03"))

    with pytest.raises(DomainError, match="disagree with the correlation matrix"):
        RiskModel(
            symbols=("AAAUSDT",),
            daily_volatility_ratio=volatilities,
            correlations=matrix,
            shrinkage_intensity_ratio=_ZERO,
            psd_repair_ratio=_ZERO,
            clusters=clusters,
        )
    with pytest.raises(DomainError, match="must not be assumed calm"):
        RiskModel(
            symbols=("AAAUSDT", "BBBUSDT"),
            daily_volatility_ratio={"AAAUSDT": Decimal("0.03"), "BBBUSDT": _ZERO},
            correlations=matrix,
            shrinkage_intensity_ratio=_ZERO,
            psd_repair_ratio=_ZERO,
            clusters=clusters,
        )
    with pytest.raises(DomainError, match="partition the universe"):
        RiskModel(
            symbols=("AAAUSDT", "BBBUSDT"),
            daily_volatility_ratio=volatilities,
            correlations=matrix,
            shrinkage_intensity_ratio=_ZERO,
            psd_repair_ratio=_ZERO,
            clusters=(CorrelationCluster(cluster_id="AAAUSDT", symbols=("AAAUSDT",)),),
        )


def test_cluster_of_refuses_a_symbol_outside_the_universe() -> None:
    model = _equicorrelated(("AAAUSDT", "BBBUSDT"), Decimal("0.5"))
    with pytest.raises(DomainError, match="outside the estimated universe"):
        model.cluster_of("ZZZUSDT")


def test_a_weight_on_an_uncovered_symbol_is_refused() -> None:
    """Dropping it would understate sigma_p by exactly the position nobody has priced."""
    model = _equicorrelated(("AAAUSDT", "BBBUSDT"), Decimal("0.5"))
    with pytest.raises(DomainError, match="outside the estimated universe"):
        portfolio_risk(weight_ratio_by_symbol={"ZZZUSDT": Decimal("0.1")}, model=model)


def test_a_flat_book_has_no_risk_share_and_raises_nothing() -> None:
    """The zero-weight case is the one that divides by zero if it is not handled."""
    model = _equicorrelated(("AAAUSDT", "BBBUSDT"), Decimal("0.5"))
    risk = portfolio_risk(weight_ratio_by_symbol={"AAAUSDT": _ZERO, "BBBUSDT": _ZERO}, model=model)

    assert risk.is_degenerate
    assert risk.portfolio_volatility_ratio == _ZERO
    assert risk.risk_share_of("AAAUSDT") == _ZERO
    assert risk.risk_share_of("ZZZUSDT") == _ZERO
    assert risk.cluster_risk_share_ratio == {"AAAUSDT": _ZERO, "BBBUSDT": _ZERO}


def test_a_flat_book_is_approved_with_no_rows_to_evaluate() -> None:
    model = _equicorrelated(("AAAUSDT", "BBBUSDT"), Decimal("0.5"))
    assessment = assess_concentration(
        weight_ratio_by_symbol={"AAAUSDT": _ZERO},
        model=model,
        limits=ConcentrationLimits(),
        clock=lambda: _AS_OF,
    )
    assert assessment.is_approved
    assert assessment.evaluations == ()


def test_a_hedged_pair_breaches_the_asset_row_the_cluster_row_nets_away() -> None:
    """The case the per-asset risk-share limit exists for, and the only one.

    A near-perfectly-correlated long/short pair inside one cluster nets to a small cluster
    share while one leg carries more than half the risk of the whole book. Every cluster
    row here passes. A cluster limit on its own would approve this book.
    """
    hedged = ("AAAUSDT", "BBBUSDT")
    independent = ("CCCUSDT", "DDDUSDT", "EEEUSDT", "FFFUSDT")
    symbols = hedged + independent

    def correlation_between(symbol_a: str, symbol_b: str) -> Decimal:
        if symbol_a == symbol_b:
            return _ONE
        if symbol_a in hedged and symbol_b in hedged:
            return Decimal("0.99")
        return _ZERO

    matrix = CorrelationMatrix(
        symbols=symbols,
        entries={
            symbol_a: {symbol_b: correlation_between(symbol_a, symbol_b) for symbol_b in symbols}
            for symbol_a in symbols
        },
    )
    model = RiskModel(
        symbols=symbols,
        daily_volatility_ratio=dict.fromkeys(symbols, Decimal("0.03")),
        correlations=matrix,
        shrinkage_intensity_ratio=_ZERO,
        psd_repair_ratio=_ZERO,
        clusters=cluster_by_correlation(matrix, cut_correlation=CLUSTER_CUT_CORRELATION),
    )
    weights = {"AAAUSDT": _ONE, "BBBUSDT": Decimal("-0.9")} | dict.fromkeys(
        independent, Decimal("0.2")
    )

    assessment = assess_concentration(
        weight_ratio_by_symbol=weights,
        model=model,
        limits=ConcentrationLimits(),
        clock=lambda: _AS_OF,
    )

    limits = ConcentrationLimits()
    for cluster in model.clusters:
        assert (
            assessment.portfolio_risk.cluster_risk_share_ratio[cluster.cluster_id]
            <= limits.max_cluster_risk_share_ratio
        )
    assert not assessment.is_approved
    assert assessment.rejection is not None
    assert assessment.rejection.binding_limit_name == "max_asset_risk_share_ratio"
    payload = assessment.audit_payload()
    assert payload["verdict"] == "rejected"
    assert payload["binding_limit_name"] == "max_asset_risk_share_ratio"
    # Everything in the payload is a string, because it lands in jsonb and a JSON encoder
    # that has not been told otherwise turns a Decimal into a float on the way in.
    assert isinstance(payload["portfolio_volatility_ratio"], str)


def test_an_approved_assessment_still_records_every_limit() -> None:
    """Five genuinely uncorrelated names at equal weight: 20% of risk each, inside both rows.

    Two positions cannot pass a 25% cluster limit however uncorrelated they are -- each is
    half the book's risk. That is the limit working, not a test fixture problem, and it is
    the arithmetic behind "new strategies earn allocation by being different".
    """
    symbols = ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT")
    model = _equicorrelated(symbols, _ZERO)
    assessment = assess_concentration(
        weight_ratio_by_symbol=dict.fromkeys(symbols, Decimal("0.05")),
        model=model,
        limits=ConcentrationLimits(),
        clock=lambda: _AS_OF,
    )

    assert assessment.is_approved
    payload = assessment.audit_payload()
    assert payload["verdict"] == "approved"
    assert payload["binding_limit_name"] is None
    # Five asset rows and five singleton cluster rows: every limit that governs the
    # book is recorded, not only the one that bound.
    expected_row_count = 10
    assert len(assessment.evaluations) == expected_row_count
    assert all(not evaluation.is_breached for evaluation in assessment.evaluations)
    assert all(evaluation.headroom_ratio > _ZERO for evaluation in assessment.evaluations)


def test_weights_are_signed_fractions_of_equity() -> None:
    weights = weight_ratios_from_notional(
        signed_notional_usd_by_symbol={
            "AAAUSDT": Decimal("5000"),
            "BBBUSDT": Decimal("-2500"),
        },
        equity_usd=Decimal("100000"),
    )
    assert weights == {"AAAUSDT": Decimal("0.05"), "BBBUSDT": Decimal("-0.025")}


def test_weights_refuse_a_wiped_account() -> None:
    with pytest.raises(DomainError, match="equity"):
        weight_ratios_from_notional(
            signed_notional_usd_by_symbol={"AAAUSDT": Decimal("1")},
            equity_usd=Decimal("-1"),
        )
