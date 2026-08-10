"""`AuditFinding` and `BacktestResult`'s own schema-level refusals.

Three refusals, each named by an acceptance criterion of issue #44:

- An `AuditFinding` with empty evidence fails schema validation.
- Any check left `inconclusive` blocks `credibility="credible"`.
- `credibility` cannot be asserted independently of what the battery computes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from fking.backtest.results import (
    AuditCheck,
    AuditFinding,
    AuditStatus,
    BacktestResult,
    CredibilityInvariantError,
    ResultCredibility,
)
from tests.backtest.results_support import (
    PRODUCTION_SOURCE,
    RUN_ID,
    complete_battery,
    finding,
    result_for,
)

pytestmark = pytest.mark.unit


def test_a_finding_with_empty_evidence_fails_schema_validation() -> None:
    with pytest.raises(ValidationError, match="not a claim"):
        AuditFinding(check=AuditCheck.LOOK_AHEAD, status=AuditStatus.PASS, evidence="")


def test_a_finding_with_whitespace_only_evidence_fails_schema_validation() -> None:
    with pytest.raises(ValidationError, match="not a claim"):
        AuditFinding(check=AuditCheck.LOOK_AHEAD, status=AuditStatus.PASS, evidence="   ")


def test_the_default_credibility_is_unaudited() -> None:
    result = result_for(credibility=ResultCredibility.UNAUDITED, audit_findings=())
    assert result.credibility is ResultCredibility.UNAUDITED


def test_an_incomplete_battery_cannot_claim_credible() -> None:
    six_of_seven = complete_battery()[:-1]
    with pytest.raises(CredibilityInvariantError, match="unaudited"):
        result_for(credibility=ResultCredibility.CREDIBLE, audit_findings=six_of_seven)


def test_a_duplicated_check_cannot_claim_credible() -> None:
    """Two `look_ahead` findings and a missing `sample_size` is not a complete battery."""
    duplicated = (finding(AuditCheck.LOOK_AHEAD), *complete_battery()[:-1])
    with pytest.raises(CredibilityInvariantError, match="unaudited"):
        result_for(credibility=ResultCredibility.CREDIBLE, audit_findings=duplicated)


def test_an_inconclusive_check_blocks_credible_even_with_six_passes() -> None:
    battery = complete_battery(
        overrides={
            AuditCheck.PARITY: finding(
                AuditCheck.PARITY, status=AuditStatus.INCONCLUSIVE, evidence="no paper run yet"
            )
        }
    )
    with pytest.raises(CredibilityInvariantError, match="unaudited"):
        result_for(credibility=ResultCredibility.CREDIBLE, audit_findings=battery)

    # The honest claim -- unaudited -- is accepted with the identical battery.
    result = result_for(credibility=ResultCredibility.UNAUDITED, audit_findings=battery)
    assert result.credibility is ResultCredibility.UNAUDITED


def test_a_failed_check_blocks_credible_even_though_the_battery_is_complete() -> None:
    battery = complete_battery(
        overrides={
            AuditCheck.FILL_OPTIMISM: finding(
                AuditCheck.FILL_OPTIMISM,
                status=AuditStatus.FAIL,
                evidence="20/20 limit orders filled with 0 rejections",
            )
        }
    )
    with pytest.raises(CredibilityInvariantError, match="not_credible"):
        result_for(credibility=ResultCredibility.CREDIBLE, audit_findings=battery)

    result = result_for(credibility=ResultCredibility.NOT_CREDIBLE, audit_findings=battery)
    assert result.credibility is ResultCredibility.NOT_CREDIBLE


def test_claiming_not_credible_when_the_battery_actually_passes_is_also_refused() -> None:
    """Under-claiming is refused too, not only over-claiming."""
    with pytest.raises(CredibilityInvariantError, match="credible"):
        result_for(credibility=ResultCredibility.NOT_CREDIBLE)


def test_a_risk_limit_breach_blocks_credible() -> None:
    with pytest.raises(CredibilityInvariantError, match="not_credible"):
        result_for(credibility=ResultCredibility.CREDIBLE, risk_limit_breaches=1)


def test_window_end_must_follow_window_start() -> None:
    with pytest.raises(ValidationError, match="must follow"):
        BacktestResult(
            run_id=RUN_ID,
            strategy_id="breakout-4h",
            strategy_version="1.3.0",
            config_hash="a" * 64,
            cost_model_version="costs-2026.05.1",
            cost_model_calibration_source=PRODUCTION_SOURCE,
            window_start=datetime(2026, 6, 30, tzinfo=UTC),
            window_end=datetime(2026, 1, 1, tzinfo=UTC),
            trade_count=250,
            gross_return=Decimal("0.08"),
            total_cost=Decimal("0.02"),
            net_return=Decimal("0.06"),
            gross_edge_per_trade_bp=Decimal("10"),
            round_trip_cost_bp=Decimal("4"),
            edge_to_cost_ratio=Decimal("2.5"),
            sharpe=Decimal("1.1"),
            trials_at_time_of_run=40,
            deflated_sharpe=Decimal("0.6"),
            max_drawdown=Decimal("0.12"),
            risk_limit_breaches=0,
            credibility=ResultCredibility.CREDIBLE,
            audit_findings=complete_battery(),
        )


def test_a_negative_edge_to_cost_ratio_is_refused_rather_than_reported() -> None:
    """A void ratio never reaches this schema; it is a signal to void the result upstream."""
    with pytest.raises(ValidationError, match="cost model did not run"):
        result_for(edge_to_cost_ratio=Decimal("-1"), credibility=ResultCredibility.NOT_CREDIBLE)
