"""Properties of `RiskEngine.decide()` over batches of signals.

The guarantees under test hold whatever the batch looks like:

1. Every signal produces exactly one audit row. A refused signal that left no row cannot be
   told apart from a consumer that crashed before running.
2. Whatever is approved, the attributions sum to the order that was sent.
3. Nothing is ever sent below the venue's `MIN_NOTIONAL`, and every order carries a strictly
   positive quantity -- a quantity of zero is not a small order, it is a rejected one.
4. The decision is a pure function of its arguments: two calls on the same inputs produce
   byte-identical orders.

`docs/rules/testing-rules.md` clause 2: property tests are mandatory for every function in
`fking.risk`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fking.domain import Direction, RiskVerdict, Side, Signal
from fking.risk import DecisionBatch
from tests.support.risk_engine import (
    CORRELATION_ID,
    HELD_SYMBOLS,
    INSTRUMENTS,
    SEED,
    TRADED_SYMBOL,
    frozen_clock,
    make_engine,
    make_market_state,
    make_portfolio_state,
    make_signal,
    make_strategy_state,
    mark_usd,
)

_ZERO: Final = Decimal("0")
# Kept to the symbols the fixture universe prices, so a batch is refused for a limit rather
# than for a missing mark -- the missing-mark refusals have their own example-based tests.
_TRADABLE: Final[tuple[str, ...]] = (TRADED_SYMBOL, *HELD_SYMBOLS)


@st.composite
def one_signal(draw: st.DrawFn) -> Signal:
    symbol = draw(st.sampled_from(_TRADABLE))
    # The invalidation level is drawn as a fraction of the symbol's own mark rather than as
    # an absolute price: a fixed 63000 is a plausible stop on BTC and nonsense on ADA, and a
    # batch where every non-BTC signal is refused for MIN_NOTIONAL exercises one path.
    # 0.98 is a tight stop and a large position; 0.50 is a wide one and a small position.
    invalidation_ratio = draw(st.sampled_from(("0.98", "0.90", "0.50", "1.02", "1.60")))
    return make_signal(
        draw(st.sampled_from(("alpha", "beta", "gamma", "delta"))),
        direction=draw(st.sampled_from(tuple(Direction))),
        symbol=symbol,
        conviction=draw(st.sampled_from(("0.05", "0.15", "0.35", "0.60", "0.85", "1.00"))),
        invalidation_quote_price=str(mark_usd(symbol) * Decimal(invalidation_ratio)),
    )


@st.composite
def signal_batches(draw: st.DrawFn) -> list[Signal]:
    return draw(st.lists(one_signal(), min_size=1, max_size=6))


def _decide(signals: list[Signal]) -> DecisionBatch:
    strategies = {
        strategy_id: make_strategy_state(strategy_id)
        for strategy_id in {signal.strategy_id for signal in signals}
    }
    return make_engine().decide(
        signals=signals,
        portfolio_state=make_portfolio_state(strategies=strategies),
        market_state=make_market_state(),
        clock=frozen_clock,
        correlation_id=CORRELATION_ID,
        seed=SEED,
    )


_SETTINGS = settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


@given(signals=signal_batches())
@_SETTINGS
def test_every_signal_leaves_exactly_one_audit_row(signals: list[Signal]) -> None:
    batch = _decide(signals)
    assert len(batch.audits) == len(signals)
    for audit in batch.audits:
        assert (audit.rejection is None) == (audit.verdict is RiskVerdict.APPROVED)


@given(signals=signal_batches())
@_SETTINGS
def test_attributions_sum_to_the_order_that_was_sent(signals: list[Signal]) -> None:
    batch = _decide(signals)
    orders_by_instrument = {order.instrument: order for order in batch.orders}
    for plan in batch.plans:
        attributed = sum(
            (attribution.signed_base_quantity for attribution in plan.attributions),
            start=_ZERO,
        )
        order = orders_by_instrument.get(plan.instrument)
        if order is None:
            # No venue leg: the batch crossed perfectly, and zero is what it sums to.
            assert plan.net_signed_base_quantity == _ZERO
            assert attributed == _ZERO
        else:
            assert attributed == order.signed_base_quantity


@given(signals=signal_batches())
@_SETTINGS
def test_no_order_is_ever_sent_below_the_venue_floor(signals: list[Signal]) -> None:
    """Rounding *up* to `MIN_NOTIONAL` would be the risk engine authorising a position
    larger than any of its own terms permitted, so the only honest outcome is no order."""
    batch = _decide(signals)
    for order in batch.orders:
        assert order.base_quantity > _ZERO
        assert order.base_quantity == order.instrument.quantize_base_quantity(order.base_quantity)
        assert order.instrument.meets_min_notional(
            order.base_quantity, mark_usd(order.instrument.symbol)
        )
        assert order.side in (Side.BUY, Side.SELL)


@given(signals=signal_batches())
@_SETTINGS
def test_the_decision_is_a_pure_function_of_its_arguments(signals: list[Signal]) -> None:
    first = _decide(signals)
    second = _decide(signals)
    assert first.orders == second.orders
    assert [audit.stage for audit in first.audits] == [audit.stage for audit in second.audits]
    assert first.stages_executed == second.stages_executed


@given(signals=signal_batches())
@_SETTINGS
def test_one_instrument_never_receives_two_orders(signals: list[Signal]) -> None:
    """The whole point of netting: two opposing signals cross rather than each paying the
    spread to reach a net position of nearly nothing."""
    batch = _decide(signals)
    instruments = [order.instrument for order in batch.orders]
    assert len(instruments) == len(set(instruments))


@given(signals=signal_batches())
@_SETTINGS
def test_an_approved_audit_always_names_the_order_it_contributed_to(
    signals: list[Signal],
) -> None:
    batch = _decide(signals)
    client_order_ids = {order.client_order_id for order in batch.orders}
    for audit in batch.audits:
        if audit.verdict is not RiskVerdict.APPROVED:
            assert audit.client_order_id is None
            continue
        assert audit.attributed_signed_base_quantity is not None
        # `None` where the batch crossed perfectly: there is an attribution and no order.
        assert audit.client_order_id is None or audit.client_order_id in client_order_ids


@given(signals=signal_batches())
@_SETTINGS
def test_a_halted_kill_switch_refuses_every_signal_whatever_the_batch(
    signals: list[Signal],
) -> None:
    strategies = {
        strategy_id: make_strategy_state(strategy_id)
        for strategy_id in {signal.strategy_id for signal in signals}
    }
    batch = make_engine(halted=True).decide(
        signals=signals,
        portfolio_state=make_portfolio_state(strategies=strategies),
        market_state=make_market_state(),
        clock=frozen_clock,
        correlation_id=CORRELATION_ID,
        seed=SEED,
    )
    assert batch.orders == ()
    assert len(batch.rejections) == len(signals)
    assert batch.stages_executed == ("kill_switch",)


@given(
    symbol=st.sampled_from(_TRADABLE),
    conviction=st.sampled_from(("0.20", "0.60", "1.00")),
)
@_SETTINGS
def test_the_audit_row_of_a_lone_signal_always_carries_the_limits_it_was_judged_on(
    symbol: str, conviction: str
) -> None:
    signals = [make_signal("alpha", symbol=symbol, conviction=conviction)]
    payload = _decide(signals).audit_payloads()[0]
    rows = payload["limits_evaluated"]
    assert isinstance(rows, tuple)
    assert rows, "a decision judged on no limits is a decision nobody can review"
    assert payload["symbol"] == INSTRUMENTS[symbol].symbol
