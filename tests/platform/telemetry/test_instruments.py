"""Instruments refuse a label set the metric did not declare.

The failure being guarded does not raise in the SDK: a mistyped attribute key produces a
second time series that looks like the first, sums to a different number, and is queried
by nothing.
"""

from __future__ import annotations

import pytest

from fking.platform.telemetry import MetricLabelError, MetricSpec, counter
from fking.platform.telemetry._registry import BUS_EVENTS_PUBLISHED, LOG_FIELDS_DROPPED

pytestmark = pytest.mark.unit


def test_the_declared_label_set_is_accepted() -> None:
    """Guards the rejection tests: a handle that refused everything would pass them."""
    counter(BUS_EVENTS_PUBLISHED).increment(stream="fking.data.bar.ingested", event_type="x")


def test_a_missing_label_is_refused() -> None:
    with pytest.raises(MetricLabelError, match="incremented with"):
        counter(BUS_EVENTS_PUBLISHED).increment(stream="s")


def test_an_extra_label_is_refused() -> None:
    with pytest.raises(MetricLabelError, match="second series"):
        counter(BUS_EVENTS_PUBLISHED).increment(stream="s", event_type="e", venue="binance")


def test_a_mistyped_label_is_refused_rather_than_forking_the_series() -> None:
    with pytest.raises(MetricLabelError):
        counter(LOG_FIELDS_DROPPED).increment(fieldname="symbol")


def test_a_counter_cannot_decrease() -> None:
    with pytest.raises(MetricLabelError, match="cannot decrease"):
        counter(LOG_FIELDS_DROPPED).increment(-1, field_name="symbol")


def test_asking_for_a_counter_handle_on_a_gauge_is_refused() -> None:
    gauge = MetricSpec(
        name="fking_risk_portfolio_drawdown_ratio",
        kind="gauge",
        labels=(),
        description="Portfolio drawdown against peak equity.",
    )
    with pytest.raises(MetricLabelError, match="not a counter"):
        counter(gauge)
