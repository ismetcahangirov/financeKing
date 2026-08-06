"""Shared plan fixtures for the walk-forward tests.

One declaration, reused everywhere, so that a change to the purge or embargo derivation
moves every test at once rather than moving the ones somebody remembered to update.

The horizons are the worked example from `BACKTEST_ENGINE.md` section 6.2 -- a four-hour
feature lookback and a six-hour maximum hold, whose stated embargo floor is ten hours --
paired with a 24-hour label horizon that dominates it. That pairing is deliberate: it
exercises the branch where the label horizon is the binding constraint, which is the one
that silently disappears if `embargo` is ever written as the floor alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from fking.backtest.walkforward import (
    WalkForwardDeclaration,
    WalkForwardPlan,
    WalkForwardScheme,
)

DECLARATION: Final = WalkForwardDeclaration(
    label_horizon=timedelta(hours=24),
    availability_lag=timedelta(0),
    max_feature_lookback=timedelta(hours=4),
    max_holding_horizon=timedelta(hours=6),
)

WINDOW_START: Final = datetime(2024, 1, 1, tzinfo=UTC)
TRAIN_SPAN: Final = timedelta(days=90)
TEST_SPAN: Final = timedelta(days=30)
STEP: Final = timedelta(days=30)


def plan_for(
    scheme: WalkForwardScheme,
    *,
    fold_target: int = 9,
    declaration: WalkForwardDeclaration = DECLARATION,
) -> WalkForwardPlan:
    """A plan whose window is exactly long enough for `fold_target` folds.

    Sized by arithmetic rather than by a hand-picked end date, so that a test asserting
    "twelve re-fits charge twelve trials" cannot silently become a test about eleven when
    the gap derivation changes.
    """
    first_test_start = WINDOW_START + TRAIN_SPAN + declaration.gap
    end_utc = first_test_start + (fold_target - 1) * STEP + TEST_SPAN
    return WalkForwardPlan(
        scheme=scheme,
        start_utc=WINDOW_START,
        end_utc=end_utc,
        train_span=TRAIN_SPAN,
        test_span=TEST_SPAN,
        step=STEP,
        declaration=declaration,
    )
