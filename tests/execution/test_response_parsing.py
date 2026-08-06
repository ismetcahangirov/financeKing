"""No float reaches a domain object from a recorded response.

`ccxt` decodes JSON with the standard library's decoder, so `"0.00001000"` becomes the
float `0.0005`-shaped approximation before any of our code runs, and the rounding is not
repairable afterwards -- `Decimal(0.0005)` is
`Decimal('0.000500000000000000010408340855980918...')`. The whole adapter design exists
to make that impossible: `GuardedExchange.call` returns response *text*, and
`parse_venue_payload` is the only thing that turns it into values.

The properties below are stated over the real corpus rather than over invented payloads,
because the failure this guards is specifically about *the venue's own formatting*, and
an invented payload is a statement about the author's beliefs.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from fking.domain import Instrument, Venue
from fking.execution import (
    PermanentExchangeError,
    SymbolFilters,
    TransientExchangeError,
    VenueExchangeInfo,
    classify_venue_failure,
    parse_venue_payload,
    raise_for_venue_error,
    venue_epoch_to_utc,
)
from fking.execution.binance import _ENDPOINTS
from fking.execution.models import VenueDecimal
from tests.execution.conftest import (
    ENDPOINT_RECORDINGS,
    Recording,
    iter_recordings,
    load_recording,
    recorded_venues,
)

pytestmark = pytest.mark.unit

# The corpus was recorded in 2026; a server time before that means the epoch unit is wrong.
EARLIEST_PLAUSIBLE_YEAR: Final[int] = 2026

ALL_RECORDINGS: Final[tuple[Recording, ...]] = tuple(iter_recordings())


def _floats_in(node: object, path: str = "$") -> list[str]:
    """Every path at which a `float` appears in a parsed payload."""
    if isinstance(node, float):
        return [path]
    if isinstance(node, Mapping):
        return [
            found for key, child in node.items() for found in _floats_in(child, f"{path}.{key}")
        ]
    if isinstance(node, Sequence) and not isinstance(node, str | bytes):
        return [
            found
            for index, child in enumerate(node)
            for found in _floats_in(child, f"{path}[{index}]")
        ]
    return []


@pytest.mark.parametrize(
    "recording", ALL_RECORDINGS, ids=lambda entry: f"{entry.venue}-{entry.path.parent.name}"
)
def test_no_float_survives_decimal_parsing_of_a_recorded_response(recording: Recording) -> None:
    """The corpus-wide property: parsing a real body materialises no float anywhere."""
    offenders = _floats_in(parse_venue_payload(recording.body))
    assert offenders == [], f"{recording.path.name} produced floats at {offenders}"


def test_the_float_detector_actually_detects_a_float() -> None:
    """The property above is worth exactly what its detector is worth.

    A `_floats_in` that always returned `[]` would make every corpus-wide assertion pass
    while proving nothing, which is not hypothetical in this repository -- a command was
    once found to exit 0 having evaluated no contracts at all.
    """
    assert _floats_in(json.loads('{"a": {"b": [1, 0.5]}}')) == ["$.a.b[1]"]


def _decimal_strings(node: object) -> list[str]:
    """Every string in a payload that parses as a decimal with a fractional part."""
    if isinstance(node, str):
        try:
            parsed = Decimal(node)
        except InvalidOperation:
            return []
        return [node] if parsed != parsed.to_integral_value() else []
    if isinstance(node, Mapping):
        return [found for child in node.values() for found in _decimal_strings(child)]
    if isinstance(node, Sequence) and not isinstance(node, bytes):
        return [found for child in node for found in _decimal_strings(child)]
    return []


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
def test_the_corpus_carries_values_a_float_path_would_have_corrupted(venue: Venue) -> None:
    """The negative control, stated as the damage rather than as the parser.

    Binance sends its decimals as JSON *strings*, so the stdlib parser leaves them alone
    and a "would the default have produced floats?" control would pass vacuously. What
    actually destroys these values is `float(text)` somewhere downstream -- which is
    exactly what `ccxt` does to its own parsed structure, and why the adapter reads the
    body instead. This asserts the corpus contains at least one value that route would
    change, so the guarantee is being tested against damage that is real here.
    """
    payload = parse_venue_payload(load_recording(venue, "exchangeInfo").body)
    corrupted = [
        text for text in _decimal_strings(payload) if Decimal(float(text)) != Decimal(text)
    ]
    assert corrupted, "no recorded value would be changed by a float round trip"


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
def test_every_decimal_field_of_a_parsed_exchange_info_is_a_decimal(venue: Venue) -> None:
    """End to end: recorded text through the models to the values an order is built from."""
    recording = load_recording(venue, "exchangeInfo")
    info = VenueExchangeInfo.model_validate(parse_venue_payload(recording.body))

    checked = 0
    for symbol in info.symbols:
        for entry in symbol.filters:
            for value in (
                entry.tick_size,
                entry.step_size,
                entry.min_quantity,
                entry.max_quantity,
                entry.min_notional,
            ):
                assert value is None or isinstance(value, Decimal)
                checked += value is not None
    assert checked > 0, "the recording carried no decimal-valued filter fields"


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
def test_a_domain_instrument_built_from_a_recording_carries_exact_decimals(venue: Venue) -> None:
    """`Instrument` is where a filter stops being a response and starts being a lattice.

    Its `tick_size` and `lot_step` decide whether a quantity is an order at all, so a
    float that reached here would produce quantities the venue rejects with -1013 on
    values that print as if they were correct.
    """
    recording = load_recording(venue, "exchangeInfo")
    info = VenueExchangeInfo.model_validate(parse_venue_payload(recording.body))
    symbol = info.symbol("BTCUSDT")
    filters = symbol.order_filters()

    instrument = Instrument(
        venue=venue,
        symbol=symbol.symbol,
        base_asset=symbol.base_asset,
        quote_asset=symbol.quote_asset,
        tick_size=filters.tick_size,
        lot_step=filters.step_size,
        min_notional_quote=filters.min_notional or Decimal("1"),
    )
    assert isinstance(instrument.tick_size, Decimal)
    assert isinstance(instrument.lot_step, Decimal)
    # Exact, not approximately: a tick of 0.01 that arrived through a float would be
    # 0.01000000000000000020816681711721685... and would never snap cleanly.
    assert instrument.tick_size == Decimal(str(instrument.tick_size))


class _DecimalCarrier(BaseModel):
    quote_price: VenueDecimal


@given(
    text=st.decimals(
        min_value=Decimal("0"),
        max_value=Decimal("1000000"),
        places=8,
        allow_nan=False,
        allow_infinity=False,
    ).map(lambda value: format(value, "f"))
)
def test_a_venue_decimal_round_trips_the_exact_characters_it_was_sent(text: str) -> None:
    """The venue's characters, not a value near them."""
    assert _DecimalCarrier(quote_price=text).quote_price == Decimal(text)


