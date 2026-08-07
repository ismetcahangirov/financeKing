"""The assembled report, and the order it is read in.

**Credibility, then economics, then risk, then statistics. Never lead with the Sharpe.**
The order is a property of the type -- `SECTION_ORDER` is fixed and `summary_lines`
walks it -- because a reader who sees a Sharpe first has already formed a view by the
time they reach the participation rate and the breach count that qualify it. Putting the
qualifications first costs nothing and changes what gets believed.

**`time_in_market_pct` is a required field.** It has no default, so a report cannot be
constructed without it and a Sharpe cannot be read without it. A ratio of 2.1 at 6%
participation and the same ratio at 94% are different findings; only the second is a
strategy, and the first is an allocation problem that will be misread as a strategy for
as long as the number travels alone.

**A run that breached a risk limit cannot be reported as a clean result.**
`risk_limit_breach_count` is required, `is_clean` is derived from it and cannot be set,
and `sharpe_evidence` refuses outright -- so a breached run has no path to the
validation gate. A breach that merely lowered a score would still let a large enough
Sharpe carry a strategy through, and that is the trade `SURVIVAL_PROTOCOL.md` exists to
refuse.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final, Never

from fking.backtest.portfolio._errors import (
    MetricInputError,
    PortfolioError,
    RiskLimitBreachedError,
)
from fking.backtest.portfolio._grid import EquityPath
from fking.backtest.portfolio._metrics import (
    PathEconomics,
    PathStatistics,
    RiskProfile,
    path_economics,
    path_statistics,
    risk_profile,
)
from fking.backtest.portfolio._regime import RegimeCoverage, RegimeSlice, regime_breakdown
from fking.backtest.portfolio._sample import EffectiveSample
from fking.backtest.portfolio._state import PortfolioState
from fking.backtest.validation import SharpeEvidence


class ReportSection(StrEnum):
    """The sections of a portfolio report, named so the order can be asserted on."""

    CREDIBILITY = "credibility"
    ECONOMICS = "economics"
    RISK = "risk"
    STATISTICS = "statistics"
    REGIMES = "regimes"


# Fixed, and the reason is in the module docstring: a Sharpe read before the
# participation rate and the breach count has already been believed.
SECTION_ORDER: Final[tuple[ReportSection, ...]] = (
    ReportSection.CREDIBILITY,
    ReportSection.ECONOMICS,
    ReportSection.RISK,
    ReportSection.STATISTICS,
    ReportSection.REGIMES,
)


@dataclass(frozen=True, slots=True)
class LedgerTotals:
    """The money of record, `Decimal` from the fill that produced it.

    Kept apart from `PathEconomics` because these cannot be sliced by regime: a realised
    PnL belongs to the fill that closed a position, and no rule attributes that fill to
    one of the days the position was open without inventing one.
    """

    starting_equity_usd: Decimal
    ending_equity_usd: Decimal
    realised_pnl_usd: Decimal
    fee_paid_usd: Decimal
    funding_paid_usd: Decimal
    fill_count: int


@dataclass(frozen=True, slots=True)
class Credibility:
    """What qualifies every number that follows it.

    Every field is required. `time_in_market_pct` and `risk_limit_breach_count` in
    particular have no defaults, so neither can be dropped by omission -- which is the
    only way either of them ever goes missing.
    """

    observation_count: int
    effective_sample: EffectiveSample | None
    time_in_market_pct: Decimal
    fill_count: int
    risk_limit_breach_count: int
    thin_regimes: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        """Derived, never stored, so it cannot contradict the breach counter."""
        return self.risk_limit_breach_count == 0

    @property
    def n_eff(self) -> Decimal | None:
        """The whole path's effective sample size, or `None` if it could not be estimated."""
        return None if self.effective_sample is None else self.effective_sample.n_eff


