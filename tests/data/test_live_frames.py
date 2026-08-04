"""Parsing every recorded frame, and refusing the shapes that must be refused.

The whole recorded corpus is replayed through `parse_frame` rather than a chosen sample,
because the frames that break a parser are the ones nobody would have picked: the
futures venue whitespaces its kline payload and the spot venue does not, and a
`quote_volume` of `"0.00000000"` looks like every other string until `Decimal` disagrees
with `float`.

The refusals are asserted against *mutations of real frames*, never hand-written
documents. A hand-written frame encodes what its author believes the venue emits, so a
test built on one proves the parser agrees with the author.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import get_args

import pytest

from fking.data.format_resolver import EpochUnit
from fking.data.live.frames import (
    STREAM_EPOCH_UNIT,
    AggTradeFrame,
    BookTickerFrame,
    KlineFrame,
    LiveFrame,
    MarkPriceFrame,
    parse_frame,
)
from fking.platform.errors import DataIntegrityError
from tests.support.stream_fixtures import RecordedStream, recorded_streams

pytestmark = pytest.mark.unit

ALL_RECORDINGS = recorded_streams()
ALL_FRAMES = tuple(
    (recording.label, frame) for recording in ALL_RECORDINGS for frame in recording.frames()
)

# The reference instant for epoch plausibility. Later than every recording and stable,
# so this suite does not start failing on the day a `datetime.now()` would have drifted
# past the recordings -- and does not pass for a frame whose epoch is absurd.
NOW_UTC = datetime(2026, 12, 31, tzinfo=UTC)


def _first_frame_of_type(event_type: str) -> str:
    for _label, frame in ALL_FRAMES:
        if json.loads(frame)["data"]["e"] == event_type:
            return frame
    raise AssertionError(f"the recorded corpus holds no {event_type} frame")


def _mutated(frame: str, **fields: object) -> str:
    """A real frame with named payload fields replaced. Derived, never authored."""
    document = json.loads(frame)
    document["data"].update(fields)
    return json.dumps(document)


def test_the_stream_epoch_unit_is_milliseconds() -> None:
    """The archive's microsecond cutover is an archive fact; the stream never moved.

    Asserted rather than assumed because reusing `resolve_archive_format` here would
    make a live frame's unit a function of the calendar, and a 1000x timestamp error is
    the least subtle and most expensive normalisation bug in the pipeline.
    """
    assert STREAM_EPOCH_UNIT is EpochUnit.MILLISECONDS


def test_a_closed_kline_becomes_a_bar_with_decimal_fields_from_the_venue_text() -> None:
    frame = next(
        candidate
        for _label, candidate in ALL_FRAMES
        if json.loads(candidate)["data"]["e"] == "kline" and json.loads(candidate)["data"]["k"]["x"]
    )
    payload = json.loads(frame)["data"]["k"]
    parsed = parse_frame(frame)
    assert isinstance(parsed, KlineFrame)

    record = parsed.to_record(now_utc=NOW_UTC)
    # Compared against Decimal(the venue's own characters), not against a float: this is
    # the assertion that would fail if anything on the path had gone through a double.
    assert record.close_quote_price == Decimal(payload["c"])
    assert record.base_volume == Decimal(payload["v"])
    assert record.trade_count == int(payload["n"])
    assert record.open_time_utc == datetime.fromtimestamp(payload["t"] / 1000, tz=UTC)
    assert record.close_time_utc > record.open_time_utc


def test_an_open_kline_refuses_to_become_a_bar() -> None:
    """The single most important refusal in the live path (`DATA_PIPELINE.md` 5)."""
    frame = next(
        candidate
        for _label, candidate in ALL_FRAMES
        if json.loads(candidate)["data"]["e"] == "kline"
        and not json.loads(candidate)["data"]["k"]["x"]
    )
    parsed = parse_frame(frame)
    assert isinstance(parsed, KlineFrame)
    assert parsed.is_closed is False
    with pytest.raises(DataIntegrityError, match="still open"):
        parsed.to_record(now_utc=NOW_UTC)


def test_an_agg_trade_is_keyed_on_trade_time_not_event_time() -> None:
    """Keying on `E` would put a live print and its archived twin in different minutes."""
    frame = _first_frame_of_type("aggTrade")
    payload = json.loads(frame)["data"]
    parsed = parse_frame(frame)
    assert isinstance(parsed, AggTradeFrame)

    record = parsed.to_record(now_utc=NOW_UTC)
    assert record.event_time_utc == datetime.fromtimestamp(payload["T"] / 1000, tz=UTC)
    assert record.venue_trade_id == str(payload["a"])
    assert record.quote_quantity == Decimal(payload["p"]) * Decimal(payload["q"])


def test_a_decimal_arriving_as_a_json_number_is_refused() -> None:
    """A number has already lost precision in `json.loads`; `Decimal(str(...))` would
    launder the damage rather than repair it."""
    frame = _mutated(_first_frame_of_type("aggTrade"), p=63927.54)
    with pytest.raises(DataIntegrityError, match="string-encoded decimal"):
        parse_frame(frame)


def test_a_frame_missing_a_required_field_is_refused_by_name() -> None:
    document = json.loads(_first_frame_of_type("aggTrade"))
    del document["data"]["q"]
    with pytest.raises(DataIntegrityError, match="failed validation"):
        parse_frame(json.dumps(document))


def test_an_unknown_event_type_is_refused_rather_than_ignored() -> None:
    """Dropping it silently is how a feed goes quiet in one dataset while the process
    reports health."""
    frame = _mutated(_first_frame_of_type("aggTrade"), e="forceOrder")
    with pytest.raises(DataIntegrityError, match="did not subscribe"):
        parse_frame(frame)


def test_an_unknown_extra_field_is_tolerated() -> None:
    """Binance adds fields without notice; breaking on one we do not read is an
    outage we inflicted on ourselves."""
    frame = _mutated(_first_frame_of_type("aggTrade"), somethingNew="whatever")
    assert isinstance(parse_frame(frame), AggTradeFrame)


def test_a_non_json_frame_is_refused() -> None:
    with pytest.raises(DataIntegrityError, match="not JSON"):
        parse_frame("<html>502 Bad Gateway</html>")


def test_a_frame_without_the_combined_stream_envelope_still_routes() -> None:
    """A single-stream subscription emits the payload bare, with no `stream` wrapper."""
    document = json.loads(_first_frame_of_type("aggTrade"))
    assert isinstance(parse_frame(json.dumps(document["data"])), AggTradeFrame)


def test_the_futures_only_frames_parse_into_their_own_models() -> None:
    assert isinstance(parse_frame(_first_frame_of_type("bookTicker")), BookTickerFrame)
    mark = parse_frame(_first_frame_of_type("markPriceUpdate"))
    assert isinstance(mark, MarkPriceFrame)
    # A fraction, not a percentage: 0.0001 is one basis point per funding interval.
    assert mark.funding_rate_fraction < Decimal("0.01")


@pytest.mark.parametrize("recording", ALL_RECORDINGS, ids=lambda recording: recording.label)
def test_a_whole_recording_replays_without_a_single_refusal(recording: RecordedStream) -> None:
    """The end-to-end shape check: a real session, start to finish, parsed.

    One test per recording rather than one per frame. A thousand parametrised ids buys
    no diagnostic value here -- the assertion error names the offending frame either way
    -- and it inflates the suite's count until nobody reads it.
    """
    parsed = [parse_frame(frame) for frame in recording.frames()]
    assert len(parsed) == recording.frame_count
    assert all(isinstance(frame, get_args(LiveFrame)) for frame in parsed)
    assert sum(1 for frame in parsed if isinstance(frame, KlineFrame) and frame.is_closed) == (
        recording.closed_kline_count
    )
