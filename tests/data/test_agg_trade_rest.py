"""The `/aggTrades` page parser, and the walk that pages it on `fromId`.

Pure, so it is asserted without a socket. The pages come from `tests/support/tape_prints`,
which renders them from frames captured off a live testnet socket -- the REST row and the
stream payload carry the same keys with the same encoding, so a page built that way carries
the venue's spelling of every field rather than this file's beliefs about it.

The two beliefs that would be wrong if hand-written are the two the parser exists to
enforce: decimals arrive as JSON strings, and epochs arrive as integer milliseconds whose
unit decides whether a print lands in 2026 or in 1970.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from fking.data.backfill.agg_trades import parse_agg_trade_page
from fking.data.live.frames import AggTradeFrame, parse_frame
from fking.platform.errors import DataIntegrityError
from tests.support import tape_prints

pytestmark = pytest.mark.unit

NOW_UTC = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
TAPE_START = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


def _page(count: int, *, offset: int = 0) -> str:
    payloads = tape_prints.recorded_payloads()[offset : offset + count]
    return tape_prints.rest_page(tape_prints.shift_to(payloads, first_event_utc=TAPE_START))


def test_a_recorded_page_parses_to_exact_decimals_and_utc_instants() -> None:
    parsed = parse_agg_trade_page(_page(3), now_utc=NOW_UTC)

    assert [record.venue_trade_id for record in parsed] == ["6210072", "6210073", "6210074"]
    assert parsed[0].event_time_utc == TAPE_START
    # The recorded testnet print, to the digit, including its trailing zeros.
    assert parsed[0].quote_price == Decimal("63871.48000000")
    assert parsed[0].base_quantity == Decimal("0.00031000")
    assert all(isinstance(record.quote_price, Decimal) for record in parsed)


def test_a_rest_print_and_the_same_print_off_the_socket_are_one_record() -> None:
    """The property the seam depends on, asserted at the boundary that produces it.

    The two paths build a `TradeRecord` from the same venue fields, and each derives
    `quote_quantity` the same way -- the endpoint files none for an aggregate print.
    Deriving it differently on the two sides would make one execution a seam disagreement
    about a column neither source ever sent.
    """
    payloads = tape_prints.shift_to(tape_prints.recorded_payloads()[:1], first_event_utc=TAPE_START)
    over_rest = parse_agg_trade_page(tape_prints.rest_page(payloads), now_utc=NOW_UTC)[0]
    frame = parse_frame(json.dumps(payloads[0]))
    assert isinstance(frame, AggTradeFrame)

    assert over_rest == frame.to_record(now_utc=NOW_UTC)


def test_a_json_number_in_a_price_field_is_refused() -> None:
    """Binance sends decimals as strings so they survive a parser with no Decimal support.
    One arriving as a number has already lost precision before we see it."""
    rows = json.loads(_page(1))
    rows[0]["p"] = 63871.48

    with pytest.raises(DataIntegrityError, match="string-encoded decimal"):
        parse_agg_trade_page(json.dumps(rows), now_utc=NOW_UTC)


def test_a_renamed_key_is_refused_by_name() -> None:
    """The response is keyed rather than positional, so a renamed field is a contract
    change and not a shifted column -- the message names the key that went missing."""
    rows = json.loads(_page(1))
    rows[0]["aggId"] = rows[0].pop("a")

    with pytest.raises(DataIntegrityError, match="carries no 'a' key"):
        parse_agg_trade_page(json.dumps(rows), now_utc=NOW_UTC)


def test_a_non_boolean_aggressor_flag_is_refused() -> None:
    """`m` is the aggressor side inverted, and a truthy string read as a flag would leave
    every other column of the print correct."""
    rows = json.loads(_page(1))
    rows[0]["m"] = "false"

    with pytest.raises(DataIntegrityError, match="not a JSON boolean"):
        parse_agg_trade_page(json.dumps(rows), now_utc=NOW_UTC)


def test_an_error_envelope_is_not_read_as_an_empty_page() -> None:
    """`{"code": -1121, "msg": "Invalid symbol."}` is an object. Treating it as zero prints
    would close a gap by deciding the venue has nothing for it."""
    with pytest.raises(DataIntegrityError, match="not a JSON array"):
        parse_agg_trade_page('{"code":-1121,"msg":"Invalid symbol."}', now_utc=NOW_UTC)


def test_a_millisecond_epoch_read_as_something_else_would_not_survive_the_range_check() -> None:
    """The plausibility window is the cheapest detector of a wrong unit that exists, and it
    works because both failure directions are absurd rather than subtle."""
    rows = json.loads(_page(1))
    rows[0]["T"] = rows[0]["T"] * 1000  # microseconds where milliseconds are declared

    with pytest.raises(DataIntegrityError, match="outside the plausible window"):
        parse_agg_trade_page(json.dumps(rows), now_utc=NOW_UTC)