@dataclass(frozen=True, slots=True)
class PortfolioReport:
    """One run's portfolio accounting, reduced to the suite a reader needs.

    Field order is the reading order, and `summary_lines` walks `SECTION_ORDER` rather
    than the dataclass, so a later field reshuffle cannot quietly promote the Sharpe.
    """

    credibility: Credibility
    ledger: LedgerTotals
    economics: PathEconomics
    risk: RiskProfile
    statistics: PathStatistics
    regimes: tuple[RegimeSlice, ...]

    @property
    def is_clean(self) -> bool:
        """Whether the run finished without breaching a risk limit."""
        return self.credibility.is_clean

    def sharpe_evidence(
        self, *, trials_at_time_of_run: int, sharpe_variance_across_trials: Decimal
    ) -> SharpeEvidence:
        """Hand this path's Sharpe to the overfitting gate, with `n_eff` as the sample.

        `independent_episode_count` is the effective sample, never the raw observation
        count -- a five-day-hold strategy reporting 1000 daily observations as 1000
        independent episodes overstates its t-statistic by about 2.2, which is the whole
        reason `n_eff` is computed.

        Refuses a breached run. The gate exists to decide whether an edge is real, and
        an edge earned through a risk limit is not one the gate is competent to judge.
        """
        if not self.is_clean:
            raise RiskLimitBreachedError(
                f"the run breached a risk limit {self.credibility.risk_limit_breach_count} "
                f"time(s); its Sharpe is not evidence and does not reach the validation "
                f"gate"
            )
        sample = self.statistics.effective_sample
        if sample is None:
            raise MetricInputError(
                "the path is too short to estimate an effective sample size, and the "
                "raw observation count is never substituted for it"
            )
        if (
            self.statistics.sharpe_ratio is None
            or self.statistics.skewness is None
            or self.statistics.kurtosis is None
        ):
            raise MetricInputError(
                "the path has no return variation, so its Sharpe and its moments do not "
                "exist; there is nothing here to deflate"
            )
        return SharpeEvidence(
            observed_sharpe=self.statistics.sharpe_ratio,
            trials_at_time_of_run=trials_at_time_of_run,
            independent_episode_count=sample.episode_count,
            skewness=self.statistics.skewness,
            kurtosis=self.statistics.kurtosis,
            sharpe_variance_across_trials=sharpe_variance_across_trials,
        )

    def summary_lines(self) -> tuple[str, ...]:
        """The report as text, in `SECTION_ORDER`. Credibility first, always."""
        lines: list[str] = []
        for section in SECTION_ORDER:
            lines.append(f"[{section.value}]")
            lines.extend(self._lines_for(section))
        return tuple(lines)

    def _lines_for(self, section: ReportSection) -> tuple[str, ...]:
        """The lines of one section. Every `ReportSection` is handled, explicitly.

        The trailing `raise` is what makes that a runtime guarantee rather than only a
        type-checked one. Without it this dispatch falls off the end and returns `None`
        for anything outside the five members -- and `ReportSection` is a `StrEnum`, so
        the value that reaches here from deserialised or otherwise untyped code is
        exactly the one mypy's exhaustiveness proof does not cover. A report section
        that silently renders as nothing is the failure this file exists to prevent:
        the missing section would be credibility, and the Sharpe below it would be read
        without it.

        An `if`/`elif` chain rather than a `match`: CodeQL does not model `case _` as
        exhausting the subject, so every arrangement of `match` here reports
        `py/mixed-returns` whether or not a wildcard arm exists. A shape that three
        readers -- a person, mypy and the scanner -- all agree terminates is worth more
        than the two lines the `match` saved.
        """
        if section is ReportSection.CREDIBILITY:
            credibility = self.credibility
            return (
                f"observation_count={credibility.observation_count}",
                f"n_eff={credibility.n_eff}",
                f"time_in_market_pct={credibility.time_in_market_pct}",
                f"fill_count={credibility.fill_count}",
                f"risk_limit_breach_count={credibility.risk_limit_breach_count}",
                f"is_clean={credibility.is_clean}",
                f"thin_regimes={list(credibility.thin_regimes)}",
            )
        if section is ReportSection.ECONOMICS:
            return (
                f"total_return_fraction={self.economics.total_return_fraction}",
                f"annualised_return_fraction={self.economics.annualised_return_fraction}",
                f"realised_pnl_usd={self.ledger.realised_pnl_usd}",
                f"fee_paid_usd={self.ledger.fee_paid_usd}",
                f"funding_paid_usd={self.ledger.funding_paid_usd}",
                f"ending_equity_usd={self.ledger.ending_equity_usd}",
            )
        if section is ReportSection.RISK:
            return (
                f"annualised_volatility_fraction={self.risk.annualised_volatility_fraction}",
                f"max_drawdown_fraction={self.risk.max_drawdown_fraction}",
                f"ulcer_index_pct={self.risk.ulcer_index_pct}",
                f"worst_daily_return_fraction={self.risk.worst_daily_return_fraction}",
            )
        if section is ReportSection.STATISTICS:
            return (
                f"sharpe_ratio={self.statistics.sharpe_ratio}",
                f"sortino_ratio={self.statistics.sortino_ratio}",
                f"calmar_ratio={self.statistics.calmar_ratio}",
                f"skewness={self.statistics.skewness}",
                f"kurtosis={self.statistics.kurtosis}",
                f"sharpe_t_statistic={self.statistics.sharpe_t_statistic}",
            )
        if section is ReportSection.REGIMES:
            return tuple(
                f"{bucket.regime}: observation_count={bucket.observation_count} "
                f"n_eff={bucket.n_eff} "
                f"sharpe_ratio="
                f"{None if bucket.statistics is None else bucket.statistics.sharpe_ratio} "
                f"regime_coverage={bucket.regime_coverage.value}"
                for bucket in self.regimes
            )
        raise _unhandled_section(section)  # pragma: no cover - the branches above are exhaustive


