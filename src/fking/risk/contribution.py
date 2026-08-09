"""Risk contribution, risk share, and the concentration limits that bind on them.

    sigma_p   = sqrt(w' Sigma w)          portfolio volatility
    MCR_i     = (Sigma w)_i / sigma_p     marginal contribution to risk
    CTR_i     = w_i * MCR_i               component contribution, and sum_i CTR_i = sigma_p

`RISK_PHILOSOPHY.md` section 5. Limits bind on `CTR_i / sigma_p` -- the *share of portfolio
risk* -- and not on notional, because the notional ranking has the concentration ranking
backwards: a 3% notional position in a high-beta alt can carry more risk share than a 10%
position in BTC.

The argument for doing it this way at all is that per-strategy limits constrain
bookkeeping rather than concentration. Five strategies, each inside a 5%-of-equity budget,
each long a different large-cap alt, is presented by a per-strategy design as a diversified
25% book. In a crypto drawdown it is one 25% position, because pairwise correlations among
majors and large alts run 0.80-0.95 in stress. The cluster row below is the only thing in
the system that sees that.

Three properties of the arithmetic are worth stating where the code is:

**`sum_i CTR_i == sigma_p` is an identity, and it is checked numerically.** Every limit is
a ratio against `sigma_p`, so a decomposition that does not sum back to the whole silently
rescales every share. `RISK_CONTRIBUTION_TOLERANCE` is the stated bound.

**The division by `sigma_p` is always defined for a non-zero book.** Not because of a
guard, but because `fking.risk.covariance` caps correlations strictly below one, which
makes `Sigma` positive definite, which makes `w' Sigma w > 0` for every non-zero `w`. The
zero-weight case is handled explicitly and returns zero shares.

**A breach is a refusal, not an exception.** `assess_concentration` returns an assessment
carrying an optional `Rejection`, matching `fking.risk.exposure`. Raising would put an
ordinary, expected, frequent outcome on the same path as a bug.

The evaluation rows are `RiskShareEvaluation` rather than `LimitEvaluation` because these
thresholds are ratios and that type's fields are USD. Reusing it would mean writing a
dimensionless number into a field named `_usd`, which is exactly the confusion
`docs/rules/naming.md` exists to prevent.

Pure: no clock read, no I/O, no randomness. `decided_at_utc` comes from an injected clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from types import MappingProxyType
from typing import Final

from fking.domain import DomainError
from fking.risk.ceilings import Ceiling, assert_within_ceilings
from fking.risk.covariance import MATH_PRECISION, RiskModel
from fking.risk.exposure import Clock, Rejection

__all__ = [
    "CONCENTRATION_HARD_CEILINGS",
    "RISK_CONTRIBUTION_TOLERANCE",
    "ConcentrationAssessment",
    "ConcentrationLimits",
    "PortfolioRisk",
    "RiskContribution",
    "RiskShareEvaluation",
    "assess_concentration",
    "portfolio_risk",
    "weight_ratios_from_notional",
]

_ZERO: Final = Decimal("0")

# The identity `sum_i CTR_i == sigma_p` holds exactly in real arithmetic. This is the
# rounding budget for a *caller* adding the contributions up -- the decomposition itself is
# computed at `MATH_PRECISION`, but the sum is performed in whatever context the caller is
# running under, and the default `decimal` context carries 28 significant digits. Sixty
# additions there accumulate roughly 1e-26 of relative error, so the bound is stated three
# orders of magnitude above that and is still absurdly tight for a risk share.
RISK_CONTRIBUTION_TOLERANCE: Final = Decimal("1e-24")

CONCENTRATION_HARD_CEILINGS: Final[Mapping[str, Ceiling]] = MappingProxyType(
    {
        # 40% of portfolio sigma from one correlation cluster. RISK_PHILOSOPHY.md
        # section 4's portfolio-limits table, the row that per-strategy limits cannot
        # express at all.
        "max_cluster_risk_share_ratio": Ceiling(Decimal("0.40")),
        # 50% from one asset. This number is NOT in that table -- the table's per-asset
        # row is a notional limit, and a notional limit and a risk-share limit are
        # different quantities that happen to share a name. It is set here so that the
        # cluster row is the one that binds in an ordinary book (five equally risky names
        # in one cluster breach the cluster row at 20% each while sitting far inside this
        # one), while a single name carrying half the portfolio's risk is still refused.
        # The per-asset row earns its place on the case the cluster row nets away: a long
        # and a short inside one cluster can leave the cluster share small while one leg
        # carries the entire risk of the book.
        "max_asset_risk_share_ratio": Ceiling(Decimal("0.50")),
    }
)


@dataclass(frozen=True, slots=True)
class ConcentrationLimits:
    """Risk-share limits, refused at construction if a compiled-in ceiling is crossed.

    Never clamped, for the same reason as `ExposureLimits`: `min(configured, ceiling)`
    leaves the system safe, the operator wrong and nobody informed.
    """

    max_cluster_risk_share_ratio: Decimal = Decimal("0.25")
    max_asset_risk_share_ratio: Decimal = Decimal("0.35")

    def __post_init__(self) -> None:
        assert_within_ceilings(
            {name: getattr(self, name) for name in CONCENTRATION_HARD_CEILINGS},
            CONCENTRATION_HARD_CEILINGS,
            scope="concentration",
        )


@dataclass(frozen=True, slots=True)
class RiskContribution:
    """One symbol's share of portfolio risk, with the terms it was built from.

    Every intermediate is kept rather than only the share, because the question after a
    refusal is always "was it the weight or the covariance", and a lone ratio cannot
    answer it.
    """

    symbol: str
    weight_ratio: Decimal
    marginal_contribution_ratio: Decimal
    risk_contribution_ratio: Decimal
    risk_share_ratio: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioRisk:
    """The whole book's risk decomposition at one instant, against one model."""

    portfolio_volatility_ratio: Decimal
    contributions: tuple[RiskContribution, ...]
    cluster_risk_share_ratio: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "cluster_risk_share_ratio", MappingProxyType(dict(self.cluster_risk_share_ratio))
        )

    def risk_share_of(self, symbol: str) -> Decimal:
        """`CTR_i / sigma_p` for one symbol, zero if it carries no weight."""
        for contribution in self.contributions:
            if contribution.symbol == symbol:
                return contribution.risk_share_ratio
        return _ZERO

    @property
    def is_degenerate(self) -> bool:
        """True when the book carries no risk at all, so no share is defined.

        Only reachable from an all-zero weight vector: a positive definite `Sigma` gives
        `w' Sigma w > 0` for every other one.
        """
        return self.portfolio_volatility_ratio == _ZERO


