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
    ACTIVE_RENAMES,
    ACTIVE_SERIES_BUDGET,
    FORBIDDEN_LABELS,
    FORBIDDEN_VALUE_SOURCES,
    LABEL_DOMAINS,
    MAX_SERIES_PER_METRIC,
    METRIC_PREFIX,
    MIN_DUAL_EMISSION_DAYS,
    MONETARY_UNIT_SUFFIXES,
    PERMITTED_SUBSYSTEMS,
    PERMITTED_UNIT_SUFFIXES,
    REGISTERED_METRICS,
    SERIES_PER_INSTRUMENT,
    MetricNameError,
    MetricRename,
    MetricRenameError,
    MetricSpec,
    reconcile_with_pin,
)
from fking.platform.telemetry._setup import Telemetry, build_resource, configure_telemetry
from fking.platform.telemetry._spans import (
    CORRELATION_ATTRIBUTE,
    SpanContractError,
    traced,
    tracer,
)

__all__ = [
    "ACTIVE_RENAMES",
    "ACTIVE_SERIES_BUDGET",
    "CORRELATION_ATTRIBUTE",
    "FORBIDDEN_LABELS",
    "FORBIDDEN_VALUE_SOURCES",
    "LABEL_DOMAINS",
    "MAX_SERIES_PER_METRIC",
    "METRIC_PREFIX",
    "MIN_DUAL_EMISSION_DAYS",
    "MONETARY_UNIT_SUFFIXES",
    "PERMITTED_SUBSYSTEMS",
    "PERMITTED_UNIT_SUFFIXES",
    "REGISTERED_METRICS",
    "SERIES_PER_INSTRUMENT",
    "CounterHandle",
    "MetricLabelError",
    "MetricNameError",
    "MetricRename",
    "MetricRenameError",
    "MetricSpec",
    "SpanContractError",
    "Telemetry",
    "build_resource",
    "configure_telemetry",
    "counter",
    "reconcile_with_pin",
    "reset_instrument_cache",
    "traced",
    "tracer",
]
