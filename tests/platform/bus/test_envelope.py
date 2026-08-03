"""The envelope's identity is derived from content, and lossy payloads are refused.

The property that matters: two producers describing the same fact must produce the same
`event_id`, and any change to the fact must change it. Deduplication is exactly as good as
that property, and its failures are silent in both directions -- a key that is too
sensitive lets a duplicate through, one that is too insensitive collapses two real events.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from fking.platform.bus import EventEnvelope, PayloadError

pytestmark = pytest.mark.unit

CORRELATION_ID = UUID("0192f3c8-1e5b-7c0d-8a41-2b9d4e6f8a11")
OTHER_CORRELATION_ID = UUID("0192f3c8-2222-7c0d-8a41-2b9d4e6f8a22")
OCCURRED_AT = datetime(2026, 8, 3, 10, 15, tzinfo=UTC)


def _envelope(**overrides: object) -> EventEnvelope:
    fields: dict[str, object] = {
        "event_type": "fking.data.bar.ingested",
        "schema_version": 1,
        "correlation_id": CORRELATION_ID,
        "occurred_at_utc": OCCURRED_AT,
        "payload": {"symbol": "BTCUSDT", "close_quote_price": Decimal("64000.10")},
    }
    fields.update(overrides)
    return EventEnvelope.create(**fields)  # type: ignore[arg-type]


def test_the_same_fact_produces_the_same_event_id() -> None:
    """A producer retrying after a socket timeout republishes the same identity, which is
    what lets the consumer's claim collapse the duplicate."""
    assert _envelope().event_id == _envelope().event_id


def test_the_event_id_is_not_a_random_uuid() -> None:
    """Guards the test above against a `default_factory=uuid4` that happened to be
    memoised: the digest carries its recipe version, a uuid4 would not."""
    assert _envelope().event_id.startswith("ev1_")


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_type": "fking.data.bar.corrected"},
        {"schema_version": 2},
        {"correlation_id": OTHER_CORRELATION_ID},
        {"occurred_at_utc": OCCURRED_AT + timedelta(minutes=1)},
        {"payload": {"symbol": "ETHUSDT", "close_quote_price": Decimal("64000.10")}},
        {"causation_id": OTHER_CORRELATION_ID},
    ],
)
def test_changing_any_semantic_field_changes_the_identity(overrides: dict[str, object]) -> None:
    """A key insensitive to a field is a key that collapses two different facts into one
    -- and the second one is then never applied."""
    assert _envelope(**overrides).event_id != _envelope().event_id


def test_two_spellings_of_the_same_decimal_collapse_to_one_identity() -> None:
    """`Decimal("1.50")` and `Decimal("1.5")` are the same economic quantity. Without
    normalisation, two producers formatting a quantity differently publish "different"
    events that are the same fact, and both get applied."""
    trailing_zero = _envelope(payload={"base_quantity": Decimal("1.50")})
    minimal = _envelope(payload={"base_quantity": Decimal("1.5")})
    assert trailing_zero.event_id == minimal.event_id
    assert trailing_zero.payload["base_quantity"] == "1.5"


def test_a_decimal_crosses_the_wire_as_a_normalised_string() -> None:
    """Normalised, so the wire form carries the *value* and not one producer's spelling of
    it. `Decimal("64000.10") == Decimal("64000.1")`, and a consumer that re-quantises to a
    venue's tick size works from the value either way."""
    assert _envelope().payload["close_quote_price"] == "64000.1"


def test_a_float_in_a_payload_is_refused() -> None:
    """`.claude/rules/decimal-and-money.md`: a float never crosses a module boundary, and
    the bus is the module boundary."""
    with pytest.raises(PayloadError, match="float"):
        _envelope(payload={"close_quote_price": 64000.10})


def test_a_nested_float_is_refused_too() -> None:
    with pytest.raises(PayloadError, match=r"payload\.levels\[1\]"):
        _envelope(payload={"levels": ["1", 2.0]})


def test_a_naive_datetime_is_refused_at_construction() -> None:
    """Crypto trades 24/7 with no session boundary to make a timezone error obvious."""
    with pytest.raises(ValidationError):
        _envelope(occurred_at_utc=datetime(2026, 8, 3, 10, 15))  # noqa: DTZ001 - the point


def test_an_aware_but_non_utc_datetime_is_refused_rather_than_converted() -> None:
    """Converting would silently accept a value whose offset was guessed wrong upstream."""
    baku = timezone(timedelta(hours=4))
    with pytest.raises(ValidationError, match="UTC"):
        _envelope(occurred_at_utc=datetime(2026, 8, 3, 14, 15, tzinfo=baku))


def test_an_envelope_survives_the_json_round_trip_with_its_identity_intact() -> None:
    original = _envelope()
    restored = EventEnvelope.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.event_id == original.event_id


def test_an_envelope_whose_event_id_disagrees_with_its_content_is_refused() -> None:
    """A producer whose canonicalisation drifted would otherwise present as duplicate
    deliveries that deduplication silently fails to collapse."""
    tampered = _envelope().model_dump_json().replace("BTCUSDT", "ETHUSDT")
    with pytest.raises(ValidationError, match="does not match the digest"):
        EventEnvelope.model_validate_json(tampered)


def test_an_unknown_field_on_the_envelope_is_refused() -> None:
    """A producer that adds a field without registering it is a producer whose consumers
    fail on data written an hour ago."""
    with_extra = _envelope().model_dump_json()[:-1] + ',"venue":"binance"}'
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate_json(with_extra)


@pytest.mark.property
@given(
    symbol=st.text(min_size=1, max_size=12),
    quantity=st.decimals(
        min_value=Decimal("-1000000"),
        max_value=Decimal("1000000"),
        places=18,
        allow_nan=False,
        allow_infinity=False,
    ),
)
def test_identity_is_deterministic_over_arbitrary_payloads(symbol: str, quantity: Decimal) -> None:
    """Determinism is what a restart depends on: the same event rebuilt after a redeploy
    must hash to the row already in `processed_events`."""
    payload = {"symbol": symbol, "base_quantity": quantity}
    assert _envelope(payload=payload).event_id == _envelope(payload=payload).event_id


@pytest.mark.property
@given(
    keys=st.lists(st.text(min_size=1, max_size=8), min_size=2, max_size=5, unique=True),
)
def test_identity_does_not_depend_on_payload_key_order(keys: list[str]) -> None:
    """A dict built in a different order is the same payload. Without canonical sorting,
    two producers with different construction order publish two identities for one fact."""
    forwards = {key: str(index) for index, key in enumerate(keys)}
    backwards = {key: forwards[key] for key in reversed(keys)}
    assert _envelope(payload=forwards).event_id == _envelope(payload=backwards).event_id