@dataclass(frozen=True, slots=True)
class RiskShareEvaluation:
    """One concentration limit, its threshold, what was observed, and what is left.

    `scope_id` is the symbol or the cluster id the row is about. Headroom is positive when
    room remains and negative when the limit is already breached -- the same sign
    convention as `fking.risk.exposure.LimitEvaluation`, in ratio units rather than USD.
    """

    limit_name: str
    scope_id: str
    threshold_ratio: Decimal
    observed_ratio: Decimal

    @property
    def headroom_ratio(self) -> Decimal:
        """Risk share that may still be added before this limit binds."""
        return self.threshold_ratio - self.observed_ratio

    @property
    def is_breached(self) -> bool:
        """Whether the book already sits the wrong side of this limit."""
        return self.headroom_ratio < _ZERO


@dataclass(frozen=True, slots=True)
class ConcentrationAssessment:
    """The verdict on one candidate book, approved or refused.

    Evaluated against the *prospective* weight vector -- the book as it would stand if the
    order were filled -- because a concentration limit is a predicate on a portfolio, not
    on an order. `RiskEngine.decide()` supplies the post-trade weights.
    """

    decided_at_utc: datetime
    portfolio_risk: PortfolioRisk
    evaluations: tuple[RiskShareEvaluation, ...]
    rejection: Rejection | None

    @property
    def is_approved(self) -> bool:
        """Whether the candidate book satisfies every concentration limit."""
        return self.rejection is None

    def audit_payload(self) -> Mapping[str, object]:
        """The row body: every limit evaluated, with threshold, observed value and headroom.

        Decimals render as strings. The payload lands in `jsonb`, and a JSON encoder that
        has not been told otherwise turns a `Decimal` into a float on the way in -- a
        precision loss into an append-only table that can never be corrected.
        """
        return MappingProxyType(
            {
                "decided_at_utc": self.decided_at_utc.isoformat(),
                "portfolio_volatility_ratio": str(self.portfolio_risk.portfolio_volatility_ratio),
                "verdict": "approved" if self.is_approved else "rejected",
                "binding_limit_name": (
                    None if self.rejection is None else self.rejection.binding_limit_name
                ),
                "risk_contributions": tuple(
                    MappingProxyType(
                        {
                            "symbol": contribution.symbol,
                            "weight_ratio": str(contribution.weight_ratio),
                            "risk_contribution_ratio": str(contribution.risk_contribution_ratio),
                            "risk_share_ratio": str(contribution.risk_share_ratio),
                        }
                    )
                    for contribution in self.portfolio_risk.contributions
                ),
                "limits_evaluated": tuple(
                    MappingProxyType(
                        {
                            "limit_name": evaluation.limit_name,
                            "scope_id": evaluation.scope_id,
                            "threshold_ratio": str(evaluation.threshold_ratio),
                            "observed_ratio": str(evaluation.observed_ratio),
                            "headroom_ratio": str(evaluation.headroom_ratio),
                            "is_breached": evaluation.is_breached,
                        }
                    )
                    for evaluation in self.evaluations
                ),
            }
        )


