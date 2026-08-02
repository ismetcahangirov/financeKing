"""Codec behaviour that the round-trip property cannot express.

A round-trip property proves the codec agrees with itself. These prove it disagrees
with everything else -- a JSON number where a Decimal belongs, a payload missing a
field, an annotation shape the codec was never taught.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

import pytest

from fking.domain import (
    Account,
    Balance,
    Direction,
    DomainError,
    Fill,
    Instrument,
    Order,
    Position,
    PositionTransition,
    Side,
    Signal,
    Venue,
    decode,
    encode,
)
from tests.support.domain_factory import (
    BTCUSDT,
    EPOCH,
    make_bar,
    make_fill,
    make_order,
    make_signal,
)

pytestmark = pytest.mark.unit


def test_decimals_encode_as_strings_never_as_json_numbers() -> None:
    """A JSON number is a double to any parser without a `parse_float` hook."""
    payload = encode(make_fill(side=Side.BUY, quote_price="64000.01", base_quantity="0.00001"))
    assert isinstance(payload, dict)
    assert payload["quote_price"] == "64000.01"
    assert payload["base_quantity"] == "0.00001"
    # The claim that matters: nothing anywhere in the payload is a float. A single one
    # would round on the way out and be unrecoverable on the way back.
    assert not any(isinstance(item, float) for item in payload.values())
    assert "0.00001" in json.dumps(payload)  # not 1e-05


def test_a_timedelta_encodes_as_integer_microseconds() -> None:
    """`total_seconds()` returns a float, which is the thing this package excludes."""
    payload = encode(make_signal())
    assert isinstance(payload, dict)
    assert payload["horizon"] == 8 * 60 * 60 * 1_000_000
    assert decode(Signal, payload).horizon == timedelta(hours=8)


def test_enums_encode_as_their_values() -> None:
    payload = encode(make_signal())
    assert isinstance(payload, dict)
    assert payload["direction"] == "long"
    assert decode(Signal, payload).direction is Direction.LONG


def test_a_nested_instrument_round_trips() -> None:
    restored = decode(Order, encode(make_order()))
    assert restored.instrument == BTCUSDT
    assert isinstance(restored.instrument, Instrument)


def test_a_decimal_arriving_as_a_json_number_is_refused() -> None:
    payload = encode(make_fill(side=Side.BUY, quote_price="64000.01", base_quantity="0.001"))
    assert isinstance(payload, dict)
    payload["quote_price"] = 64000.01  # type: ignore[assignment]  # a float on the wire is the test
    with pytest.raises(DomainError, match="must arrive as a JSON string"):
        decode(Fill, payload)


def test_a_missing_field_is_refused_rather_than_defaulted() -> None:
    """A tolerant decoder reconstructs an object that never existed and reports success."""
    payload = encode(make_fill(side=Side.BUY, quote_price="64000.01", base_quantity="0.001"))
    assert isinstance(payload, dict)
    del payload["fee_quote"]
    with pytest.raises(DomainError, match=r"missing \('fee_quote',\)"):
        decode(Fill, payload)


def test_an_unexpected_field_is_refused() -> None:
    payload = encode(make_fill(side=Side.BUY, quote_price="64000.01", base_quantity="0.001"))
    assert isinstance(payload, dict)
    payload["commission_asset"] = "BNB"
    with pytest.raises(DomainError, match=r"unexpected \('commission_asset',\)"):
        decode(Fill, payload)


def test_domain_invariants_are_re_checked_on_decode() -> None:
    """Decoding calls the real constructor, so a tampered payload fails here."""
    payload = encode(make_signal())
    assert isinstance(payload, dict)
    payload["conviction"] = "1.5"
    with pytest.raises(DomainError, match=r"\[0, 1\]"):
        decode(Signal, payload)


def test_a_naive_datetime_on_the_wire_is_refused() -> None:
    payload = encode(make_fill(side=Side.BUY, quote_price="64000.01", base_quantity="0.001"))
    assert isinstance(payload, dict)
    payload["event_time_utc"] = "2026-08-01T12:00:00"
    with pytest.raises(DomainError, match="timezone-aware"):
        decode(Fill, payload)


@pytest.mark.parametrize(
    ("field_name", "payload_value", "expected"),
    [
        ("event_time_utc", "not-a-date", "not an ISO 8601 datetime"),
        ("event_time_utc", 17_524_837, "must arrive as an ISO 8601 string"),
        ("quote_price", "not-a-number", "not a decimal"),
        ("fill_id", "not-a-uuid", "is not a UUID"),
        ("fill_id", 42, "must arrive as a string"),
        ("venue_trade_id", 42, "expected a string"),
        ("side", "sideways", "not a member of Side"),
    ],
)
def test_malformed_scalars_are_refused(
    field_name: str, payload_value: object, expected: str
) -> None:
    payload = encode(make_fill(side=Side.BUY, quote_price="64000.01", base_quantity="0.001"))
    assert isinstance(payload, dict)
    payload[field_name] = payload_value  # type: ignore[assignment]  # malformed input is the test
    with pytest.raises(DomainError, match=expected):
        decode(Fill, payload)


@pytest.mark.parametrize("payload_value", [True, "4210", 4210.0])
def test_an_int_field_refuses_bools_and_strings(payload_value: object) -> None:
    """`True` satisfies `isinstance(x, int)`; a trade_count of True is not a number."""
    payload = encode(make_bar())
    assert isinstance(payload, dict)
    payload["trade_count"] = payload_value  # type: ignore[assignment]  # malformed input is the test
    with pytest.raises(DomainError, match="expected an int"):
        decode(type(make_bar()), payload)


def test_an_optional_field_accepts_null() -> None:
    order = make_order(order_type=make_order().order_type)
    payload = encode(order)
    assert isinstance(payload, dict)
    assert decode(Order, payload).limit_quote_price is not None

    signal = make_signal(direction=Direction.FLAT, invalidation_quote_price=None)
    flat_payload = encode(signal)
    assert isinstance(flat_payload, dict)
    assert flat_payload["invalidation_quote_price"] is None
    assert decode(Signal, flat_payload) == signal


def test_a_dataclass_payload_must_be_a_json_object() -> None:
    with pytest.raises(DomainError, match="must arrive as a JSON object"):
        decode(Fill, ["not", "an", "object"])


def test_a_target_the_codec_was_never_taught_is_refused() -> None:
    with pytest.raises(DomainError, match="no decoding for complex"):
        decode(complex, "1+2j")


def test_a_bar_round_trips_with_its_integer_trade_count() -> None:
    bar = make_bar()
    assert decode(type(bar), encode(bar)) == bar


def test_a_position_transition_round_trips_with_its_boolean_flag() -> None:
    """`crossed_flat` is the one boolean field in the package.

    It is also the field an audit reader uses to tell a flip from a close, so a codec
    that could not carry it would silently merge two different economic events.
    """
    transition = (
        Position.flat(BTCUSDT)
        .with_fill(make_fill(side=Side.BUY, quote_price="64000.00", base_quantity="0.01"))
        .after.with_fill(make_fill(side=Side.SELL, quote_price="65000.00", base_quantity="0.03"))
    )
    assert transition.crossed_flat is True

    restored = decode(PositionTransition, encode(transition))
    assert restored == transition
    assert restored.crossed_flat is True


def test_a_timedelta_arriving_as_a_string_is_refused() -> None:
    payload = encode(make_signal())
    assert isinstance(payload, dict)
    payload["horizon"] = "PT8H"
    with pytest.raises(DomainError, match="must arrive as microseconds"):
        decode(Signal, payload)


def test_a_boolean_field_refuses_a_non_boolean() -> None:
    flat = Position.flat(BTCUSDT)
    payload = encode(
        PositionTransition(
            before=flat,
            after=flat,
            closed_base_quantity=Decimal("0"),
            opened_base_quantity=Decimal("0"),
            realised_pnl_quote=Decimal("0"),
            crossed_flat=False,
        )
    )
    assert isinstance(payload, dict)
    payload["crossed_flat"] = "false"
    with pytest.raises(DomainError, match="expected a bool"):
        decode(PositionTransition, payload)


# ---------------------------------------------------------------------------
# annotation shapes the codec deliberately refuses
# ---------------------------------------------------------------------------

_ANY_UUID: Final = UUID(int=3)


@dataclass(frozen=True, slots=True)
class _WiderUnion:
    field: Decimal | UUID | None


@dataclass(frozen=True, slots=True)
class _FixedTuple:
    field: tuple[Decimal, UUID]


@dataclass(frozen=True, slots=True)
class _NonStringKeys:
    field: dict[int, Decimal]


@dataclass(frozen=True, slots=True)
class _UnsupportedField:
    field: complex


@dataclass(frozen=True, slots=True)
class _MutableSequenceField:
    field: list[Decimal]


@dataclass(frozen=True, slots=True)
class _Parameterised[Element]:
    field: Element


def test_a_union_wider_than_x_or_none_is_refused() -> None:
    """Deciding which arm a payload belongs to needs a discriminator.

    Guessing produces a value that decodes into the wrong arm and type-checks
    perfectly everywhere downstream.
    """
    with pytest.raises(DomainError, match=r"only `X \| None` unions"):
        decode(_WiderUnion, {"field": "1.5"})


def test_a_fixed_length_tuple_is_refused() -> None:
    with pytest.raises(DomainError, match="only homogeneous tuple"):
        decode(_FixedTuple, {"field": ["1.5", str(_ANY_UUID)]})


def test_a_non_string_keyed_mapping_is_refused() -> None:
    with pytest.raises(DomainError, match="only str-keyed mappings"):
        decode(_NonStringKeys, {"field": {"1": "1.5"}})


def test_an_unsupported_field_type_is_refused() -> None:
    with pytest.raises(DomainError, match="no decoding for complex"):
        decode(_UnsupportedField, {"field": "1+2j"})


def test_a_mutable_sequence_annotation_is_refused() -> None:
    """No domain type may declare one, and the codec refuses to make one either.

    A `list` field on a frozen dataclass is still mutable through the reference, so a
    codec that happily built one would be a way around the immutability sweep.
    """
    with pytest.raises(DomainError, match="no decoding for the generic annotation"):
        decode(_MutableSequenceField, {"field": ["1.5"]})


def test_an_unresolved_type_variable_is_refused() -> None:
    with pytest.raises(DomainError, match="no decoding for the annotation"):
        decode(_Parameterised, {"field": "1.5"})


def test_a_sequence_field_must_arrive_as_a_list() -> None:
    position = (
        Position.flat(BTCUSDT)
        .with_fill(make_fill(side=Side.BUY, quote_price="64000.00", base_quantity="0.01"))
        .after
    )
    payload = encode(position)
    assert isinstance(payload, dict)
    payload["applied_fill_ids"] = {"not": "a list"}
    with pytest.raises(DomainError, match="must arrive as a JSON list"):
        decode(Position, payload)


def test_a_mapping_field_must_arrive_as_a_json_object() -> None:
    account = Account(
        venue=Venue.BINANCE_SPOT_TESTNET,
        account_id="demo-1",
        balances={
            "USDT": Balance(asset="USDT", free_quantity=Decimal("1"), locked_quantity=Decimal("0"))
        },
        updated_at_utc=EPOCH,
    )
    payload = encode(account)
    assert isinstance(payload, dict)
    assert decode(Account, payload) == account

    payload["balances"] = ["USDT"]
    with pytest.raises(DomainError, match="must arrive as a JSON object"):
        decode(Account, payload)


def test_encoding_an_unsupported_object_is_refused() -> None:
    with pytest.raises(DomainError, match="no encoding for complex"):
        encode(complex(1, 2))


def test_encoding_handles_the_container_shapes_in_use() -> None:
    encoded = encode([Decimal("1.5"), (Decimal("2.5"),), datetime(2026, 8, 1, tzinfo=UTC)])
    assert encoded == ["1.5", ["2.5"], "2026-08-01T00:00:00+00:00"]
