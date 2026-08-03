"""Seeding reference data: deterministic ids, and nothing on the second run."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.domain.enums import Venue
from fking.platform.persistence.seed import (
    INSTRUMENTS,
    VENUES,
    count_reference_rows,
    seed_reference_data,
)


@pytest.mark.unit
def test_seeded_venue_ids_are_domain_venues() -> None:
    """Reference data cannot name a venue the domain does not know about."""
    assert {row.venue_id for row in VENUES} <= {member.value for member in Venue}


@pytest.mark.unit
def test_instrument_ids_are_deterministic_and_distinct() -> None:
    """UUIDv5 from `(venue_id, symbol)`.

    A random id would make an instrument's identity a function of when somebody first
    ran the seed, so every fixture, recorded response and hand-written query naming one
    would be machine-local. Recomputing must give the same answer on every clone.
    """
    identifiers = [row.instrument_id for row in INSTRUMENTS]
    assert len(set(identifiers)) == len(identifiers)
    assert [row.instrument_id for row in INSTRUMENTS] == identifiers


@pytest.mark.unit
def test_the_same_symbol_on_two_venues_gets_two_ids() -> None:
    """BTCUSDT spot and BTCUSDT futures are different instruments with different filters,
    and an id keyed on the symbol alone would silently merge them."""
    btc = [row for row in INSTRUMENTS if row.symbol == "BTCUSDT"]
    assert {row.venue_id for row in btc} == {"binance-spot-testnet", "binance-futures-testnet"}
    assert len({row.instrument_id for row in btc}) == len(btc)


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
async def test_seeding_twice_inserts_nothing_the_second_time(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        first = await seed_reference_data(connection)
        counts_after_first = await count_reference_rows(connection)
        second = await seed_reference_data(connection)
        counts_after_second = await count_reference_rows(connection)

    assert (first.inserted_venue_count, first.inserted_instrument_count) == (
        len(VENUES),
        len(INSTRUMENTS),
    )
    assert (second.inserted_venue_count, second.inserted_instrument_count) == (0, 0)
    assert counts_after_first == counts_after_second == (len(VENUES), len(INSTRUMENTS))


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
async def test_a_production_venue_row_cannot_be_recorded(engine: AsyncEngine) -> None:
    """The compiled-in host allowlist is what stops a production *request*. This stops
    the database from becoming a second, softer answer to which venues exist."""
    async with engine.begin() as connection:
        with pytest.raises(sa.exc.IntegrityError, match="venue_id_is_known"):
            await connection.execute(
                sa.text(
                    "INSERT INTO venue (venue_id, display_name) "
                    "VALUES ('binance-production', 'Binance')"
                )
            )
