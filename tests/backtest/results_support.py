"""Builders shared by the `fking.backtest.results` suites.

No tests of its own. `complete_battery()` is the one place a passing seven-check battery
is assembled, so a change to `AuditCheck`'s membership breaks exactly one function rather
than seven test files.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from fking.backtest.results import (
    AuditCheck,
    AuditFinding,
    AuditStatus,
    BacktestResult,
    ResultCredibility,
)

RUN_ID: Final[UUID] = UUID("00000000-0000-4000-8000-000000000001")
WINDOW_START: Final = datetime(2026, 1, 1, tzinfo=UTC)
WINDOW_END: Final = datetime(2026, 6, 30, tzinfo=UTC)
PRODUCTION_SOURCE: Final = "binance_um_production_2026-03..2026-05"


def finding(
    check: AuditCheck, *, status: AuditStatus = AuditStatus.PASS, evidence: str | None = None
) -> AuditFinding:
    return AuditFinding(
        check=check, status=status, evidence=evidence or f"{check.value} evidence: nothing found"
    )


def complete_battery(
    *, overrides: Mapping[AuditCheck, AuditFinding] | None = None
) -> tuple[AuditFinding, ...]:
    """One passing finding per required check, with any override substituted in."""
    overrides = overrides or {}
    return tuple(overrides.get(check, finding(check)) for check in AuditCheck)


def result_for(  # noqa: PLR0913 - one keyword per field a test commonly needs to vary
    *,
    trade_count: int = 250,
    risk_limit_breaches: int = 0,
    edge_to_cost_ratio: Decimal = Decimal("2.5"),
    cost_model_calibration_source: str = PRODUCTION_SOURCE,
    credibility: ResultCredibility = ResultCredibility.CREDIBLE,
    audit_findings: tuple[AuditFinding, ...] | None = None,
    sharpe: Decimal = Decimal("1.1"),
    deflated_sharpe: Decimal = Decimal("0.6"),
    trials_at_time_of_run: int = 40,
) -> BacktestResult:
    return BacktestResult(
        run_id=RUN_ID,
        strategy_id="breakout-4h",
        strategy_version="1.3.0",
        config_hash="a" * 64,
        cost_model_version="costs-2026.05.1",
        cost_model_calibration_source=cost_model_calibration_source,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        trade_count=trade_count,
        gross_return=Decimal("0.08"),
        total_cost=Decimal("0.02"),
        net_return=Decimal("0.06"),
        gross_edge_per_trade_bp=Decimal("10"),
        round_trip_cost_bp=Decimal("4"),
        edge_to_cost_ratio=edge_to_cost_ratio,
        sharpe=sharpe,
        trials_at_time_of_run=trials_at_time_of_run,
        deflated_sharpe=deflated_sharpe,
        max_drawdown=Decimal("0.12"),
        risk_limit_breaches=risk_limit_breaches,
        credibility=credibility,
        audit_findings=audit_findings if audit_findings is not None else complete_battery(),
    )
