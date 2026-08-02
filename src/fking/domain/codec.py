"""One generic JSON codec for every type in this package.

Twelve hand-written encoder/decoder pairs was the alternative and was rejected. The
failure mode of hand-written encoders is specific and silent: somebody writes
`{"quote_price": obj.quote_price}` for the type they are adding, `json.dumps` refuses
a `Decimal`, they reach for `float(obj.quote_price)`, and the value is now rounded
forever in an append-only audit row. A single codec means that mistake is available in
exactly one place, and that place is covered by a round-trip property over every type.

Four encoding choices are load-bearing:

**`Decimal` becomes a JSON string**, never a JSON number. A number is parsed back as a
double by any consumer whose parser has no `parse_float` hook, which is most of them.

**`timedelta` becomes an integer count of microseconds**, not `total_seconds()`.
`total_seconds()` returns a float, so the encoder for a duration would itself be the
float that the rest of this package exists to keep out.

**`frozenset` becomes a list sorted by its own encoded form.** Set iteration order
varies between processes; an unsorted list would make the encoding of an unchanged
`Position` differ run to run, which breaks content addressing and makes an audit
hash-chain unverifiable.

**Missing and unexpected keys both fail.** A tolerant decoder that fills a missing
field with a default reconstructs an object that never existed and reports success.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import NoneType, UnionType
from typing import Final, Protocol, Union, cast, get_args, get_origin, get_type_hints
from uuid import UUID

from fking.domain._errors import DomainError
from fking.domain._guards import require_utc

type JsonValue = str | int | bool | list[JsonValue] | dict[str, JsonValue] | None

_MICROSECOND: Final = timedelta(microseconds=1)
# A homogeneous tuple annotation is `tuple[X, ...]` -- two args, the second Ellipsis.
# A str-keyed mapping annotation is `Mapping[str, X]` -- also two.
_PARAMETERISED_PAIR: Final = 2


# ---------------------------------------------------------------------------
# encoding
# ---------------------------------------------------------------------------


def encode(candidate: object) -> JsonValue:  # noqa: PLR0911 - see the docstring
    """Render a domain object as JSON-serialisable data, losing nothing.

    One return per supported shape, deliberately over the PLR0911 threshold. The
    alternative is a predicate/converter dispatch table, which for a fixed and closed
    set of eleven cases replaces a chain a reader can verify by eye with indirection
    they have to trace -- and the ordering constraint below (Enum before str) is the
    kind of thing that gets lost in a table.

    Accepts any domain type, including the nested ones -- an `Order` carries an
    `Instrument`, which carries a `Venue`, and all three are handled by the same
    recursion.
    """
    if candidate is None:
        return None
    # Before the str branch: every enum here is a StrEnum, so a member IS a str and
    # would otherwise encode as "Direction.LONG" or, worse, pass through unchanged and
    # decode into something that is no longer an enum.
    if isinstance(candidate, Enum):
        return encode(candidate.value)
    if isinstance(candidate, Decimal):
        return str(candidate)
    if isinstance(candidate, datetime):
        return candidate.isoformat()
    if isinstance(candidate, timedelta):
        return candidate // _MICROSECOND
    if isinstance(candidate, UUID):
        return str(candidate)
    if isinstance(candidate, bool | int | str):
        return candidate
    if is_dataclass(candidate) and not isinstance(candidate, type):
        return {
            declared.name: encode(getattr(candidate, declared.name))
            for declared in fields(candidate)
        }
    if isinstance(candidate, Mapping):
        return {str(key): encode(item) for key, item in candidate.items()}
    if isinstance(candidate, frozenset | set):
        return sorted(
            (encode(item) for item in candidate),
            key=lambda encoded: json.dumps(encoded, sort_keys=True),
        )
    if isinstance(candidate, tuple | list):
        return [encode(item) for item in candidate]
    raise DomainError(f"no encoding for {type(candidate).__name__}: {candidate!r}")


# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------


def _decode_decimal(payload: JsonValue) -> object:
    if not isinstance(payload, str):
        raise DomainError(
            f"a Decimal must arrive as a JSON string, got {type(payload).__name__} {payload!r}"
        )
    try:
        return Decimal(payload)
    except InvalidOperation as invalid:
        raise DomainError(f"{payload!r} is not a decimal") from invalid


def _decode_datetime(payload: JsonValue) -> object:
    if not isinstance(payload, str):
        raise DomainError(f"a datetime must arrive as an ISO 8601 string, got {payload!r}")
    try:
        parsed = datetime.fromisoformat(payload)
    except ValueError as invalid:
        raise DomainError(f"{payload!r} is not an ISO 8601 datetime") from invalid
    return require_utc(parsed, "datetime")


def _decode_timedelta(payload: JsonValue) -> object:
    if not isinstance(payload, int) or isinstance(payload, bool):
        raise DomainError(f"a timedelta must arrive as microseconds, got {payload!r}")
    return timedelta(microseconds=payload)


def _decode_uuid(payload: JsonValue) -> object:
    if not isinstance(payload, str):
        raise DomainError(f"a UUID must arrive as a string, got {payload!r}")
    try:
        return UUID(payload)
    except ValueError as invalid:
        raise DomainError(f"{payload!r} is not a UUID") from invalid


def _decode_str(payload: JsonValue) -> object:
    if not isinstance(payload, str):
        raise DomainError(f"expected a string, got {type(payload).__name__} {payload!r}")
    return payload


def _decode_int(payload: JsonValue) -> object:
    if not isinstance(payload, int) or isinstance(payload, bool):
        raise DomainError(f"expected an int, got {type(payload).__name__} {payload!r}")
    return payload


def _decode_bool(payload: JsonValue) -> object:
    if not isinstance(payload, bool):
        raise DomainError(f"expected a bool, got {type(payload).__name__} {payload!r}")
    return payload


_SCALAR_DECODERS: Final[Mapping[type[object], Callable[[JsonValue], object]]] = {
    Decimal: _decode_decimal,
    datetime: _decode_datetime,
    timedelta: _decode_timedelta,
    UUID: _decode_uuid,
    str: _decode_str,
    # bool before int is irrelevant to a dict lookup, which is by identity -- but the
    # int decoder still rejects bools, because `True` satisfies `isinstance(x, int)`
    # and a trade_count of True is not a number anybody meant to write.
    int: _decode_int,
    bool: _decode_bool,
}


def _decode_optional(args: tuple[object, ...], payload: JsonValue) -> object:
    present = tuple(arg for arg in args if arg is not NoneType)
    if len(present) != len(args) - 1 or len(present) != 1:
        # Every union in this package is `X | None`. A wider one would need a
        # discriminator to decode unambiguously, and inventing one silently is how a
        # payload decodes into the wrong arm and passes every type check afterwards.
        raise DomainError(f"only `X | None` unions can be decoded; got {args}")
    if payload is None:
        return None
    return _decode(present[0], payload)


def _decode_sequence(args: tuple[object, ...], payload: JsonValue, *, label: str) -> list[object]:
    if len(args) != 1 and not (len(args) == _PARAMETERISED_PAIR and args[1] is Ellipsis):
        raise DomainError(f"only homogeneous {label} annotations can be decoded; got {args}")
    if not isinstance(payload, list):
        raise DomainError(f"a {label} must arrive as a JSON list, got {payload!r}")
    return [_decode(args[0], item) for item in payload]


def _decode_mapping(args: tuple[object, ...], payload: JsonValue) -> dict[str, object]:
    if len(args) != _PARAMETERISED_PAIR or args[0] is not str:
        raise DomainError(f"only str-keyed mappings can be decoded; got {args}")
    if not isinstance(payload, dict):
        raise DomainError(f"a mapping must arrive as a JSON object, got {payload!r}")
    return {key: _decode(args[1], item) for key, item in payload.items()}


def _decode_generic(annotation: object, origin: object, payload: JsonValue) -> object:
    args = get_args(annotation)
    if origin is UnionType or origin is Union:
        return _decode_optional(args, payload)
    if origin is tuple:
        return tuple(_decode_sequence(args, payload, label="tuple"))
    if origin is frozenset:
        return frozenset(_decode_sequence(args, payload, label="frozenset"))
    if isinstance(origin, type) and issubclass(origin, Mapping):
        return _decode_mapping(args, payload)
    raise DomainError(f"no decoding for the generic annotation {annotation!r}")


class _KeywordConstructor(Protocol):
    """A dataclass type, viewed only as something callable with keyword arguments."""

    def __call__(self, **arguments: object) -> object:
        """Construct the dataclass. Never called through this Protocol at runtime."""


def _decode_dataclass(annotation: type[object], payload: JsonValue) -> object:
    if not isinstance(payload, dict):
        raise DomainError(
            f"{annotation.__name__} must arrive as a JSON object, got {type(payload).__name__}"
        )
    hints = get_type_hints(annotation)
    declared = tuple(field.name for field in fields(annotation))  # type: ignore[arg-type]  # narrowed by is_dataclass at the call site, which mypy does not carry across the call
    missing = tuple(name for name in declared if name not in payload)
    unexpected = tuple(key for key in payload if key not in declared)
    if missing or unexpected:
        raise DomainError(
            f"{annotation.__name__} payload does not match its fields: "
            f"missing {missing}, unexpected {unexpected}"
        )
    arguments = {name: _decode(hints[name], payload[name]) for name in declared}
    # A dataclass type is callable with its field names as keywords, which `type[object]`
    # does not express. `Callable[..., object]` would, and is rejected by
    # disallow_any_explicit because the ellipsis parameter list is an Any. This Protocol
    # says the same thing without one.
    constructor = cast("_KeywordConstructor", annotation)
    return constructor(**arguments)


def _decode(annotation: object, payload: JsonValue) -> object:
    origin = get_origin(annotation)
    if origin is not None:
        return _decode_generic(annotation, origin, payload)
    if not isinstance(annotation, type):
        raise DomainError(f"no decoding for the annotation {annotation!r}")
    scalar = _SCALAR_DECODERS.get(annotation)
    if scalar is not None:
        return scalar(payload)
    if issubclass(annotation, Enum):
        try:
            return annotation(payload)
        except ValueError as invalid:
            raise DomainError(f"{payload!r} is not a member of {annotation.__name__}") from invalid
    if is_dataclass(annotation):
        return _decode_dataclass(annotation, payload)
    raise DomainError(f"no decoding for {annotation.__name__}")


def decode[T](target: type[T], payload: JsonValue) -> T:
    """Rebuild a `target` from what `encode` produced.

    Every domain invariant is re-checked on the way in, because decoding calls the
    real constructor. A payload that has been tampered with -- or hand-written against
    an older schema -- fails here rather than becoming an object that looks valid.
    """
    decoded = _decode(target, payload)
    if not isinstance(decoded, target):  # pragma: no cover - unreachable while _decode is correct
        # Not reachable through any payload: `_decode` either produces a `target` or
        # raises. It is a narrowing check that also converts a future dispatch bug in
        # `_decode` into a named failure here, instead of a mystery `AttributeError`
        # several modules downstream on a value whose type nobody has questioned.
        raise DomainError(
            f"decoding produced a {type(decoded).__name__} where a {target.__name__} was asked for"
        )
    return decoded
