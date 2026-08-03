"""The metric name registry refuses the names that break dashboards silently.

Every case here is a name that would be accepted by the OpenTelemetry SDK without
complaint and would then produce a series nobody queries or a rename nobody notices.
"""

from __future__ import annotations

import pytest

from fking.platform.telemetry import (
    FORBIDDEN_LABELS,
    METRIC_PREFIX,
    REGISTERED_METRICS,
    MetricNameError,
    MetricSpec,
)

pytestmark = pytest.mark.unit


def test_every_registered_metric_is_prefixed_and_described() -> None:
    """The prefix is what makes one Grafana regex select everything this system emits."""
    for spec in REGISTERED_METRICS:
        assert spec.name.startswith(METRIC_PREFIX), spec.name
        assert spec.description.strip(), f"{spec.name} has no description"


def test_registered_names_are_unique() -> None:
    names = [spec.name for spec in REGISTERED_METRICS]
    assert sorted(names) == sorted(set(names))


def test_a_missing_prefix_is_refused() -> None:
    with pytest.raises(MetricNameError, match="does not start with"):
        MetricSpec(name="platform_bus_events_total", kind="counter", labels=(), description="x")


def test_a_counter_without_the_total_suffix_is_refused() -> None:
    """Prometheus convention, and every recording rule written against it."""
    with pytest.raises(MetricNameError, match="must end with _total"):
        MetricSpec(name="fking_platform_bus_events", kind="counter", labels=(), description="x")


def test_a_gauge_ending_in_total_is_refused() -> None:
    """`_total` on a gauge reads as monotonic, so `rate()` over it returns nonsense."""
    with pytest.raises(MetricNameError, match="ends with _total"):
        MetricSpec(name="fking_risk_drawdown_total", kind="gauge", labels=(), description="x")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("fking_execution_stage_latency_ms", "seconds"),
        ("fking_execution_stage_latency_milliseconds", "seconds"),
        ("fking_execution_shortfall_bps", "basis_points"),
        ("fking_data_archive_mb", "bytes"),
    ],
)
def test_a_non_base_unit_suffix_is_refused_and_names_the_replacement(
    name: str, expected: str
) -> None:
    """A dashboard mixing `_seconds` and `_ms` panels misreads by 1000x and looks fine."""
    with pytest.raises(MetricNameError, match=expected):
        MetricSpec(name=name, kind="gauge", labels=(), description="x")


def test_an_unknown_subsystem_is_refused() -> None:
    """The subsystem segment is what makes the producing module readable from a panel."""
    with pytest.raises(MetricNameError, match="not a module in this system"):
        MetricSpec(name="fking_utils_things_total", kind="counter", labels=(), description="x")


@pytest.mark.parametrize("label", sorted(FORBIDDEN_LABELS))
def test_every_unbounded_label_is_refused(label: str) -> None:
    """One time series per order id takes Prometheus down during the incident that
    generated the order ids."""
    with pytest.raises(MetricNameError, match="unbounded label"):
        MetricSpec(
            name="fking_execution_orders_total", kind="counter", labels=(label,), description="x"
        )


def test_a_duplicated_label_is_refused() -> None:
    with pytest.raises(MetricNameError, match="more than once"):
        MetricSpec(
            name="fking_execution_orders_total",
            kind="counter",
            labels=("venue", "venue"),
            description="x",
        )


def test_a_permitted_name_is_accepted() -> None:
    """Guards the tests above: a validator that rejected everything would pass them all."""
    spec = MetricSpec(
        name="fking_execution_stage_latency_seconds",
        kind="histogram",
        labels=("stage",),
        description="Per-stage latency in the order path.",
    )
    assert spec.name.endswith("_seconds")
