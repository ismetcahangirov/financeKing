"""The regime breakdown, the reading order, and the two fields that cannot be dropped.

An aggregate Sharpe of 1.2 that is 3.0 in one regime and -0.4 in another is a regime bet
wearing a strategy's clothes, and the aggregate conceals that completely -- so the tests
here assert that every metric is emitted per bucket, that a thin bucket is flagged rather
than deleted, that credibility is read before the Sharpe, and that a run which breached a
risk limit has no path to being reported as clean.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

import pytest

from fking.backtest.portfolio import (
    MIN_REGIME_EFFECTIVE_SAMPLE,
    Credibility,
    RegimeCoverage,
    ReportSection,
    RiskLimitBreachedError,
    assemble_report,
    regime_breakdown,
    require_clean_result,
)
from tests.backtest.portfolio_support import (
    grid_day,
    path_from_returns,
    state_with_fill_count,
)

pytestmark = pytest.mark.unit

_REGIME_DAYS: Final = 30
_THIN_BUCKET_DAYS: Final = 9
_BREACH_COUNT: Final = 2


def _two_regime_returns() -> tuple[tuple[Decimal, ...], tuple[str, ...]]:
    """Thirty calm days that grind upward, then thirty stressed days that do not.

    Deliberately shaped so the aggregate hides the split: the two halves roughly cancel,
    and a reader of the aggregate alone would see a mediocre strategy rather than a
    regime bet.
    """
    calm = tuple(
        Decimal("0.004") if index % 3 else Decimal("-0.001") for index in range(_REGIME_DAYS)
    )
    stressed = tuple(
        Decimal("-0.006") if index % 3 else Decimal("0.002") for index in range(_REGIME_DAYS)
    )
    return calm + stressed, ("calm",) * _REGIME_DAYS + ("stressed",) * _REGIME_DAYS


def test_every_metric_is_emitted_for_every_regime_bucket() -> None:
    """The breakdown carries the same suite as the whole path, per bucket."""
    return_fractions, regimes = _two_regime_returns()
    path = path_from_returns(return_fractions, regimes=regimes)
    buckets = regime_breakdown(path)

    assert [bucket.regime for bucket in buckets] == ["calm", "stressed"]
    for bucket in buckets:
        assert bucket.risk is not None
        assert bucket.statistics is not None
        assert bucket.economics.observation_count == _REGIME_DAYS
        assert bucket.n_eff is not None
        assert bucket.regime_coverage is RegimeCoverage.SUFFICIENT

    calm, stressed = buckets
    assert calm.statistics is not None
    assert stressed.statistics is not None
    assert calm.statistics.sharpe_ratio is not None
    assert stressed.statistics.sharpe_ratio is not None
    # The finding the aggregate conceals: opposite signs either side of the split.
    assert calm.statistics.sharpe_ratio > Decimal("0")
    assert stressed.statistics.sharpe_ratio < Decimal("0")


def test_a_bucket_below_the_effective_sample_floor_is_flagged_and_not_dropped() -> None:
    """`regime_coverage` says THIN and the numbers are printed anyway."""
    return_fractions = tuple(
        Decimal("0.003") if index % 2 else Decimal("-0.002") for index in range(40)
    )
    # Nine days of a second regime: enough to estimate a sample, not enough to weigh.
    regimes = ("calm",) * 31 + ("shock",) * _THIN_BUCKET_DAYS
    path = path_from_returns(return_fractions, regimes=regimes)
    buckets = {bucket.regime: bucket for bucket in regime_breakdown(path)}

    assert set(buckets) == {"calm", "shock"}
    shock = buckets["shock"]
    assert shock.observation_count == _THIN_BUCKET_DAYS
    assert shock.regime_coverage is RegimeCoverage.THIN
    assert shock.n_eff is not None
    assert shock.n_eff < MIN_REGIME_EFFECTIVE_SAMPLE
    assert shock.statistics is not None
    assert buckets["calm"].regime_coverage is RegimeCoverage.SUFFICIENT


def test_a_single_day_bucket_reports_its_return_and_no_dispersion() -> None:
    """One day has no dispersion at all, so the ratios are absent rather than invented."""
    return_fractions = tuple(
        Decimal("0.003") if index % 2 else Decimal("-0.002") for index in range(20)
    )
    regimes = ("calm",) * 19 + ("halt",)
    buckets = {
        bucket.regime: bucket
        for bucket in regime_breakdown(path_from_returns(return_fractions, regimes=regimes))
    }

    halt = buckets["halt"]
    assert halt.observation_count == 1
    assert halt.risk is None
    assert halt.statistics is None
    assert halt.n_eff is None
    assert halt.regime_coverage is RegimeCoverage.THIN
    assert halt.economics.total_return_fraction != Decimal("0")


def test_the_report_names_its_thin_regimes_in_the_credibility_section() -> None:
    """A reader must not have to scan the breakdown to learn the evidence is thin."""
    return_fractions = tuple(
        Decimal("0.003") if index % 2 else Decimal("-0.002") for index in range(40)
    )
    regimes = ("calm",) * 31 + ("shock",) * _THIN_BUCKET_DAYS
    report = assemble_report(
        path=path_from_returns(return_fractions, regimes=regimes),
        final_state=state_with_fill_count(4),
    )

    assert report.credibility.thin_regimes == ("shock",)


def test_the_report_is_read_credibility_first_and_never_leads_with_the_sharpe() -> None:
    """The order is a property of the type, not of the caller's formatting."""
    return_fractions, regimes = _two_regime_returns()
    report = assemble_report(
        path=path_from_returns(return_fractions, regimes=regimes),
        final_state=state_with_fill_count(12),
    )
    lines = report.summary_lines()

    assert lines[0] == f"[{ReportSection.CREDIBILITY.value}]"
    statistics_at = lines.index(f"[{ReportSection.STATISTICS.value}]")
    before_statistics = lines[:statistics_at]
    assert not any("sharpe" in line for line in before_statistics)
    assert any(line.startswith("time_in_market_pct=") for line in before_statistics)
    assert any(line.startswith("risk_limit_breach_count=") for line in before_statistics)


