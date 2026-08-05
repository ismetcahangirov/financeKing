"""One `RunConfig` builder for the engine suite.

Every field has a default because most tests care about exactly one of them, and a
builder that forces all eight makes the field under test invisible in the noise. The
window is a day wide and the budget small, so a runaway handler fails a test in
milliseconds rather than filling a machine.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fking.backtest import RunConfig

DEFAULT_START = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
DEFAULT_PARAMETERS: Mapping[str, Decimal] = {
    "lookback_bars": Decimal("20"),
    "entry_threshold_bps": Decimal("12.5"),
}


def config_for(
    *,
    strategy_id: str = "breakout-4h",
    strategy_version: str = "1.3.0",
    symbols: tuple[str, ...] = ("BTCUSDT",),
    parameters: Mapping[str, Decimal] | None = None,
    start_utc: datetime = DEFAULT_START,
    window: timedelta = timedelta(days=1),
    run_seed: int = 20260801,
    event_budget: int = 10_000,
) -> RunConfig:
    return RunConfig(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        symbols=symbols,
        parameters=DEFAULT_PARAMETERS if parameters is None else parameters,
        start_utc=start_utc,
        end_utc=start_utc + window,
        run_seed=run_seed,
        event_budget=event_budget,
    )
