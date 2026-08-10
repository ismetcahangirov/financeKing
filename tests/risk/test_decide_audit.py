"""Every path through `decide()` writes a row, and the row names every limit evaluated.

`ARCHITECTURE.md` section 11 requires a trade -- including the trade that did not happen --
to be reconstructable months later from these rows alone. A row carrying only the limit that
bound cannot answer the question every post-incident review actually asks, which is how
close the others were.

The refusals are exercised one per test, so a failure names which path moved.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from decimal import Decimal

import pytest

from fking.domain import Direction, DomainError, Order, RiskVerdict, Signal
from fking.risk import DecisionBatch, PortfolioState, RiskEngine
from tests.support.risk_engine import (
    CORRELATION_ID,
    DECIDED_AT,
    EQUITY_USD,
    HELD_SYMBOLS,
    INSTRUMENTS,
    SEED,
    drawdown_state,
    frozen_clock,
    make_engine,
    make_market_state,
    make_policy,
    make_portfolio_state,
    make_signal,
    make_strategy_state,
)

_HALVED: Decimal = EQUITY_USD * Decimal("0.5")
# One approved signal and one refused, so the batch iterates as a mixed list.
_MIXED_BATCH_SIZE = 2


def decide(
    signals: Sequence[Signal],
    *,
    engine: RiskEngine | None = None,
    **overrides: object,
) -> DecisionBatch:
    """One call with the standard fixtures, overriding only what a test is about."""
    return (engine or make_engine()).decide(
        signals=signals,
        portfolio_state=overrides.pop("portfolio_state", None) or make_portfolio_state(),  # type: ignore[arg-type]
        market_state=overrides.pop("market_state", None) or make_market_state(),  # type: ignore[arg-type]
        clock=frozen_clock,
        correlation_id=CORRELATION_ID,
        seed=SEED,
    )


def _limit_names(payload: Mapping[str, object]) -> set[str]:
    rows = payload["limits_evaluated"]
    assert isinstance(rows, tuple)
    return {str(row["limit_name"]) for row in rows}


def test_an_approved_decision_records_every_limit_with_threshold_and_observed() -> None:
    batch = decide([make_signal("alpha")])
    payload = batch.audit_payloads()[0]

    assert payload["verdict"] == str(RiskVerdict.APPROVED)
    assert payload["client_order_id"] is not None
    names = _limit_names(payload)
    # One row from each family: the conviction channel, the notional limits, the drawdown
    # budgets and the covariance-based concentration limits.
    assert "conviction_floor" in names
    assert "max_position_notional_usd" in names
    assert "portfolio_drawdown" in names
    assert any(name.startswith("max_asset_risk_share_ratio:") for name in names)

    rows = payload["limits_evaluated"]
    assert isinstance(rows, tuple)
    for row in rows:
        assert set(row) == {"limit_name", "threshold", "observed", "is_breached"}
        # Strings, not Decimals: the payload lands in jsonb, and a JSON encoder that has not
        # been told otherwise turns a Decimal into a float on the way into an append-only
        # table that can never be corrected.
        assert isinstance(row["threshold"], str)
        assert isinstance(row["observed"], str)
        Decimal(str(row["threshold"]))
        Decimal(str(row["observed"]))


def test_a_conviction_refusal_still_records_the_exposure_limits_it_never_reached() -> None:
    """A signal discarded at the floor is not a signal nobody looked at."""
    batch = decide([make_signal("alpha", conviction="0.05")])
    audit = batch.audits[0]

    assert audit.verdict is RiskVerdict.REJECTED
    assert audit.stage == "conviction"
    assert audit.rejection is not None
    assert audit.rejection.binding_limit_name == "conviction_floor"
    names = _limit_names(audit.audit_payload())
    assert "conviction_floor" in names
    assert "max_position_notional_usd" in names


def test_a_symbol_outside_the_tradable_universe_is_refused_before_it_is_priced() -> None:
    batch = decide(
        [make_signal("alpha")],
        engine=make_engine(policy=make_policy(tradable_symbols=frozenset({"ETHUSDT"}))),
    )
    assert batch.rejections[0].binding_limit_name == "tradable_universe"


def test_a_signal_from_a_strategy_with_no_record_is_refused_rather_than_defaulted() -> None:
    """Defaulting the calibration map would size it off somebody else's record."""
    batch = decide([make_signal("ghost")])
    assert batch.rejections[0].binding_limit_name == "strategy_record_present"


def test_a_signal_on_an_unpriced_instrument_is_refused() -> None:
    batch = decide([make_signal("alpha")], market_state=make_market_state(symbols=HELD_SYMBOLS))
    assert batch.rejections[0].binding_limit_name == "market_state_present"


def test_two_signals_from_one_strategy_on_one_symbol_cannot_both_be_attributed() -> None:
    batch = decide([make_signal("alpha"), make_signal("alpha")])
    assert len(batch.orders) == 1
    assert batch.rejections[0].binding_limit_name == "duplicate_signal"


def test_a_signal_decided_after_the_risk_decision_is_look_ahead_and_refused() -> None:
    late = Signal(
        strategy_id="alpha",
        instrument=INSTRUMENTS["BTCUSDT"],
        direction=Direction.LONG,
        conviction=Decimal("0.6"),
        horizon=timedelta(hours=8),
        invalidation_quote_price=Decimal("63000.00"),
        rationale="close above the 20-bar high",
        decided_at_utc=DECIDED_AT + timedelta(seconds=1),
    )
    assert decide([late]).rejections[0].binding_limit_name == "signal_not_from_the_future"


