"""The structlog processor chain. Order matters and is asserted by a test.

Every clause here is `OBSERVABILITY.md` section 6 or section 7 made mechanical:

- **JSON in every environment, including local.** A format that differs between
  environments is a format whose parsing is only tested in one of them.
- **Static message, structured values.** `{message="position_sized"} | json |
  binding_limit="max_notional"` is a query. A formatted sentence is not.
- **Redaction in the pipeline, allowlist-based.** A call site that remembers to redact is
  a call site whose next editor forgets, and a denylist means every new field is exposed
  until someone remembers to add it -- which nobody does during an incident, which is
  exactly when new fields get added and debug logging gets switched on.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, Final

from opentelemetry import trace
from structlog.typing import EventDict, Processor, WrappedLogger

from fking.platform.correlation import MissingCorrelationIdError, current_correlation_id
from fking.platform.telemetry import counter
from fking.platform.telemetry._registry import LOG_FIELDS_DROPPED, LOG_ORPHAN_RECORDS

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from fking.platform.config.settings import TelemetrySettings

# The value substituted for a record emitted outside any correlation scope. One literal,
# never an empty string and never a fresh id: an invented id creates a chain that looks
# complete, which is the one outcome worse than a broken chain.
ORPHAN: Final[str] = "orphan"

# Substrings whose presence in a *value* means key material reached the record regardless
# of what the field was called. The allowlist governs field names; this governs contents,
# because a permitted field can still be handed the wrong string.
#
# Public so the test fixtures are generated from it rather than written by hand: adding a
# marker here extends coverage automatically, and a hand-written fixture would also be a
# literal PEM header committed to the repository, which the secret scanner correctly
# refuses. The two halves are stored separately for the same reason -- concatenated, this
# file would contain the header it exists to detect.
KEY_MATERIAL_MARKERS: Final[tuple[str, ...]] = (
    "-----BEGIN",
    "PRIVATE KEY",
)


class LoggedSecretError(RuntimeError):
    """Key material reached the renderer. Never caught; the record is not emitted."""


def bind_service_identity(settings: TelemetrySettings) -> Processor:
    """Attach `service`, `version` and `environment` to every record.

    Returned as a closure over settings rather than reading a module global, so a test
    can build a chain for one configuration without disturbing the process's.
    """
    service = settings.service_name
    version = settings.git_sha or "unknown"
    environment = settings.environment

    def processor(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
        event_dict["service"] = service
        event_dict["version"] = version
        event_dict["environment"] = environment
        return event_dict

    return processor


def bind_correlation_id(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Copy the active correlation scope onto the record, if the call site did not.

    An explicit keyword wins: `boot.py` passes `correlation_id="boot"` before any scope
    exists, and a consumer re-binds the id it read out of an event envelope.
    """
    if event_dict.get("correlation_id") is None:
        active = current_correlation_id()
        if active is not None:
            event_dict["correlation_id"] = active
    return event_dict


def bind_otel_context(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Attach `trace_id` and `span_id` when a span is active, for Loki-to-Tempo linking.

    Never written by hand at a call site: a hand-written trace id is a trace id that
    disagrees with the span it claims to belong to.
    """
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = format(span_context.trace_id, "032x")
        event_dict["span_id"] = format(span_context.span_id, "016x")
    return event_dict


def require_correlation_id(*, strict: bool) -> Processor:
    """Refuse (strict) or repair-and-count (runtime) a record with no correlation id.

    The split is the whole enforcement design. Raising inside a production log call lets
    logging take the process down, which inverts the point of the rule; passing silently
    in a test suite means the rule is never enforced anywhere. So the test suite is where
    a violation hurts, and the runtime emits `fking_platform_log_orphan_records_total`,
    which is alerted on and treated as a defect in the emitting module.
    """
    orphans = counter(LOG_ORPHAN_RECORDS)

    def processor(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
        if event_dict.get("correlation_id"):
            return event_dict
        if strict:
            raise MissingCorrelationIdError(
                f"log record {event_dict.get('event', '<unnamed>')!r} carries no "
                f"correlation_id. Open a correlation_scope() at the top of the flow"
            )
        orphans.increment(logger=str(event_dict.get("logger", "unknown")))
        event_dict["correlation_id"] = ORPHAN
        return event_dict

    return processor


def redact_to_allowlist(allowlist: Collection[str]) -> Processor:
    """Drop every field not explicitly permitted, and count what was dropped.

    The counter is what makes an allowlist maintainable rather than a silent hole. A
    field silently dropped is a bug; a field dropped *and counted* shows up on a
    dashboard and gets added deliberately, which is the review step the allowlist exists
    to force.
    """
    permitted = frozenset(allowlist)
    dropped = counter(LOG_FIELDS_DROPPED)

    def processor(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
        offending = [key for key in event_dict if key not in permitted]
        for key in offending:
            del event_dict[key]
            dropped.increment(field_name=key)
        return event_dict

    return processor


def _first_key_material_path(node: object, path: str) -> str | None:
    """Depth-first search for key material, returning the dotted path to it.

    Recursive rather than a scan of top-level values: the boot record's `config` field is
    the whole settings tree, so a PEM that leaked into it would sit several levels down
    and a shallow check would report clean.
    """
    if isinstance(node, str):
        if any(marker in node for marker in KEY_MATERIAL_MARKERS):
            return path
        return None
    if isinstance(node, Mapping):
        for key, child in node.items():
            found = _first_key_material_path(child, f"{path}.{key}")
            if found is not None:
                return found
        return None
    if isinstance(node, (list, tuple)):
        for index, child in enumerate(node):
            found = _first_key_material_path(child, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def assert_no_key_material(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Raise if a permitted field was handed key material.

    The allowlist answers "which field names may be emitted". It cannot answer "was this
    permitted field given a PEM", and an Ed25519 private key logged under `config` is a
    key that must be rotated -- Loki has no delete-by-line.
    """
    for key, mapped in event_dict.items():
        found = _first_key_material_path(mapped, key)
        if found is not None:
            raise LoggedSecretError(
                f"{found} contains key material; log the key *path* and the audit row "
                f"id, never the key"
            )
    return event_dict
