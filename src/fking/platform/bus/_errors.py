"""The bus error taxonomy, and the closed set of reasons a message is dead-lettered.

Reasons are a closed enum rather than free text because they are a Prometheus label. An
open reason string on `fking_platform_bus_dead_lettered_total` is an unbounded label, and
an unbounded label takes down Prometheus during the incident that generated the variety.
"""

from __future__ import annotations

from enum import StrEnum


class BusError(RuntimeError):
    """Base for every error this package raises deliberately."""


class EventSchemaError(BusError):
    """An event type is unregistered, or a registered schema changed without a bump."""


class DeadLetterReason(StrEnum):
    """Why a message could not be delivered to a handler.

    Every member describes a property of the *message*, never of the handler. A handler
    that raises is not dead-lettered: its message stays in the pending-entries list, is
    reclaimed, and is retried -- because a handler failure is usually the system being
    wrong about the world rather than the message being malformed, and discarding the
    message would hide that.
    """

    UNDECODABLE = "undecodable"
    """The stream entry is missing its payload field or is not valid JSON."""

    SCHEMA_INVALID = "schema_invalid"
    """The envelope or its payload failed validation against the registered model."""

    UNREGISTERED_EVENT_TYPE = "unregistered_event_type"
    """No schema is registered for this `event_type` at this `schema_version`."""

    MISSING_CORRELATION_ID = "missing_correlation_id"
    """No correlation id. Never invented -- an invented id creates a chain that looks
    complete, which is the one outcome worse than a broken chain."""
