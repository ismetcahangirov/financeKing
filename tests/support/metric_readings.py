"""Reading back what the metric instruments actually exported, in process.

The alternative -- asserting that a handle's `increment` was called -- proves the mock
was called. What matters here is the exported series: its name, its label set and its
value, because a metric with the right name and the wrong labels is a metric no
dashboard queries and no alert fires on.

The provider is swapped through the private global for the same reason
`tests/platform/telemetry/conftest.py` does it: the SDK keeps the first provider set and
logs a warning for the rest, so a public `set_meter_provider` would make exactly one test
in the suite able to read metrics. The instrument cache in `fking.platform.telemetry` is
reset on both sides, because a counter created under the previous provider keeps writing
to it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry.metrics import _internal as metrics_internal
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from fking.platform.telemetry import reset_instrument_cache

__all__ = ["MetricReadings", "metric_readings"]

# One label set, as a sorted tuple of pairs, so a reading can be looked up by labels
# without depending on the order the attributes were supplied in.
LabelSet = tuple[tuple[str, str], ...]


class MetricReadings:
    """An accumulating view over one in-memory metric reader.

    Every read collects and *merges* into a running snapshot rather than replacing it.
    That is not tidiness: a synchronous gauge reports its last value once and is empty
    on the next collection, so a test that reads two metric names in two statements
    would find the counter and lose the gauge, which looks exactly like an instrument
    that was never moved.
    """

    __slots__ = ("_reader", "_snapshot")

    def __init__(self, reader: InMemoryMetricReader) -> None:
        self._reader = reader
        self._snapshot: dict[str, dict[LabelSet, float]] = {}

    def _collect(self) -> None:
        data = self._reader.get_metrics_data()
        if data is None:
            return
        for resource_metric in data.resource_metrics:
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    points = self._snapshot.setdefault(metric.name, {})
                    for point in metric.data.data_points:
                        # Counters and gauges only. A histogram point carries buckets
                        # rather than a value, and this helper deliberately does not
                        # pretend to summarise one -- a test that needs a histogram
                        # should assert on its buckets.
                        if not isinstance(point, NumberDataPoint):
                            continue
                        attributes = point.attributes or {}
                        labels: LabelSet = tuple(
                            sorted((str(key), str(value)) for key, value in attributes.items())
                        )
                        points[labels] = float(point.value)

    def by_labels(self, metric_name: str) -> Mapping[LabelSet, float]:
        """Every exported data point for `metric_name`, keyed by its label set."""
        self._collect()
        return dict(self._snapshot.get(metric_name, {}))

    def names(self) -> frozenset[str]:
        """Every metric name that has exported at least one point."""
        self._collect()
        return frozenset(self._snapshot)


@contextmanager
def metric_readings() -> Iterator[MetricReadings]:
    """Install a real SDK meter provider writing to an in-memory reader."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    previous = metrics_internal._METER_PROVIDER
    metrics_internal._METER_PROVIDER = provider
    reset_instrument_cache()
    try:
        yield MetricReadings(reader)
    finally:
        provider.shutdown()
        metrics_internal._METER_PROVIDER = previous
        reset_instrument_cache()
