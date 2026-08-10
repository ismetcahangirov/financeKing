"""The four audit checks that are not look-ahead, cost model or sample size.

Each has one test proving it FIRES on a deliberately bad input and one proving it passes
on a good one, per this package's own testing instruction: a check that has never been
seen to fail is not evidence of anything (`tests/lookahead/leaky.py`'s own framing).
"""

from __future__ import annotations

import pytest

from fking.backtest.results import (
    AuditCheck,
    AuditStatus,
    check_fill_optimism,
    check_parity,
    check_survivorship,
    check_timestamp_alignment,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# timestamp_alignment
# ---------------------------------------------------------------------------


def test_check_timestamp_alignment_fires_when_a_market_resolves_to_the_wrong_unit() -> None:
    """The exact bug `docs/rules/no-lookahead.md` names: spot ingested as microseconds
    from 2025-01-01, futures stayed milliseconds, and a loader that applies one divisor
    globally misaligns whichever market it guessed wrong."""
    declared = {"BTCUSDT-spot": "microseconds", "BTCUSDT-futures": "milliseconds"}
    resolved = {"BTCUSDT-spot": "milliseconds", "BTCUSDT-futures": "milliseconds"}

    result = check_timestamp_alignment(
        declared_epoch_unit_by_market=declared, resolved_epoch_unit_by_market=resolved
    )

    assert result.check is AuditCheck.TIMESTAMP_ALIGNMENT
    assert result.status is AuditStatus.FAIL
    assert "BTCUSDT-spot" in result.evidence


def test_check_timestamp_alignment_passes_when_two_markets_legitimately_differ() -> None:
    """Two markets using different units is not itself a defect."""
    declared = {"BTCUSDT-spot": "microseconds", "BTCUSDT-futures": "milliseconds"}

    result = check_timestamp_alignment(
        declared_epoch_unit_by_market=declared, resolved_epoch_unit_by_market=declared
    )

    assert result.status is AuditStatus.PASS


def test_check_timestamp_alignment_is_inconclusive_with_no_declared_markets() -> None:
    result = check_timestamp_alignment(
        declared_epoch_unit_by_market={}, resolved_epoch_unit_by_market={}
    )
    assert result.status is AuditStatus.INCONCLUSIVE


# ---------------------------------------------------------------------------
# fill_optimism
# ---------------------------------------------------------------------------


def test_check_fill_optimism_fires_on_a_100_percent_fill_rate_with_no_rejections() -> None:
    result = check_fill_optimism(
        submitted_order_count=20, filled_order_count=20, rejected_order_count=0
    )
    assert result.check is AuditCheck.FILL_OPTIMISM
    assert result.status is AuditStatus.FAIL
    assert "does not exist" in result.evidence


def test_check_fill_optimism_passes_a_realistic_partial_fill_rate() -> None:
    result = check_fill_optimism(
        submitted_order_count=20, filled_order_count=14, rejected_order_count=3
    )
    assert result.status is AuditStatus.PASS


def test_check_fill_optimism_passes_a_full_fill_rate_that_still_saw_rejections() -> None:
    """100% of *submitted* orders filling is fine if the venue rejected some before that."""
    result = check_fill_optimism(
        submitted_order_count=17, filled_order_count=17, rejected_order_count=3
    )
    assert result.status is AuditStatus.PASS


def test_check_fill_optimism_is_inconclusive_with_no_orders_submitted() -> None:
    result = check_fill_optimism(
        submitted_order_count=0, filled_order_count=0, rejected_order_count=0
    )
    assert result.status is AuditStatus.INCONCLUSIVE


# ---------------------------------------------------------------------------
# survivorship
# ---------------------------------------------------------------------------


def test_check_survivorship_fires_when_the_universe_was_not_resolved_point_in_time() -> None:
    result = check_survivorship(
        universe_symbols=("BTCUSDT", "ETHUSDT"), universe_resolved_as_of=False
    )
    assert result.check is AuditCheck.SURVIVORSHIP
    assert result.status is AuditStatus.FAIL
    assert "selection bias" in result.evidence


def test_check_survivorship_passes_a_point_in_time_universe() -> None:
    result = check_survivorship(
        universe_symbols=("BTCUSDT", "ETHUSDT"), universe_resolved_as_of=True
    )
    assert result.status is AuditStatus.PASS


def test_check_survivorship_is_inconclusive_with_an_empty_universe() -> None:
    result = check_survivorship(universe_symbols=(), universe_resolved_as_of=True)
    assert result.status is AuditStatus.INCONCLUSIVE


# ---------------------------------------------------------------------------
# parity
# ---------------------------------------------------------------------------


def test_check_parity_fires_when_backtest_and_paper_decisions_diverge() -> None:
    result = check_parity(
        backtest_decision_digest="deadbeef" * 8, paper_decision_digest="cafebabe" * 8
    )
    assert result.check is AuditCheck.PARITY
    assert result.status is AuditStatus.FAIL
    assert "diverged" in result.evidence


def test_check_parity_passes_identical_digests() -> None:
    digest = "deadbeef" * 8
    result = check_parity(backtest_decision_digest=digest, paper_decision_digest=digest)
    assert result.status is AuditStatus.PASS


def test_check_parity_is_inconclusive_with_a_missing_digest() -> None:
    result = check_parity(backtest_decision_digest="", paper_decision_digest="cafebabe" * 8)
    assert result.status is AuditStatus.INCONCLUSIVE
