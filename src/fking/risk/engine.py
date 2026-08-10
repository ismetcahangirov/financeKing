"""`RiskEngine.decide()`: the only path from `Signal` to `Order`.

Three designs for a risk engine get confused with each other, and only one of them
survives having its callers written by an LLM agent that has not read this file:

- **Risk as a library** (`quantity = risk.size_for(...)`). A strategy can call it, ignore
  it, call it wrong, or cache the answer and reuse it against a portfolio that has since
  moved. The check is then worth exactly the caller's discipline.
- **Risk as middleware.** A strategy can have an order rejected after it exists -- but the
  quantity in it was chosen by somebody else, so the rejection is a veto over a decision
  already taken rather than authority over the decision.
- **Risk as the sole constructor of `Order`.** A strategy can emit a `Signal` and nothing
  else, because a strategy that wants to size has no type in which to express it.

This module is the third. The rigidity is not about human discipline. The quieter second
reason is attribution: because exactly one component constructs orders, exactly one
component sees the whole portfolio at decision time -- and per-strategy sizing does not
merely permit mistakes, it makes correlation-aware limits *uncomputable*, since a strategy
that sees only its own book cannot evaluate `CTR_i / sigma_p` (`RISK_PHILOSOPHY.md`).

**The kill-switch check is the first statement.** Before the conviction floor, before
sizing, before any portfolio read. A tripped switch returns rejections and does no sizing
work: a halted system should not be computing covariance matrices, and every microsecond
spent doing so is a microsecond in which the halt is not yet reflected anywhere.
`KillSwitchGate.ensure_trading()` is synchronous and this method is synchronous, so there
is no interleaving between the check and the construction of an `Order` (issue #53).

**Nothing here is reimplemented.** Conviction calibration (#49), position sizing (#48),
exposure limits (#50), correlation-aware concentration (#51), drawdown and loss limits
(#52) and the kill switch (#53) are all composed as they stand. A second implementation of
any of them inside this file would be a second opinion about the same limit, and the one
that disagreed would be whichever the reader was not looking at.

**Order identity is derived, never drawn.** `order_id` and `client_order_id` come from a
digest over the correlation id, the run seed, the instrument, the side, the quantity and
the decision instant. The same inputs replay to byte-identical orders in another process,
which is what makes a backtest and a demo run comparable, and it is also what makes a
retried submission recognisable to the venue as the same order rather than a second one
(`docs/rules/idempotency.md`). The `client_order_id` is emitted in the intersection of
every shipped venue's charset -- `fk-` plus 32 lowercase hex, 35 characters, inside
Binance's 36-character `[.A-Z:/a-z0-9_-]` filter -- so `execution` validates it against the
venue rather than deriving a second one that would not match this audit row.

**A breach is a returned value, never an exception.** `decide()` raises only for inputs
that are malformed rather than refused. A limit breach is an ordinary, frequent, expected
outcome, and raising would put it on the same code path as a bug
(`docs/rules/error-handling.md`).

Pure: no clock read, no I/O, no randomness. `clock` is a parameter, called exactly once, and
the single instant it returns is threaded through every term so that no two limits are
evaluated against different moments.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Final
from uuid import UUID, uuid5

from fking.domain import (
    Direction,
    DomainError,
    Instrument,
    Order,
    OrderType,
    Portfolio,
    RiskVerdict,
    Side,
    Signal,
    TimeInForce,
)
from fking.risk._killswitch import KillSwitchGate, KillSwitchTrippedError
from fking.risk.calibration import (
    CalibrationMap,
    ConvictionAssessment,
    ConvictionParameters,
    assess_conviction,
)
from fking.risk.contribution import (
    ConcentrationAssessment,
    ConcentrationLimits,
    assess_concentration,
    weight_ratios_from_notional,
)
from fking.risk.covariance import RiskModel
from fking.risk.drawdown import DrawdownBudgets, DrawdownState, LimitVerdict, evaluate
from fking.risk.exposure import (
    Clock,
    ExposureAssessment,
    ExposureLimits,
    PreTradeContext,
    Rejection,
    validate_pre_trade,
)
from fking.risk.limits import RiskLimits
from fking.risk.netting import (
    Attribution,
    CrossingResidual,
    NetOrderPlan,
    StrategyRequest,
    net_requests,
)
from fking.risk.sizing import PositionSizing, SizingInputs, SizingParameters, size_position

__all__ = [
    "ORDER_ID_NAMESPACE",
    "STAGES",
    "DecisionBatch",
    "InstrumentMarketState",
    "MarketState",
    "PortfolioState",
    "RiskEngine",
    "RiskPolicy",
    "SignalAudit",
    "StrategyState",
]

_ZERO: Final = Decimal("0")

# A fixed UUIDv5 namespace, so an order id is a pure function of the decision that produced
# it and reproduces in any process. Generated once and pinned here; changing it would make
# every historical order id unreproducible, which is a change to the audit trail rather
# than a refactor.
ORDER_ID_NAMESPACE: Final = UUID("6f2b7d64-7f43-5f9c-9f0e-2c9c8f9a4d11")

# Length chosen against the tightest shipped venue filter: Binance's clientOrderId is at
# most 36 characters, so `fk-` plus 32 hex leaves a character of margin and uses only
# characters that filter admits.
_CLIENT_ORDER_ID_DIGEST_LENGTH: Final = 32

_STAGE_KILL_SWITCH: Final = "kill_switch"
_STAGE_PORTFOLIO_DRAWDOWN: Final = "portfolio_drawdown"
_STAGE_SIGNAL_ADMISSION: Final = "signal_admission"
_STAGE_CONVICTION: Final = "conviction"
_STAGE_EXPOSURE: Final = "exposure"
_STAGE_SIZING: Final = "sizing"
_STAGE_NETTING: Final = "netting"
_STAGE_CONCENTRATION: Final = "concentration"
_STAGE_ORDER_CONSTRUCTION: Final = "order_construction"

# Declared in execution order. `DecisionBatch.stages_executed` is a prefix of this tuple,
# which is what lets a test assert "the kill switch ran and nothing else did" against an
# artifact rather than against a mock's call log.
STAGES: Final[tuple[str, ...]] = (
    _STAGE_KILL_SWITCH,
    _STAGE_PORTFOLIO_DRAWDOWN,
    _STAGE_SIGNAL_ADMISSION,
    _STAGE_CONVICTION,
    _STAGE_EXPOSURE,
    _STAGE_SIZING,
    _STAGE_NETTING,
    _STAGE_CONCENTRATION,
    _STAGE_ORDER_CONSTRUCTION,
)


def _limit_row(
    *, limit_name: str, threshold: Decimal, observed: Decimal, is_breached: bool
) -> Mapping[str, object]:
    """One evaluated limit in the flat shape every audit row uses.

    Decimals render as strings. The payload lands in `jsonb`, and a JSON encoder that has
    not been told otherwise turns a `Decimal` into a float on the way into an append-only
    table that can never be corrected.
    """
    return MappingProxyType(
        {
            "limit_name": limit_name,
            "threshold": str(threshold),
            "observed": str(observed),
            "is_breached": is_breached,
        }
    )


def _require_finite_decimal(candidate: object, field_name: str) -> Decimal:
    """A finite `Decimal`, refusing a `float` by name.

    Typed `object` rather than `Decimal` on purpose: annotating the parameter lets mypy
    prove the `float` branch unreachable and report it as dead code, which is true of every
    *typed* caller and false of the untyped ones this guard exists for. `fking.risk.drawdown`
    takes the same shape for the same reason.
    """
    if isinstance(candidate, float):
        raise DomainError(
            f"{field_name} must be a Decimal constructed from str, not a float; "
            f"got {candidate!r}, which is already rounded"
        )
    if not isinstance(candidate, Decimal):
        raise DomainError(
            f"{field_name} must be a Decimal, got {type(candidate).__name__} {candidate!r}"
        )
    if not candidate.is_finite():
        raise DomainError(f"{field_name} must be finite; got {candidate}")
    return candidate


@dataclass(frozen=True, slots=True)
class InstrumentMarketState:
    """Everything the terms read about one instrument, at one instant.

    `mark_usd` prices the book; `mid_quote_price` is the booking price for a perfect
    internal cross. They are separate fields because they have different provenance -- a
    mark can be a last trade, an index or a settlement price, while a mid is a live
    top-of-book quantity -- and substituting one for the other is how an internal cross
    ends up booked at a price nobody was ever quoted.
    """

    instrument: Instrument
    mark_usd: Decimal
    mid_quote_price: Decimal
    atr_14_quote: Decimal
    return_series: tuple[Decimal, ...]
    volatility_floor_annualised: Decimal

    def __post_init__(self) -> None:
        for field_name in ("mark_usd", "mid_quote_price"):
            candidate = _require_finite_decimal(getattr(self, field_name), field_name)
            if candidate <= _ZERO:
                raise DomainError(
                    f"{field_name} for {self.instrument.symbol} is {candidate}; a position "
                    f"cannot be sized or booked against a non-positive price"
                )


@dataclass(frozen=True, slots=True)
class MarketState:
    """The market half of a decision: one snapshot per instrument, plus the risk model.

    One value rather than four parameters because these must all describe the same
    instant. A mark read at one moment, evaluated against a covariance matrix estimated at
    another, produces a `sigma_p` that describes no portfolio that ever existed.
    """

    instruments: Mapping[Instrument, InstrumentMarketState]
    risk_model: RiskModel

    def __post_init__(self) -> None:
        for instrument, state in self.instruments.items():
            if state.instrument != instrument:
                # A key that disagrees with its value is the shape a partial rename leaves
                # behind, and every lookup afterwards silently prices another symbol.
                raise DomainError(
                    f"instruments is keyed {instrument.symbol!r} but holds market state "
                    f"for {state.instrument.symbol!r}"
                )
        object.__setattr__(self, "instruments", MappingProxyType(dict(self.instruments)))

    @property
    def marks_usd(self) -> Mapping[Instrument, Decimal]:
        """The mark per instrument, in the shape `fking.risk.exposure` reads."""
        return MappingProxyType(
            {instrument: state.mark_usd for instrument, state in self.instruments.items()}
        )


@dataclass(frozen=True, slots=True)
class StrategyState:
    """One strategy's own record, as of the decision instant.

    The realised moments are the strategy's own closed-trade distribution, not the
    backtest's: sizing a live strategy off its backtest's Kelly is sizing off the
    distribution that was optimised against (`docs/rules/overfitting-defences.md`).
    """

    strategy_id: str
    calibration: CalibrationMap
    closed_trade_count: int
    realised_mean_return_fraction: Decimal
    realised_return_stdev_fraction: Decimal
    drawdown_state: DrawdownState | None = None

    def __post_init__(self) -> None:
        if self.calibration.strategy_id != self.strategy_id:
            raise DomainError(
                f"strategy state for {self.strategy_id!r} carries a calibration map "
                f"belonging to {self.calibration.strategy_id!r}; a map is a claim about "
                f"one strategy's record and means nothing applied to another"
            )


@dataclass(frozen=True, slots=True)
class PortfolioState:
    """The account half of a decision.

    `drawdown_state` is portfolio-scoped and optional in exactly one situation: a process
    that has never marked equity has no high-water mark to measure against. It is not a
    way to skip the limit -- `evaluate` is called whenever the state is present, and a
    state absent while positions are open is refused below.
    """

    portfolio: Portfolio
    equity_usd: Decimal
    strategies: Mapping[str, StrategyState]
    drawdown_state: DrawdownState | None = None

    def __post_init__(self) -> None:
        _require_finite_decimal(self.equity_usd, "equity_usd")
        for strategy_id, state in self.strategies.items():
            if state.strategy_id != strategy_id:
                raise DomainError(
                    f"strategies is keyed {strategy_id!r} but holds state for {state.strategy_id!r}"
                )
        object.__setattr__(self, "strategies", MappingProxyType(dict(self.strategies)))


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """Every configurable limit the engine applies, as one value.

    Defaults are the shipped baseline of each term, so an engine constructed with no policy
    runs at the tightest limits this system ships. A missing configuration file must never
    produce a less conservative system than a present one.
    """

    limits: RiskLimits = field(default_factory=RiskLimits)
    exposure_limits: ExposureLimits = field(default_factory=ExposureLimits)
    concentration_limits: ConcentrationLimits = field(default_factory=ConcentrationLimits)
    sizing_parameters: SizingParameters = field(default_factory=SizingParameters)
    conviction_parameters: ConvictionParameters = field(default_factory=ConvictionParameters)
    portfolio_drawdown_budgets: DrawdownBudgets = field(
        default_factory=lambda: DrawdownBudgets(
            scope="portfolio",
            drawdown_ratio=Decimal("0.10"),
            daily_loss_ratio=Decimal("0.03"),
        )
    )
    strategy_drawdown_budgets: DrawdownBudgets = field(
        default_factory=lambda: DrawdownBudgets(
            scope="strategy",
            drawdown_ratio=Decimal("0.15"),
            daily_loss_ratio=Decimal("0.03"),
        )
    )
    tradable_symbols: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.tradable_symbols, frozenset):
            raise DomainError(
                f"tradable_symbols must be a frozenset, got "
                f"{type(self.tradable_symbols).__name__}; a mutable universe is a universe "
                f"that can widen between two checks"
            )


@dataclass(frozen=True, slots=True)
class SignalAudit:
    """One signal's complete record: the verdict, and every limit that produced it.

    Written on both paths. A rejection that produced no row would leave the audit log
    unable to distinguish "the risk engine said no" from "the consumer crashed before it
    ran" (`fking.domain.risk_decision`), and those are different incidents with different
    responses.
    """

    strategy_id: str
    instrument: Instrument
    direction: Direction
    decided_at_utc: datetime
    verdict: RiskVerdict
    stage: str
    rejection: Rejection | None
    conviction: ConvictionAssessment | None
    exposure: ExposureAssessment | None
    sizing: PositionSizing | None
    portfolio_drawdown: LimitVerdict | None
    strategy_drawdown: LimitVerdict | None
    portfolio_drawdown_budgets: DrawdownBudgets | None
    strategy_drawdown_budgets: DrawdownBudgets | None
    concentration: ConcentrationAssessment | None
    client_order_id: str | None
    attributed_signed_base_quantity: Decimal | None
    booked_quote_price: Decimal | None

    @property
    def is_approved(self) -> bool:
        """Whether this signal contributed quantity to an order."""
        return self.verdict is RiskVerdict.APPROVED

    def limit_rows(self) -> tuple[Mapping[str, object], ...]:
        """Every limit evaluated for this signal, with its threshold and observed value.

        Flattened from the terms rather than nested per term. The question asked in every
        post-incident review is "how close were the others", and a reader scanning one flat
        list can answer it without knowing which module owns which limit.
        """
        rows: list[Mapping[str, object]] = []
        if self.conviction is not None:
            rows.append(
                _limit_row(
                    limit_name="conviction_floor",
                    threshold=self.conviction.conviction_floor,
                    observed=self.conviction.reported_conviction,
                    is_breached=not self.conviction.is_approved,
                )
            )
        if self.exposure is not None:
            rows.extend(
                _limit_row(
                    limit_name=evaluation.limit_name,
                    threshold=evaluation.threshold_usd,
                    observed=evaluation.observed_usd,
                    is_breached=evaluation.is_breached,
                )
                for evaluation in self.exposure.evaluations
            )
        for scope, verdict, budgets in (
            ("portfolio", self.portfolio_drawdown, self.portfolio_drawdown_budgets),
            ("strategy", self.strategy_drawdown, self.strategy_drawdown_budgets),
        ):
            if verdict is None or budgets is None:
                continue
            breach = verdict.breach
            rows.extend(
                _limit_row(
                    limit_name=f"{scope}_{limit_name}",
                    threshold=budgets.budget_for(limit_name),
                    observed=observed_ratio,
                    is_breached=breach is not None and breach.limit_name == limit_name,
                )
                for limit_name, observed_ratio in verdict.observed_ratios.items()
            )
        if self.concentration is not None:
            rows.extend(
                _limit_row(
                    limit_name=f"{evaluation.limit_name}:{evaluation.scope_id}",
                    threshold=evaluation.threshold_ratio,
                    observed=evaluation.observed_ratio,
                    is_breached=evaluation.is_breached,
                )
                for evaluation in self.concentration.evaluations
            )
        return tuple(rows)

    def audit_payload(self) -> Mapping[str, object]:
        """The row body for this signal, accepted or rejected."""
        return MappingProxyType(
            {
                "strategy_id": self.strategy_id,
                "venue": str(self.instrument.venue),
                "symbol": self.instrument.symbol,
                "direction": str(self.direction),
                "decided_at_utc": self.decided_at_utc.isoformat(),
                "verdict": str(self.verdict),
                "stage": self.stage,
                "rejection_reason": None if self.rejection is None else self.rejection.reason,
                "binding_limit_name": (
                    None if self.rejection is None else self.rejection.binding_limit_name
                ),
                "client_order_id": self.client_order_id,
                "attributed_signed_base_quantity": (
                    None
                    if self.attributed_signed_base_quantity is None
                    else str(self.attributed_signed_base_quantity)
                ),
                "booked_quote_price": (
                    None if self.booked_quote_price is None else str(self.booked_quote_price)
                ),
                "sizing": None if self.sizing is None else self.sizing.audit_payload(),
                "limits_evaluated": self.limit_rows(),
            }
        )


@dataclass(frozen=True, slots=True)
class DecisionBatch:
    """Everything one call to `decide()` produced.

    Issue #55 specifies `decide()` as returning `list[Order | Rejection]`, and `outcomes`
    is exactly that list; iterating the batch iterates it. The wrapper exists because the
    same call must also emit the audit rows and the crossing attribution, and a caller
    obliged to correlate three return values will eventually drop one -- most likely the
    audit row, which is the one nobody misses until an investigation needs it.
    """

    correlation_id: UUID
    decided_at_utc: datetime
    outcomes: tuple[Order | Rejection, ...]
    plans: tuple[NetOrderPlan, ...]
    audits: tuple[SignalAudit, ...]
    stages_executed: tuple[str, ...]

    def __iter__(self) -> Iterator[Order | Rejection]:
        return iter(self.outcomes)

    @property
    def orders(self) -> tuple[Order, ...]:
        """The orders to submit. Empty is a legitimate outcome, not a failure."""
        return tuple(outcome for outcome in self.outcomes if isinstance(outcome, Order))

    @property
    def rejections(self) -> tuple[Rejection, ...]:
        """Every refusal, as a value."""
        return tuple(outcome for outcome in self.outcomes if isinstance(outcome, Rejection))

    @property
    def residuals(self) -> tuple[CrossingResidual, ...]:
        """The crossing rows: one per instrument on which anything crossed internally."""
        return tuple(plan.residual for plan in self.plans if plan.residual is not None)

    def attributions_for(self, instrument: Instrument) -> tuple[Attribution, ...]:
        """Who owns which part of the net order on `instrument`."""
        for plan in self.plans:
            if plan.instrument == instrument:
                return plan.attributions
        return ()

    def audit_payloads(self) -> tuple[Mapping[str, object], ...]:
        """One row per signal, accepted and rejected alike."""
        return tuple(audit.audit_payload() for audit in self.audits)


@dataclass(frozen=True, slots=True)
class _Admitted:
    """One signal that survived to netting, with the terms that sized it."""

    signal: Signal
    market: InstrumentMarketState
    request: StrategyRequest
    conviction: ConvictionAssessment | None
    exposure: ExposureAssessment
    sizing: PositionSizing | None
    strategy_drawdown: LimitVerdict | None


class RiskEngine:
    """The sole constructor of `Order`.

    Holds the policy and the kill-switch gate; holds no state that a decision mutates. The
    gate is mutable by design -- it is infrastructure, not a domain object -- and the state
    it hands out is frozen, so a decision reads a value nothing can change underneath it.
    """

    __slots__ = ("_gate", "_policy")

    def __init__(self, *, policy: RiskPolicy, kill_switch: KillSwitchGate) -> None:
        self._policy = policy
        self._gate = kill_switch

    @property
    def policy(self) -> RiskPolicy:
        """The limits in force. Frozen; safe to log beside the decision it produced."""
        return self._policy

    def decide(
        self,
        *,
        signals: Sequence[Signal],
        portfolio_state: PortfolioState,
        market_state: MarketState,
        clock: Clock,
        correlation_id: UUID,
        seed: int,
    ) -> DecisionBatch:
        """Turn a batch of signals into net orders and audited refusals.

        `seed` participates in order identity and in nothing else. The engine draws no
        randomness; the seed is what keeps two different replay runs of the same batch --
        the same signals re-decided under a different scenario -- from colliding on a
        `client_order_id` at the venue, while keeping one run byte-identical to itself.
        """
        # First statement. Nothing above it, including the clock read: a halted system
        # should not be doing work, and "no sizing ran" is only checkable if nothing did.
        try:
            self._gate.ensure_trading()
        except KillSwitchTrippedError as tripped:
            return self._halted_batch(
                signals=signals,
                clock=clock,
                correlation_id=correlation_id,
                halted_reason=str(tripped),
            )

        decided_at_utc = clock()

        def frozen_clock() -> datetime:
            """One instant for every term. Two limits evaluated at two moments can both
            pass while the portfolio satisfied neither at any single one."""
            return decided_at_utc

        stages = [_STAGE_KILL_SWITCH]
        audits: list[SignalAudit] = []
        outcomes: list[Order | Rejection] = []

        stages.append(_STAGE_PORTFOLIO_DRAWDOWN)
        portfolio_drawdown = (
            evaluate(portfolio_state.drawdown_state, self._policy.portfolio_drawdown_budgets)
            if portfolio_state.drawdown_state is not None
            else None
        )
        halt_reason = self._portfolio_halt_reason(
            portfolio_state=portfolio_state,
            market_state=market_state,
            verdict=portfolio_drawdown,
        )
        if halt_reason is not None:
            return self._refuse_batch(
                signals=signals,
                decided_at_utc=decided_at_utc,
                correlation_id=correlation_id,
                stages=tuple(stages),
                stage=_STAGE_PORTFOLIO_DRAWDOWN,
                reason=halt_reason[0],
                binding_limit_name=halt_reason[1],
                portfolio_drawdown=portfolio_drawdown,
            )

        stages.extend((_STAGE_SIGNAL_ADMISSION, _STAGE_CONVICTION, _STAGE_EXPOSURE, _STAGE_SIZING))
        admitted, sized_audits, refusals = self._size_signals(
            signals=signals,
            portfolio_state=portfolio_state,
            market_state=market_state,
            decided_at_utc=decided_at_utc,
            frozen_clock=frozen_clock,
            portfolio_drawdown=portfolio_drawdown,
        )
        audits.extend(sized_audits)
        outcomes.extend(refusals)

        stages.append(_STAGE_NETTING)
        plans, netted, rejected_groups = self._net(
            admitted=admitted, market_state=market_state, decided_at_utc=decided_at_utc
        )
        for admission, rejection in rejected_groups:
            audits.append(
                self._audit_for(
                    admission=admission,
                    decided_at_utc=decided_at_utc,
                    stage=_STAGE_NETTING,
                    rejection=rejection,
                    portfolio_drawdown=portfolio_drawdown,
                )
            )
            outcomes.append(rejection)

        stages.append(_STAGE_CONCENTRATION)
        concentration = self._assess_concentration(
            plans=plans,
            portfolio_state=portfolio_state,
            market_state=market_state,
            frozen_clock=frozen_clock,
        )
        if isinstance(concentration, Rejection):
            for admission in netted:
                audits.append(
                    self._audit_for(
                        admission=admission,
                        decided_at_utc=decided_at_utc,
                        stage=_STAGE_CONCENTRATION,
                        rejection=concentration,
                        portfolio_drawdown=portfolio_drawdown,
                    )
                )
                outcomes.append(concentration)
            return DecisionBatch(
                correlation_id=correlation_id,
                decided_at_utc=decided_at_utc,
                outcomes=tuple(outcomes),
                plans=(),
                audits=tuple(audits),
                stages_executed=tuple(stages),
            )

        stages.append(_STAGE_ORDER_CONSTRUCTION)
        orders_by_instrument = self._construct_orders(
            plans=plans,
            decided_at_utc=decided_at_utc,
            correlation_id=correlation_id,
            seed=seed,
        )
        outcomes.extend(orders_by_instrument.values())

        plans_by_instrument = {plan.instrument: plan for plan in plans}
        for admission in netted:
            plan = plans_by_instrument[admission.signal.instrument]
            attribution = next(
                candidate
                for candidate in plan.attributions
                if candidate.strategy_id == admission.signal.strategy_id
            )
            order = orders_by_instrument.get(plan.instrument)
            audits.append(
                self._audit_for(
                    admission=admission,
                    decided_at_utc=decided_at_utc,
                    stage=_STAGE_ORDER_CONSTRUCTION,
                    rejection=None,
                    portfolio_drawdown=portfolio_drawdown,
                    concentration=concentration,
                    attribution=attribution,
                    client_order_id=None if order is None else order.client_order_id,
                )
            )

        return DecisionBatch(
            correlation_id=correlation_id,
            decided_at_utc=decided_at_utc,
            outcomes=tuple(outcomes),
            plans=tuple(plans),
            audits=tuple(audits),
            stages_executed=tuple(stages),
        )

    # -- stage 0: the kill switch and the portfolio-wide halts ---------------------------

    def _halted_batch(
        self,
        *,
        signals: Sequence[Signal],
        clock: Clock,
        correlation_id: UUID,
        halted_reason: str,
    ) -> DecisionBatch:
        """Every signal refused, with no sizing and no covariance work performed."""
        decided_at_utc = clock()
        return self._refuse_batch(
            signals=signals,
            decided_at_utc=decided_at_utc,
            correlation_id=correlation_id,
            stages=(_STAGE_KILL_SWITCH,),
            stage=_STAGE_KILL_SWITCH,
            reason=halted_reason,
            binding_limit_name="kill_switch",
            portfolio_drawdown=None,
        )

    def _refuse_batch(
        self,
        *,
        signals: Sequence[Signal],
        decided_at_utc: datetime,
        correlation_id: UUID,
        stages: tuple[str, ...],
        stage: str,
        reason: str,
        binding_limit_name: str,
        portfolio_drawdown: LimitVerdict | None,
    ) -> DecisionBatch:
        """Refuse the whole batch for one portfolio-wide cause, with a row per signal."""
        rejection = Rejection(
            reason=reason,
            binding_limit_name=binding_limit_name,
            rejected_at_utc=decided_at_utc,
        )
        audits = tuple(
            SignalAudit(
                strategy_id=signal.strategy_id,
                instrument=signal.instrument,
                direction=signal.direction,
                decided_at_utc=decided_at_utc,
                verdict=RiskVerdict.REJECTED,
                stage=stage,
                rejection=rejection,
                conviction=None,
                exposure=None,
                sizing=None,
                portfolio_drawdown=portfolio_drawdown,
                strategy_drawdown=None,
                portfolio_drawdown_budgets=self._policy.portfolio_drawdown_budgets,
                strategy_drawdown_budgets=None,
                concentration=None,
                client_order_id=None,
                attributed_signed_base_quantity=None,
                booked_quote_price=None,
            )
            for signal in signals
        )
        return DecisionBatch(
            correlation_id=correlation_id,
            decided_at_utc=decided_at_utc,
            outcomes=tuple(rejection for _ in signals),
            plans=(),
            audits=audits,
            stages_executed=stages,
        )

    def _portfolio_halt_reason(
        self,
        *,
        portfolio_state: PortfolioState,
        market_state: MarketState,
        verdict: LimitVerdict | None,
    ) -> tuple[str, str] | None:
        """Why the whole batch must be refused before any signal is looked at, or `None`."""
        unpriced = sorted(
            position.instrument.symbol
            for position in portfolio_state.portfolio.open_positions
            if position.instrument not in market_state.instruments
        )
        if unpriced:
            # Batch-wide rather than per-signal. Every notional limit below is evaluated
            # against the whole book, so an unpriced position makes all of them wrong at
            # once -- and the direction it makes them wrong in is the one that authorises
            # the largest orders, because absent exposure reads as headroom.
            return (
                f"{unpriced} are held and carry no market state; exposure cannot be priced "
                f"and must not be assumed zero",
                "market_state_present",
            )
        uncovered = sorted(
            {position.instrument.symbol for position in portfolio_state.portfolio.open_positions}
            - set(market_state.risk_model.symbols)
        )
        if uncovered:
            return (
                f"{uncovered} are held and lie outside the estimated risk universe; the "
                f"position nobody has an estimate for is the one most likely to be the "
                f"problem",
                "risk_model_coverage",
            )
        if portfolio_state.equity_usd <= _ZERO:
            return (
                f"portfolio equity is {portfolio_state.equity_usd}; every limit here is a "
                f"fraction of equity and none of them is defined against a non-positive one",
                "equity_usd",
            )
        if portfolio_state.drawdown_state is None and portfolio_state.portfolio.open_positions:
            return (
                "the portfolio holds open positions and carries no drawdown state; a "
                "high-water mark that was lost is a drawdown limit that silently never binds",
                "drawdown_state_present",
            )
        breach = verdict.breach if verdict is not None else None
        if breach is not None:
            return (
                f"portfolio {breach.limit_name} is {breach.observed_ratio} against a budget "
                f"of {breach.budget_ratio}; the halt is latched and recovery does not "
                f"retract the evidence",
                f"portfolio_{breach.limit_name}",
            )
        return None

    # -- stages 1-4: admission, conviction, exposure, sizing -----------------------------

    def _size_signals(
        self,
        *,
        signals: Sequence[Signal],
        portfolio_state: PortfolioState,
        market_state: MarketState,
        decided_at_utc: datetime,
        frozen_clock: Clock,
        portfolio_drawdown: LimitVerdict | None,
    ) -> tuple[list[_Admitted], list[SignalAudit], list[Rejection]]:
        """Size every signal independently. Netting happens afterwards, never here."""
        admitted: list[_Admitted] = []
        audits: list[SignalAudit] = []
        refusals: list[Rejection] = []
        seen: set[tuple[str, Instrument]] = set()
        remaining_to_close: dict[Instrument, Decimal] = {}
        derisk_multiplier = (
            portfolio_drawdown.derisk_multiplier if portfolio_drawdown is not None else Decimal("1")
        )

        # Read once. The property rebuilds a proxy on every access, and a per-signal rebuild
        # would also open a window in which two signals see two mappings.
        marks_usd = market_state.marks_usd
        context = PreTradeContext(
            equity_usd=portfolio_state.equity_usd,
            exposure_limits=self._policy.exposure_limits,
            absolute_limits=self._policy.limits,
            tradable_symbols=self._policy.tradable_symbols,
        )

        for signal in signals:
            refusal = self._admission_refusal(
                signal=signal,
                seen=seen,
                portfolio_state=portfolio_state,
                market_state=market_state,
                decided_at_utc=decided_at_utc,
            )
            if refusal is not None:
                reason, binding_limit_name = refusal
                rejection = Rejection(
                    reason=reason,
                    binding_limit_name=binding_limit_name,
                    rejected_at_utc=decided_at_utc,
                )
                audits.append(
                    self._plain_audit(
                        signal=signal,
                        decided_at_utc=decided_at_utc,
                        stage=_STAGE_SIGNAL_ADMISSION,
                        rejection=rejection,
                        portfolio_drawdown=portfolio_drawdown,
                    )
                )
                refusals.append(rejection)
                continue
            seen.add((signal.strategy_id, signal.instrument))

            strategy = portfolio_state.strategies[signal.strategy_id]
            market = market_state.instruments[signal.instrument]
            strategy_drawdown = (
                evaluate(strategy.drawdown_state, self._policy.strategy_drawdown_budgets)
                if strategy.drawdown_state is not None
                else None
            )
            breach = strategy_drawdown.breach if strategy_drawdown is not None else None
            if breach is not None:
                rejection = Rejection(
                    reason=(
                        f"{signal.strategy_id} {breach.limit_name} is {breach.observed_ratio} "
                        f"against a budget of {breach.budget_ratio}; the halt is latched"
                    ),
                    binding_limit_name=f"strategy_{breach.limit_name}",
                    rejected_at_utc=decided_at_utc,
                )
                audits.append(
                    self._plain_audit(
                        signal=signal,
                        decided_at_utc=decided_at_utc,
                        stage=_STAGE_SIGNAL_ADMISSION,
                        rejection=rejection,
                        portfolio_drawdown=portfolio_drawdown,
                        strategy_drawdown=strategy_drawdown,
                    )
                )
                refusals.append(rejection)
                continue

            exposure = validate_pre_trade(
                signal=signal,
                portfolio=portfolio_state.portfolio,
                marks_usd=marks_usd,
                context=context,
                clock=frozen_clock,
            )
            if exposure.rejection is not None:
                audits.append(
                    self._plain_audit(
                        signal=signal,
                        decided_at_utc=decided_at_utc,
                        stage=_STAGE_EXPOSURE,
                        rejection=exposure.rejection,
                        portfolio_drawdown=portfolio_drawdown,
                        strategy_drawdown=strategy_drawdown,
                        exposure=exposure,
                    )
                )
                refusals.append(exposure.rejection)
                continue

            if signal.direction is Direction.FLAT:
                admitted.append(
                    self._flatten_request(
                        signal=signal,
                        market=market,
                        exposure=exposure,
                        portfolio=portfolio_state.portfolio,
                        remaining_to_close=remaining_to_close,
                        strategy_drawdown=strategy_drawdown,
                    )
                )
                continue

            outcome = self._size_directional(
                signal=signal,
                strategy=strategy,
                market=market,
                exposure=exposure,
                equity_usd=portfolio_state.equity_usd,
                decided_at_utc=decided_at_utc,
                portfolio_drawdown=portfolio_drawdown,
                strategy_drawdown=strategy_drawdown,
                derisk_multiplier=derisk_multiplier,
            )
            if isinstance(outcome, _Admitted):
                admitted.append(outcome)
            else:
                audits.append(outcome)
                # Guaranteed by `_size_directional`: it returns an audit only with a
                # rejection on it, and the pair is what the outcomes list carries.
                if outcome.rejection is not None:
                    refusals.append(outcome.rejection)

        return admitted, audits, refusals

    def _size_directional(
        self,
        *,
        signal: Signal,
        strategy: StrategyState,
        market: InstrumentMarketState,
        exposure: ExposureAssessment,
        equity_usd: Decimal,
        decided_at_utc: datetime,
        portfolio_drawdown: LimitVerdict | None,
        strategy_drawdown: LimitVerdict | None,
        derisk_multiplier: Decimal,
    ) -> _Admitted | SignalAudit:
        """The conviction, sizing and de-risk chain for one directional signal.

        Returns the admitted request or the audited refusal. The de-risk multiplier is
        applied to the quantity rather than to the risk fraction: a fraction scaled below
        the compiled-in band would be refused by `SizingParameters` as a misconfiguration,
        when in fact it is a correctly functioning drawdown response.
        """
        conviction = assess_conviction(
            signal=signal,
            calibration=strategy.calibration,
            parameters=self._policy.conviction_parameters,
            decided_at_utc=decided_at_utc,
        )
        if conviction.rejection is not None:
            return self._plain_audit(
                signal=signal,
                decided_at_utc=decided_at_utc,
                stage=_STAGE_CONVICTION,
                rejection=conviction.rejection,
                portfolio_drawdown=portfolio_drawdown,
                strategy_drawdown=strategy_drawdown,
                conviction=conviction,
                exposure=exposure,
            )

        risk_fraction_used = conviction.risk_fraction_used
        if risk_fraction_used is None:  # pragma: no cover - refused by ConvictionAssessment
            raise DomainError(
                f"{signal.strategy_id} was approved by the conviction channel with no risk "
                f"fraction; the pair disagreeing is a decision nobody can act on"
            )
        sizing = size_position(
            SizingInputs(
                instrument=signal.instrument,
                direction=signal.direction,
                equity_usd=equity_usd,
                entry_quote_price=market.mark_usd,
                invalidation_quote_price=signal.invalidation_quote_price,
                atr_14_quote=market.atr_14_quote,
                return_series=market.return_series,
                volatility_floor_annualised=market.volatility_floor_annualised,
                closed_trade_count=strategy.closed_trade_count,
                realised_mean_return_fraction=strategy.realised_mean_return_fraction,
                realised_return_stdev_fraction=strategy.realised_return_stdev_fraction,
                permitted_notional_usd=exposure.permitted_notional_usd,
            ),
            _with_risk_fraction(self._policy.sizing_parameters, risk_fraction_used),
        )

        scaled_multiplier = derisk_multiplier
        if strategy_drawdown is not None:
            scaled_multiplier = min(scaled_multiplier, strategy_drawdown.derisk_multiplier)
        base_quantity = signal.instrument.quantize_base_quantity(
            sizing.base_quantity * scaled_multiplier
        )
        if base_quantity <= _ZERO or not signal.instrument.meets_min_notional(
            base_quantity, market.mark_usd
        ):
            return self._plain_audit(
                signal=signal,
                decided_at_utc=decided_at_utc,
                stage=_STAGE_SIZING,
                rejection=Rejection(
                    reason=(
                        f"{signal.strategy_id} on {signal.instrument.symbol} sizes to "
                        f"{base_quantity} after a de-risk multiplier of {scaled_multiplier}, "
                        f"below the venue MIN_NOTIONAL of "
                        f"{signal.instrument.min_notional_quote}"
                    ),
                    binding_limit_name="min_notional_quote",
                    rejected_at_utc=decided_at_utc,
                ),
                portfolio_drawdown=portfolio_drawdown,
                strategy_drawdown=strategy_drawdown,
                conviction=conviction,
                exposure=exposure,
                sizing=sizing,
            )

        signed = base_quantity if signal.direction is Direction.LONG else -base_quantity
        return _Admitted(
            signal=signal,
            market=market,
            request=StrategyRequest(strategy_id=signal.strategy_id, signed_base_quantity=signed),
            conviction=conviction,
            exposure=exposure,
            sizing=sizing,
            strategy_drawdown=strategy_drawdown,
        )

    def _admission_refusal(
        self,
        *,
        signal: Signal,
        seen: set[tuple[str, Instrument]],
        portfolio_state: PortfolioState,
        market_state: MarketState,
        decided_at_utc: datetime,
    ) -> tuple[str, str] | None:
        """Why this signal cannot be evaluated at all, or `None`. Reason then limit name."""
        if (signal.strategy_id, signal.instrument) in seen:
            return (
                f"{signal.strategy_id} emitted two signals on {signal.instrument.symbol} in "
                f"one batch; two opinions from one strategy cannot be attributed to one "
                f"slice of a net order",
                "duplicate_signal",
            )
        if signal.decided_at_utc > decided_at_utc:
            return (
                f"{signal.strategy_id} decided at {signal.decided_at_utc.isoformat()}, after "
                f"the risk decision at {decided_at_utc.isoformat()}; a signal from the "
                f"future is look-ahead reaching the order path",
                "signal_not_from_the_future",
            )
        if signal.strategy_id not in portfolio_state.strategies:
            return (
                f"no strategy record for {signal.strategy_id}; a signal with no calibration "
                f"map and no realised distribution cannot be sized, and defaulting one would "
                f"size it off somebody else's record",
                "strategy_record_present",
            )
        if signal.instrument not in market_state.instruments:
            return (
                f"no market state for {signal.instrument.venue}:{signal.instrument.symbol}; a "
                f"position cannot be sized against a price the system does not have",
                "market_state_present",
            )
        if signal.instrument.symbol not in market_state.risk_model.symbols:
            return (
                f"{signal.instrument.symbol} is outside the estimated risk universe "
                f"{market_state.risk_model.symbols}; its risk contribution cannot be "
                f"computed and must not be assumed zero",
                "risk_model_coverage",
            )
        return None

    def _flatten_request(
        self,
        *,
        signal: Signal,
        market: InstrumentMarketState,
        exposure: ExposureAssessment,
        portfolio: Portfolio,
        remaining_to_close: dict[Instrument, Decimal],
        strategy_drawdown: LimitVerdict | None,
    ) -> _Admitted:
        """A flat signal's contribution: whatever of the position is still open.

        Two strategies flattening the same instrument in one batch do not close it twice.
        The first takes the whole position and the second is attributed zero -- a real,
        audited outcome, not a rejection, because the second strategy asked for something
        legitimate and got exactly what it asked for.
        """
        if signal.instrument not in remaining_to_close:
            position = portfolio.position_for(signal.instrument)
            remaining_to_close[signal.instrument] = (
                _ZERO if position is None else position.signed_base_quantity
            )
        open_quantity = remaining_to_close[signal.instrument]
        remaining_to_close[signal.instrument] = _ZERO
        return _Admitted(
            signal=signal,
            market=market,
            request=StrategyRequest(
                strategy_id=signal.strategy_id, signed_base_quantity=-open_quantity
            ),
            conviction=None,
            exposure=exposure,
            sizing=None,
            strategy_drawdown=strategy_drawdown,
        )

    # -- stage 5: netting ----------------------------------------------------------------

    def _net(
        self,
        *,
        admitted: Sequence[_Admitted],
        market_state: MarketState,
        decided_at_utc: datetime,
    ) -> tuple[list[NetOrderPlan], list[_Admitted], list[tuple[_Admitted, Rejection]]]:
        """Net per instrument, in first-appearance order so replays attribute identically."""
        grouped: dict[Instrument, list[_Admitted]] = {}
        for admission in admitted:
            grouped.setdefault(admission.signal.instrument, []).append(admission)

        plans: list[NetOrderPlan] = []
        netted: list[_Admitted] = []
        rejected: list[tuple[_Admitted, Rejection]] = []

        for instrument, group in grouped.items():
            market = market_state.instruments[instrument]
            plan = net_requests(
                instrument=instrument,
                requests=tuple(admission.request for admission in group),
                reference_quote_price=market.mark_usd,
                decision_mid_quote_price=market.mid_quote_price,
            )
            if plan.has_venue_portion and not instrument.meets_min_notional(
                abs(plan.net_signed_base_quantity), market.mark_usd
            ):
                # The crossed portion is not booked either. Booking it while the venue leg
                # never happens is exactly the fiction this module exists to prevent: two
                # strategies would hold positions at a mid, against a venue that traded
                # nothing.
                rejection = Rejection(
                    reason=(
                        f"the net order on {instrument.symbol} is "
                        f"{plan.net_signed_base_quantity}, below the venue MIN_NOTIONAL of "
                        f"{instrument.min_notional_quote}; the internal cross is not booked "
                        f"either, because a crossed position with no venue leg is a "
                        f"position at a price nobody traded"
                    ),
                    binding_limit_name="net_min_notional_quote",
                    rejected_at_utc=decided_at_utc,
                )
                rejected.extend((admission, rejection) for admission in group)
                continue
            plans.append(plan)
            netted.extend(group)

        return plans, netted, rejected

    # -- stage 6: concentration ----------------------------------------------------------

    def _assess_concentration(
        self,
        *,
        plans: Sequence[NetOrderPlan],
        portfolio_state: PortfolioState,
        market_state: MarketState,
        frozen_clock: Clock,
    ) -> ConcentrationAssessment | Rejection:
        """Evaluate the *prospective* book: the portfolio as it would stand if all filled.

        A concentration limit is a predicate on a portfolio, not on an order, so a breach is
        a property of the whole prospective book. The batch is refused entire rather than
        having orders dropped one at a time until it clears: that search's result depends on
        the arrival order of signals, which means two replays of the same batch could send
        different orders.
        """
        # Every open position is known to carry market state and to lie inside the estimated
        # universe: `_portfolio_halt_reason` refused the batch otherwise, before any sizing
        # ran, and `_admission_refusal` did the same for every traded symbol.
        signed_notional_usd_by_symbol: dict[str, Decimal] = {}
        for position in portfolio_state.portfolio.open_positions:
            symbol = position.instrument.symbol
            signed_notional_usd_by_symbol[symbol] = (
                signed_notional_usd_by_symbol.get(symbol, _ZERO)
                + position.signed_base_quantity
                * market_state.instruments[position.instrument].mark_usd
            )

        for plan in plans:
            signed_notional_usd_by_symbol[plan.symbol] = (
                signed_notional_usd_by_symbol.get(plan.symbol, _ZERO)
                + plan.venue_signed_notional_quote
            )

        assessment = assess_concentration(
            weight_ratio_by_symbol=weight_ratios_from_notional(
                signed_notional_usd_by_symbol=signed_notional_usd_by_symbol,
                equity_usd=portfolio_state.equity_usd,
            ),
            model=market_state.risk_model,
            limits=self._policy.concentration_limits,
            clock=frozen_clock,
        )
        if assessment.rejection is not None:
            return assessment.rejection
        return assessment

    # -- stage 7: order construction -----------------------------------------------------

    def _construct_orders(
        self,
        *,
        plans: Sequence[NetOrderPlan],
        decided_at_utc: datetime,
        correlation_id: UUID,
        seed: int,
    ) -> dict[Instrument, Order]:
        """One order per instrument with a venue portion. This is the only `Order(...)`."""
        orders: dict[Instrument, Order] = {}
        for plan in plans:
            if not plan.has_venue_portion:
                # A perfect internal cross. Zero venue orders is the correct outcome, and the
                # crossing row on the plan is what records that it happened.
                continue
            instrument = plan.instrument
            side = Side.BUY if plan.net_signed_base_quantity > _ZERO else Side.SELL
            base_quantity = abs(plan.net_signed_base_quantity)
            identity = _order_identity(
                correlation_id=correlation_id,
                seed=seed,
                instrument=instrument,
                side=side,
                base_quantity=base_quantity,
                decided_at_utc=decided_at_utc,
            )
            orders[instrument] = Order(
                order_id=identity[0],
                client_order_id=identity[1],
                correlation_id=correlation_id,
                instrument=instrument,
                side=side,
                # MARKET/IOC, not LIMIT/GTC. The risk engine holds the invalidation level
                # and acts on it; a resting order is a decision the venue keeps making on
                # our behalf while the kill switch is not looking (`fking.domain.enums`).
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.IOC,
                base_quantity=base_quantity,
                limit_quote_price=None,
                created_at_utc=decided_at_utc,
            )
        return orders

    # -- audit ---------------------------------------------------------------------------

    def _plain_audit(
        self,
        *,
        signal: Signal,
        decided_at_utc: datetime,
        stage: str,
        rejection: Rejection,
        portfolio_drawdown: LimitVerdict | None,
        strategy_drawdown: LimitVerdict | None = None,
        conviction: ConvictionAssessment | None = None,
        exposure: ExposureAssessment | None = None,
        sizing: PositionSizing | None = None,
    ) -> SignalAudit:
        return SignalAudit(
            strategy_id=signal.strategy_id,
            instrument=signal.instrument,
            direction=signal.direction,
            decided_at_utc=decided_at_utc,
            verdict=RiskVerdict.REJECTED,
            stage=stage,
            rejection=rejection,
            conviction=conviction,
            exposure=exposure,
            sizing=sizing,
            portfolio_drawdown=portfolio_drawdown,
            strategy_drawdown=strategy_drawdown,
            portfolio_drawdown_budgets=self._policy.portfolio_drawdown_budgets,
            strategy_drawdown_budgets=self._policy.strategy_drawdown_budgets,
            concentration=None,
            client_order_id=None,
            attributed_signed_base_quantity=None,
            booked_quote_price=None,
        )

    def _audit_for(
        self,
        *,
        admission: _Admitted,
        decided_at_utc: datetime,
        stage: str,
        rejection: Rejection | None,
        portfolio_drawdown: LimitVerdict | None,
        concentration: ConcentrationAssessment | None = None,
        attribution: Attribution | None = None,
        client_order_id: str | None = None,
    ) -> SignalAudit:
        return SignalAudit(
            strategy_id=admission.signal.strategy_id,
            instrument=admission.signal.instrument,
            direction=admission.signal.direction,
            decided_at_utc=decided_at_utc,
            verdict=RiskVerdict.REJECTED if rejection is not None else RiskVerdict.APPROVED,
            stage=stage,
            rejection=rejection,
            conviction=admission.conviction,
            exposure=admission.exposure,
            sizing=admission.sizing,
            portfolio_drawdown=portfolio_drawdown,
            strategy_drawdown=admission.strategy_drawdown,
            portfolio_drawdown_budgets=self._policy.portfolio_drawdown_budgets,
            strategy_drawdown_budgets=self._policy.strategy_drawdown_budgets,
            concentration=concentration,
            client_order_id=client_order_id,
            attributed_signed_base_quantity=(
                None if attribution is None else attribution.signed_base_quantity
            ),
            booked_quote_price=None if attribution is None else attribution.booked_quote_price,
        )


def _with_risk_fraction(
    parameters: SizingParameters, risk_fraction_per_trade: Decimal
) -> SizingParameters:
    """`parameters` with the calibrated risk fraction substituted. Returns a new object.

    The conviction channel is the one route by which a strategy influences its own size, and
    it influences it here and nowhere else. Reconstructing rather than mutating means the
    substituted value goes back through `SizingParameters.__post_init__`, so a fraction
    outside the compiled-in band is refused at the point of substitution rather than
    surviving into a quantity.
    """
    return SizingParameters(
        risk_fraction_per_trade=risk_fraction_per_trade,
        target_volatility_annualised=parameters.target_volatility_annualised,
        max_kelly_fraction=parameters.max_kelly_fraction,
        min_trades_for_kelly=parameters.min_trades_for_kelly,
        atr_invalidation_multiple=parameters.atr_invalidation_multiple,
        unstated_invalidation_risk_multiplier=parameters.unstated_invalidation_risk_multiplier,
    )


def _order_identity(
    *,
    correlation_id: UUID,
    seed: int,
    instrument: Instrument,
    side: Side,
    base_quantity: Decimal,
    decided_at_utc: datetime,
) -> tuple[UUID, str]:
    """The order's two identifiers, derived from the decision rather than drawn.

    `format(quantity, "f")` rather than `str(quantity)`: `str` renders small Decimals in
    scientific notation (`1E-8`), and a quantity that crosses that threshold would change
    the digest without the order changing, which is a duplicate submission wearing a new id.
    """
    material = "|".join(
        (
            str(correlation_id),
            str(seed),
            str(instrument.venue),
            instrument.symbol,
            str(side),
            format(base_quantity, "f"),
            decided_at_utc.isoformat(),
        )
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return uuid5(ORDER_ID_NAMESPACE, material), f"fk-{digest[:_CLIENT_ORDER_ID_DIGEST_LENGTH]}"