def _unhandled_section(section: Never) -> PortfolioError:
    """The error for a report section no branch of `_lines_for` handles.

    Returns the exception rather than raising it, so the `raise` stays lexically in
    `_lines_for`. That is not a style preference: CodeQL's fall-through analysis is
    intraprocedural, so a helper annotated `NoReturn` reads to it -- and to a human
    skimming the call -- as a statement that completes normally, leaving a function
    declared to return `tuple[str, ...]` falling off its end. A reader should not have
    to resolve a call to know whether control leaves the function.

    The `Never` parameter is `typing.assert_never`'s mechanism, carrying this project's
    own exception instead of the `AssertionError` that `.claude/rules/error-handling.md`
    keeps out of the taxonomy. Once every `ReportSection` member is covered above, mypy
    narrows `section` to `Never` at the call and it type-checks; add a sixth member
    without a branch for it and `section` narrows to that member, which is not
    assignable to `Never` -- so the omission is a type error at the point of the
    omission rather than a section that renders as nothing at 03:00.
    """
    return PortfolioError(
        f"no report section is defined for {section!r}; SECTION_ORDER and _lines_for have diverged"
    )


def require_clean_result(report: PortfolioReport) -> PortfolioReport:
    """Return the report, or refuse if the run breached a risk limit.

    The call site is the point: a consumer that wants to treat a run as clean has to say
    so, and saying so is what raises when the run was not.
    """
    if not report.is_clean:
        raise RiskLimitBreachedError(
            f"the run breached a risk limit "
            f"{report.credibility.risk_limit_breach_count} time(s) and cannot be "
            f"reported as a clean result"
        )
    return report


def assemble_report(*, path: EquityPath, final_state: PortfolioState) -> PortfolioReport:
    """Build the whole suite from a daily equity path and the state that produced it.

    The path supplies every statistic and the state supplies every money-of-record
    total, and the two do not overlap. Nothing in this function can route the trade
    count into a ratio, because `path_statistics` does not accept one.
    """
    return_fractions = path.daily_return_fractions
    economics = path_economics(return_fractions)
    risk = risk_profile(return_fractions)
    statistics = path_statistics(return_fractions, risk=risk, economics=economics)
    regimes = regime_breakdown(path)

    return PortfolioReport(
        credibility=Credibility(
            observation_count=path.observation_count,
            effective_sample=statistics.effective_sample,
            time_in_market_pct=path.time_in_market_pct,
            fill_count=final_state.fill_count,
            risk_limit_breach_count=final_state.risk_limit_breach_count,
            thin_regimes=tuple(
                bucket.regime for bucket in regimes if bucket.regime_coverage is RegimeCoverage.THIN
            ),
        ),
        ledger=LedgerTotals(
            starting_equity_usd=path.starting_equity_usd,
            ending_equity_usd=path.ending_equity_usd,
            realised_pnl_usd=final_state.realised_pnl_usd,
            fee_paid_usd=final_state.fee_paid_usd,
            funding_paid_usd=final_state.funding_paid_usd,
            fill_count=final_state.fill_count,
        ),
        economics=economics,
        risk=risk,
        statistics=statistics,
        regimes=regimes,
    )
