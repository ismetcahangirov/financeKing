"""`python -m fking.platform.persistence` -- seed reference data into the configured database.

Idempotent: run it as often as you like. The second run reports zero insertions, which
is the signal that the first one worked rather than that nothing happened -- the row
counts are printed alongside so the difference is visible.

Exits `EX_CONFIG` (78) on invalid configuration, matching
`python -m fking.platform.config`, so a deploy script can tell a misconfiguration from a
database that is simply not up yet.
"""

from __future__ import annotations

import asyncio
import json
import sys

from fking.platform.config import EX_CONFIG, ConfigError, load_settings
from fking.platform.persistence.engine import build_engine
from fking.platform.persistence.seed import count_reference_rows, seed_reference_data


async def _seed() -> dict[str, int]:
    settings = load_settings()
    engine = build_engine(settings.database)
    try:
        async with engine.begin() as connection:
            report = await seed_reference_data(connection)
            venue_count, instrument_count = await count_reference_rows(connection)
    finally:
        await engine.dispose()
    return {
        "inserted_venue_count": report.inserted_venue_count,
        "inserted_instrument_count": report.inserted_instrument_count,
        "venue_count": venue_count,
        "instrument_count": instrument_count,
    }


def main() -> int:
    try:
        counts = asyncio.run(_seed())
    except ConfigError as invalid:
        print(f"configuration error: {invalid}", file=sys.stderr)
        return EX_CONFIG

    print(json.dumps({"event": "seed_completed", **counts}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
