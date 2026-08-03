"""The metric name registry, validated at import time.

`OBSERVABILITY.md` section 4 makes the case that this file exists to enforce: renaming a
metric breaks every dashboard, every alert and every historical query simultaneously,
silently, and in the direction of "no data" rather than "error". A panel goes blank and
an alert stops firing -- and an alert that stops firing looks exactly like an alert with
nothing to report, so a rename can disable a safety alert invisibly until the thing it
watched for happens. Prometheus cannot rename history either: old samples stay under the
old name, so a rename forks the series rather than migrating it.

So names are declared here, as data, and validated when this module is imported. A
misnamed instrument fails at import rather than at the first scrape, which is the
difference between a failed test run and a dashboard nobody notices is empty.

Only the platform and bus instruments this issue needs are declared. The trading metric
set -- data, strategy, risk, execution, agents -- is #98's, and declaring it here before
anything emits it would freeze names nobody has yet had to use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

METRIC_PREFIX: Final[str] = "fking_"

# Matches the module map in `.claude/rules/module-boundaries.md`, plus `telemetry` for
# the SDK's own self-reporting, which belongs to no trading module.
PERMITTED_SUBSYSTEMS: Final[frozenset[str]] = frozenset(
    {
        "agents",
        "backtest",
        "data",
        "evolution",
        "execution",
        "platform",
        "risk",
        "strategy",
        "telemetry",
    }
)

# Base units only. `_milliseconds` and `_bp` are absent on purpose: a dashboard that
# mixes `_seconds` and `_milliseconds` panels produces a 1000x reading error that looks
# like a plausible number, which is worse than one that looks wrong.
PERMITTED_UNIT_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        "basis_points",
        "bytes",
        "fields",
        "messages",
        "ratio",
        "seconds",
        "usd",
    }
)

# Suffixes that are a unit spelled in a non-base form. Named explicitly so the failure
# message can say what to write instead, rather than "not a permitted suffix".
_REJECTED_UNIT_SUFFIXES: Final[dict[str, str]] = {
    "bp": "basis_points",
    "bps": "basis_points",
    "kb": "bytes",
    "mb": "bytes",
    "microseconds": "seconds",
    "millis": "seconds",
    "milliseconds": "seconds",
    "ms": "seconds",
    "nanoseconds": "seconds",
    "secs": "seconds",
}

# `OBSERVABILITY.md` section 4: an unbounded label takes down Prometheus, and it does it
# during an incident, because an incident is when the unbounded thing gets interesting.
# High-cardinality identifiers belong on log lines and span attributes, which are indexed
# for exactly that.
FORBIDDEN_LABELS: Final[frozenset[str]] = frozenset(
    {
        "audit_ref",
        "client_order_id",
        "correlation_id",
        "event_id",
        "fill_id",
        "message_id",
        "order_id",
        "prompt",
        "trace_id",
        "trade_id",
    }
)

_SNAKE_CASE: Final[re.Pattern[str]] = re.compile(r"\A[a-z][a-z0-9_]*[a-z0-9]\Z")

type InstrumentKind = Literal["counter", "gauge", "histogram"]


class MetricNameError(ValueError):
    """A declared metric name violates the frozen naming convention."""


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """One metric, named once and never renamed.

    `description` is not decoration: it is what a reader sees in Grafana's metric browser
    when deciding whether this is the series they meant, and the alternative to writing
    it here is writing it in a dashboard JSON that diverges.
    """

    name: str
    kind: InstrumentKind
    labels: tuple[str, ...]
    description: str

    def __post_init__(self) -> None:
        _validate(self)


def _validate(spec: MetricSpec) -> None:
    name = spec.name
    if not name.startswith(METRIC_PREFIX):
        raise MetricNameError(
            f"{name!r} does not start with {METRIC_PREFIX!r}; the prefix is what makes "
            f"every metric this system emits selectable with one regex and separates it "
            f"from the exporters sharing the same Prometheus"
        )
    if _SNAKE_CASE.fullmatch(name) is None:
        raise MetricNameError(f"{name!r} is not lower snake_case")

    remainder = name.removeprefix(METRIC_PREFIX)
    subsystem = remainder.split("_", 1)[0]
    if subsystem not in PERMITTED_SUBSYSTEMS:
        raise MetricNameError(
            f"{name!r} names subsystem {subsystem!r}, which is not a module in this "
            f"system; permitted: {sorted(PERMITTED_SUBSYSTEMS)}"
        )

    is_counter = spec.kind == "counter"
    if is_counter and not name.endswith("_total"):
        raise MetricNameError(f"counter {name!r} must end with _total (Prometheus convention)")
    if not is_counter and name.endswith("_total"):
        raise MetricNameError(
            f"{spec.kind} {name!r} ends with _total, which every reader and every "
            f"recording rule will take to mean a monotonic counter"
        )

    measured = name.removesuffix("_total")
    trailing = measured.rsplit("_", 1)[-1]
    if trailing in _REJECTED_UNIT_SUFFIXES:
        raise MetricNameError(
            f"{name!r} ends in the non-base unit {trailing!r}; write "
            f"{_REJECTED_UNIT_SUFFIXES[trailing]!r} and convert at the call site"
        )

    forbidden = sorted(set(spec.labels) & FORBIDDEN_LABELS)
    if forbidden:
        raise MetricNameError(
            f"{name!r} declares unbounded label(s) {forbidden}; an unbounded label "
            f"builds one time series per value. Put the identifier on the log line and "
            f"the span attribute instead"
        )
    duplicated = sorted({label for label in spec.labels if spec.labels.count(label) > 1})
    if duplicated:
        raise MetricNameError(f"{name!r} declares label(s) {duplicated} more than once")


BUS_EVENTS_PUBLISHED: Final = MetricSpec(
    name="fking_platform_bus_events_published_total",
    kind="counter",
    labels=("stream", "event_type"),
    description="Envelopes accepted by XADD, by stream and event type.",
)

BUS_EVENTS_CONSUMED: Final = MetricSpec(
    name="fking_platform_bus_events_consumed_total",
    kind="counter",
    labels=("stream", "consumer_group", "outcome"),
    description=(
        "Envelopes a consumer reached a terminal decision on. `outcome` is applied, "
        "duplicate or dead_lettered -- a duplicate is normal operation, not an error."
    ),
)

BUS_MESSAGES_RECLAIMED: Final = MetricSpec(
    name="fking_platform_bus_messages_reclaimed_total",
    kind="counter",
    labels=("stream", "consumer_group"),
    description=(
        "Messages taken from a dead consumer's pending-entries list by XAUTOCLAIM. "
        "Non-zero after every restart, by design."
    ),
)

BUS_DEAD_LETTERED: Final = MetricSpec(
    name="fking_platform_bus_dead_lettered_total",
    kind="counter",
    labels=("stream", "reason"),
    description=(
        "Messages routed to the dead-letter stream. `reason` is a closed enum: "
        "undecodable, unregistered_event_type, schema_invalid, missing_correlation_id."
    ),
)

LOG_FIELDS_DROPPED: Final = MetricSpec(
    name="fking_platform_log_fields_dropped_total",
    kind="counter",
    labels=("field_name",),
    # Labelled by the dropped field name, which looks like a cardinality violation and is
    # not, for the same reason fking_platform_allowlist_rejections_total is labelled by
    # host: in correct operation this metric is always zero, and knowing *which* field
    # was dropped is the entire value of it. A field being silently dropped is a bug; a
    # field being dropped and counted is a signal that gets it added deliberately.
    description="Log fields discarded because they are absent from the field allowlist.",
)

LOG_ORPHAN_RECORDS: Final = MetricSpec(
    name="fking_platform_log_orphan_records_total",
    kind="counter",
    labels=("logger",),
    description=(
        "Log records emitted with no correlation scope active, repaired to "
        "correlation_id=orphan. Non-zero means a flow is not opening a scope, which is "
        "a defect in the emitting module rather than a threshold to raise."
    ),
)

TELEMETRY_SPANS_DROPPED: Final = MetricSpec(
    name="fking_telemetry_spans_dropped_total",
    kind="counter",
    labels=(),
    description=(
        "Spans the exporter failed to deliver. Silent trace loss during an incident is "
        "the worst possible moment to lose traces, so it is counted rather than logged."
    ),
)

REGISTERED_METRICS: Final[tuple[MetricSpec, ...]] = (
    BUS_DEAD_LETTERED,
    BUS_EVENTS_CONSUMED,
    BUS_EVENTS_PUBLISHED,
    BUS_MESSAGES_RECLAIMED,
    LOG_FIELDS_DROPPED,
    LOG_ORPHAN_RECORDS,
    TELEMETRY_SPANS_DROPPED,
)

_declared_names = [spec.name for spec in REGISTERED_METRICS]
_duplicates = sorted({name for name in _declared_names if _declared_names.count(name) > 1})
if _duplicates:  # pragma: no cover - a module-level guard against a copy-paste mistake
    raise MetricNameError(f"metric name(s) declared more than once: {_duplicates}")
