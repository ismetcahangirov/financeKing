"""Metrics and tracing. Mechanism, not policy.

Three things live here and nothing else:

- **A frozen metric name registry** (`_registry`). Names are declared as data and
  validated at import, because renaming a metric later breaks every dashboard and alert
  at once, silently, in the direction of "no data".
- **Instruments bound to their declared labels** (`_instruments`). An increment carrying
  a label set the metric did not declare is refused, because a mistyped attribute key
  does not raise -- it forks the series.
- **Span helpers that stamp the correlation id** (`_spans`), and SDK wiring
  (`_setup`).

What is deliberately absent: the trading metric set (#98), span contracts and the
coverage audit over every decision path (#97), and dashboards (#99). Declaring a name
here before anything emits it freezes a name nobody has had to use.

Everything not in `__all__` is private and may change without notice.
"""

from fking.platform.telemetry._instruments import (
    CounterHandle,
    MetricLabelError,
    counter,
    reset_instrument_cache,
)
from fking.platform.telemetry._registry import (
    FORBIDDEN_LABELS,
    METRIC_PREFIX,
    PERMITTED_SUBSYSTEMS,
    PERMITTED_UNIT_SUFFIXES,
    REGISTERED_METRICS,
    MetricNameError,
    MetricSpec,
)
from fking.platform.telemetry._setup import Telemetry, build_resource, configure_telemetry
from fking.platform.telemetry._spans import (
    CORRELATION_ATTRIBUTE,
    SpanContractError,
    traced,
    tracer,
)

__all__ = [
    "CORRELATION_ATTRIBUTE",
    "FORBIDDEN_LABELS",
    "METRIC_PREFIX",
    "PERMITTED_SUBSYSTEMS",
    "PERMITTED_UNIT_SUFFIXES",
    "REGISTERED_METRICS",
    "CounterHandle",
    "MetricLabelError",
    "MetricNameError",
    "MetricSpec",
    "SpanContractError",
    "Telemetry",
    "build_resource",
    "configure_telemetry",
    "counter",
    "reset_instrument_cache",
    "traced",
    "tracer",
]
