"""Telemetry fixtures: real SDK providers writing to in-memory exporters.

In-memory rather than mocked. What is under test is what the SDK actually records --
resource attributes, sampler decisions, the attribute keys that end up on a span -- and a
mock tracer would record whatever the test author believed those to be.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fking.platform.telemetry import reset_instrument_cache


@pytest.fixture
def span_exporter() -> Iterator[InMemorySpanExporter]:
    """A tracer provider installed globally for the duration of one test.

    `SimpleSpanProcessor` rather than `BatchSpanProcessor`: a batch processor exports on a
    timer, so an assertion immediately after the span closes reads an empty exporter and
    the test becomes a race.

    The global provider cannot be replaced once set -- the SDK logs a warning and keeps
    the first -- so the previous one is swapped back through the private attribute rather
    than through `set_tracer_provider`. Test-support only, and the alternative is a suite
    in which exactly one test can install a provider.

    The previous value is read from the raw global rather than from
    `get_tracer_provider()`, and that distinction is load-bearing. With no provider
    installed the accessor *materialises* a `ProxyTracerProvider` without storing it, and
    restoring that object as the global makes the proxy delegate to itself: the next test
    anywhere in the suite that opens a span dies with `RecursionError` inside
    `get_tracer`, attributed to whichever test happened to run next.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    previous = trace._TRACER_PROVIDER  # may be None, and restoring None is the point
    trace._TRACER_PROVIDER = provider  # the SDK has no public replacement
    reset_instrument_cache()
    try:
        yield exporter
    finally:
        provider.shutdown()
        trace._TRACER_PROVIDER = previous  # restore the previous global
        reset_instrument_cache()