def test_a_traded_symbol_outside_the_estimated_risk_universe_is_refused() -> None:
    """Not assumed uncorrelated. Its risk contribution cannot be computed, and a symbol
    whose contribution is unknown is the one most likely to be the problem."""
    batch = decide(
        [make_signal("alpha", symbol="SOLUSDT")],
        market_state=make_market_state(symbols=("ADAUSDT", "BNBUSDT", "BTCUSDT")),
        portfolio_state=make_portfolio_state(held_symbols=("ADAUSDT", "BNBUSDT")),
    )
    assert batch.rejections[0].binding_limit_name == "market_state_present"


def test_a_held_position_with_no_market_state_refuses_the_batch_before_sizing() -> None:
    """Batch-wide: every notional limit is evaluated against the whole book, so one unpriced
    position makes all of them wrong at once -- in the direction that reads as headroom."""
    batch = decide(
        [make_signal("alpha")],
        market_state=make_market_state(symbols=("ADAUSDT", "BNBUSDT", "BTCUSDT")),
    )
    assert batch.stages_executed == ("kill_switch", "portfolio_drawdown")
    assert batch.rejections[0].binding_limit_name == "market_state_present"


def test_a_held_position_outside_the_risk_model_refuses_the_batch() -> None:
    """The shape a stale covariance estimate arrives in: marks for a name the model has
    never seen. Priced is not the same as understood."""
    batch = decide(
        [make_signal("alpha")],
        market_state=make_market_state(model_symbols=("BTCUSDT",)),
    )
    assert batch.stages_executed == ("kill_switch", "portfolio_drawdown")
    assert batch.rejections[0].binding_limit_name == "risk_model_coverage"


def test_a_traded_symbol_the_model_does_not_cover_is_refused_at_admission() -> None:
    batch = decide(
        [make_signal("alpha")],
        market_state=make_market_state(model_symbols=HELD_SYMBOLS),
        portfolio_state=make_portfolio_state(held_symbols=HELD_SYMBOLS),
    )
    assert batch.rejections[0].binding_limit_name == "risk_model_coverage"


def test_a_single_name_prospective_book_breaches_the_concentration_limits() -> None:
    """Recorded rather than worked around. A book holding one name genuinely carries 100% of
    its own risk, so as the terms stand today the first order into an empty portfolio is
    refused. This test states the behaviour; the pull request for #55 raises it."""
    batch = decide([make_signal("alpha")], portfolio_state=make_portfolio_state(held_symbols=()))
    assert batch.orders == ()
    assert batch.rejections[0].binding_limit_name in {
        "max_asset_risk_share_ratio",
        "max_cluster_risk_share_ratio",
    }
    assert batch.audits[0].stage == "concentration"


def test_a_halted_portfolio_drawdown_refuses_the_batch_before_any_signal_is_sized() -> None:
    batch = decide(
        [make_signal("alpha")],
        portfolio_state=make_portfolio_state(
            portfolio_drawdown=drawdown_state("portfolio", "portfolio", current_equity_usd=_HALVED)
        ),
    )
    assert batch.stages_executed == ("kill_switch", "portfolio_drawdown")
    assert batch.rejections[0].binding_limit_name.startswith("portfolio_")
    assert batch.audits[0].stage == "portfolio_drawdown"


def test_a_strategy_level_halt_refuses_only_that_strategy() -> None:
    batch = decide(
        [make_signal("alpha"), make_signal("beta", direction=Direction.SHORT)],
        portfolio_state=make_portfolio_state(
            strategies={
                "alpha": make_strategy_state("alpha"),
                "beta": make_strategy_state(
                    "beta",
                    drawdown_state=drawdown_state("strategy", "beta", current_equity_usd=_HALVED),
                ),
            }
        ),
    )
    assert len(batch.rejections) == 1
    assert batch.rejections[0].binding_limit_name.startswith("strategy_")
    assert len(batch.orders) == 1


def test_an_open_book_with_no_drawdown_state_is_refused() -> None:
    """A high-water mark that was lost is a drawdown limit that silently never binds."""
    base = make_portfolio_state()
    state = PortfolioState(
        portfolio=base.portfolio,
        equity_usd=base.equity_usd,
        strategies=base.strategies,
        drawdown_state=None,
    )
    batch = decide([make_signal("alpha")], portfolio_state=state)
    assert batch.rejections[0].binding_limit_name == "drawdown_state_present"


def test_non_positive_equity_refuses_the_batch_rather_than_being_divided_by() -> None:
    """Every limit here is a fraction of equity, and none of them is defined against zero."""
    base = make_portfolio_state()
    state = PortfolioState(
        portfolio=base.portfolio,
        equity_usd=Decimal("0"),
        strategies=base.strategies,
        drawdown_state=base.drawdown_state,
    )
    batch = decide([make_signal("alpha")], portfolio_state=state)
    assert batch.rejections[0].binding_limit_name == "equity_usd"


def test_a_float_equity_is_refused_by_name() -> None:
    with pytest.raises(DomainError, match="equity_usd must be a Decimal"):
        make_portfolio_state(equity_usd=100000.0)  # type: ignore[arg-type]


def test_the_batch_iterates_as_the_list_the_issue_specifies() -> None:
    """`decide()` is specified as returning `list[Order | Rejection]`; iterating is that."""
    outcomes = list(decide([make_signal("alpha"), make_signal("nobody")]))
    assert len(outcomes) == _MIXED_BATCH_SIZE
    assert sum(isinstance(outcome, Order) for outcome in outcomes) == 1
