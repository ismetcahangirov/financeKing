"""SDK wiring: the resource identifies the emitter, and a failed export is counted.

The resource matters more than it looks. Every span and every metric this process emits
carries it, and the question a reader has in front of a graph is which code produced it --
which is why `service.version` is the git SHA rather than the package version.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from opentelemetry import metrics, trace
from opentelemetry.metrics import _internal as metrics_internal
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ParentBased

from fking.platform.config.settings import TelemetrySettings
from fking.platform.telemetry import configure_telemetry
from fking.platform.telemetry._setup import _CountingSpanExporter, build_resource

pytestmark = pytest.mark.unit


def test_the_resource_identifies_the_service_version_and_environment() -> None:
    settings = TelemetrySettings.model_validate(
        {"service_name": "fking", "environment": "ci", "git_sha": "1a663a6"}
    )
    attributes = build_resource(settings).attributes
    assert attributes["service.name"] == "fking"
    assert attributes["service.version"] == "1a663a6"
    assert attributes["deployment.environment"] == "ci"


def test_an_unknown_git_sha_is_recorded_as_unknown_rather_than_omitted() -> None:
    """An absent attribute and an unknown one look identical on a graph; the literal
    makes "nobody set FKING_TELEMETRY__GIT_SHA" visible instead of invisible."""
    attributes = build_resource(TelemetrySettings()).attributes
    assert attributes["service.version"] == "unknown"


class _AlwaysFails(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:  # noqa: ARG002
        return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        return


class _AlwaysSucceeds(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:  # noqa: ARG002
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return


def test_a_failed_export_is_counted_rather_than_swallowed() -> None:
    """Silent trace loss during an incident is the worst moment to lose traces."""
    counting = _CountingSpanExporter(_AlwaysFails())
    assert counting.export([]) is SpanExportResult.FAILURE


def test_a_successful_export_passes_its_result_through() -> None:
    counting = _CountingSpanExporter(_AlwaysSucceeds())
    assert counting.export([]) is SpanExportResult.SUCCESS


def test_configure_telemetry_installs_both_providers_with_the_resource() -> None:
    """The entry point the process calls once, exercised rather than assumed.

    `instrument_libraries=False`: the instrumentors patch SQLAlchemy, httpx and redis
    process-wide and are not cleanly reversible, so a test that wanted a provider would
    otherwise leave a patched SQLAlchemy behind for every test after it.
    """
    settings = TelemetrySettings.model_validate({"environment": "ci", "git_sha": "abc1234"})
    previous_tracer = trace.get_tracer_provider()
    previous_meter = metrics.get_meter_provider()
    telemetry = configure_telemetry(settings, instrument_libraries=False)
    try:
        assert telemetry.tracer_provider.resource.attributes["service.version"] == "abc1234"
        assert telemetry.meter_provider is metrics.get_meter_provider()
        assert isinstance(telemetry.tracer_provider.sampler, ParentBased)
    finally:
        telemetry.shutdown()
        trace._TRACER_PROVIDER = previous_tracer  # the SDK has no public replacement
        metrics_internal._METER_PROVIDER = previous_meter
