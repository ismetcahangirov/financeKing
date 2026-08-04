"""`python -m fking.data.backfill` -- the two commands an operator runs.

```
python -m fking.data.backfill ingest --symbols BTCUSDT,ETHUSDT --interval 1m
python -m fking.data.backfill coverage
```

`ingest` walks each symbol's full discovered range to T-1 and prints the run summary.
`coverage` prints the per-series report `backtest` reads before every run: first timestamp,
last timestamp, gap count and total gapped duration.

**T-1, not today.** Today's archive does not exist yet -- the host publishes a day's file
after the day ends -- and asking for it produces a 404 that a reader has to interpret. The
default therefore stops at yesterday, and `--through` moves it deliberately.

**The clock is read once, here, and passed down.** Everything below this module takes
`today_utc` and `now_utc` as parameters: the first decides which archives are monthly and
which daily, so a replayed run resolves the same URLs, and the second is the plausibility
reference for every timestamp in the run. A clock read per file drifts mid-run, and then
the same raw integer is accepted at the top of a range and rejected at the bottom.

Exit codes match `python -m fking.platform.config` and `python -m fking.platform.persistence`:
`EX_CONFIG` (78) for invalid configuration, `EX_DATAERR` (65) for a refused archive. A deploy
script can tell "you configured this wrongly" from "upstream changed" without parsing text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Final
from uuid import uuid4

from fking.data.archive import ArchiveFetcher
from fking.data.backfill.registry import IngestRegistry
from fking.data.backfill.report import BackfillReport
from fking.data.backfill.runner import BackfillRequest, run_backfill
from fking.data.format_resolver import Dataset, Market
from fking.platform.config import EX_CONFIG, ConfigError, Settings, load_settings
from fking.platform.correlation import correlation_scope
from fking.platform.errors import DataIntegrityError
from fking.platform.persistence.engine import build_engine
from fking.platform.safety.archive import GuardedArchiveEgress

# sysexits.h: the input data was incorrect. Distinct from EX_CONFIG so that "an archive
# refused a gate" and "the settings tree is invalid" are separable without reading stderr.
EX_DATAERR: Final[int] = 65

_ONE_DAY: Final[timedelta] = timedelta(days=1)

# The earliest date any Binance archive can exist. A floor for the publication search, not
# a claim about any symbol: each symbol's own earliest date is discovered by probing
# (`DATA_PIPELINE.md` section 2), and this is only where the search starts.
_ARCHIVE_FLOOR: Final[date] = date(2017, 1, 1)


async def _ingest(arguments: argparse.Namespace, settings: Settings) -> BackfillReport:
    now_utc = datetime.now(UTC)
    today_utc = now_utc.date()
    through_date = (
        date.fromisoformat(arguments.through) if arguments.through else today_utc - _ONE_DAY
    )
    request = BackfillRequest(
        market=Market(arguments.market),
        dataset=Dataset(arguments.dataset),
        symbols=_symbols(arguments.symbols),
        interval=arguments.interval if arguments.dataset == Dataset.KLINES.value else None,
        through_date=through_date,
        today_utc=today_utc,
        now_utc=now_utc,
        history_floor_date=max(_ARCHIVE_FLOOR, settings.data.history_start)
        if arguments.from_history_start
        else _ARCHIVE_FLOOR,
        write_root=settings.data.parquet_root,
        max_rejection_fraction=settings.data.max_row_rejection_ratio,
    )
    engine = build_engine(settings.database)
    try:
        # Minted here, at the top of the flow, and never regenerated below. Every log line
        # a four-hour run emits joins on this one id -- which is the difference between
        # "which archive produced this row" being a query and being an afternoon
        # (`OBSERVABILITY.md` section 3).
        with correlation_scope(uuid4()):
            async with GuardedArchiveEgress() as egress:
                return await run_backfill(
                    request,
                    fetcher=ArchiveFetcher(
                        egress=egress, cache_root=settings.data.archive_cache_root
                    ),
                    egress=egress,
                    registry=IngestRegistry(engine),
                )
    finally:
        await engine.dispose()


async def _coverage(settings: Settings) -> str:
    engine = build_engine(settings.database)
    try:
        rows = await IngestRegistry(engine).coverage()
    finally:
        await engine.dispose()
    if not rows:
        return "no ingested partitions; run `make ingest` first"
    lines = [
        f"{'market':<10} {'dataset':<8} {'symbol':<12} {'interval':<8} "
        f"{'first':<26} {'last':<26} {'rows':>12} {'gaps':>6} {'gapped':>16}"
    ]
    lines.extend(
        f"{row.market:<10} {row.dataset:<8} {row.symbol:<12} "
        f"{row.bar_interval or '-':<8} {row.first_event_time_utc.isoformat():<26} "
        f"{row.last_event_time_utc.isoformat():<26} {row.row_count:>12} "
        f"{row.gap_count:>6} {row.total_gapped_duration!s:>16}"
        for row in rows
    )
    return "\n".join(lines)


def _symbols(raw: str) -> tuple[str, ...]:
    """Split and normalise `--symbols`, refusing an empty list rather than doing nothing.

    A run with no symbols would report a clean, complete backfill of nothing, which is the
    most misleading possible output for a command whose whole purpose is coverage.
    """
    symbols = tuple(dict.fromkeys(part.strip().upper() for part in raw.split(",") if part.strip()))
    if not symbols:
        raise ValueError(
            "--symbols is empty. A backfill of no symbols reports a complete run having "
            "fetched nothing, which reads exactly like success"
        )
    return symbols


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m fking.data.backfill")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="backfill archives into the Parquet corpus")
    ingest.add_argument("--symbols", required=True, help="comma-separated, e.g. BTCUSDT,ETHUSDT")
    ingest.add_argument("--interval", default="1m", help="kline interval; ignored for trades")
    ingest.add_argument(
        "--market", default=Market.SPOT.value, choices=[member.value for member in Market]
    )
    ingest.add_argument(
        "--dataset",
        default=Dataset.KLINES.value,
        choices=[Dataset.KLINES.value, Dataset.TRADES.value],
        help="only the datasets with a declared format and a parser",
    )
    ingest.add_argument(
        "--through",
        default=None,
        help="ISO date to stop at, inclusive. Defaults to yesterday: today's archive does "
        "not exist until the day is over",
    )
    ingest.add_argument(
        "--from-history-start",
        action="store_true",
        help="start the publication search at data.history_start rather than at the "
        "archive floor, for a deliberately shorter run",
    )

    commands.add_parser("coverage", help="print the coverage report per series")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    # `argv or []` rather than letting argparse fall back to sys.argv: an in-process caller
    # that passes nothing means "no arguments", and inheriting the host process's command
    # line makes the function's behaviour depend on who imported it -- which under pytest
    # means parsing the test paths as a subcommand.
    arguments = _parse(argv or [])
    try:
        settings = load_settings()
        if arguments.command == "coverage":
            print(asyncio.run(_coverage(settings)))
            return 0
        report = asyncio.run(_ingest(arguments, settings))
    except ConfigError as invalid:
        print(f"configuration error: {invalid}", file=sys.stderr)
        return EX_CONFIG
    except ValueError as unusable:
        print(f"invalid argument: {unusable}", file=sys.stderr)
        return EX_CONFIG
    except DataIntegrityError as refused:
        # Deliberately not a stack trace and deliberately not a retry. A refused archive is
        # a format that drifted or bytes that changed upstream, and both are conditions a
        # human has to look at -- DATA_PIPELINE.md section 11.
        print(f"ingestion refused: {refused}", file=sys.stderr)
        return EX_DATAERR

    print(report.render())
    print(
        json.dumps(
            {
                "event": "backfill_completed",
                "rows_in": report.rows_in,
                "rows_out": report.rows_out,
                "rows_rejected": report.rows_rejected,
                "rejection_reasons": dict(report.rejection_reasons),
                "archives_ingested": report.archives_ingested,
                "archives_absent": report.archives_absent,
                "gap_count": report.gaps_recorded,
                "gaps_newly_discovered": report.gaps_newly_discovered,
                "total_gapped_seconds": report.gapped_duration.total_seconds(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
