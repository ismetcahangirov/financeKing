"""Instruments built from the registry, with their label sets checked at the call site.

An OpenTelemetry counter accepts any attribute mapping. That is the right decision for a
general-purpose SDK and the wrong one here: a typo in an attribute key does not raise, it
creates a second time series that looks like the first and sums to a different number.
The declared label tuple in `_registry` is what a panel is written against, so an
increment that does not carry exactly those labels is refused.

Instruments are cached by name. Creating two instruments with the same name against the
same meter is not an error in the SDK -- it is a warning and a duplicate registration --
so the cache is what keeps a module imported twice from producing two series.
"""

from __future__ import annotations

from typing import Final

from opentelemetry import metrics

# The pinned opentelemetry-api build exports the synchronous gauge instrument as `_Gauge`
# rather than `Gauge` -- `Meter.create_gauge`'s own return annotation names it `Gauge` and
# the class's `__qualname__` agrees, but the module's `__all__` only lists the
# underscore-prefixed name. Aliased on import so nothing downstream carries the upstream
# quirk in its own name.
from opentelemetry.metrics import Counter
from opentelemetry.metrics import _Gauge as Gauge

from fking.platform.telemetry._registry import MetricSpec

_METER_NAME: Final[str] = "fking.platform.telemetry"

_counters: Final[dict[str, Counter]] = {}
_gauges: Final[dict[str, Gauge]] = {}


class MetricLabelError(ValueError):
    """An increment supplied a label set the metric did not declare."""


class CounterHandle:
    """A counter bound to its declared labels.

    Deliberately not a subclass of the SDK's `Counter`: the point is that `add()` with an
    arbitrary attribute mapping is unreachable from application code.
    """

    __slots__ = ("_spec",)

    def __init__(self, spec: MetricSpec) -> None:
        self._spec = spec

    @property
    def spec(self) -> MetricSpec:
        return self._spec

    def increment(self, by: int = 1, /, **labels: str) -> None:
        """Add `by` to the counter, refusing any label set other than the declared one.

        `by` is positional-only so a label happening to be called `by` cannot silently
        bind to it.
        """
        supplied = frozenset(labels)
        declared = frozenset(self._spec.labels)
        if supplied != declared:
            raise MetricLabelError(
                f"{self._spec.name} declares labels {sorted(declared)} but was "
                f"incremented with {sorted(supplied)}; a mismatched label set creates a "
                f"second series that no dashboard queries"
            )
        if by < 0:
            raise MetricLabelError(
                f"{self._spec.name} is a counter and cannot decrease; got by={by}"
            )
        _counter(self._spec).add(by, dict(labels))


class GaugeHandle:
    """A gauge bound to its declared labels.

    Deliberately not a subclass of the SDK's `Gauge`, for the same reason as
    `CounterHandle`: `set()` with an arbitrary attribute mapping is unreachable from
    application code. Unlike a counter a gauge may decrease and may carry a negative
    reading -- a signed risk share or a beta is a legitimate gauge value -- so `set()`
    imposes no sign constraint of its own.
    """

    __slots__ = ("_spec",)

    def __init__(self, spec: MetricSpec) -> None:
        self._spec = spec

    @property
    def spec(self) -> MetricSpec:
        return self._spec

    def set(self, reading: float, /, **labels: str) -> None:
        """Record the current reading, refusing any label set other than the declared one."""
        supplied = frozenset(labels)
        declared = frozenset(self._spec.labels)
        if supplied != declared:
            raise MetricLabelError(
                f"{self._spec.name} declares labels {sorted(declared)} but was set with "
                f"{sorted(supplied)}; a mismatched label set creates a second series that "
                f"no dashboard queries"
            )
        _gauge(self._spec).set(reading, dict(labels))


def _counter(spec: MetricSpec) -> Counter:
    cached = _counters.get(spec.name)
    if cached is not None:
        return cached
    # get_meter() resolves against whatever provider is installed *at call time*, so
    # deferring instrument creation until first use is what lets configure_telemetry()
    # run after modules have already been imported. Before it runs, this is the API's
    # no-op provider and increments cost a few attribute lookups.
    created = metrics.get_meter(_METER_NAME).create_counter(
        name=spec.name, description=spec.description
    )
    _counters[spec.name] = created
    return created


def counter(spec: MetricSpec) -> CounterHandle:
    """A handle for a declared counter. Raises if the spec is not a counter."""
    if spec.kind != "counter":
        raise MetricLabelError(f"{spec.name} is a {spec.kind}, not a counter")
    return CounterHandle(spec)


def _gauge(spec: MetricSpec) -> Gauge:
    cached = _gauges.get(spec.name)
    if cached is not None:
        return cached
    # Same deferred-creation reasoning as `_counter`: resolving against the meter at call
    # time, not at import time, is what lets `configure_telemetry()` run after modules
    # have already imported their instruments.
    created = metrics.get_meter(_METER_NAME).create_gauge(
        name=spec.name, description=spec.description
    )
    _gauges[spec.name] = created
    return created


def gauge(spec: MetricSpec) -> GaugeHandle:
    """A handle for a declared gauge. Raises if the spec is not a gauge."""
    if spec.kind != "gauge":
        raise MetricLabelError(f"{spec.name} is a {spec.kind}, not a gauge")
    return GaugeHandle(spec)


def reset_instrument_cache() -> None:
    """Drop cached instruments so a test can install a fresh meter provider.

    Test-support only. Without it the second test to configure a provider silently keeps
    the first one's instruments, and its assertions read zero.
    """
    _counters.clear()
    _gauges.clear()
