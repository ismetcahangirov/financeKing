"""The label's windowing, stated as exact values rather than as descriptions.

`tests/lookahead/test_probe.py` asserts the *alignment* -- that the label does not depend
on the decision bar's own close. This asserts the arithmetic around it: which bar is the
entry, which is the exit, and what happens when the horizon reaches no bar at all.

The last case is the one worth a test of its own. A decision whose horizon expires before
the next bar closes could not have been opened and closed inside it, and the tempting
answer -- measure over whatever window is available -- produces a label whose horizon
varies with how sparse the data happened to be, which is a different statistic reported
under one name.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from fking.data.features.labels import forward_return_label
from fking.data.features.spec import FeatureObservation

pytestmark = pytest.mark.unit

_START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _series(
    prices: tuple[tuple[str, str], ...], *, step: timedelta = timedelta(minutes=15)
) -> tuple[FeatureObservation, ...]:
    """Bars given as `(open, close)` pairs, so entry and exit prices are independent."""
    return tuple(
        FeatureObservation(
            event_time_utc=_START + step * index,
            open_quote_price=Decimal(opened),
            close_quote_price=Decimal(closed),
        )
        for index, (opened, closed) in enumerate(prices)
    )


def test_the_return_is_measured_from_the_next_open_to_the_last_close_in_the_horizon() -> None:
    """An exact value, so the two endpoints are pinned rather than described.

    Decision on bar 0. Entry is bar 1's open of 200; a 30-minute horizon reaches bar 2,
    whose close is 220. The label is 220 / 200 - 1, and it involves neither of bar 0's
    prices at all.
    """
    series = _series((("100", "150"), ("200", "210"), ("215", "220"), ("221", "999")))
    labels = forward_return_label(series, horizon=timedelta(minutes=30))

    assert labels[0].decision_time_utc == _START
    assert labels[0].entry_time_utc == _START + timedelta(minutes=15)
    assert labels[0].exit_time_utc == _START + timedelta(minutes=30)
    assert labels[0].return_fraction == Decimal("0.1")


def test_a_horizon_reaching_no_bar_produces_no_label_rather_than_a_shorter_one() -> None:
    """Two bars two hours apart, a thirty-minute horizon: nothing could have been held.

    Emitting a label here would report a two-hour outcome under a thirty-minute horizon,
    and it would do so most often exactly where the data is sparsest -- which is where a
    strategy's measured edge is least trustworthy to begin with.
    """
    sparse = _series((("100", "110"), ("111", "200")), step=timedelta(hours=2))
    assert forward_return_label(sparse, horizon=timedelta(minutes=30)) == ()


def test_the_last_bar_has_no_label_because_it_has_no_entry() -> None:
    """The decision at the newest bar has no bar after it to transact in.

    In live operation this is every decision until the next bar closes, which is why it
    is an absence rather than a value: a label that existed for the newest bar would be
    the one place the system reported an outcome it could not yet have had.
    """
    series = _series((("100", "110"), ("111", "120"), ("121", "130")))
    labels = forward_return_label(series, horizon=timedelta(minutes=30))
    assert [label.decision_time_utc for label in labels] == [
        _START,
        _START + timedelta(minutes=15),
    ]
