"""Round-trip properties for the JSON codec.

The comparison that matters is `Decimal.compare_total`, not `==`. `Decimal("1.50") ==
Decimal("1.5")` is True, so an equality-only round-trip test passes against a codec
that normalises exponents -- and the exponent is what carries the venue's step size. A
quantity that comes back as `1.5` where the venue sent `1.50000` has lost the evidence
that it was snapped to a five-decimal lot step.
"""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.domain import Fill, JsonValue, Position, decode, encode
from tests.support.domain_factory import BTCUSDT, fills

pytestmark = [pytest.mark.property, pytest.mark.unit]


def _decimals_compare_total_equal(left: object, right: object) -> bool:
    """Recursively compare two domain objects, `Decimal`s by exact representation."""
    if isinstance(left, Decimal) and isinstance(right, Decimal):
        return left.compare_total(right) == Decimal("0")
    if is_dataclass(left) and not isinstance(left, type):
        if type(left) is not type(right):
            return False
        return all(
            _decimals_compare_total_equal(
                getattr(left, declared.name), getattr(right, declared.name)
            )
            for declared in fields(left)
        )
    if isinstance(left, tuple | list) and isinstance(right, tuple | list):
        return len(left) == len(right) and all(
            _decimals_compare_total_equal(one, other)
            for one, other in zip(left, right, strict=True)
        )
    return bool(left == right)


def _over_the_wire(payload: JsonValue) -> JsonValue:
    """Force the payload through a real JSON encode/decode.

    Asserting on the intermediate mapping alone would not catch a value that is
    JSON-serialisable in principle and not in practice -- a `Decimal` that reached the
    mapping unencoded raises here and passes an in-memory comparison.
    """
    decoded: JsonValue = json.loads(json.dumps(payload))
    return decoded


@given(fill=fills())
def test_fill_survives_the_json_wire_round_trip(fill: Fill) -> None:
    restored = decode(Fill, _over_the_wire(encode(fill)))
    assert restored == fill
    assert _decimals_compare_total_equal(restored, fill)


@given(sequence=st.lists(fills(), min_size=1, max_size=8, unique_by=lambda fill: fill.fill_id))
def test_position_survives_the_json_wire_round_trip(sequence: list[Fill]) -> None:
    position = Position.flat(BTCUSDT)
    for fill in sequence:
        position = position.with_fill(fill).after

    restored = decode(Position, _over_the_wire(encode(position)))
    assert restored == position
    assert _decimals_compare_total_equal(restored, position)
    # The fill-id set is the idempotency key set; losing one silently re-admits a fill
    # the position has already applied.
    assert restored.applied_fill_ids == position.applied_fill_ids


@given(sequence=st.lists(fills(), min_size=2, max_size=8, unique_by=lambda fill: fill.fill_id))
def test_encoding_is_stable_across_set_iteration_order(sequence: list[Fill]) -> None:
    """The same position must encode to the same bytes every time.

    `frozenset` iteration order varies with hash randomisation, so an unsorted encoding
    would make an unchanged `Position` hash differently between processes -- which
    breaks content addressing and makes an audit hash chain unverifiable for reasons
    that have nothing to do with tampering.
    """
    position = Position.flat(BTCUSDT)
    for fill in sequence:
        position = position.with_fill(fill).after

    shuffled = Position(
        instrument=position.instrument,
        signed_base_quantity=position.signed_base_quantity,
        average_entry_quote_price=position.average_entry_quote_price,
        realised_pnl_quote=position.realised_pnl_quote,
        fee_quote_paid=position.fee_quote_paid,
        opened_at_utc=position.opened_at_utc,
        applied_fill_ids=frozenset(sorted(position.applied_fill_ids, reverse=True)),
    )
    assert json.dumps(encode(shuffled), sort_keys=True) == json.dumps(
        encode(position), sort_keys=True
    )
