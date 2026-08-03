"""A payload schema cannot change shape without somebody noticing in the same diff.

The failure this guards is not a crash. A producer adds a field, the consumer's model does
not have it, and the consumer applies a decision made from a payload it only partly
understood -- with nothing in the run reporting an error.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field

from fking.platform.bus import EventSchemaError, register_event, registered_events, resolve_event
from fking.platform.bus._registry import schema_digest

pytestmark = pytest.mark.unit

# Two registrations: v1 and v2 of the same event type, coexisting during an upgrade.
EXPECTED_REGISTRATIONS = 2


class BarPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    close_quote_price: str


class WiderBarPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    close_quote_price: str
    base_volume: str


class BoundedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    conviction: int = Field(ge=0, le=100)


class TighterBoundedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    conviction: int = Field(ge=10, le=100)


def _register(model: type[BaseModel], *, version: int = 1, declared: str | None = None) -> None:
    register_event(
        event_type="fking.data.bar.ingested",
        schema_version=version,
        payload_model=model,
        declared_digest=declared if declared is not None else schema_digest(model),
    )


def test_a_registered_event_resolves() -> None:
    _register(BarPayload)
    assert resolve_event("fking.data.bar.ingested", 1).payload_model is BarPayload


def test_an_unregistered_event_type_raises_and_lists_what_is_registered() -> None:
    """The message is what an operator reads at 03:00 in front of a dead-letter stream."""
    _register(BarPayload)
    with pytest.raises(EventSchemaError, match=r"fking\.data\.bar\.ingested v1"):
        resolve_event("fking.risk.order.approved", 1)


def test_an_unregistered_version_of_a_registered_type_raises() -> None:
    _register(BarPayload)
    with pytest.raises(EventSchemaError, match="no schema registered"):
        resolve_event("fking.data.bar.ingested", 2)


def test_adding_a_field_without_updating_the_declared_digest_is_refused() -> None:
    """The review step: the author must state whether this needs a version bump."""
    stale = schema_digest(BarPayload)
    with pytest.raises(EventSchemaError, match="payload shape changed"):
        _register(WiderBarPayload, declared=stale)


def test_tightening_a_constraint_is_treated_as_a_shape_change() -> None:
    """A tightened bound rejects payloads that used to validate, which is precisely a
    compatibility change -- and it is invisible in a field-name diff."""
    stale = schema_digest(BoundedPayload)
    with pytest.raises(EventSchemaError, match="payload shape changed"):
        register_event(
            event_type="fking.agents.thesis.proposed",
            schema_version=1,
            payload_model=TighterBoundedPayload,
            declared_digest=stale,
        )


def test_a_new_version_may_be_registered_alongside_the_old_one() -> None:
    """Consumers mid-upgrade keep working, which is the whole reason versions exist."""
    _register(BarPayload, version=1)
    _register(WiderBarPayload, version=2)
    assert resolve_event("fking.data.bar.ingested", 1).payload_model is BarPayload
    assert resolve_event("fking.data.bar.ingested", 2).payload_model is WiderBarPayload
    assert len(registered_events()) == EXPECTED_REGISTRATIONS


def test_two_models_for_one_version_is_refused() -> None:
    """Which one validates would otherwise depend on import order."""
    _register(BarPayload)
    with pytest.raises(EventSchemaError, match="already registered"):
        _register(WiderBarPayload)


def test_a_version_below_one_is_refused() -> None:
    with pytest.raises(EventSchemaError, match="minimum 1"):
        _register(BarPayload, version=0)


def test_the_digest_is_stable_across_calls() -> None:
    """If it were not, every import would look like a shape change and the check would be
    switched off within a week."""
    assert schema_digest(BarPayload) == schema_digest(BarPayload)
