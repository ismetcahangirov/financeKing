"""The credibility gate: one pure function, the single place a verdict is computed.

`credibility` starts at `unaudited` and only a *complete* battery -- all seven checks
present, exactly once, none `inconclusive` -- can move it (issue #44's own wording). This
module is that rule, applied literally and nowhere else: `BacktestResult` calls it and
refuses to disagree with it (`fking.backtest.results._result`), rather than each caller
re-deriving the verdict its own way and the two drifting apart.

Disqualification is deliberately a single flat `any(...)` over five independent
conditions rather than five separate early returns. Every one of them is a hard
rejection with no partial credit -- a `not_credible` result from a failed check reads
identically to one from a thin sample -- and `assess_credibility` does not try to explain
*why* here; `AuditFinding.evidence` and the caller's own fields already carry that.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from fking.backtest.costs import MIN_EDGE_TO_COST_RATIO, names_testnet
from fking.backtest.results._checks import MIN_CREDIBLE_TRADE_COUNT
from fking.backtest.results._finding import (
    REQUIRED_AUDIT_CHECKS,
    AuditFinding,
    AuditStatus,
    ResultCredibility,
)


def assess_credibility(
    *,
    audit_findings: Sequence[AuditFinding],
    trade_count: int,
    risk_limit_breaches: int,
    edge_to_cost_ratio: Decimal,
    cost_model_calibration_source: str,
) -> ResultCredibility:
    """The verdict a `BacktestResult` is required to carry, derived from its inputs.

    `unaudited` for an incomplete or unresolved battery -- a check missing, repeated, or
    left `inconclusive` -- because that is a weaker statement than `not_credible` and
    conflating the two would let a caller who simply skipped a check report the stronger
    "we checked and it failed" claim instead of the honest "we have not finished
    checking" one.
    """
    checks_present = tuple(finding.check for finding in audit_findings)
    battery_is_complete = (
        len(checks_present) == len(REQUIRED_AUDIT_CHECKS)
        and set(checks_present) == REQUIRED_AUDIT_CHECKS
    )
    if not battery_is_complete:
        return ResultCredibility.UNAUDITED
    if any(finding.status is AuditStatus.INCONCLUSIVE for finding in audit_findings):
        return ResultCredibility.UNAUDITED

    disqualified = (
        any(finding.status is AuditStatus.FAIL for finding in audit_findings)
        or trade_count < MIN_CREDIBLE_TRADE_COUNT
        or risk_limit_breaches > 0
        or edge_to_cost_ratio < MIN_EDGE_TO_COST_RATIO
        or names_testnet(cost_model_calibration_source)
    )
    return ResultCredibility.NOT_CREDIBLE if disqualified else ResultCredibility.CREDIBLE
