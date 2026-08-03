"""OpenTelemetry SDK wiring: one provider pair, one exporter endpoint, one resource.

The pipeline is `OBSERVABILITY.md` section 2: the SDK exports OTLP/gRPC to the Collector,
which fans metrics out to Prometheus, logs to Loki and traces to Tempo. Audit rows do
**not** travel this path -- they are written to Postgres in the same transaction as the
state change they describe, because an observability outage must not be able to destroy
the record that the reconstruction guarantee rests on.

Sampling: the configured ratio applies to ordinary work; `ParentBased` means a sampled
parent keeps its whole subtree, so a trace is never half-recorded. The order path is
never sampled -- `TelemetrySettings.order_path_sample_ratio` is `Literal[1]` and cannot
be configured downwards -- and #97 wires the always-on sampler into the order path when
that path exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from fking.platform.telemetry._instruments import counter
from fking.platform.telemetry._registry import TELEMETRY_SPANS_DROPPED

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    # Type-only, so that `fking.platform.logging` and `fking.platform.telemetry` carry no
    # runtime import of the settings tree. `fking.platform.config.boot` logs, so a
    # runtime edge in that direction would close a cycle between configuration and the
    # pipeline that reports on it.
    from fking.platform.config.settings import TelemetrySettings

# 60s rather than the SDK's 60s default restated for no reason: it is stated here because
# it is the interval a Grafana panel's rate() window has to accommodate, and a value
# nobody wrote down is a value nobody can reason about when a panel looks jagged.
_METRIC_EXPORT_INTERVAL_MS: Final[int] = 60_000


class _CountingSpanExporter(SpanExporter):
    """Wraps an exporter and counts the spans it failed to deliver.

    `OBSERVABILITY.md` section 2: the SDK buffers and then drops. Silent trace loss during
    an incident is the worst possible moment to lose traces, so the drop is counted and
    alertable rather than logged at debug and forgotten.
    """

    def __init__(self, wrapped: SpanExporter) -> None:
        self._wrapped = wrapped
        self._dropped = counter(TELEMETRY_SPANS_DROPPED)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        outcome = self._wrapped.export(spans)
        if outcome is SpanExportResult.FAILURE:
            self._dropped.increment(len(spans))
        return outcome

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._wrapped.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self._wrapped.shutdown()


@dataclass(frozen=True, slots=True)
class Telemetry:
    """The configured providers, with an explicit shutdown.

    Held rather than discarded because a process that exits without flushing loses the
    spans covering whatever it was doing when it decided to exit -- which is the window
    an investigation always wants.
    """

    tracer_provider: TracerProvider
    meter_provider: MeterProvider

    def shutdown(self) -> None:
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()


def build_resource(settings: TelemetrySettings) -> Resource:
    """Emitter identity, attached to every span and every metric.

    `service.version` carries the git SHA rather than the package version: the package
    version changes at a release and the SHA changes at every deploy, and the question a
    reader has in front of a graph is which code produced it.
    """
    return Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": settings.git_sha or "unknown",
            "deployment.environment": settings.environment,
        }
    )


def _instrument_libraries() -> None:
    """Auto-instrument the libraries that are dependencies today.

    Guarded, because the instrumentors patch library internals process-wide and are not
    cleanly reversible: instrumenting twice double-wraps every call, so every span in the
    process acquires a duplicate parent. The guard is the SDK's own flag rather than a
    module-level boolean of ours -- `BaseInstrumentor` is a singleton, so the flag is the
    same object on every construction, and it stays correct if something else in the
    process instruments one of these first.

    FastAPI is absent because `fking.api` does not exist yet; it joins with #102.
    """
    for instrumentor in (
        HTTPXClientInstrumentor(),
        RedisInstrumentor(),
        SQLAlchemyInstrumentor(),
    ):
        if not instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.instrument()


def configure_telemetry(
    settings: TelemetrySettings, *, instrument_libraries: bool = True
) -> Telemetry:
    """Install the global tracer and meter providers. Called once, at process start.

    `instrument_libraries=False` exists for tests: the instrumentors patch library
    internals process-wide and are not cleanly reversible, so a test that only wants a
    provider should not leave a patched SQLAlchemy behind for every test after it.
    """
    resource = build_resource(settings)

    tracer_provider = TracerProvider(
        resource=resource,
        # ParentBased so a sampled parent keeps its whole subtree: a trace that is
        # sampled in the middle shows a gap that reads as a missing hop rather than as a
        # sampling decision.
        sampler=ParentBased(TraceIdRatioBased(float(settings.trace_sample_ratio))),
    )
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            _CountingSpanExporter(
                OTLPSpanExporter(
                    endpoint=settings.otlp_endpoint,
                    timeout=int(settings.otlp_timeout_seconds),
                    insecure=settings.otlp_endpoint.startswith("http://"),
                )
            )
        )
    )

    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=settings.otlp_endpoint,
                    timeout=int(settings.otlp_timeout_seconds),
                    insecure=settings.otlp_endpoint.startswith("http://"),
                ),
                export_interval_millis=_METRIC_EXPORT_INTERVAL_MS,
            )
        ],
    )

    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)

    if instrument_libraries:
        _instrument_libraries()

    return Telemetry(tracer_provider=tracer_provider, meter_provider=meter_provider)
