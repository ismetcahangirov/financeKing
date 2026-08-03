"""The event schema registry: what may be published, at which version, in what shape.

An event bus with silently drifting schemas produces consumers that fail on data written
an hour ago. The failure is worse than a crash: the producer's new field is simply
absent from the consumer's view, so the consumer applies a decision made from a payload
it only partly understood, and nothing about the run reports an error.

So a registration declares three things and the registry checks the third against the
first two: the `event_type`, the `schema_version`, and a digest of the payload model's
JSON schema. Editing the model without touching the digest fails **at import**, printing
the digest that would make it pass. That failure is the review step: the author must then
decide whether the change is compatible -- in which case they update the digest in the
same diff, where a reviewer sees both -- or incompatible, in which case they register a
new `schema_version` and leave the old one registered so consumers mid-upgrade keep
working.

The registry ships empty. There is no producer yet: the first real registration arrives
with live ingestion in #27, and inventing `fking.data.bar.ingested`'s payload here would
freeze a schema whose owner has not been written. Tests register their own.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel

from fking.platform.bus._errors import EventSchemaError

_DIGEST_PREFIX: Final[str] = "sch1_"


@dataclass(frozen=True, slots=True)
class RegisteredEvent:
    """One event type at one schema version."""

    event_type: str
    schema_version: int
    payload_model: type[BaseModel]
    schema_digest: str


_REGISTRY: Final[dict[tuple[str, int], RegisteredEvent]] = {}


def schema_digest(payload_model: type[BaseModel]) -> str:
    """A stable digest over a payload model's JSON schema.

    JSON Schema rather than the field annotations directly: it captures types,
    constraints, defaults, required-ness and nested models in one canonical structure, so
    tightening a `ge=` bound moves the digest exactly as adding a field does. A tightened
    bound *is* a compatibility change -- it rejects payloads that used to validate.
    """
    canonical = json.dumps(payload_model.model_json_schema(), sort_keys=True, separators=(",", ":"))
    return _DIGEST_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def register_event(
    *,
    event_type: str,
    schema_version: int,
    payload_model: type[BaseModel],
    declared_digest: str,
) -> RegisteredEvent:
    """Register a payload schema, refusing a model whose shape has drifted.

    `declared_digest` is what the author asserts the model hashes to; a mismatch is the
    review trigger.
    """
    if schema_version < 1:
        raise EventSchemaError(f"{event_type} declares schema_version={schema_version}; minimum 1")
    actual = schema_digest(payload_model)
    if actual != declared_digest:
        raise EventSchemaError(
            f"{event_type} v{schema_version} declares schema digest {declared_digest!r} but "
            f"{payload_model.__name__} hashes to {actual!r}. The payload shape changed. If "
            f"the change is backward compatible, update the declared digest in this diff so "
            f"a reviewer sees both; if it is not, register a new schema_version and leave "
            f"this one in place for consumers mid-upgrade"
        )
    key = (event_type, schema_version)
    existing = _REGISTRY.get(key)
    if existing is not None and existing.payload_model is not payload_model:
        raise EventSchemaError(
            f"{event_type} v{schema_version} is already registered to "
            f"{existing.payload_model.__name__}; two models for one version means which "
            f"one validates depends on import order"
        )
    registered = RegisteredEvent(
        event_type=event_type,
        schema_version=schema_version,
        payload_model=payload_model,
        schema_digest=actual,
    )
    _REGISTRY[key] = registered
    return registered


def resolve_event(event_type: str, schema_version: int) -> RegisteredEvent:
    """The registration for `event_type` at `schema_version`, or `EventSchemaError`."""
    found = _REGISTRY.get((event_type, schema_version))
    if found is None:
        known = sorted(f"{name} v{version}" for name, version in _REGISTRY)
        raise EventSchemaError(
            f"no schema registered for {event_type!r} v{schema_version}; registered: "
            f"{known or '(none)'}"
        )
    return found


def registered_events() -> tuple[RegisteredEvent, ...]:
    """Every registration, ordered, for the schema-registry test to iterate."""
    return tuple(_REGISTRY[key] for key in sorted(_REGISTRY))


def unregister_all() -> None:
    """Empty the registry. Test-support only, so one test's events cannot leak into the next."""
    _REGISTRY.clear()