def weight_ratios_from_notional(
    *, signed_notional_usd_by_symbol: Mapping[str, Decimal], equity_usd: Decimal
) -> Mapping[str, Decimal]:
    """Signed notional over equity: the one place USD becomes a portfolio weight.

    Signed, not absolute. A short's negative weight is what makes an offsetting position
    reduce `sigma_p` instead of adding to it, and a hedge booked as a positive weight
    reads as a doubled position.
    """
    if equity_usd <= _ZERO:
        raise DomainError(
            f"equity is {equity_usd}; a portfolio weight is a fraction of equity and "
            f"cannot be formed against a non-positive one"
        )
    return MappingProxyType(
        {
            symbol: notional_usd / equity_usd
            for symbol, notional_usd in signed_notional_usd_by_symbol.items()
        }
    )


def portfolio_risk(
    *, weight_ratio_by_symbol: Mapping[str, Decimal], model: RiskModel
) -> PortfolioRisk:
    """Decompose `sigma_p` into per-symbol and per-cluster shares.

    A weight on a symbol the model does not cover is refused rather than dropped. Dropping
    it would understate `sigma_p` by exactly the position nobody has an estimate for,
    which is the position most likely to be the problem.
    """
    unknown = sorted(set(weight_ratio_by_symbol) - set(model.symbols))
    if unknown:
        raise DomainError(
            f"{unknown} carry weight but are outside the estimated universe; portfolio "
            f"risk cannot be computed with a position the model does not cover"
        )

    weights = tuple(weight_ratio_by_symbol.get(symbol, _ZERO) for symbol in model.symbols)
    # The whole decomposition runs at an explicitly set precision rather than at whatever
    # the caller's context happens to be. `sum_i CTR_i == sigma_p` is promised to a stated
    # tolerance, and a promise that silently depends on an ambient global is not one.
    with localcontext() as context:
        context.prec = MATH_PRECISION
        covariance_weighted = tuple(
            sum(
                (
                    model.covariance_between(symbol, other) * weights[other_index]
                    for other_index, other in enumerate(model.symbols)
                ),
                start=_ZERO,
            )
            for symbol in model.symbols
        )
        variance = sum(
            (weight * entry for weight, entry in zip(weights, covariance_weighted, strict=True)),
            start=_ZERO,
        )

        if variance <= _ZERO:
            return _degenerate_risk(model, weights)

        volatility = variance.sqrt()
        contributions = tuple(
            _contribution_for(
                symbol=symbol,
                weight_ratio=weights[index],
                covariance_weighted=covariance_weighted[index],
                portfolio_volatility_ratio=volatility,
            )
            for index, symbol in enumerate(model.symbols)
        )
        cluster_shares = _cluster_shares(model, contributions)

    return PortfolioRisk(
        portfolio_volatility_ratio=volatility,
        contributions=contributions,
        cluster_risk_share_ratio=cluster_shares,
    )


