"""Reference data for local development: the venues, and the instruments to trade them on.

Idempotent by construction. Every row is keyed by a UUIDv5 derived from
`(venue_id, symbol)`, so the same clone on two machines produces the same identifiers
and re-running the command inserts nothing. A random UUID here would make an
instrument's id a function of when somebody first ran the seed, and every fixture,
recorded response and hand-written query referencing it would then be machine-local.

The instrument *filters* below are development defaults, not authority. `tick_size`,
`lot_step` and `min_notional_quote` are the venue's own numbers and change without
notice; the venue adapter reads them from `exchangeInfo` at startup and reconciles this
table against what the exchange actually says. Seeding them lets a fresh clone run
offline paths before any credentials exist -- it does not make them true.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from fking.platform.persistence.schema import instrument, venue

# A fixed namespace so instrument ids are reproducible across machines and across
# re-seeds. Chosen once and frozen: changing this value re-keys every instrument in
# every database that has ever been seeded, which is a data migration rather than a
# constant edit.
INSTRUMENT_NAMESPACE: Final[UUID] = UUID("6b1f4a2e-9d3c-4a77-8f21-0a5c7e4d1b60")

# The archive's first date for each symbol on Binance production, which is the value the
# point-in-time universe query needs: `listed_at_utc <= as_of` is what stops a 2019
# backtest trading a contract that did not exist. Testnet mirrors production listings
# but publishes no listing feed, so these are reconciled against the archive manifest
# during ingestion (#21) and are development defaults until then.
_BTC_SPOT_LISTED: Final = datetime(2017, 8, 17, tzinfo=UTC)
_ETH_SPOT_LISTED: Final = datetime(2017, 8, 17, tzinfo=UTC)
_BTC_PERP_LISTED: Final = datetime(2019, 9, 8, tzinfo=UTC)
_ETH_PERP_LISTED: Final = datetime(2019, 11, 27, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class VenueSeed:
    venue_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class InstrumentSeed:
    venue_id: str
    symbol: str
    market: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    lot_step: Decimal
    min_notional_quote: Decimal
    listed_at_utc: datetime

    @property
    def instrument_id(self) -> UUID:
        return uuid5(INSTRUMENT_NAMESPACE, f"{self.venue_id}:{self.symbol}")


@dataclass(frozen=True, slots=True)
class SeedReport:
    """What the seed actually changed. Zero on a re-run is the expected outcome."""

    inserted_venue_count: int
    inserted_instrument_count: int


# Bybit is present as a venue with no instruments: the fallback adapter is #112, and a
# venue row with symbols nothing can trade would be reference data asserting a
# capability the system does not have.
VENUES: Final[tuple[VenueSeed, ...]] = (
    VenueSeed("binance-spot-testnet", "Binance Spot Testnet"),
    VenueSeed("binance-futures-testnet", "Binance USD-M Futures Testnet"),
    VenueSeed("bybit-testnet", "Bybit Testnet"),
)

INSTRUMENTS: Final[tuple[InstrumentSeed, ...]] = (
    InstrumentSeed(
        venue_id="binance-futures-testnet",
        symbol="BTCUSDT",
        market="futures_um",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.10"),
        lot_step=Decimal("0.001"),
        min_notional_quote=Decimal("100"),
        listed_at_utc=_BTC_PERP_LISTED,
    ),
    InstrumentSeed(
        venue_id="binance-futures-testnet",
        symbol="ETHUSDT",
        market="futures_um",
        base_asset="ETH",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        lot_step=Decimal("0.001"),
        min_notional_quote=Decimal("20"),
        listed_at_utc=_ETH_PERP_LISTED,
    ),
    InstrumentSeed(
        venue_id="binance-spot-testnet",
        symbol="BTCUSDT",
        market="spot",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        lot_step=Decimal("0.00001"),
        min_notional_quote=Decimal("10"),
        listed_at_utc=_BTC_SPOT_LISTED,
    ),
    InstrumentSeed(
        venue_id="binance-spot-testnet",
        symbol="ETHUSDT",
        market="spot",
        base_asset="ETH",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        lot_step=Decimal("0.0001"),
        min_notional_quote=Decimal("10"),
        listed_at_utc=_ETH_SPOT_LISTED,
    ),
)


async def seed_reference_data(connection: AsyncConnection) -> SeedReport:
    """Insert the venues and instruments a local stack needs, skipping what exists.

    `ON CONFLICT DO NOTHING` rather than `DO UPDATE`: an upsert would silently rewrite a
    filter the venue adapter had already reconciled against `exchangeInfo`, replacing a
    measured value with a development default. Existing rows are left alone, and
    correcting one is a deliberate statement rather than a side effect of running a
    convenience command.
    """
    venue_result = await connection.execute(
        pg_insert(venue)
        .values(
            [
                {"venue_id": row.venue_id, "display_name": row.display_name, "is_testnet": True}
                for row in VENUES
            ]
        )
        .on_conflict_do_nothing(index_elements=["venue_id"])
    )
    instrument_result = await connection.execute(
        pg_insert(instrument)
        .values(
            [
                {
                    "instrument_id": row.instrument_id,
                    "venue_id": row.venue_id,
                    "symbol": row.symbol,
                    "market": row.market,
                    "base_asset": row.base_asset,
                    "quote_asset": row.quote_asset,
                    "tick_size": row.tick_size,
                    "lot_step": row.lot_step,
                    "min_notional_quote": row.min_notional_quote,
                    "listed_at_utc": row.listed_at_utc,
                    "delisted_at_utc": None,
                }
                for row in INSTRUMENTS
            ]
        )
        .on_conflict_do_nothing(index_elements=["instrument_id"])
    )
    return SeedReport(
        inserted_venue_count=venue_result.rowcount,
        inserted_instrument_count=instrument_result.rowcount,
    )


async def count_reference_rows(connection: AsyncConnection) -> tuple[int, int]:
    """`(venue_count, instrument_count)` currently in the database."""
    venue_count = await connection.scalar(sa.select(sa.func.count()).select_from(venue))
    instrument_count = await connection.scalar(sa.select(sa.func.count()).select_from(instrument))
    return int(venue_count or 0), int(instrument_count or 0)
