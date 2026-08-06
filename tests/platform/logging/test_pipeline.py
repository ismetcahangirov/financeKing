"""Every line is valid JSON with the mandatory fields, and nothing else gets out.

These tests run the **real** processor chain over real records and parse the rendered
output with `json.loads`. Asserting on the event dict before the renderer would test the
processors and not the thing an investigation reads, and the difference between those two
is exactly where a non-serialisable value turns a log line into a traceback.
"""

from __future__ import annotations

import io
import json
import pathlib
import re
import sys
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Final
from uuid import UUID

import pytest
import structlog
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from structlog.typing import EventDict

from fking.platform.config.settings import TelemetrySettings
from fking.platform.correlation import MissingCorrelationIdError, correlation_scope
from fking.platform.logging import (
    EVENT_KEY,
    KEY_MATERIAL_MARKERS,
    ORPHAN,
    PAYLOAD_KEYS,
    LoggedPayloadError,
    LoggedSecretError,
    build_processor_chain,
    configure_logging,
    get_logger,
)
from fking.platform.telemetry import traced

pytestmark = pytest.mark.unit

CORRELATION_ID = UUID("0192f3c8-1e5b-7c0d-8a41-2b9d4e6f8a11")

# OBSERVABILITY.md section 6. Every one of these must be on every record; a line missing
# any of them cannot be joined to the chain it belongs to.
MANDATORY_FIELDS: Final[tuple[str, ...]] = (
    "timestamp",
    "level",
    "logger",
    EVENT_KEY,
    "correlation_id",
    "service",
    "version",
    "environment",
)

SETTINGS: Final = TelemetrySettings.model_validate(
    {"service_name": "fking", "environment": "ci", "git_sha": "1a663a6"}
)


def _render(fields: dict[str, object] | None = None, *, strict: bool = True) -> dict[str, object]:
    """Push one record through the whole chain and parse what comes out.

    A mapping rather than `**kwargs`: a field name supplied from a parametrize case would
    otherwise bind to `strict` whenever the two happened to collide.
    """
    # `logger` is bound by get_logger(), not added by the chain, so it is seeded here to
    # match what a real call site produces.
    event_dict: EventDict = {
        "event": "position_sized",
        "logger": "fking.test",
        **(fields or {}),
    }
    rendered: object = event_dict
    for processor in build_processor_chain(SETTINGS, strict=strict):
        rendered = processor(None, "info", rendered)  # type: ignore[arg-type]
    assert isinstance(rendered, str), "the last processor must be the JSON renderer"
    parsed = json.loads(rendered)
    assert isinstance(parsed, dict)
    return parsed


def test_a_record_inside_a_scope_is_json_carrying_every_mandatory_field() -> None:
    with correlation_scope(CORRELATION_ID):
        record = _render({"symbol": "BTCUSDT"})
    for field in MANDATORY_FIELDS:
        assert field in record, f"{field} missing from {sorted(record)}"
    assert record[EVENT_KEY] == "position_sized"
    assert record["correlation_id"] == str(CORRELATION_ID)
    assert record["service"] == "fking"
    assert record["environment"] == "ci"
    assert record["version"] == "1a663a6"


def test_the_event_key_is_renamed_to_message_rather_than_dropped() -> None:
    """`{message="position_sized"}` is the documented Loki selector. structlog calls the
    key `event`, and the allowlist names `message` -- so getting the rename order wrong
    drops the event name itself."""
    with correlation_scope(CORRELATION_ID):
        record = _render()
    assert "event" not in record
    assert record[EVENT_KEY] == "position_sized"


def test_an_unpermitted_field_is_dropped() -> None:
    """A denylist would expose every new field until somebody remembered to add it."""
    with correlation_scope(CORRELATION_ID):
        record = _render({"unregistered_thing": "leaked"})
    assert "unregistered_thing" not in record
    assert "leaked" not in json.dumps(record)


@pytest.mark.parametrize(
    "field_name", ["symbol", "venue", "order_id", "strategy_id", "notional_usd", "audit_ref"]
)
def test_a_documented_conditional_field_survives(field_name: str) -> None:
    """Guards the drop test: an allowlist that dropped everything would pass it."""
    with correlation_scope(CORRELATION_ID):
        record = _render({field_name: "x"})
    assert record[field_name] == "x"


def test_a_record_with_no_scope_raises_under_strict() -> None:
    """Strict is the test configuration. The runtime repairs, because raising inside a
    log call lets logging take the process down."""
    with pytest.raises(MissingCorrelationIdError, match="position_sized"):
        _render()


def test_a_record_with_no_scope_is_repaired_to_orphan_at_runtime() -> None:
    record = _render(strict=False)
    assert record["correlation_id"] == ORPHAN