def test_time_in_market_travels_with_the_score_and_reflects_the_flat_days() -> None:
    """A strategy in the market a fifth of the time says so, in the credibility block."""
    return_fractions = tuple(
        Decimal("0.002") if index % 5 == 0 else Decimal("0") for index in range(20)
    )
    path = path_from_returns(return_fractions, is_in_market=False)
    assert path.time_in_market_pct == Decimal("0")

    engaged = path_from_returns(return_fractions, is_in_market=True)
    assert engaged.time_in_market_pct == Decimal("100")


def test_credibility_cannot_be_constructed_without_its_qualifying_fields() -> None:
    """No defaults: the two fields that qualify every number cannot be dropped."""
    with pytest.raises(TypeError, match="time_in_market_pct"):
        Credibility(  # type: ignore[call-arg]  # the omission is the assertion
            observation_count=20,
            effective_sample=None,
            fill_count=3,
            risk_limit_breach_count=0,
            thin_regimes=(),
        )


def test_a_breached_run_cannot_be_reported_as_a_clean_result() -> None:
    """A hard negative. `is_clean` is derived, and there is no field that overrides it."""
    return_fractions, regimes = _two_regime_returns()
    breached = state_with_fill_count(7).with_risk_limit_breach(
        occurs_at_utc=grid_day(3), breach_count=_BREACH_COUNT
    )
    report = assemble_report(
        path=path_from_returns(return_fractions, regimes=regimes), final_state=breached
    )

    assert report.credibility.risk_limit_breach_count == _BREACH_COUNT
    assert report.is_clean is False
    assert "is_clean=False" in report.summary_lines()
    with pytest.raises(RiskLimitBreachedError, match="cannot be reported as a clean result"):
        require_clean_result(report)


def test_a_clean_run_passes_the_same_gate_untouched() -> None:
    """The refusal above is a property of the breach, not of the gate being on."""
    return_fractions, regimes = _two_regime_returns()
    report = assemble_report(
        path=path_from_returns(return_fractions, regimes=regimes),
        final_state=state_with_fill_count(7),
    )

    assert report.is_clean is True
    assert require_clean_result(report) is report