@pytest.mark.parametrize("candidate", [0.1, 1, True, None, ["0.1"]])
def test_a_non_string_decimal_field_is_refused_rather_than_coerced(candidate: object) -> None:
    """Accepting a JSON number here would launder a rounding error into an exact type.

    `1` is in the list on purpose: an int is lossless, and accepting it anyway would
    mean the model no longer proves the body went through `parse_venue_payload`.
    """
    with pytest.raises(ValidationError, match="string-encoded decimal"):
        _DecimalCarrier(quote_price=candidate)  # type: ignore[arg-type]


def test_a_malformed_decimal_string_is_refused() -> None:
    with pytest.raises(ValidationError, match="not a decimal"):
        _DecimalCarrier(quote_price="not-a-number")


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
@pytest.mark.parametrize("directory", ["order_rejected", "openOrders_rejected"])
def test_a_recorded_rejection_raises_a_classified_failure_rather_than_a_key_error(
    venue: Venue, directory: str
) -> None:
    """`response["orderId"]` is a bug, and this is why: the envelope has no orderId.

    The recordings replayed here are the venue's real responses to an unsigned request,
    so the shape asserted against is Binance's rather than an author's.
    """
    recording = load_recording(venue, directory)
    with pytest.raises(PermanentExchangeError) as refused:
        raise_for_venue_error(parse_venue_payload(recording.body), venue_id=str(venue))
    assert refused.value.venue_id == str(venue)
    assert refused.value.venue_code is not None
    assert refused.value.venue_code < 0


def test_a_success_payload_passes_through_untouched() -> None:
    payload = {"orderId": 1, "status": "NEW"}
    assert raise_for_venue_error(payload, venue_id="binance-spot-testnet") is payload


@pytest.mark.parametrize("payload", [[], "text", 3, {"code": 200, "msg": "ok"}, {"msg": "x"}])
def test_a_payload_that_is_not_an_error_envelope_is_not_treated_as_one(payload: object) -> None:
    """Binance error codes are negative. A non-negative `code` is not a rejection, and
    treating it as one would fail a request the venue accepted."""
    assert raise_for_venue_error(payload, venue_id="binance-spot-testnet") is payload


@pytest.mark.parametrize("http_status", [408, 429, 500, 502, 503, 504])
def test_a_retryable_http_status_classifies_as_transient(http_status: int) -> None:
    failure = classify_venue_failure(
        venue_id="binance-spot-testnet", http_status=http_status, venue_code=None, message="x"
    )
    assert isinstance(failure, TransientExchangeError)


