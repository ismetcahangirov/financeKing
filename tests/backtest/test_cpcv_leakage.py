"""The leak, priced. Remove either gap and these tests fail on a number, not a claim.

The model here is the simplest thing that leaks the way real feature pipelines leak: it
memorises every bar its training samples can see, and at test time it trades with perfect
foresight on any bar it recognises and with a useless prior on the rest. A training sample
at instant `s` sees `[s - max_feature_lookback, s + label_horizon]` -- its feature window
reaching backwards and its label resolving forwards -- which is exactly the pair of
horizons the embargo and the purge are sized from.

That gives each gap a separate, checkable job:

* Delete the **embargo** and the training samples sitting immediately after the test block
  see the last `max_feature_lookback` bars *of the test block*, through their own lookback.
  Serial correlation leaking backwards, which is the half people skip because nothing
  about the fold table looks wrong when it is missing.
* Delete the **purge** and the training samples sitting immediately before the test block
  see the first `label_horizon` bars of it, through labels that resolve inside the test
  window.

The returns are pure noise from a written-out LCG, so a Sharpe above the baseline cannot
have come from the data. Whatever these tests measure is the leak.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest

from fking.backtest.cpcv import (
    CpcvPlan,
    TimeInterval,
    build_splits,
    purged_train_intervals,
)
from fking.backtest.walkforward import WalkForwardDeclaration
from tests.backtest.cpcv_support import WINDOW_START, bar_times, pseudo_returns

# purge = 24h; embargo floor = 36h + 6h = 42h. The two are deliberately different numbers,
# so a test that passed by applying one of them twice would still fail.
DECLARATION: Final = WalkForwardDeclaration(
    label_horizon=timedelta(hours=24),
    availability_lag=timedelta(0),
    max_feature_lookback=timedelta(hours=36),
    max_holding_horizon=timedelta(hours=6),
)

# Sixteen days of hourly bars in eight groups of forty-eight. The group is short relative
# to the 36-hour lookback on purpose: three quarters of a test block is within one lookback
# of its right edge, so an absent embargo shows up as a large number rather than a marginal
# one.
BAR_TOTAL: Final = 384
GROUP_TOTAL: Final = 8
GROUP_SPAN: Final = timedelta(hours=48)
WINDOW_END: Final = WINDOW_START + timedelta(hours=BAR_TOTAL)

_BARS: Final = bar_times(BAR_TOTAL)
_RETURNS: Final = pseudo_returns(BAR_TOTAL)

_QUANTUM: Final = Decimal("0.000001")


def _window() -> TimeInterval:
    return TimeInterval(start_utc=WINDOW_START, end_utc=WINDOW_END)


def _test_block() -> TimeInterval:
    """Group 4 of 8, so that both a left and a right training block exist."""
    return TimeInterval(
        start_utc=WINDOW_START + 4 * GROUP_SPAN,
        end_utc=WINDOW_START + 5 * GROUP_SPAN,
    )


def _bars_visible_to(train_intervals: Sequence[TimeInterval]) -> frozenset[datetime]:
    """Every bar some training sample could have seen, through a lookback or a label.

    Bar `b` is visible when a training instant `s` exists with
    `b - label_horizon <= s <= b + max_feature_lookback`: the first bound is the sample
    whose label resolves on `b`, the second is the sample whose feature window reaches
    back to `b`.
    """
    label_horizon = DECLARATION.label_horizon
    lookback = DECLARATION.max_feature_lookback
    return frozenset(
        bar
        for bar in _BARS
        if any(
            interval.start_utc <= bar + lookback and bar - label_horizon < interval.end_utc
            for interval in train_intervals
        )
    )


def _sharpe(pnl: Sequence[Decimal]) -> Decimal:
    """Per-bar Sharpe, in exact `Decimal`, so the delta is the same on every machine."""
    observation_total = Decimal(len(pnl))
    mean = sum(pnl, Decimal(0)) / observation_total
    variance = sum(((observation - mean) ** 2 for observation in pnl), Decimal(0))
    variance /= observation_total
    if variance == 0:
        raise AssertionError("a constant pnl series has no Sharpe; the fixture is degenerate")
    return (mean / variance.sqrt()).quantize(_QUANTUM)


def _memorising_model_sharpe(train_intervals: Sequence[TimeInterval]) -> Decimal:
    """Score the test block with a model that trades perfectly on bars it recognises.

    Unrecognised bars are traded long, which over a zero-edge series is worth nothing --
    the baseline. Recognised bars are traded on the sign of their own return, which is
    worth everything and is not available to any model that has not seen them.
    """
    visible = _bars_visible_to(train_intervals)
    block = _test_block()
    pnl = [
        abs(bar_return) if bar in visible else bar_return
        for bar, bar_return in zip(_BARS, _RETURNS, strict=True)
        if block.contains(bar)
    ]
    return _sharpe(pnl)


def _baseline_sharpe() -> Decimal:
    """The test block with nothing recognised: what the series is actually worth."""
    return _memorising_model_sharpe([])


def test_deleting_the_embargo_inflates_the_sharpe_and_the_correct_embargo_removes_it() -> None:
    """The acceptance criterion, as three numbers rather than an assertion of intent."""
    block = _test_block()
    purge = DECLARATION.purge
    embargo = DECLARATION.max_feature_lookback + DECLARATION.max_holding_horizon

    without_embargo = _memorising_model_sharpe(
        purged_train_intervals(_window(), [block], purge=purge, embargo=timedelta(0))
    )
    with_embargo = _memorising_model_sharpe(
        purged_train_intervals(_window(), [block], purge=purge, embargo=embargo)
    )
    baseline = _baseline_sharpe()

    # Measured on this fixture: 0.154012 with the correct embargo -- identical to the
    # baseline, so the gap removes the leak exactly rather than merely reducing it --
    # against 0.803749 with none. The strategy has not changed between those two numbers.
    assert with_embargo == baseline
    assert without_embargo > baseline
    assert without_embargo - with_embargo > Decimal("0.5")


def test_deleting_the_purge_inflates_the_sharpe_and_the_correct_purge_removes_it() -> None:
    """The other half, and it fails for a different reason: labels, not lookbacks."""
    block = _test_block()
    purge = DECLARATION.purge
    embargo = DECLARATION.max_feature_lookback + DECLARATION.max_holding_horizon

    without_purge = _memorising_model_sharpe(
        purged_train_intervals(_window(), [block], purge=timedelta(0), embargo=embargo)
    )
    with_purge = _memorising_model_sharpe(
        purged_train_intervals(_window(), [block], purge=purge, embargo=embargo)
    )
    baseline = _baseline_sharpe()

    assert with_purge == baseline
    assert without_purge > baseline
    assert without_purge - with_purge > Decimal("0.35")


def test_a_one_bar_embargo_is_not_enough_because_it_is_crypto_and_it_is_fast() -> None:
    """The failure mode the floor exists to close, priced rather than argued.

    One bar is 1/36 of the declared lookback, so thirty-five of the thirty-six leaking
    bars survive it. The number it produces is not a little worse than the correct
    embargo; it is most of the way to no embargo at all.
    """
    block = _test_block()
    purge = DECLARATION.purge
    correct = DECLARATION.max_feature_lookback + DECLARATION.max_holding_horizon

    one_bar = _memorising_model_sharpe(
        purged_train_intervals(_window(), [block], purge=purge, embargo=timedelta(hours=1))
    )
    none = _memorising_model_sharpe(
        purged_train_intervals(_window(), [block], purge=purge, embargo=timedelta(0))
    )
    correct_sharpe = _memorising_model_sharpe(
        purged_train_intervals(_window(), [block], purge=purge, embargo=correct)
    )

    assert one_bar - correct_sharpe > (none - correct_sharpe) * Decimal("0.8")


def test_no_path_of_a_real_partition_leaks_a_single_test_bar() -> None:
    """The property, over every path the sanctioned builder emits.

    The two tests above prove the gaps do something. This one proves the plan applies
    them everywhere, including the paths whose test groups sit at the window's edges,
    where one of the two training blocks does not exist.
    """
    plan = CpcvPlan(
        start_utc=WINDOW_START,
        end_utc=WINDOW_END,
        group_total=GROUP_TOTAL,
        test_group_size=2,
        declaration=DECLARATION,
        embargo=DECLARATION.embargo,
    )

    for split in build_splits(plan).splits:
        visible = _bars_visible_to(split.train_intervals)
        leaked = [
            bar
            for bar in _BARS
            if any(interval.contains(bar) for interval in split.test_intervals) and bar in visible
        ]
        assert leaked == [], f"path {split.path_index} leaked {len(leaked)} test bars"


@pytest.mark.parametrize("bar_total", [64, 384])
def test_the_synthetic_series_carries_no_edge_of_its_own(bar_total: int) -> None:
    """If the noise had a drift, every delta above would be measuring the drift instead."""
    returns = pseudo_returns(bar_total)

    assert returns == pseudo_returns(bar_total)
    assert abs(_sharpe(list(returns))) < Decimal("0.35")
