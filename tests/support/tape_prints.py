"""Trade prints built from recorded `aggTrade` frames, never hand-authored.

The tape has no archive fixture to build from -- `(spot, aggTrades)` is deliberately
undeclared in `fking.data.format_resolver`, because its CSV boolean encoding has never
been read -- so the recording this module uses is a *socket* capture:
`tests/fixtures/streams/`, committed verbatim and re-verified against its SHA-256 sidecar
by `tests/data/test_stream_fixture_integrity.py`.

That is the same argument `tests/support/rest_klines` makes for klines, and the fields at
risk are the same two. The decimals are JSON strings whose scale is the venue's, and the
epochs are integer milliseconds whose unit decides whether a print lands in 2026 or in
1970. Transcribing a recording keeps both from the venue's own characters; inventing a
frame would encode this module's beliefs about them, and every assertion downstream would
then be checking those beliefs.

Frames are parsed by `fking.data.live.frames.parse_frame` rather than by anything here,
so the records under test have been through the production parser and nothing else.

`shift_to` re-bases the recorded milliseconds onto a test's own instant and touches
nothing else: prices, quantities, sides and aggregate trade ids stay exactly as recorded,
which is what makes a seam comparison in a test a comparison of real venue values.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from fking.data.live.frames import AggTradeFrame, parse_frame
from fking.data.loaders.records import TradeRecord
from tests.support.stream_fixtures import recorded_streams

__all__ = ["SPOT_VENUE_ID", "StreamPayload", "prints", "recorded_payloads", "shift_to"]

StreamPayload = dict[str, object]
"""One decoded `aggTrade` frame payload, as `json.loads` hands it over.

`object` rather than `Any`: the two fields this module touches are read through an
explicit int conversion, and a payload typed `Any` would let a re-based epoch be
whatever the recording happened to hold.
"""

SPOT_VENUE_ID: Final[str] = "binance-spot-testnet"

# `E` is the publication instant and `T` the trade instant; both are re-based together so
# a shifted frame stays internally consistent. `to_record` keys on `T`.
_EPOCH_FIELDS: Final[tuple[str, ...]] = ("E", "T")


def recorded_payloads(venue_id: str = SPOT_VENUE_ID) -> tuple[StreamPayload, ...]:
    """Every recorded `aggTrade` payload for one venue, in arrival order."""
    for recording in recorded_streams():
        if recording.venue_id != venue_id:
            continue
        payloads: list[StreamPayload] = []
        for raw in recording.frames():
            decoded: object = json.loads(raw)
            if not isinstance(decoded, dict):
                raise TypeError(f"recorded frame is a {type(decoded).__name__}, not an object")
            payload = decoded.get("data", decoded)
            if isinstance(payload, dict) and payload.get("e") == "aggTrade":
                payloads.append(payload)
        return tuple(payloads)
    raise LookupError(f"no recorded stream for venue {venue_id!r}")


def shift_to(
    payloads: Sequence[StreamPayload], *, first_event_utc: datetime
) -> tuple[StreamPayload, ...]:
    """The same payloads with their first print re-based onto `first_event_utc`.

    The *relative* spacing is preserved rather than replaced by a fixed step, because the
    spacing is what makes two recorded prints share a millisecond -- the case a
    timestamp-keyed dedupe loses -- and imposing a step would remove the very shape the
    tests exist to exercise.
    """
    if not payloads:
        return ()
    delta_ms = _epoch_ms(first_event_utc) - _recorded_epoch(payloads[0], "T")
    shifted: list[StreamPayload] = []
    for payload in payloads:
        moved = dict(payload)
        for field_name in _EPOCH_FIELDS:
            moved[field_name] = _recorded_epoch(payload, field_name) + delta_ms
        shifted.append(moved)
    return tuple(shifted)


def _recorded_epoch(payload: StreamPayload, field_name: str) -> int:
    raw_field = payload[field_name]
    if not isinstance(raw_field, int):
        raise TypeError(
            f"recorded {field_name!r} is a {type(raw_field).__name__}; the fixture-integrity "
            f"suite should have caught a frame whose epochs are not integers"
        )
    return raw_field


def prints(
    count: int,
    *,
    first_event_utc: datetime,
    now_utc: datetime,
    offset: int = 0,
    venue_id: str = SPOT_VENUE_ID,
) -> tuple[TradeRecord, ...]:
    """`count` recorded prints whose tape starts at `first_event_utc`.

    `offset` picks a different stretch of the recording, which is how a test gets two
    prints that genuinely differ -- rather than by editing a price, which would stop the
    comparison being against real venue values.
    """
    selected = recorded_payloads(venue_id)[offset : offset + count]
    if len(selected) != count:
        raise LookupError(
            f"the {venue_id} recording holds {len(recorded_payloads(venue_id))} aggTrade "
            f"frames; {count} from offset {offset} is beyond it"
        )
    return tuple(
        _record(payload, now_utc=now_utc)
        for payload in shift_to(selected, first_event_utc=first_event_utc)
    )


def _record(payload: StreamPayload, *, now_utc: datetime) -> TradeRecord:
    frame = parse_frame(json.dumps(payload))
    if not isinstance(frame, AggTradeFrame):  # pragma: no cover - filtered on `e` above
        raise TypeError(f"expected an aggTrade frame, got {type(frame).__name__}")
    return frame.to_record(now_utc=now_utc)


def _epoch_ms(moment: datetime) -> int:
    return (int(moment.timestamp()) * 1000) + (moment.microsecond // 1000)
