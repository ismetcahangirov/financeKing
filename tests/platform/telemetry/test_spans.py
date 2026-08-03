"""Spans carry the correlation id, refuse a drifting name, and refuse secret attributes.

`OBSERVABILITY.md` section 5: a span whose attributes are empty is treated as no span. The
attribute that makes a span a record rather than a stopwatch is the correlation id, and
the test that matters is that a span cannot be opened without one.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fking.platform.correlation import MissingCorrelationIdError, correlation_scope
from fking.platform.telemetry import CORRELATION_ATTRIBUTE, SpanContractError, traced

pytestmark = pytest.mark.unit

CORRELATION_ID = UUID("0192f3c8-1e5b-7c0d-8a41-2b9d4e6f8a11")


def test_a_span_carries_the_active_correlation_id(span_exporter: InMemorySpanExporter) -> None:
    with correlation_scope(CORRELATION_ID), traced("risk.size_position", symbol="BTCUSDT"):
        pass

    (span,) = span_exporter.get_finished_spans()
    assert span.name == "risk.size_position"
    assert span.attributes is not None
    assert span.attributes[CORRELATION_ATTRIBUTE] == str(CORRELATION_ID)
    assert span.attributes["fking.symbol"] == "BTCUSDT"


def test_a_span_opened_outside_a_scope_refuses_rather_than_emitting_an_orphan(
    span_exporter: InMemorySpanExporter,
) -> None:
    """The correct fix is a scope at the top of the flow, not an invented id here."""
    with (
        pytest.raises(MissingCorrelationIdError, match=r"risk\.size_position"),
        traced("risk.size_position"),
    ):
        pass
    assert span_exporter.get_finished_spans() == ()


@pytest.mark.parametrize(
    "span_name",
    ["SizePosition", "risk", "risk.Size_Position", "risk..size", "risk.size position", ""],
)
def test_a_span_name_outside_module_operation_is_refused(
    span_name: str, span_exporter: InMemorySpanExporter
) -> None:
    """Span names are frozen; a rename empties every query built on them."""
    with (
        correlation_scope(CORRELATION_ID),
        pytest.raises(SpanContractError, match="module"),
        traced(span_name),
    ):
        pass
    assert span_exporter.get_finished_spans() == ()


@pytest.mark.parametrize(
    "attribute_name", ["api_key", "prompt", "venue_secret", "request_signature", "auth_token"]
)
def test_an_attribute_naming_secret_material_is_refused(
    attribute_name: str, span_exporter: InMemorySpanExporter
) -> None:
    """Tempo has seven-day retention and no access control worth the name."""
    with (
        correlation_scope(CORRELATION_ID),
        pytest.raises(SpanContractError, match="Tempo"),
        traced("agents.complete", **{attribute_name: "x"}),
    ):
        pass
    assert span_exporter.get_finished_spans() == ()


def test_a_nested_span_inherits_the_trace_of_its_parent(
    span_exporter: InMemorySpanExporter,
) -> None:
    """Loki-to-Tempo linking is only useful if one flow is one trace."""
    with correlation_scope(CORRELATION_ID), traced("risk.evaluate"), traced("risk.size_position"):
        pass

    child, parent = span_exporter.get_finished_spans()
    assert child.name == "risk.size_position"
    assert parent.name == "risk.evaluate"
    assert child.context is not None
    assert parent.context is not None
    assert child.context.trace_id == parent.context.trace_id
