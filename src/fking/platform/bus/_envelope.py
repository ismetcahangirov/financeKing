"""The event envelope, its deterministic identity, and the payload canonicalisation.

Three decisions carry this module.

**`event_id` is derived from content, never minted.** `uuid4()` at publish time gives a
fresh identity to every attempt, which is the same as having no identity at all: a
producer that retried after a socket timeout republishes the same fact under a new id,
the consumer's deduplication misses it, and the second `apply_fill` doubles a position.
The stream message id is not an identity either -- it is a delivery coordinate, and it
fails in both directions. `XAUTOCLAIM` redelivers under the *same* id (so deduplicating on
it is accidentally right), and a republish gets a *new* one (so deduplicating on it is
wrong). Only the content is stable across both.

**Money is a decimal string on the wire, and a `float` in a payload is refused.** A JSON
number is an IEEE 754 double in every parser that will ever read it, and this payload is
a reconstruction source. `.claude/rules/decimal-and-money.md` permits `float` inside
statistical computation in `backtest` and `data`; it never permits one to cross a module
boundary, and the bus is the module boundary.

**The envelope carries `event_id` as a field and validates it.** Recomputing on receipt
and comparing costs one hash and catches a producer whose canonicalisation drifted from
this one -- which would otherwise present as duplicate deliveries that deduplication
silently fails to collapse.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Final, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

PAYLOAD_FIELD: Final[str] = "data"
"""The Redis stream field the serialised envelope is written to."""

_DIGEST_PREFIX: Final[str] = "ev1_"
"""Version tag on the digest recipe. If the canonicalisation ever changes, the prefix
changes with it, so an old id and a new id are visibly different rather than colliding
in the deduplication table."""


class PayloadError(ValueError):
    """A payload value cannot be represented on the wire without losing precision."""


def _canonicalise(node: object, path: str = "payload") -> JsonValue:
    """Convert a payload value to its canonical JSON form, refusing lossy types.

    `Decimal` becomes its normalised plain string, so `Decimal("1.50")` and
    `Decimal("1.5")` -- the same economic quantity written two ways -- produce the same
    bytes and therefore the same `event_id`. Without normalisation two producers
    formatting a quantity differently publish "different" events that are the same fact.
    """
    if isinstance(node, Decimal):
        return format(node.normalize(), "f")
    if isinstance(node, float):
        raise PayloadError(
            f"{path} is a float. Money and quantities cross this boundary as decimal "
            f"strings; a JSON number is an IEEE 754 double in every parser that will "
            f"read this event, and this payload is a reconstruction source"
        )
    if isinstance(node, bool | int | str) or node is None:
        return node
    if isinstance(node, UUID | datetime):
        return str(node)
    if isinstance(node, Mapping):
        return {str(key): _canonicalise(child, f"{path}.{key}") for key, child in node.items()}
    if isinstance(node, Sequence):
        return [_canonicalise(child, f"{path}[{index}]") for index, child in enumerate(node)]
    raise PayloadError(f"{path} is {type(node).__name__}, which has no canonical JSON form")


def _canonical_payload(payload: object) -> dict[str, JsonValue]:
    if not isinstance(payload, Mapping):
        raise PayloadError(f"payload must be a mapping, got {type(payload).__name__}")
    return {str(key): _canonicalise(child, f"payload.{key}") for key, child in payload.items()}


CanonicalPayload = Annotated[Mapping[str, JsonValue], BeforeValidator(_canonical_payload)]


class EventEnvelope(BaseModel):
    """One fact that happened, addressed to whoever is listening.

    Frozen and `extra="forbid"`: a producer that adds a field without registering it is a
    producer whose consumers will fail on data written an hour ago, and silently
    accepting the field defers that failure to whichever consumer first depends on it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # No default: an envelope is built through `create()`, which derives this. Making it
    # optional would let a producer omit it and get one computed on the spot, which is
    # indistinguishable from a correct id and hides a producer that never computed one.
    event_id: str = Field(min_length=1)
    # `fking.<module>.<noun>.<verb>`, verb in the past tense: an event is a fact that
    # already happened. `.claude/rules/naming.md`. The module segment makes the producer
    # readable from a Grafana panel with no lookup.
    event_type: str = Field(min_length=1)
    schema_version: int = Field(ge=1)
    correlation_id: UUID
    causation_id: UUID | None = None
    occurred_at_utc: AwareDatetime
    payload: CanonicalPayload

    @field_validator("occurred_at_utc")
    @classmethod
    def _is_utc(cls, moment: datetime) -> datetime:
        # Rejected rather than converted. `astimezone(UTC)` would silently accept a value
        # whose offset was guessed wrong upstream; raising forces the guess to be made,
        # and reviewed, where the data enters.
        if moment.utcoffset() != UTC.utcoffset(None):
            raise ValueError(
                f"occurred_at_utc carries offset {moment.utcoffset()!r}; every datetime "
                f"in this system is UTC"
            )
        return moment

    @model_validator(mode="after")
    def _event_id_matches_the_content(self) -> Self:
        expected = content_digest(
            event_type=self.event_type,
            schema_version=self.schema_version,
            correlation_id=self.correlation_id,
            causation_id=self.causation_id,
            occurred_at_utc=self.occurred_at_utc,
            payload=self.payload,
        )
        if self.event_id != expected:
            raise ValueError(
                f"event_id {self.event_id!r} does not match the digest of this "
                f"envelope's content ({expected!r}). Either the producer canonicalises "
                f"differently from this version, or the payload was edited in flight -- "
                f"and both make deduplication silently fail to collapse a duplicate"
            )
        return self

    @classmethod
    def create(  # noqa: PLR0913 - one keyword per envelope field, by construction
        cls,
        *,
        event_type: str,
        schema_version: int,
        correlation_id: UUID,
        occurred_at_utc: datetime,
        payload: Mapping[str, object],
        causation_id: UUID | None = None,
    ) -> Self:
        """Build an envelope, deriving `event_id` from the canonicalised content.

        The payload is canonicalised twice -- once here to compute the digest and once by
        the field validator -- and that is the point: if the two disagreed, the digest
        would not describe the bytes that go on the wire.
        """
        canonical = _canonical_payload(payload)
        return cls(
            event_id=content_digest(
                event_type=event_type,
                schema_version=schema_version,
                correlation_id=correlation_id,
                causation_id=causation_id,
                occurred_at_utc=occurred_at_utc,
                payload=canonical,
            ),
            event_type=event_type,
            schema_version=schema_version,
            correlation_id=correlation_id,
            causation_id=causation_id,
            occurred_at_utc=occurred_at_utc,
            payload=canonical,
        )


def content_digest(  # noqa: PLR0913 - the digest inputs are the envelope's fields
    *,
    event_type: str,
    schema_version: int,
    correlation_id: UUID,
    causation_id: UUID | None,
    occurred_at_utc: datetime,
    payload: Mapping[str, JsonValue],
) -> str:
    """The stable identity of an event: a digest over everything that makes it that event.

    `occurred_at_utc` is included because the same fact at two instants is two facts --
    two bars for the same symbol differ in nothing else. `correlation_id` is included
    because the same fact reached along two causal chains is two events, and collapsing
    them would let one chain's consumer skip work the other chain needs done.
    """
    material = json.dumps(
        {
            "type": event_type,
            "version": schema_version,
            "correlation_id": str(correlation_id),
            "causation_id": str(causation_id) if causation_id is not None else None,
            "occurred_at_utc": occurred_at_utc.astimezone(UTC).isoformat(),
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return _DIGEST_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()
