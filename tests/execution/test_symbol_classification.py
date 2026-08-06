"""Symbol classification against the venue's own deliberate Unicode symbols.

Binance testnet serves non-ASCII symbols on purpose. They are there to break parsers,
and they do -- `str.isalnum()` returns `True` for many of them and `False` for others, so
a filter built on it drops some silently and the universe is quietly wrong with no log
line naming what went missing.

Every case below is drawn from the recorded corpus rather than typed here, so the test
asserts against what the venue actually sends. The one hardcoded symbol is in the
assertion that the corpus still *contains* such a symbol, which is what stops a future
re-record from quietly cleaning it up.
"""

from __future__ import annotations

import pytest

from fking.domain import Venue
from fking.execution import (
    VenueExchangeInfo,
    classify_symbol,
    parse_venue_payload,
    tradable_symbols,
)
from tests.execution.conftest import load_recording, recorded_venues

pytestmark = pytest.mark.unit


def _recorded_symbols(venue: Venue) -> tuple[str, ...]:
    recording = load_recording(venue, "exchangeInfo")
    info = VenueExchangeInfo.model_validate(parse_venue_payload(recording.body))
    return tuple(entry.symbol for entry in info.symbols)


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
def test_every_recorded_symbol_round_trips_byte_for_byte(venue: Venue) -> None:
    """Never coerce. NFKC-normalising a symbol changes the code points the venue expects
    back, and a normalised symbol is a different symbol."""
    for symbol in _recorded_symbols(venue):
        assert classify_symbol(symbol).symbol == symbol


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
def test_the_recorded_universe_still_contains_a_non_ascii_symbol(venue: Venue) -> None:
    """A guard against a future re-record tidying the corpus.

    A parser that has never seen one of these is a parser that raises
    `UnicodeEncodeError` inside a symbol-universe load on a Windows console's default
    codepage -- a startup crash whose diagnostic channel is the thing that failed.
    """
    non_ascii = [symbol for symbol in _recorded_symbols(venue) if not symbol.isascii()]
    assert non_ascii, f"{venue} exchangeInfo recording carries no non-ascii symbol"


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
def test_a_non_ascii_symbol_is_quarantined_with_its_code_points_named(venue: Venue) -> None:
    """Quarantined, not dropped: the reason names the code points in ASCII so the log
    sink can render it whatever the console codepage is."""
    non_ascii = next(symbol for symbol in _recorded_symbols(venue) if not symbol.isascii())
    classification = classify_symbol(non_ascii)

    assert classification.is_tradable is False
    assert classification.reason is not None
    assert "non-ascii code points" in classification.reason
    assert classification.reason.isascii()
    assert f"U+{ord(next(c for c in non_ascii if not c.isascii())):04X}" in classification.reason


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
def test_the_tradable_set_excludes_the_unicode_symbols_and_keeps_the_ordinary_ones(
    venue: Venue,
) -> None:
    symbols = _recorded_symbols(venue)
    tradable = tradable_symbols(symbols)

    assert "BTCUSDT" in tradable
    assert all(symbol.isascii() for symbol in tradable)
    assert len(tradable) < len(symbols), "the recording should contain untradable symbols"


@pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT", "AB", "A1B2C3D4E5"])
def test_an_ordinary_symbol_is_tradable(symbol: str) -> None:
    classification = classify_symbol(symbol)
    assert classification.is_tradable is True
    assert classification.reason is None


@pytest.mark.parametrize(
    ("symbol", "why"),
    [
        ("btcusdt", "Binance symbols are uppercase; a lowercase one is a caller's mistake"),
        ("B", "a one-character symbol is a truncated field"),
        ("BTC-USDT", "a separator means the caller sent a ccxt unified symbol, not a venue one"),
        ("BTCUSDT_251226", "a dated futures contract is not a spot symbol"),
        ("", "an empty symbol is a missing field the venue happened to include"),
        ("A" * 33, "a symbol-shaped sentence is a payload field that is not a symbol"),
    ],
)
def test_an_ascii_symbol_outside_the_venue_shape_is_refused_with_a_reason(
    symbol: str, why: str
) -> None:
    classification = classify_symbol(symbol)
    assert classification.is_tradable is False
    assert classification.reason is not None
    assert "ascii symbol" in classification.reason, why
