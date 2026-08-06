"""Structured logging. `structlog`, JSON, one processor chain, configured once.

`OBSERVABILITY.md` section 6 is the specification. The three properties that carry it:

- **JSON in every environment.** There is no console renderer, not even for local
  development. A format that differs between environments is a format whose parsing is
  only tested in one of them, and the untested one is the one an investigation reads.
- **The message is an event type, not a sentence.** Values are fields. A formatted
  sentence cannot be aggregated: you cannot ask for the p99 submitted quantity without a
  regex over your own log format, and the regex breaks the first time someone reorders
  the words.
- **Redaction is in the pipeline and is allowlist-based**, so it cannot be forgotten at a
  call site and a field added tomorrow is dropped-and-counted rather than exposed.

Correlation ids live in `fking.platform.correlation`, not here, because the tracing
helpers need them too and neither package may import the other.

Everything not in `__all__` is private and may change without notice.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Final, TextIO, cast

import structlog
from structlog.typing import FilteringBoundLogger, Processor

from fking.platform.correlation import MissingCorrelationIdError
from fking.platform.logging._processors import (
    KEY_MATERIAL_MARKERS,
    ORPHAN,
    PAYLOAD_KEYS,
    LoggedPayloadError,
    LoggedSecretError,
    assert_no_key_material,
    bind_correlation_id,
    bind_otel_context,
    bind_service_identity,
    redact_to_allowlist,
    refuse_agent_payload,
    require_correlation_id,
)

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from fking.platform.config.settings import TelemetrySettings

# structlog's own key for the static event name is `event`; OBSERVABILITY.md section 6
# calls the field `message` and every documented Loki query is written against that
# spelling. Renamed once, in the chain, rather than asking every call site to pass
# `message=` -- which would collide with structlog's first positional argument.
EVENT_KEY: Final[str] = "message"

_LEVEL_NUMBERS: Final[dict[str, int]] = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
}


def get_logger(name: str) -> FilteringBoundLogger:
    """A logger that carries its module path as the mandatory `logger` field.

    `structlog.get_logger(__name__)` does *not* do this: the positional argument is
    handed to the logger factory, and `WriteLoggerFactory` discards it -- so the name
    silently never reaches the record. Binding it explicitly is the only spelling that
    survives, and every module in this repository uses this function rather than
    structlog's directly.

    Returns a lazy proxy, so calling it at module scope does not force configuration to
    have happened yet.
    """
    # structlog.get_logger() is typed as returning Any; the cast names what
    # configure_logging() actually installs as the wrapper class.
    return cast("FilteringBoundLogger", structlog.get_logger().bind(logger=name))


def build_processor_chain(settings: TelemetrySettings, *, strict: bool) -> list[Processor]:
    """The chain, in the order it must run.

    Order is load-bearing at three points, and a test asserts each:

    1. `bind_correlation_id` runs *before* `require_correlation_id`, or every record
       emitted inside a scope would be reported as an orphan.
    2. `EventRenamer` runs *before* `redact_to_allowlist`, because the allowlist names
       `message` and would otherwise drop the event name itself.
    3. `assert_no_key_material` runs *after* the allowlist, so it inspects exactly what is
       about to be serialised rather than what was offered.
    4. `refuse_agent_payload` runs *before* the allowlist, for the opposite reason: a
       prompt dropped silently teaches nobody, and the author keeps writing the call.
    """
    return [
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        bind_service_identity(settings),
        bind_correlation_id,
        bind_otel_context,
        require_correlation_id(strict=strict),
        structlog.processors.EventRenamer(EVENT_KEY),
        refuse_agent_payload,
        redact_to_allowlist(settings.log_field_allowlist),
        assert_no_key_material,
        # sort_keys so two processes emitting the same record emit the same bytes, which
        # is what lets a fixture assert on a whole line. default=str because Decimal and
        # UUID are not JSON types and str() on a Decimal is exact -- a float coercion
        # here would put a rounded price in the reconstruction source.
        #
        # Compact separators are load-bearing, not cosmetic: Grafana's Loki derived field
        # (ops/grafana/provisioning/datasources/datasources.yaml) matches
        # `"trace_id":"<32 hex>"` with no space, and json.dumps' default `", "` / `": "`
        # would make every log line fail that regex -- so the log-to-trace link would
        # silently not exist, with nothing reporting an error.
        structlog.processors.JSONRenderer(sort_keys=True, default=str, separators=(",", ":")),
    ]


def configure_logging(
    settings: TelemetrySettings, *, strict: bool = False, stream: TextIO | None = None
) -> None:
    """Install the process-wide logging configuration. Called once, at boot.

    `strict=True` makes a record with no correlation id raise instead of being repaired
    to `orphan`. The test suite sets it; the runtime does not, because raising inside a
    log call lets logging take the process down -- which inverts the point of the rule.

    `stream` defaults to stdout and exists so a test can assert on the bytes this writes
    rather than on the event dict a processor returned. structlog's writer captures the
    file object once, at configuration time, so a test that swaps `sys.stdout` afterwards
    is writing to a handle this logger has never seen.
    """
    structlog.configure(
        processors=build_processor_chain(settings, strict=strict),
        wrapper_class=structlog.make_filtering_bound_logger(_LEVEL_NUMBERS[settings.log_level]),
        # stdout, not stderr: the container runtime collects stdout and the Collector
        # reads it. Sending records to stderr splits the stream in two and only one half
        # reaches Loki.
        logger_factory=structlog.WriteLoggerFactory(file=stream or sys.stdout),
        cache_logger_on_first_use=True,
    )


__all__ = [
    "EVENT_KEY",
    "KEY_MATERIAL_MARKERS",
    "ORPHAN",
    "PAYLOAD_KEYS",
    "LoggedPayloadError",
    "LoggedSecretError",
    "MissingCorrelationIdError",
    "build_processor_chain",
    "configure_logging",
    "get_logger",
]
