"""Portfolio exposure limits and the pre-trade validation that runs before an order exists.

This module owns the `q_exposure` and `q_venue_filters` terms of the sizing `min()`
(`RISK_PHILOSOPHY.md` section 3): per-position, per-asset, gross, net and free-margin
headroom computed at portfolio level, intersected with the absolute USD caps from
`RiskLimits`, then snapped onto the venue's own lattice.

Four decisions here are load-bearing.

**A breach is a refusal, not an error.** `validate_pre_trade` returns an
`ExposureAssessment` carrying an optional `Rejection`; it never raises for a limit
breach. Raising would put a refusal -- an ordinary, expected, frequent outcome -- on the
same code path as a bug, and the caller would then be obliged to write a handler that is
indistinguishable from swallowing a real error (`docs/rules/error-handling.md`).

**Every limit is recorded, not only the one that bound.** The audit row carries a
`LimitEvaluation` per limit with its threshold, its observed value and its remaining
headroom, because the question asked in every post-incident review is "how close were the
others", and a row holding only the binding limit cannot answer it
(`docs/rules/append-only-audit.md`).

**Relative and absolute caps both enter the same `min()`, always.** A fraction-of-equity
limit scales with equity, so anything that inflates the equity number -- a bad mark on an
illiquid symbol, a duplicated fill from a redelivered event, a quote-conversion error --
silently authorises a larger position, and a limit expressed as a fraction of the thing
that broke cannot detect it. An absolute USD cap does not scale with anything and so does
not participate in that failure. Conversely an absolute cap alone stops constraining the
book as equity grows. Neither subsumes the other.

**Reduce-only is never refused.** A signal that shrinks exposure is permitted even when
the book is already over a limit. Refusing to close while over the gross cap is the limit
working exactly backwards: it would trap the portfolio in the state the limit exists to
prevent.

Pure throughout. No clock read, no I/O, no unseeded randomness -- `decided_at_utc` is
supplied by an injected `clock` so the same portfolio and the same signal replay to the
same decision (`CLAUDE.md` section 4).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Literal

from fking.domain import (
    Direction,
    DomainError,
    Instrument,
    Portfolio,
    Signal,
)
from fking.risk.ceilings import (
    Ceiling,
    Floor,
    assert_above_floors,
    assert_within_ceilings,
)
from fking.risk.limits import RiskLimits

__all__ = [
    "EXPOSURE_HARD_CEILINGS",
    "EXPOSURE_HARD_FLOORS",
    "ExposureAssessment",
    "ExposureLimits",
    "LimitEvaluation",
    "PortfolioExposure",
    "PreTradeContext",
    "Rejection",
    "ViolationTally",
    "portfolio_exposure",
    "validate_pre_trade",
]

_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")

# The shape every injected clock in this repository has. Spelled here rather than
# imported from `fking.strategy._contract` because that is a private submodule of another
# package, and a cross-package import of a `_`-prefixed module is a boundary violation
# regardless of what the layer contract permits (`docs/rules/module-boundaries.md`).
Clock = Callable[[], datetime]

BoundKind = Literal["ceiling", "floor"]

# Ratio limits, bounded in the direction that is dangerous for each. Values from the
# portfolio-limits table in issue #50, which restates `RISK_PHILOSOPHY.md` section 4.
# These are separate from `fking.risk.ceilings.HARD_CEILINGS` because that mapping mirrors
# the absolute USD caps the configuration tree validates on `RiskSettings`; these five are
# fractions of equity and have no counterpart there. Same `Ceiling`/`Floor` machinery, so
# the direction of every comparison is still decided by the type of the bound rather than
# by whoever wrote the loop.
EXPOSURE_HARD_CEILINGS: Final[Mapping[str, Ceiling]] = MappingProxyType(
    {
        # 10% of equity in one position. Above this a single bad mark on one symbol
        # dominates the drawdown limit before the drawdown limit can observe it.
        "max_position_equity_ratio": Ceiling(Decimal("0.10")),
        # 300% gross. Gross is the number that decides how much of the book a
        # correlated move touches at once; net can be flat while gross is lethal.
        "max_gross_exposure_ratio": Ceiling(Decimal("3.00")),
        # 150% net directional.
        "max_net_exposure_ratio": Ceiling(Decimal("1.50")),
        # 25% to one base asset across every strategy. Per-strategy limits do not
        # compose: five strategies each inside their own cap can hold one asset between
        # them at five times the intended concentration.
        "max_asset_exposure_ratio": Ceiling(Decimal("0.25")),
    }
)

EXPOSURE_HARD_FLOORS: Final[Mapping[str, Floor]] = MappingProxyType(
    {
        # 25% free margin. Below that a maintenance-margin call can precede the drawdown
        # kill switch, which inverts the order in which the two safety mechanisms fire.
        # Same value and same reason as `fking.risk.ceilings.HARD_FLOORS`; restated
        # against this model because this is the object that governs the pre-trade check.
        "min_free_margin_ratio": Floor(Decimal("0.25")),
    }
)


@dataclass(frozen=True, slots=True)
class ExposureLimits:
    """Fraction-of-equity exposure limits, refused at construction if a bound is crossed.

    Defaults are the shipped baseline from the issue #50 table, every one of them well
    inside its compiled-in bound. A process constructed with no arguments runs at those
    values rather than at the bounds, because a missing configuration file must never
    produce a less safe system than a present one.

    Never clamped. `min(configured, ceiling)` would leave the system safe, the operator
    wrong, and nobody informed -- and the next decision gets made on the belief they
    still hold. Both numbers appear in the refusal message.
    """

    max_position_equity_ratio: Decimal = Decimal("0.05")
    max_gross_exposure_ratio: Decimal = Decimal("2.00")
    max_net_exposure_ratio: Decimal = Decimal("1.00")
    max_asset_exposure_ratio: Decimal = Decimal("0.15")
    min_free_margin_ratio: Decimal = Decimal("0.40")

    def __post_init__(self) -> None:
        assert_within_ceilings(
            {name: getattr(self, name) for name in EXPOSURE_HARD_CEILINGS},
            EXPOSURE_HARD_CEILINGS,
            scope="exposure",
        )
        assert_above_floors(
            {name: getattr(self, name) for name in EXPOSURE_HARD_FLOORS},
            EXPOSURE_HARD_FLOORS,
            scope="exposure",
        )


@dataclass(frozen=True, slots=True)
class PreTradeContext:
    """Everything about the account and the rules, as one immutable value.

    Bundled rather than passed as five parameters because these four move together and
    must describe the same instant: an equity number read at one moment, evaluated
    against limits loaded at another, against a universe resolved at a third, is three
    snapshots wearing one decision's clothing. Carrying them as a value also means the
    whole policy can be logged as one object beside the decision it produced.
    """

    equity_usd: Decimal
    exposure_limits: ExposureLimits
    absolute_limits: RiskLimits
    tradable_symbols: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.tradable_symbols, frozenset):
            raise DomainError(
                f"tradable_symbols must be a frozenset, got "
                f"{type(self.tradable_symbols).__name__}; a mutable universe is a "
                f"universe that can widen between two checks"
            )


@dataclass(frozen=True, slots=True)
class LimitEvaluation:
    """One limit, its threshold, what was observed against it, and what is left.

    Both directions live in one type with a `bound_kind` discriminator rather than in two
    types, because the audit row is a flat list that a reader scans -- and a reader
    comparing a free-margin row against a gross-exposure row needs `headroom_usd` to mean
    the same thing in both. It does: positive is room remaining, negative is a live
    breach, in USD, always.
    """

    limit_name: str
    bound_kind: BoundKind
    threshold_usd: Decimal
    observed_usd: Decimal

    def __post_init__(self) -> None:
        if self.bound_kind not in ("ceiling", "floor"):
            raise DomainError(f"bound_kind must be 'ceiling' or 'floor', got {self.bound_kind!r}")

    @property
    def headroom_usd(self) -> Decimal:
        """Notional that may still be added before this limit binds. Negative when breached."""
        if self.bound_kind == "ceiling":
            return self.threshold_usd - self.observed_usd
        return self.observed_usd - self.threshold_usd

    @property
    def is_breached(self) -> bool:
        """Whether the book already sits the wrong side of this limit."""
        return self.headroom_usd < _ZERO


@dataclass(frozen=True, slots=True)
class PortfolioExposure:
    """What the book already holds, priced at one set of marks.

    A snapshot rather than a live view: every limit below must be evaluated against the
    same instant, and a view that re-reads marks between two checks can pass both while
    satisfying neither at any single moment.
    """

    gross_notional_usd: Decimal
    net_notional_usd: Decimal
    notional_usd_by_base_asset: Mapping[str, Decimal]
    notional_usd_by_instrument: Mapping[Instrument, Decimal]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "notional_usd_by_base_asset",
            MappingProxyType(dict(self.notional_usd_by_base_asset)),
        )
        object.__setattr__(
            self,
            "notional_usd_by_instrument",
            MappingProxyType(dict(self.notional_usd_by_instrument)),
        )

    def held_notional_usd(self, instrument: Instrument) -> Decimal:
        """Absolute notional already held in `instrument`, zero if none."""
        return self.notional_usd_by_instrument.get(instrument, _ZERO)

    def asset_notional_usd(self, base_asset: str) -> Decimal:
        """Absolute notional held in one base asset across every instrument and strategy."""
        return self.notional_usd_by_base_asset.get(base_asset, _ZERO)


def portfolio_exposure(
    portfolio: Portfolio, marks_usd: Mapping[Instrument, Decimal]
) -> PortfolioExposure:
    """Price the open book at `marks_usd`.

    A missing mark raises rather than defaulting to zero. A position priced at zero is
    indistinguishable from a position that is not held, and every limit below would then
    report headroom that does not exist -- which is the failure mode that authorises the
    largest orders at exactly the moment the mark feed is broken.
    """
    gross_notional_usd = _ZERO
    net_notional_usd = _ZERO
    by_base_asset: dict[str, Decimal] = {}
    by_instrument: dict[Instrument, Decimal] = {}

    for position in portfolio.open_positions:
        mark_usd = marks_usd.get(position.instrument)
        if mark_usd is None:
            raise DomainError(
                f"no mark for open position in {position.instrument.venue}:"
                f"{position.instrument.symbol}; exposure cannot be priced and must not "
                f"be assumed zero"
            )
        notional_usd = position.notional_quote(mark_usd)
        gross_notional_usd += notional_usd
        net_notional_usd += position.signed_base_quantity * mark_usd
        base_asset = position.instrument.base_asset
        by_base_asset[base_asset] = by_base_asset.get(base_asset, _ZERO) + notional_usd
        by_instrument[position.instrument] = (
            by_instrument.get(position.instrument, _ZERO) + notional_usd
        )

    return PortfolioExposure(
        gross_notional_usd=gross_notional_usd,
        net_notional_usd=net_notional_usd,
        notional_usd_by_base_asset=by_base_asset,
        notional_usd_by_instrument=by_instrument,
    )


@dataclass(frozen=True, slots=True)
class Rejection:
    """A refusal, as a value.

    `binding_limit_name` names the limit that bound; `ExposureAssessment.evaluations`
    carries every limit that was looked at. Both are needed: the first answers "why not",
    the second answers "how close was everything else".
    """

    reason: str
    binding_limit_name: str
    rejected_at_utc: datetime


@dataclass(frozen=True, slots=True)
class ExposureAssessment:
    """The complete pre-trade verdict on one signal, approved or refused.

    Returned in both outcomes. A rejection that produced no assessment would leave the
    audit log unable to distinguish "the risk engine said no" from "the consumer crashed
    before it ran" (`fking.domain.risk_decision`), and those are different incidents.
    """

    strategy_id: str
    instrument: Instrument
    direction: Direction
    decided_at_utc: datetime
    equity_usd: Decimal
    evaluations: tuple[LimitEvaluation, ...]
    permitted_notional_usd: Decimal
    permitted_base_quantity: Decimal
    rejection: Rejection | None

    @property
    def is_approved(self) -> bool:
        """Whether any quantity at all may be sent."""
        return self.rejection is None

    @property
    def breached_limit_names(self) -> tuple[str, ...]:
        """Every limit the book already sits the wrong side of."""
        return tuple(
            evaluation.limit_name for evaluation in self.evaluations if evaluation.is_breached
        )

    def audit_payload(self) -> Mapping[str, object]:
        """The row body. Every limit evaluated, with threshold, observed value and headroom.

        Decimals are rendered as strings rather than left as `Decimal`: the payload lands
        in `jsonb`, and a JSON encoder that has not been told otherwise turns a `Decimal`
        into a float on the way in, which is a precision loss into an append-only table
        that can never be corrected.
        """
        return MappingProxyType(
            {
                "strategy_id": self.strategy_id,
                "venue": str(self.instrument.venue),
                "symbol": self.instrument.symbol,
                "direction": str(self.direction),
                "decided_at_utc": self.decided_at_utc.isoformat(),
                "equity_usd": str(self.equity_usd),
                "permitted_notional_usd": str(self.permitted_notional_usd),
                "permitted_base_quantity": str(self.permitted_base_quantity),
                "verdict": "approved" if self.is_approved else "rejected",
                "rejection_reason": None if self.rejection is None else self.rejection.reason,
                "binding_limit_name": (
                    None if self.rejection is None else self.rejection.binding_limit_name
                ),
                "limits_evaluated": tuple(
                    MappingProxyType(
                        {
                            "limit_name": evaluation.limit_name,
                            "bound_kind": evaluation.bound_kind,
                            "threshold_usd": str(evaluation.threshold_usd),
                            "observed_usd": str(evaluation.observed_usd),
                            "headroom_usd": str(evaluation.headroom_usd),
                            "is_breached": evaluation.is_breached,
                        }
                    )
                    for evaluation in self.evaluations
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class ViolationTally:
    """Risk violations per strategy, including the ones that never reached the book.

    A refused signal counts (`SURVIVAL_PROTOCOL.md` section 9). Intent predicts future
    behaviour: a strategy routinely clipped by limits is being defined by the risk engine
    rather than by its own thesis, and scoring it only on the exposure it was permitted
    grades the risk engine instead of the strategy.
    """

    violations_by_strategy: Mapping[str, int] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "violations_by_strategy", MappingProxyType(dict(self.violations_by_strategy))
        )

    def violation_count_for(self, strategy_id: str) -> int:
        """How many refusals this strategy has accumulated."""
        return self.violations_by_strategy.get(strategy_id, 0)

    def with_assessment(self, assessment: ExposureAssessment) -> ViolationTally:
        """The tally after `assessment`. Unchanged when the signal was approved."""
        if assessment.is_approved:
            return self
        tally = dict(self.violations_by_strategy)
        tally[assessment.strategy_id] = tally.get(assessment.strategy_id, 0) + 1
        return ViolationTally(violations_by_strategy=tally)


def _evaluations_for(
    *,
    instrument: Instrument,
    direction: Direction,
    exposure: PortfolioExposure,
    context: PreTradeContext,
) -> tuple[LimitEvaluation, ...]:
    """Every limit that governs opening exposure, evaluated against the current book.

    Relative and absolute caps are separate rows rather than a pre-computed `min()`, so
    the audit row shows which of the pair was tighter at this equity. Collapsing them
    loses exactly the information that distinguishes "the book is large" from "the equity
    number is wrong".
    """
    equity_usd = context.equity_usd
    exposure_limits = context.exposure_limits
    absolute_limits = context.absolute_limits
    directional_sign = _ONE if direction is Direction.LONG else -_ONE
    # Margin consumed by the open book. Free margin is what is left of equity after it,
    # so the gross the book may carry scales with leverage -- which is why this reads
    # `max_leverage` from the absolute limits rather than assuming 1x.
    used_margin_usd = exposure.gross_notional_usd / absolute_limits.max_leverage

    return (
        LimitEvaluation(
            limit_name="max_position_equity_ratio",
            bound_kind="ceiling",
            threshold_usd=exposure_limits.max_position_equity_ratio * equity_usd,
            observed_usd=exposure.held_notional_usd(instrument),
        ),
        LimitEvaluation(
            limit_name="max_position_notional_usd",
            bound_kind="ceiling",
            threshold_usd=absolute_limits.max_position_notional_usd,
            observed_usd=exposure.held_notional_usd(instrument),
        ),
        LimitEvaluation(
            limit_name="max_asset_exposure_ratio",
            bound_kind="ceiling",
            threshold_usd=exposure_limits.max_asset_exposure_ratio * equity_usd,
            observed_usd=exposure.asset_notional_usd(instrument.base_asset),
        ),
        LimitEvaluation(
            limit_name="max_gross_exposure_ratio",
            bound_kind="ceiling",
            threshold_usd=exposure_limits.max_gross_exposure_ratio * equity_usd,
            observed_usd=exposure.gross_notional_usd,
        ),
        LimitEvaluation(
            limit_name="max_portfolio_notional_usd",
            bound_kind="ceiling",
            threshold_usd=absolute_limits.max_portfolio_notional_usd,
            observed_usd=exposure.gross_notional_usd,
        ),
        # Net is signed *toward the direction being opened*: a long added to a net-short
        # book reduces the number this limit governs, so the observed value is the net
        # projected onto the proposed direction rather than its magnitude.
        LimitEvaluation(
            limit_name="max_net_exposure_ratio",
            bound_kind="ceiling",
            threshold_usd=exposure_limits.max_net_exposure_ratio * equity_usd,
            observed_usd=exposure.net_notional_usd * directional_sign,
        ),
        # A floor: observed free margin must stay at or above the threshold, so headroom
        # is observed minus threshold and the sign convention still reads "room left".
        # Converted to a notional allowance by the caller through `max_leverage`.
        LimitEvaluation(
            limit_name="min_free_margin_ratio",
            bound_kind="floor",
            threshold_usd=exposure_limits.min_free_margin_ratio * equity_usd,
            observed_usd=equity_usd - used_margin_usd,
        ),
        # The order-level cap. Observed is zero because no order exists yet -- this limit
        # bounds the increment rather than the book, and the row records that explicitly
        # so a reader does not read a zero as "no exposure".
        LimitEvaluation(
            limit_name="max_single_order_notional_usd",
            bound_kind="ceiling",
            threshold_usd=absolute_limits.max_single_order_notional_usd,
            observed_usd=_ZERO,
        ),
    )


def _permitted_notional_usd(
    evaluations: tuple[LimitEvaluation, ...], *, max_leverage: Decimal
) -> tuple[Decimal, str]:
    """The tightest headroom across every limit, and the name of the limit that bound.

    The free-margin row is headroom in *margin* USD; every other row is headroom in
    notional USD. Multiplying by leverage converts it, and doing that conversion here --
    once, next to the comment explaining it -- is what keeps a margin number from being
    compared against a notional number somewhere further down.
    """
    binding_limit_name = evaluations[0].limit_name
    tightest_usd = evaluations[0].headroom_usd
    for evaluation in evaluations:
        headroom_usd = evaluation.headroom_usd
        if evaluation.limit_name == "min_free_margin_ratio":
            headroom_usd = headroom_usd * max_leverage
        if headroom_usd < tightest_usd:
            tightest_usd = headroom_usd
            binding_limit_name = evaluation.limit_name
    return max(tightest_usd, _ZERO), binding_limit_name


def _reduce_only_assessment(
    *,
    signal: Signal,
    exposure: PortfolioExposure,
    context: PreTradeContext,
    decided_at_utc: datetime,
) -> ExposureAssessment:
    """A flat signal closes what is open and is never refused for an exposure breach."""
    return ExposureAssessment(
        strategy_id=signal.strategy_id,
        instrument=signal.instrument,
        direction=Direction.FLAT,
        decided_at_utc=decided_at_utc,
        equity_usd=context.equity_usd,
        evaluations=(),
        permitted_notional_usd=exposure.held_notional_usd(signal.instrument),
        permitted_base_quantity=_ZERO,
        rejection=None,
    )


def _refused(
    *,
    signal: Signal,
    context: PreTradeContext,
    decided_at_utc: datetime,
    evaluations: tuple[LimitEvaluation, ...],
    rejection: Rejection,
) -> ExposureAssessment:
    return ExposureAssessment(
        strategy_id=signal.strategy_id,
        instrument=signal.instrument,
        direction=signal.direction,
        decided_at_utc=decided_at_utc,
        equity_usd=context.equity_usd,
        evaluations=evaluations,
        permitted_notional_usd=_ZERO,
        permitted_base_quantity=_ZERO,
        rejection=rejection,
    )


def validate_pre_trade(
    *,
    signal: Signal,
    portfolio: Portfolio,
    marks_usd: Mapping[Instrument, Decimal],
    context: PreTradeContext,
    clock: Clock,
) -> ExposureAssessment:
    """Decide how much exposure, if any, `signal` may be given.

    Order of operations is a correctness property, not a style choice. The universe check
    runs first and returns before `marks_usd` is read at all: a symbol the venue does not
    list cannot be sized, and sizing arithmetic against a symbol outside the resolved
    venue-intersection universe would produce a plausible number for an order that can
    never be placed (`docs/rules/exchange-integration.md` clause 10).
    """
    decided_at_utc = clock()

    if signal.instrument.symbol not in context.tradable_symbols:
        return _refused(
            signal=signal,
            context=context,
            decided_at_utc=decided_at_utc,
            evaluations=(),
            rejection=Rejection(
                reason=(
                    f"{signal.instrument.symbol} is not in the resolved tradable "
                    f"universe for {signal.instrument.venue}"
                ),
                binding_limit_name="tradable_universe",
                rejected_at_utc=decided_at_utc,
            ),
        )

    exposure = portfolio_exposure(portfolio, marks_usd)

    if signal.direction is Direction.FLAT:
        return _reduce_only_assessment(
            signal=signal,
            exposure=exposure,
            context=context,
            decided_at_utc=decided_at_utc,
        )

    mark_usd = marks_usd.get(signal.instrument)
    if mark_usd is None or mark_usd <= _ZERO:
        return _refused(
            signal=signal,
            context=context,
            decided_at_utc=decided_at_utc,
            evaluations=(),
            rejection=Rejection(
                reason=(
                    f"no usable mark for {signal.instrument.symbol}; a position cannot "
                    f"be sized against a price the system does not have"
                ),
                binding_limit_name="mark_available",
                rejected_at_utc=decided_at_utc,
            ),
        )

    evaluations = _evaluations_for(
        instrument=signal.instrument,
        direction=signal.direction,
        exposure=exposure,
        context=context,
    )
    permitted_notional_usd, binding_limit_name = _permitted_notional_usd(
        evaluations, max_leverage=context.absolute_limits.max_leverage
    )

    if permitted_notional_usd <= _ZERO:
        return _refused(
            signal=signal,
            context=context,
            decided_at_utc=decided_at_utc,
            evaluations=evaluations,
            rejection=Rejection(
                reason=(
                    f"{binding_limit_name} leaves no headroom for "
                    f"{signal.instrument.symbol}: threshold and observed values are in "
                    f"limits_evaluated"
                ),
                binding_limit_name=binding_limit_name,
                rejected_at_utc=decided_at_utc,
            ),
        )

    # ROUND_DOWN, through the instrument's own lattice. Truncation toward zero can only
    # ask for less than the headroom allows; ROUND_HALF_UP would round *up* through a
    # limit that was just proved binding, and it would do it on the largest orders.
    permitted_base_quantity = signal.instrument.quantize_base_quantity(
        permitted_notional_usd / mark_usd
    )
    if permitted_base_quantity <= _ZERO or not signal.instrument.meets_min_notional(
        permitted_base_quantity, mark_usd
    ):
        return _refused(
            signal=signal,
            context=context,
            decided_at_utc=decided_at_utc,
            evaluations=evaluations,
            rejection=Rejection(
                reason=(
                    f"permitted notional {permitted_notional_usd} on "
                    f"{signal.instrument.symbol} quantizes to "
                    f"{permitted_base_quantity}, below the venue MIN_NOTIONAL of "
                    f"{signal.instrument.min_notional_quote}"
                ),
                binding_limit_name="min_notional_quote",
                rejected_at_utc=decided_at_utc,
            ),
        )

    return ExposureAssessment(
        strategy_id=signal.strategy_id,
        instrument=signal.instrument,
        direction=signal.direction,
        decided_at_utc=decided_at_utc,
        equity_usd=context.equity_usd,
        evaluations=evaluations,
        permitted_notional_usd=permitted_notional_usd,
        permitted_base_quantity=permitted_base_quantity,
        rejection=None,
    )
