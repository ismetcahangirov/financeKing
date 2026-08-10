"""`BacktestResult`: the typed result every downstream consumer reads.

Two invariants are enforced at construction, both by re-deriving the answer rather than
trusting the field:

**`window_end` must follow `window_start`.** The same ordering `RunConfig` enforces on
`start_utc`/`end_utc`, restated here because a `BacktestResult` can be reconstructed from
storage long after the `RunConfig` that produced it is gone.

**`credibility` must equal `assess_credibility(...)` computed from this result's own
fields.** A caller cannot assert `credible` on a 40-trade sample, cannot assert
`not_credible` while leaving a check `inconclusive` (that is `unaudited`, a different and
weaker claim), and cannot assert anything at all while `cost_model_calibration_source`
names testnet except `not_credible`. `CredibilityInvariantError` is raised rather than the
field being silently corrected, for the reason `fking.backtest.costs.CostModel` refuses a
testnet provenance outright rather than downgrading a flag: a caller that could get an
under-checked claim silently repaired would stop calling `assess_credibility` at all.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Final, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from fking.backtest.results._checks import MIN_CREDIBLE_TRADE_COUNT
from fking.backtest.results._credibility import assess_credibility
from fking.backtest.results._errors import CredibilityInvariantError
from fking.backtest.results._finding import AuditFinding, ResultCredibility

__all__ = [
    "MIN_CREDIBLE_TRADE_COUNT",
    "BacktestResult",
]

_ZERO: Final = Decimal("0")


def _require_non_blank(candidate: str, field_name: str) -> str:
    if not candidate.strip():
        raise ValueError(f"{field_name} must not be blank")
    return candidate


class BacktestResult(BaseModel):
    """One run's economics, statistics and credibility verdict, together.

    `frozen=True`: a result is reported once, and a mutable result that downstream code
    could edit after the fact is indistinguishable from a post-hoc adjustment
    (`docs/rules/overfitting-defences.md`). `strict=True` so a `Decimal` field refuses a
    bare `int` or `float` exactly as `fking.domain._guards.require_decimal` does --
    `Decimal` is constructed from `str`, never coerced.

    Field order is the reading order this package's mission states: credibility first,
    then economics (`gross_return`/`total_cost`/`net_return`/`edge_to_cost_ratio`), then
    statistics (`sharpe`/`deflated_sharpe`). A reader who reaches `sharpe` has already
    passed `trade_count`, `risk_limit_breaches` and `credibility`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    run_id: UUID
    #: A run's own identifier, e.g. `"breakout-4h"` -- matching the convention every other
    #: type in this codebase uses (`fking.domain.signal.Signal.strategy_id`,
    #: `fking.backtest.RunConfig.strategy_id`), not the `UUID` a from-scratch schema might
    #: reach for. Two identity fields for one strategy would be a second thing to keep in
    #: step with the first.
    strategy_id: str
    strategy_version: str
    config_hash: str
    cost_model_version: str
    cost_model_calibration_source: str
    window_start: datetime
    window_end: datetime
    trade_count: int
    gross_return: Decimal
    total_cost: Decimal
    net_return: Decimal
    gross_edge_per_trade_bp: Decimal
    round_trip_cost_bp: Decimal
    edge_to_cost_ratio: Decimal
    sharpe: Decimal
    trials_at_time_of_run: int
    deflated_sharpe: Decimal
    max_drawdown: Decimal
    risk_limit_breaches: int
    credibility: ResultCredibility = ResultCredibility.UNAUDITED
    audit_findings: tuple[AuditFinding, ...] = ()

    @field_validator("strategy_id")
    @classmethod
    def _strategy_id_is_not_blank(cls, candidate: str) -> str:
        return _require_non_blank(candidate, "strategy_id")

    @field_validator("strategy_version")
    @classmethod
    def _strategy_version_is_not_blank(cls, candidate: str) -> str:
        return _require_non_blank(candidate, "strategy_version")

    @field_validator("config_hash")
    @classmethod
    def _config_hash_is_not_blank(cls, candidate: str) -> str:
        return _require_non_blank(candidate, "config_hash")

    @field_validator("cost_model_version")
    @classmethod
    def _cost_model_version_is_not_blank(cls, candidate: str) -> str:
        return _require_non_blank(candidate, "cost_model_version")

    @field_validator("cost_model_calibration_source")
    @classmethod
    def _calibration_source_is_not_blank(cls, candidate: str) -> str:
        return _require_non_blank(candidate, "cost_model_calibration_source")

    @field_validator("trade_count", "risk_limit_breaches")
    @classmethod
    def _counts_are_not_negative(cls, candidate: int) -> int:
        if candidate < 0:
            raise ValueError(f"must not be negative; got {candidate}")
        return candidate

    @field_validator("trials_at_time_of_run")
    @classmethod
    def _trial_count_is_at_least_one(cls, candidate: int) -> int:
        # `fking.backtest.validation.SharpeEvidence` applies the same floor for the same
        # reason: zero is what an unreadable trial ledger returns, never a legitimate
        # "nothing was searched".
        if candidate < 1:
            raise ValueError(
                f"trials_at_time_of_run must be at least 1; got {candidate}, which is what "
                f"an unreadable trial ledger returns, not a legitimate trial count"
            )
        return candidate

    @model_validator(mode="after")
    def _window_is_ordered(self) -> Self:
        if self.window_end <= self.window_start:
            raise ValueError(
                f"window_end {self.window_end.isoformat()} must follow window_start "
                f"{self.window_start.isoformat()}"
            )
        return self

    @model_validator(mode="after")
    def _edge_to_cost_ratio_is_not_void(self) -> Self:
        # `fking.backtest.costs.CostVerdict.VOID`: a ratio that computes as infinite
        # (reported by callers as a sentinel) or negative means the cost model did not
        # run. `docs/rules/...` failure handling: void the result rather than report it --
        # so a `BacktestResult` refuses to exist in that state at all, rather than reaching
        # the credibility gate with a number nobody should read.
        if self.edge_to_cost_ratio < _ZERO:
            raise ValueError(
                f"edge_to_cost_ratio={self.edge_to_cost_ratio} is negative; the cost model "
                f"did not run and this result must be voided, not reported"
            )
        return self

    @model_validator(mode="after")
    def _credibility_matches_the_audit_battery(self) -> Self:
        derived = assess_credibility(
            audit_findings=self.audit_findings,
            trade_count=self.trade_count,
            risk_limit_breaches=self.risk_limit_breaches,
            edge_to_cost_ratio=self.edge_to_cost_ratio,
            cost_model_calibration_source=self.cost_model_calibration_source,
        )
        if self.credibility is not derived:
            raise CredibilityInvariantError(
                f"credibility={self.credibility.value!r} was claimed, but the audit "
                f"battery computes {derived.value!r} from trade_count={self.trade_count}, "
                f"risk_limit_breaches={self.risk_limit_breaches}, "
                f"edge_to_cost_ratio={self.edge_to_cost_ratio}, "
                f"cost_model_calibration_source={self.cost_model_calibration_source!r} and "
                f"{len(self.audit_findings)} audit finding(s)"
            )
        return self