def _contribution_for(
    *,
    symbol: str,
    weight_ratio: Decimal,
    covariance_weighted: Decimal,
    portfolio_volatility_ratio: Decimal,
) -> RiskContribution:
    marginal = covariance_weighted / portfolio_volatility_ratio
    contribution = weight_ratio * marginal
    return RiskContribution(
        symbol=symbol,
        weight_ratio=weight_ratio,
        marginal_contribution_ratio=marginal,
        risk_contribution_ratio=contribution,
        risk_share_ratio=contribution / portfolio_volatility_ratio,
    )


def _degenerate_risk(model: RiskModel, weights: tuple[Decimal, ...]) -> PortfolioRisk:
    """The flat book: no volatility, so no share of it is defined.

    Reported as zero rather than refused. The other terms of the sizing `min()` still
    govern a flat book, and a limit on a share of nothing has nothing to say.
    """
    contributions = tuple(
        RiskContribution(
            symbol=symbol,
            weight_ratio=weights[index],
            marginal_contribution_ratio=_ZERO,
            risk_contribution_ratio=_ZERO,
            risk_share_ratio=_ZERO,
        )
        for index, symbol in enumerate(model.symbols)
    )
    return PortfolioRisk(
        portfolio_volatility_ratio=_ZERO,
        contributions=contributions,
        cluster_risk_share_ratio={cluster.cluster_id: _ZERO for cluster in model.clusters},
    )


def _cluster_shares(
    model: RiskModel, contributions: tuple[RiskContribution, ...]
) -> Mapping[str, Decimal]:
    """Each cluster's risk share: the sum of its members', signed."""
    share_by_symbol = {
        contribution.symbol: contribution.risk_share_ratio for contribution in contributions
    }
    return {
        cluster.cluster_id: sum(
            (share_by_symbol[symbol] for symbol in cluster.symbols), start=_ZERO
        )
        for cluster in model.clusters
    }


def assess_concentration(
    *,
    weight_ratio_by_symbol: Mapping[str, Decimal],
    model: RiskModel,
    limits: ConcentrationLimits,
    clock: Clock,
) -> ConcentrationAssessment:
    """Decide whether a candidate book's risk is too concentrated to permit.

    Rows are emitted for the symbols and clusters that actually carry weight. A row per
    untraded symbol would triple the size of every audit payload with values that are
    structurally zero, and the interesting question after a refusal is what the book held,
    not what it did not.
    """
    decided_at_utc = clock()
    risk = portfolio_risk(weight_ratio_by_symbol=weight_ratio_by_symbol, model=model)
    held = frozenset(symbol for symbol, weight in weight_ratio_by_symbol.items() if weight != _ZERO)

    evaluations = tuple(
        RiskShareEvaluation(
            limit_name="max_asset_risk_share_ratio",
            scope_id=contribution.symbol,
            threshold_ratio=limits.max_asset_risk_share_ratio,
            observed_ratio=contribution.risk_share_ratio,
        )
        for contribution in risk.contributions
        if contribution.symbol in held
    ) + tuple(
        RiskShareEvaluation(
            limit_name="max_cluster_risk_share_ratio",
            scope_id=cluster.cluster_id,
            threshold_ratio=limits.max_cluster_risk_share_ratio,
            observed_ratio=risk.cluster_risk_share_ratio[cluster.cluster_id],
        )
        for cluster in model.clusters
        if held & frozenset(cluster.symbols)
    )

    return ConcentrationAssessment(
        decided_at_utc=decided_at_utc,
        portfolio_risk=risk,
        evaluations=evaluations,
        rejection=_rejection_for(evaluations, decided_at_utc=decided_at_utc),
    )


def _rejection_for(
    evaluations: tuple[RiskShareEvaluation, ...], *, decided_at_utc: datetime
) -> Rejection | None:
    """The worst breach, or `None`. Ties go to the first row, which is asset before cluster."""
    binding: RiskShareEvaluation | None = None
    for evaluation in evaluations:
        if not evaluation.is_breached:
            continue
        if binding is None or evaluation.headroom_ratio < binding.headroom_ratio:
            binding = evaluation
    if binding is None:
        return None
    return Rejection(
        reason=(
            f"{binding.scope_id} carries a risk share of {binding.observed_ratio} against "
            f"a {binding.limit_name} of {binding.threshold_ratio}; notional limits do not "
            f"see this because the concentration is in the covariance, not in the book"
        ),
        binding_limit_name=binding.limit_name,
        rejected_at_utc=decided_at_utc,
    )