def test_an_explicit_correlation_id_wins_over_the_ambient_one() -> None:
    """`boot.py` passes `correlation_id="boot"` before any scope exists, and a consumer
    re-binds the id it read out of an envelope."""
    with correlation_scope(CORRELATION_ID):
        record = _render({"correlation_id": "boot"})
    assert record["correlation_id"] == "boot"


@pytest.mark.parametrize("marker", KEY_MATERIAL_MARKERS)
def test_key_material_in_a_permitted_field_raises(marker: str) -> None:
    """The allowlist answers which field names may be emitted. It cannot answer whether a
    permitted field was handed a PEM, and Loki has no delete-by-line.

    Parametrized over the module's own marker list rather than over a hand-written PEM
    header. Two reasons, and the second one bit during review: adding a marker extends
    this test with no edit, and a literal header committed here is exactly what the
    repository's secret scanner refuses -- correctly, because it cannot tell a fixture
    from a leak, and a scanner with an allowlist for test files is a scanner with a hole
    shaped like the next real key somebody pastes into one.
    """
    with correlation_scope(CORRELATION_ID), pytest.raises(LoggedSecretError, match="key material"):
        _render({"config": f"{marker} the rest of a key would follow here"})


@pytest.mark.parametrize("marker", KEY_MATERIAL_MARKERS)
def test_key_material_nested_inside_the_boot_config_raises(marker: str) -> None:
    """The boot record's `config` field is the whole settings tree; a shallow scan of
    top-level values would report it clean."""
    with correlation_scope(CORRELATION_ID), pytest.raises(LoggedSecretError, match="binance"):
        _render({"config": {"exchange": {"binance": {"spot_ed25519_key": marker}}}})


@pytest.mark.parametrize("payload_key", sorted(PAYLOAD_KEYS))
def test_an_llm_payload_raises_and_points_the_author_at_the_audit_row(payload_key: str) -> None:
    """Prompts and responses belong in `agent_call`, never in the log stream.

    Raising rather than dropping is the whole point of running this before the allowlist:
    a silently dropped prompt produces a record that looks fine, so the author keeps
    writing the same call, and the payload it carries is one that log retention will
    expire long before an investigation needs it (OBSERVABILITY.md section 1).
    """
    with correlation_scope(CORRELATION_ID), pytest.raises(LoggedPayloadError, match="audit_ref"):
        _render({payload_key: "You are an analyst. <untrusted>...</untrusted>"})


def test_the_audit_row_reference_itself_is_emittable() -> None:
    """Guards the test above: a chain that refused everything would also pass it."""
    with correlation_scope(CORRELATION_ID):
        record = _render({"audit_ref": "41822"})
    assert record["audit_ref"] == "41822"


def test_a_decimal_is_rendered_as_its_exact_string_not_a_float() -> None:
    """The log is a reconstruction source. A JSON number is an IEEE 754 double in every
    parser that will ever read it."""
    with correlation_scope(CORRELATION_ID):
        record = _render({"notional_usd": Decimal("1043.27000000")})
    assert record["notional_usd"] == "1043.27000000"