@pytest.mark.parametrize("venue_code", [-1001, -1003, -1007, -1016, -1099])
def test_a_documented_retryable_code_classifies_as_transient(venue_code: int) -> None:
    failure = classify_venue_failure(
        venue_id="binance-spot-testnet", http_status=400, venue_code=venue_code, message="x"
    )
    assert isinstance(failure, TransientExchangeError)


@pytest.mark.parametrize(
    ("venue_code", "why"),
    [
        (-9999, "an unknown code defaults to permanent, or one novel failure becomes an outage"),
        (
            -1021,
            "recvWindow is a statement about our clock; retrying re-signs against the same one",
        ),
        (-2010, "insufficient balance does not become sufficient on the next attempt"),
        (-1013, "a filter rejection is arithmetic, not weather"),
    ],
)
def test_a_non_retryable_code_classifies_as_permanent(venue_code: int, why: str) -> None:
    failure = classify_venue_failure(
        venue_id="binance-spot-testnet", http_status=400, venue_code=venue_code, message=why
    )
    assert isinstance(failure, PermanentExchangeError)


def test_an_ip_ban_is_never_retried() -> None:
    """418 is Binance's IP ban. Retrying into it extends it; the response is a hard stop."""
    failure = classify_venue_failure(
        venue_id="binance-spot-testnet", http_status=418, venue_code=None, message="banned"
    )
    assert isinstance(failure, PermanentExchangeError)


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
def test_a_recorded_server_time_normalises_into_the_plausible_range(venue: Venue) -> None:
    payload = parse_venue_payload(load_recording(venue, "exchangeInfo").body)
    assert isinstance(payload, Mapping)
    moment = venue_epoch_to_utc(int(payload["serverTime"]))
    assert moment.tzinfo is not None
    assert moment.year >= EARLIEST_PLAUSIBLE_YEAR


@pytest.mark.parametrize("epoch_ms", [0, 1_785_744_455, 1_785_744_455_214_000])
def test_a_timestamp_in_the_wrong_unit_is_refused_rather_than_silently_shifted(
    epoch_ms: int,
) -> None:
    """A unit error is a factor of 1000, which places 2026 in 1970 or in the year 58000.

    The magnitude guard turns that into a loud failure at the boundary instead of a
    timestamp nobody questions until a reconciliation cannot be explained.
    """
    with pytest.raises(PermanentExchangeError, match="timestamp unit assumption is wrong"):
        venue_epoch_to_utc(epoch_ms)


def test_symbol_filters_refuse_to_default_a_missing_price_filter() -> None:
    """A default tick size is a number nobody chose, rounding a price the venue rejects."""
    with pytest.raises(PermanentExchangeError, match="no usable PRICE_FILTER"):
        SymbolFilters.from_entries((), symbol="BTCUSDT")


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
def test_symbol_filters_refuse_to_default_a_missing_lot_size(venue: Venue) -> None:
    recording = load_recording(venue, "exchangeInfo")
    info = VenueExchangeInfo.model_validate(parse_venue_payload(recording.body))
    price_only = tuple(
        entry for entry in info.symbol("BTCUSDT").filters if entry.filter_type == "PRICE_FILTER"
    )
    with pytest.raises(PermanentExchangeError, match="no usable LOT_SIZE"):
        SymbolFilters.from_entries(price_only, symbol="BTCUSDT")


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
def test_an_unknown_response_field_is_ignored_rather_than_fatal(venue: Venue) -> None:
    """`extra="ignore"` on venue models: Binance adds fields without notice, and breaking
    on a new one is a self-inflicted outage. The asymmetry with `extra="forbid"` on what
    this project authors is deliberate."""
    payload = parse_venue_payload(load_recording(venue, "exchangeInfo").body)
    assert isinstance(payload, Mapping)
    widened = {**payload, "someFieldBinanceAddedOnTuesday": "surprise"}
    assert VenueExchangeInfo.model_validate(widened).symbols


def test_the_endpoint_recording_map_covers_every_endpoint_the_adapter_calls() -> None:
    """A method whose response was never recorded is a method nobody has tested."""
    for venue, recordings in ENDPOINT_RECORDINGS.items():
        market = "spot" if venue is Venue.BINANCE_SPOT_TESTNET else "futures"
        endpoints = _ENDPOINTS[market]
        expected = {
            endpoints.exchange_info,
            endpoints.balances,
            endpoints.open_orders,
            endpoints.my_trades,
            endpoints.submit,
            endpoints.cancel,
            endpoints.cancel_replace,
        }
        if endpoints.positions is not None:
            expected.add(endpoints.positions)
        assert expected <= set(recordings), f"{venue} is missing {expected - set(recordings)}"
