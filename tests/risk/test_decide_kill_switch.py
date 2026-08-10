"""A tripped kill switch refuses before any work is done.

Issue #55 asks for this to be asserted by *call ordering* rather than by output, and the
distinction is the whole point: an implementation that sizes every signal, computes a
covariance matrix, and then discards the result would produce identical rejections and be
wrong. A halted system should not be computing covariance matrices -- every microsecond it
spends doing so is a microsecond in which the halt is not reflected anywhere.
"""

from __future__ import annotations

from typing import Never

import pytest

from fking.domain import RiskVerdict
from fking.risk import STAGES
from fking.risk import engine as engine_module
from tests.support.risk_engine import (
    CORRELATION_ID,
    SEED,
    frozen_clock,
    make_engine,
    make_market_state,
    make_portfolio_state,
    make_signal,
)

_COLLABORATORS = (
    "assess_conviction",
    "size_position",
    "validate_pre_trade",
    "assess_concentration",
)


# Two signals in every batch below, so "one rejection per signal" is a real assertion rather
# than a coincidence of a batch of one.
_BATCH_SIZE = 2


def _explode(*_args: object, **_kwargs: object) -> Never:
    raise AssertionError("a halted kill switch must not reach this term")


def test_a_halted_switch_returns_rejections_and_runs_no_other_stage() -> None:
    batch = make_engine(halted=True).decide(
        signals=[make_signal("alpha"), make_signal("beta")],
        portfolio_state=make_portfolio_state(),
        market_state=make_market_state(),
        clock=frozen_clock,
        correlation_id=CORRELATION_ID,
        seed=SEED,
    )
    assert batch.stages_executed == STAGES[:1]
    assert batch.orders == ()
    assert len(batch.rejections) == _BATCH_SIZE
    assert all(rejection.binding_limit_name == "kill_switch" for rejection in batch.rejections)
    assert all(audit.verdict is RiskVerdict.REJECTED for audit in batch.audits)


def test_no_sizing_or_covariance_work_executes_while_halted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserted against the terms themselves, not against the returned verdicts."""
    for name in _COLLABORATORS:
        monkeypatch.setattr(engine_module, name, _explode)

    batch = make_engine(halted=True).decide(
        signals=[make_signal("alpha")],
        portfolio_state=make_portfolio_state(),
        market_state=make_market_state(),
        clock=frozen_clock,
        correlation_id=CORRELATION_ID,
        seed=SEED,
    )
    assert batch.orders == ()


def test_the_terms_do_run_when_the_switch_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """The negative control. Without it the test above passes on an engine that does
    nothing at all, which is the classic way a call-ordering assertion rots."""
    for name in _COLLABORATORS:
        monkeypatch.setattr(engine_module, name, _explode)

    with pytest.raises(AssertionError, match="must not reach this term"):
        make_engine().decide(
            signals=[make_signal("alpha")],
            portfolio_state=make_portfolio_state(),
            market_state=make_market_state(),
            clock=frozen_clock,
            correlation_id=CORRELATION_ID,
            seed=SEED,
        )


def test_a_halted_switch_still_writes_an_audit_row_per_signal() -> None:
    """A refusal that left no row cannot be told from a consumer that crashed."""
    batch = make_engine(halted=True).decide(
        signals=[make_signal("alpha"), make_signal("beta")],
        portfolio_state=make_portfolio_state(),
        market_state=make_market_state(),
        clock=frozen_clock,
        correlation_id=CORRELATION_ID,
        seed=SEED,
    )
    payloads = batch.audit_payloads()
    assert len(payloads) == _BATCH_SIZE
    for payload in payloads:
        assert payload["verdict"] == str(RiskVerdict.REJECTED)
        assert payload["stage"] == "kill_switch"
        assert payload["binding_limit_name"] == "kill_switch"
        assert "tripped by the test" in str(payload["rejection_reason"])