def test_a_record_emitted_inside_a_span_carries_the_trace_and_span_ids() -> None:
    """This is the Loki-to-Tempo link. Without it a log line and the trace that produced
    it are two records with no join key, and the "open the trace from this line" workflow
    silently does not exist."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # The raw global, not `get_tracer_provider()`: with no provider installed the
    # accessor materialises a ProxyTracerProvider without storing it, and restoring
    # that object as the global makes the proxy delegate to itself -- every later
    # span in the suite then dies with RecursionError inside get_tracer.
    previous = trace._TRACER_PROVIDER
    trace._TRACER_PROVIDER = provider  # the SDK has no public replacement
    try:
        with correlation_scope(CORRELATION_ID), traced("risk.evaluate"):
            record = _render()
    finally:
        provider.shutdown()
        trace._TRACER_PROVIDER = previous

    (span,) = exporter.get_finished_spans()
    assert span.context is not None
    assert record["trace_id"] == format(span.context.trace_id, "032x")
    assert record["span_id"] == format(span.context.span_id, "016x")


def test_a_record_emitted_outside_a_span_omits_the_trace_ids() -> None:
    """Absent rather than null: a null trace id is a value a Loki query can match on, and
    matching on it selects every line that had no span."""
    with correlation_scope(CORRELATION_ID):
        record = _render()
    assert "trace_id" not in record
    assert "span_id" not in record


def test_the_rendered_keys_are_sorted_so_two_processes_emit_the_same_bytes() -> None:
    with correlation_scope(CORRELATION_ID):
        rendered: object = {"event": "position_sized", "symbol": "BTCUSDT"}
        for processor in build_processor_chain(SETTINGS, strict=True):
            rendered = processor(None, "info", rendered)  # type: ignore[arg-type]
    assert isinstance(rendered, str)
    keys = list(json.loads(rendered))
    assert keys == sorted(keys)


@pytest.fixture
def captured_output() -> Iterator[Callable[[], list[dict[str, object]]]]:
    """Configure the real logger against a buffer and parse whatever it writes.

    End to end rather than chain-only: `configure_logging` chooses the wrapper class, the
    logger factory and the stream, and a chain that renders correctly through a factory
    nobody uses proves nothing about the bytes in the container's log.

    A buffer rather than `capsys`, because structlog's writer captures the file object at
    configuration time and pytest's capture swaps `sys.stdout` around it -- which produces
    a write to a closed handle rather than a captured line. The default stream is asserted
    separately.
    """
    buffer = io.StringIO()
    previous = structlog.get_config()
    configure_logging(SETTINGS, strict=True, stream=buffer)
    try:
        yield lambda: [json.loads(line) for line in buffer.getvalue().splitlines() if line]
    finally:
        structlog.configure(**previous)


def test_configure_logging_writes_parseable_json_with_the_logger_name(
    captured_output: Callable[[], list[dict[str, object]]],
) -> None:
    with correlation_scope(CORRELATION_ID):
        get_logger("fking.test").info("order_accepted", symbol="BTCUSDT", venue="binance-spot")

    (record,) = captured_output()
    assert record[EVENT_KEY] == "order_accepted"
    assert record["logger"] == "fking.test"
    assert record["correlation_id"] == str(CORRELATION_ID)
    assert record["symbol"] == "BTCUSDT"
    assert record["level"] == "info"


def test_the_default_stream_is_stdout() -> None:
    """stderr would split the stream in two, and only half of it reaches Loki."""
    previous = structlog.get_config()
    configure_logging(SETTINGS, strict=True)
    try:
        factory = structlog.get_config()["logger_factory"]
        assert factory._file is sys.stdout
    finally:
        structlog.configure(**previous)


def test_debug_records_are_filtered_out_at_the_configured_level(
    captured_output: Callable[[], list[dict[str, object]]],
) -> None:
    """The default level is info; a filtered record must not reach the chain at all, or
    the strict correlation check would fire on records nobody asked to emit."""
    with correlation_scope(CORRELATION_ID):
        get_logger("fking.test").debug("noise")
    assert captured_output() == []


# The Loki derived field that turns a trace_id in a log line into a link to the trace.
# Restated from ops/grafana/provisioning/datasources/datasources.yaml, and asserted to
# still be there, because the two artifacts fail independently: a renderer that starts
# emitting `"trace_id": "..."` with a space produces log lines that simply do not link,
# and nothing anywhere reports an error.
_GRAFANA_DERIVED_FIELD_REGEX: Final[str] = r'"trace_id":"([a-f0-9]{32})"'


def test_the_provisioned_grafana_derived_field_still_uses_this_regex() -> None:
    """Guards the test below: if Grafana's config changes, the pattern asserted here is
    no longer the one that has to match, and the next test would be checking nothing."""
    provisioned = (
        pathlib.Path(__file__).resolve().parents[3]
        / "ops"
        / "grafana"
        / "provisioning"
        / "datasources"
        / "datasources.yaml"
    ).read_text(encoding="utf-8")
    assert _GRAFANA_DERIVED_FIELD_REGEX in provisioned


def test_a_rendered_line_matches_grafana_s_trace_id_derived_field() -> None:
    """The log-to-trace link, asserted against the rendered bytes rather than believed."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # The raw global, not `get_tracer_provider()`: with no provider installed the
    # accessor materialises a ProxyTracerProvider without storing it, and restoring
    # that object as the global makes the proxy delegate to itself -- every later
    # span in the suite then dies with RecursionError inside get_tracer.
    previous = trace._TRACER_PROVIDER
    trace._TRACER_PROVIDER = provider  # the SDK has no public replacement
    try:
        with correlation_scope(CORRELATION_ID), traced("risk.evaluate"):
            rendered: object = {"event": "position_sized", "logger": "fking.test"}
            for processor in build_processor_chain(SETTINGS, strict=True):
                rendered = processor(None, "info", rendered)  # type: ignore[arg-type]
    finally:
        provider.shutdown()
        trace._TRACER_PROVIDER = previous

    assert isinstance(rendered, str)
    matched = re.search(_GRAFANA_DERIVED_FIELD_REGEX, rendered)
    assert matched is not None, f"Grafana's derived field would not link this line: {rendered}"
    (span,) = exporter.get_finished_spans()
    assert span.context is not None
    assert matched.group(1) == format(span.context.trace_id, "032x")
