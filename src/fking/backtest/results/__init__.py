"""`BacktestResult`, the seven-check audit battery, and the credibility gate that reads it.

`docs/rules/overfitting-defences.md` and this package's own mission state the posture:
**a good backtest result is a bug report until proven otherwise.** Everything here exists
to make that posture structural rather than aspirational.

Three properties carry the design.

**`credibility` cannot be asserted, only derived.** `assess_credibility` is the single
place a verdict is computed, and `BacktestResult` refuses to construct with a `credibility`
value that disagrees with what its own fields and `audit_findings` compute
(`fking.backtest.results._result`). A result with fewer than 200 trades, a failed check, a
breached risk limit, an `edge_to_cost_ratio` below 2.0, or a testnet-named cost model
cannot reach `credible` regardless of what value is handed to the constructor.

**The default is `unaudited`, and an incomplete battery cannot become anything else.**
Seven checks, each exactly once, none `inconclusive` -- short of that, `assess_credibility`
returns `unaudited` no matter how good every present finding looks.

**Assumptions are frozen into the run's own identity.** `frozen_assumptions_hash` mixes
the run configuration's digest with the cost model's own, so mutating a fee, a spread
quantile or a latency parameter after a result has been reported produces a different
`config_hash` -- a new trial, never a revised one.

Everything not in `__all__` is private and may change without notice.
"""

from __future__ import annotations

from fking.backtest.results._assumptions import cost_model_digest, frozen_assumptions_hash
from fking.backtest.results._checks import (
    MIN_CREDIBLE_TRADE_COUNT,
    check_cost_model,
    check_fill_optimism,
    check_parity,
    check_sample_size,
    check_survivorship,
    check_timestamp_alignment,
)
from fking.backtest.results._credibility import assess_credibility
from fking.backtest.results._errors import BacktestResultError, CredibilityInvariantError
from fking.backtest.results._finding import (
    AUDIT_ORDER,
    REQUIRED_AUDIT_CHECKS,
    AuditCheck,
    AuditFinding,
    AuditStatus,
    ResultCredibility,
)
from fking.backtest.results._lookahead_guard import Bar, Entry, check_entry_fills_are_achievable
from fking.backtest.results._result import BacktestResult

__all__: tuple[str, ...] = (
    "AUDIT_ORDER",
    "MIN_CREDIBLE_TRADE_COUNT",
    "REQUIRED_AUDIT_CHECKS",
    "AuditCheck",
    "AuditFinding",
    "AuditStatus",
    "BacktestResult",
    "BacktestResultError",
    "Bar",
    "CredibilityInvariantError",
    "Entry",
    "ResultCredibility",
    "assess_credibility",
    "check_cost_model",
    "check_entry_fills_are_achievable",
    "check_fill_optimism",
    "check_parity",
    "check_sample_size",
    "check_survivorship",
    "check_timestamp_alignment",
    "cost_model_digest",
    "frozen_assumptions_hash",
)
