"""The tradable universe is an intersection, and every symbol outside it says why.

Every symbol here comes from the recorded `exchangeInfo` corpus rather than from a
literal in this file. That matters for one case in particular: the deliberate non-ASCII
symbols testnet serves are the free fuzz test for the parser, and a hand-written fixture
is precisely the thing nobody writes them into.

The archive side is a `frozenset` built from the recorded symbols, so the two set
differences the resolver reports are differences between two real, observed sets.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Final
from uuid import UUID

import pytest

from fking.domain import Venue
from fking.execution import (
    IntersectionBaseline,
    UniverseUnavailableError,
    VenueExchangeInfo,
    check_intersection_drift,
    classify_symbol,
    parse_venue_payload,
    resolve_universe,
)
from fking.platform.config.settings import TelemetrySettings
from fking.platform.correlation import correlation_scope
from fking.platform.logging import build_processor_chain
from tests.execution.conftest import load_recording, recorded_venues

pytestmark = pytest.mark.unit

CORRELATION_ID: Final = UUID("0192f3c8-1e5b-7c0d-8a41-2b9d4e6f8a11")

TELEMETRY: Final = TelemetrySettings.model_validate(
    {"service_name": "fking", "environment": "ci", "git_sha": "1a663a6"}
)


@pytest.fixture(autouse=True)
def _bound_correlation_id() -> Iterator[None]:
    """Resolution logs, and under `pytest` an unbound correlation id raises rather than
    being repaired to `orphan`. Every test here therefore runs inside a scope, exactly
    as the startup path does."""
    with correlation_scope(CORRELATION_ID):
        yield


def _recorded_info(venue: Venue) -> VenueExchangeInfo:
    recording = load_recording(venue, "exchangeInfo")
    return VenueExchangeInfo.model_validate(parse_venue_payload(recording.body))


def _listed_tradable(info: VenueExchangeInfo) -> frozenset[str]:
    return frozenset(
        entry.symbol
        for entry in info.symbols
        if classify_symbol(entry.symbol).is_tradable and entry.status == "TRADING"
    )


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
def test_a_requested_symbol_absent_from_the_intersection_aborts_startup(venue: Venue) -> None:
    """Fatal, not a warning: a symbol the venue does not list produces zero fills, and
    zero fills are scored as "no edge" -- which retires a strategy for an
    infrastructure reason nothing downstream can distinguish from a real result."""
    info = _recorded_info(venue)
    listed = _listed_tradable(info)
    # An archive that holds one listed symbol plus one the venue does not list, so both
    # differences are non-empty and both must be named.
    archive_symbols = frozenset({next(iter(sorted(listed))), "DOGEUSDT"})

    with pytest.raises(UniverseUnavailableError) as refusal:
        resolve_universe(
            venue_id=str(venue),
            exchange_info=info,
            archive_symbols=archive_symbols,
            requested=frozenset({"NOTLISTEDUSDT"}),
        )

    message = str(refusal.value)
    assert "NOTLISTEDUSDT" in message
    # Both set differences, so the reader can tell "not tradable here" from "no history".
    assert "listed with no archive history" in message
    assert "archive history the venue does not list" in message
    assert "'DOGEUSDT'" in message
    assert "quarantined" in message


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
def test_a_non_ascii_symbol_is_quarantined_with_its_code_points_and_round_trips(
    venue: Venue,
) -> None:
    """Quarantined, never dropped. A silent drop leaves the universe quietly wrong with
    no line naming what went missing."""
    info = _recorded_info(venue)
    universe = resolve_universe(
        venue_id=str(venue),
        exchange_info=info,
        archive_symbols=_listed_tradable(info),
        requested=frozenset(),
    )

    non_ascii = [entry for entry in universe.quarantined if not entry.symbol.isascii()]
    assert non_ascii, f"{venue} exchangeInfo recording carries no non-ascii symbol"
    for entry in non_ascii:
        assert entry.symbol not in universe.symbols
        assert entry.reason is not None
        assert "U+" in entry.reason
        # The reason itself must render on a console whose codepage is not UTF-8.
        assert entry.reason.encode("ascii", errors="strict")
        # Byte for byte: NFKC would change the code points the venue expects back.
        assert classify_symbol(entry.symbol).symbol == entry.symbol


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
def test_logging_the_full_universe_emits_valid_utf8_and_raises_nothing(venue: Venue) -> None:
    """The failure this guards is a `UnicodeEncodeError` raised *inside* the log call
    that reports the quarantine -- a startup crash whose diagnostic channel is the thing
    that failed. The sink is deliberately given a non-UTF-8 encoding, which is what a
    Windows console's default codepage is."""
    info = _recorded_info(venue)
    universe = resolve_universe(
        venue_id=str(venue),
        exchange_info=info,
        archive_symbols=_listed_tradable(info),
        requested=frozenset(),
    )

    rendered: object = {
        "event": "execution.universe.resolved",
        "logger": "fking.execution.universe",
        "venue": universe.venue_id,
        "tradable_count": len(universe.symbols),
        "quarantined_count": len(universe.quarantined),
        "venue_only_count": len(universe.venue_only),
        "archive_only_count": len(universe.archive_only),
        "quarantined": [entry.reason for entry in universe.quarantined],
    }
    for processor in build_processor_chain(TELEMETRY, strict=True):
        rendered = processor(None, "info", rendered)  # type: ignore[arg-type]
    assert isinstance(rendered, str)

    buffer = io.BytesIO()
    sink = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict", newline="")
    sink.write(f"{rendered}\n")  # raises UnicodeEncodeError if a raw code point got out
    sink.flush()

    record = json.loads(buffer.getvalue().decode("utf-8"))
    assert record["message"] == "execution.universe.resolved"
    assert record["tradable_count"] == len(universe.symbols)
    assert record["quarantined_count"] == len(universe.quarantined)
    assert len(record["quarantined"]) == len(universe.quarantined)


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
def test_filters_load_as_exact_decimals_from_the_response_text(venue: Venue) -> None:
    """The venue's own characters, not a float that has already been rounded. These are
    the tolerance reconciliation compares against -- step sizes span eight orders of
    magnitude, so a global epsilon is too tight for one symbol and too loose for
    another, and too loose hides real divergence."""
    info = _recorded_info(venue)
    listed = _listed_tradable(info)
    universe = resolve_universe(
        venue_id=str(venue),
        exchange_info=info,
        archive_symbols=listed,
        requested=listed,
    )

    assert universe.symbols == listed
    body = load_recording(venue, "exchangeInfo").body
    for symbol in sorted(universe.symbols):
        filters = universe.filters_for(symbol)
        for quantity in (filters.tick_size, filters.step_size, filters.max_quantity):
            assert isinstance(quantity, Decimal)
            assert quantity > 0
            # The exact characters the venue sent, trailing zeros and all, are still in
            # the body: a float round trip would not reproduce them.
            assert f'"{quantity}"' in body


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
def test_filters_for_an_unresolved_symbol_refuses_rather_than_defaulting(venue: Venue) -> None:
    info = _recorded_info(venue)
    universe = resolve_universe(
        venue_id=str(venue),
        exchange_info=info,
        archive_symbols=_listed_tradable(info),
        requested=frozenset(),
    )
    with pytest.raises(UniverseUnavailableError, match="not in the resolved universe"):
        universe.filters_for("NOTLISTEDUSDT")


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
def test_intersection_count_drift_is_reported_against_the_recorded_baseline(
    venue: Venue,
) -> None:
    """The counts are measured, never assumed. A baseline that no longer matches means
    the venue changed, and that is a `needs-human` escalation rather than an automatic
    adjustment -- silently adopting the new number discards the only evidence it moved.
    """
    info = _recorded_info(venue)
    listed = _listed_tradable(info)
    archive_only_symbols = frozenset({f"ARCHIVEONLY{index}USDT" for index in range(3)})
    universe = resolve_universe(
        venue_id=str(venue),
        exchange_info=info,
        archive_symbols=listed | archive_only_symbols,
        requested=frozenset(),
    )

    on_baseline = IntersectionBaseline(
        venue_id=str(venue),
        archive_only_count=len(archive_only_symbols),
        venue_only_count=0,
        source=f"recorded exchangeInfo {load_recording(venue, 'exchangeInfo').path.name}",
        tolerance=0,
    )
    assert not check_intersection_drift(universe, on_baseline).is_drifted

    moved = check_intersection_drift(
        universe,
        IntersectionBaseline(
            venue_id=str(venue),
            archive_only_count=len(archive_only_symbols) + 40,
            venue_only_count=0,
            source="a stale measurement",
            tolerance=10,
        ),
    )
    assert moved.is_drifted
    assert str(len(archive_only_symbols)) in moved.describe()


@pytest.mark.parametrize("venue", recorded_venues(), ids=str)
def test_a_baseline_for_another_venue_is_refused_rather_than_compared(venue: Venue) -> None:
    """Spot and futures differ by 79 and 189 respectively; comparing across them would
    report drift that is only a venue mismatch."""
    info = _recorded_info(venue)
    universe = resolve_universe(
        venue_id=str(venue),
        exchange_info=info,
        archive_symbols=_listed_tradable(info),
        requested=frozenset(),
    )
    with pytest.raises(ValueError, match="venue mismatch"):
        check_intersection_drift(
            universe,
            IntersectionBaseline(
                venue_id="some-other-venue",
                archive_only_count=0,
                venue_only_count=0,
                source="wrong venue",
            ),
        )
