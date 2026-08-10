"""A result below 200 trades cannot reach `credible`, regardless of its Sharpe.

`BACKTEST_ENGINE.md` section 6.7's floor, applied by `check_sample_size` and enforced by
`assess_credibility` no matter how good the rest of the battery looks -- a Sharpe of 4.0
on 40 trades is still refused.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fking.backtest.results import (
    MIN_CREDIBLE_TRADE_COUNT,
    AuditCheck,
    AuditStatus,
    CredibilityInvariantError,
    ResultCredibility,
    check_sample_size,
)
from tests.backtest.results_support import result_for

pytestmark = pytest.mark.unit


def test_check_sample_size_fires_below_the_floor() -> None:
    result = check_sample_size(trade_count=MIN_CREDIBLE_TRADE_COUNT - 1)
    assert result.status is AuditStatus.FAIL
    assert result.check is AuditCheck.SAMPLE_SIZE
    assert "199" in result.evidence


def test_check_sample_size_passes_at_the_floor() -> None:
    result = check_sample_size(trade_count=MIN_CREDIBLE_TRADE_COUNT)
    assert result.status is AuditStatus.PASS


def test_check_sample_size_fires_on_a_thin_cpcv_fold_even_with_enough_trades_overall() -> None:
    result = check_sample_size(trade_count=500, per_fold_trade_counts=(40, 40, 29))
    assert result.status is AuditStatus.FAIL
    assert "29" in result.evidence


def test_a_40_trade_count_cannot_reach_credible_at_any_sharpe() -> None:
    with pytest.raises(CredibilityInvariantError, match="not_credible"):
        result_for(trade_count=40, sharpe=Decimal("4.0"), credibility=ResultCredibility.CREDIBLE)

    voided = result_for(
        trade_count=40, sharpe=Decimal("4.0"), credibility=ResultCredibility.NOT_CREDIBLE
    )
    assert voided.credibility is ResultCredibility.NOT_CREDIBLE


def test_199_trades_cannot_reach_credible_even_with_a_perfect_battery() -> None:
    """One trade short of the floor is applied literally, not rounded in the result's favour."""
    with pytest.raises(CredibilityInvariantError, match="not_credible"):
        result_for(trade_count=MIN_CREDIBLE_TRADE_COUNT - 1, credibility=ResultCredibility.CREDIBLE)
