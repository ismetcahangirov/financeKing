"""The weekly chaos check: exhausting the LLM quota must not move order flow.

`FAILSAFE.md` section 3.3 states `LLM_QUOTA_EXHAUSTED` as a non-event for trading, and
calls that a design assertion worth checking rather than a description. It is the
cheapest available verification that the agent layer sits *on top of* the deterministic
core rather than inside it: if a model's availability can change what the order path
does, an LLM has reached the order path, which `ARCHITECTURE.md` section 9 forbids and
which no amount of prompt engineering makes safe.

The check has three legs, because any one of them alone is weak:

1. The decisions are byte-identical across the mode boundary -- same orders, same client
   order ids, same refusals.
2. No exported series moves except the two degraded-mode metrics. A trading metric that
   moved would be the observable form of the coupling.
3. The mode's own rule declares it does not affect trading, and nothing about the order
   path's view of the world changes while it is entered.

Marked `slow` because it drives the whole order path twice under an SDK meter provider,
and because it is scheduled weekly rather than per commit.
"""

from __future__ import annotations

from uuid import UUID, uuid5

import pytest

from fking.risk import (
    MODE_RULES,
    DegradedMode,
    DegradedModeGate,
    ModeObservation,
    blocked_symbols,
    blocks_new_orders,
    kill_switch_trip_required,
    symbols_without_usable_data,
)
from fking.risk.engine import DecisionBatch
from tests.support.metric_readings import MetricReadings, metric_readings
from tests.support.risk_engine import (
    CORRELATION_ID,
    DECIDED_AT,
    SEED,
    frozen_clock,
    make_engine,
    make_market_state,
    make_portfolio_state,
    make_signal,
)

pytestmark = [pytest.mark.slow, pytest.mark.unit]

_NAMESPACE = UUID("55555555-5555-4555-8555-555555555555")

# The two series this test expects to move. Anything else moving is the finding.
_DEGRADED_METRICS = frozenset(
    {
        "fking_risk_degraded_mode_engaged_count",
        "fking_risk_degraded_mode_transitions_total",
    }
)


def _decide() -> DecisionBatch:
    return make_engine().decide(
        signals=[make_signal("alpha"), make_signal("beta", conviction="0.40")],
        portfolio_state=make_portfolio_state(),
        market_state=make_market_state(),
        clock=frozen_clock,
        correlation_id=CORRELATION_ID,
        seed=SEED,
    )


def _order_flow(batch: DecisionBatch) -> tuple[object, ...]:
    """Everything about a batch that reaching an LLM could plausibly perturb."""
    return (
        tuple(
            (order.client_order_id, order.base_quantity, order.side, order.instrument.symbol)
            for order in batch.orders
        ),
        tuple((refusal.reason, refusal.binding_limit_name) for refusal in batch.rejections),
    )


def _readings(readings: MetricReadings) -> dict[str, dict[tuple[tuple[str, str], ...], float]]:
    return {name: dict(readings.by_labels(name)) for name in readings.names()}


def _exhaust_quota(gate: DegradedModeGate, sequence: int) -> None:
    gate.observe(
        ModeObservation(
            observation_id=uuid5(_NAMESPACE, f"quota-{sequence}"),
            correlation_id=CORRELATION_ID,
            mode=DegradedMode.LLM_QUOTA_EXHAUSTED,
            is_faulted=True,
            observed_at_utc=DECIDED_AT,
            reason="gemini free tier exhausted and groq fallback unavailable",
        )
    )


def test_forcing_quota_exhaustion_leaves_order_flow_identical() -> None:
    with metric_readings() as readings:
        before_batch = _decide()
        before_metrics = _readings(readings)

        gate = DegradedModeGate()
        _exhaust_quota(gate, 1)
        assert gate.state.is_active(DegradedMode.LLM_QUOTA_EXHAUSTED)

        after_batch = _decide()
        after_metrics = _readings(readings)

    assert _order_flow(after_batch) == _order_flow(before_batch)
    assert after_batch.orders
    moved = {
        name
        for name in set(before_metrics) | set(after_metrics)
        if before_metrics.get(name) != after_metrics.get(name)
    }
    assert moved <= _DEGRADED_METRICS


def test_the_order_path_view_of_the_world_is_unchanged_while_the_quota_is_exhausted() -> None:
    gate = DegradedModeGate()
    _exhaust_quota(gate, 2)
    state = gate.state

    assert not MODE_RULES[DegradedMode.LLM_QUOTA_EXHAUSTED].affects_trading
    assert not blocks_new_orders(state)
    assert not kill_switch_trip_required(state)
    assert blocked_symbols(state) == frozenset()
    assert symbols_without_usable_data(state) == frozenset()


def test_the_quota_mode_is_the_only_one_that_leaves_trading_untouched() -> None:
    """If a second mode ever becomes trading-neutral, this test is where it is argued."""
    neutral = {mode for mode, rule in MODE_RULES.items() if not rule.affects_trading}
    assert neutral == {DegradedMode.LLM_QUOTA_EXHAUSTED}
