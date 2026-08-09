"""`make backtest` -- read a run's data configuration, gate it on coverage, report.

The command's whole job today is the pre-run gate plus the event stream it produces, because
the venue simulator, the cost model and the validation harness have not landed. That is
stated here rather than implied: what this prints is coverage per symbol with the gap ranges,
and either a refusal or the event-sequence digest the run would have been driven by. It does
not yet execute a strategy, and a reader of the output should not be able to mistake it for
one that did.

**Configuration is a file, not a set of flags.** A window, a symbol set, an interval and a
warm-up length are the inputs that decide what a result means, so they belong somewhere that
can be committed, diffed and cited in a pull request. Flags are typed once and lost.

**`now_utc` is stated in the file rather than read from the clock.** Two reasons, and the
second is the one that decides it. Every module under `backtest/` is forbidden from reading
the wall clock (`tools/checks/clock_isolation.py`), because one clock read is enough to make
two runs of a `config_hash` disagree. And the value is the plausibility reference for every
timestamp the corpus hands back: a bound that moves between runs can accept a raw epoch on
Monday and refuse it on Tuesday, which is exactly the property `IngestionSpec.now_utc` exists
to remove from ingestion.

Exit codes match the other entrypoints in this repository: `EX_CONFIG` (78) when the file
cannot be read as a configuration, `EX_DATAERR` (65) when the window is refused. A caller can
tell "you configured this wrongly" from "the corpus cannot serve it" without parsing text.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from fking.backtest.feed._errors import FeedError, FeedRequestError
from fking.backtest.feed._feed import MarketDataFeed
from fking.backtest.feed._request import FeedRequest, SeriesRequest
from fking.data.format_resolver import Market
from fking.domain import Instrument, Venue

__all__ = ["FeedConfig", "load_config", "main"]

# sysexits.h. 78 is an invalid configuration, 65 is input data that will not serve.
EX_CONFIG: Final[int] = 78
EX_DATAERR: Final[int] = 65


@dataclass(frozen=True, slots=True)
class FeedConfig:
    """One backtest's data configuration, as the file states it."""

    corpus_root: Path
    now_utc: datetime
    request: FeedRequest


def load_config(path: Path) -> FeedConfig:
    """Read and validate a backtest configuration file.

    Raises:
        FeedRequestError: the file is missing, is not TOML, or omits or malforms a field.
            Every failure names the key, because the reader's next action is to edit it.
    """
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as missing:
        raise FeedRequestError(
            f"no backtest configuration at {path}. `make backtest` reads "
            f"BACKTEST_CONFIG=<path>; the file states the corpus root, the window, the "
            f"interval, the warm-up length and the series it subscribes to"
        ) from missing
    except tomllib.TOMLDecodeError as malformed:
        raise FeedRequestError(f"{path} is not readable as TOML: {malformed}") from malformed

    series = payload.get("series")
    if not isinstance(series, list) or not series:
        raise FeedRequestError(
            f"{path} must declare at least one [[series]] table naming a market and an "
            f"instrument; a run over no series has no result to refuse"
        )
    return FeedConfig(
        corpus_root=Path(_text(payload, "corpus_root", path=path)),
        now_utc=_moment(payload, "now_utc", path=path),
        request=FeedRequest(
            series=tuple(
                _series(entry, path=path, ordinal=ordinal) for ordinal, entry in enumerate(series)
            ),
            bar_interval=_text(payload, "bar_interval", path=path),
            exposed_from_utc=_moment(payload, "exposed_from_utc", path=path),
            until_utc=_moment(payload, "until_utc", path=path),
            warmup_bar_count=_whole_number(payload, "warmup_bar_count", path=path),
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Gate one configured window on coverage and report what it would have driven."""
    parser = argparse.ArgumentParser(
        prog="python -m fking.backtest.feed",
        description=(
            "Resolve a backtest's market-data window against the Parquet corpus. Prints "
            "coverage per symbol with the gap ranges and refuses a window it cannot serve "
            "without inventing bars."
        ),
    )
    parser.add_argument("config", type=Path, help="path to the backtest configuration TOML")
    parser.add_argument(
        "--duckdb-threads",
        type=int,
        default=None,
        help=(
            "DuckDB thread count for the corpus scan. Changes how long the read takes and "
            "nothing about what it returns; the determinism suite varies it to prove that."
        ),
    )
    parsed = parser.parse_args(argv)

    try:
        config = load_config(parsed.config)
    except FeedRequestError as invalid:
        print(f"configuration error: {invalid}", file=sys.stderr)
        return EX_CONFIG

    feed = MarketDataFeed(
        corpus_root=config.corpus_root,
        now_utc=config.now_utc,
        duckdb_thread_count=parsed.duckdb_threads,
    )
    try:
        report = feed.coverage(config.request)
        print(report.render())
        if not report.is_servable:
            return EX_DATAERR
        loaded = feed.load(config.request)
    except FeedError as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return EX_DATAERR

    print()
    print(
        f"stream  {len(loaded.events)} events from {loaded.archive_bar_count} archive bars "
        f"({loaded.warmup_event_count} warm-up, {loaded.exposed_event_count} exposed)"
    )
    print(f"digest  {loaded.event_sequence_digest}")
    print(
        "note    the venue simulator, cost model and validation harness are not yet wired "
        "to this stream; this run resolved and gated the data only"
    )
    return 0


def _series(entry: object, *, path: Path, ordinal: int) -> SeriesRequest:
    where = f"{path} [[series]] #{ordinal}"
    if not isinstance(entry, dict):
        raise FeedRequestError(f"{where} is a {type(entry).__name__}, not a table")
    return SeriesRequest(
        market=_market(entry, path=path, where=where),
        instrument=Instrument(
            venue=_venue(entry, path=path, where=where),
            symbol=_text(entry, "symbol", path=path, where=where),
            base_asset=_text(entry, "base_asset", path=path, where=where),
            quote_asset=_text(entry, "quote_asset", path=path, where=where),
            tick_size=_decimal(entry, "tick_size", path=path, where=where),
            lot_step=_decimal(entry, "lot_step", path=path, where=where),
            min_notional_quote=_decimal(entry, "min_notional_quote", path=path, where=where),
        ),
    )


def _market(entry: Mapping[str, object], *, path: Path, where: str) -> Market:
    declared = _text(entry, "market", path=path, where=where)
    try:
        return Market(declared)
    except ValueError as unknown:
        raise FeedRequestError(
            f"{where} names market {declared!r}; declared markets are "
            f"{sorted(member.value for member in Market)}"
        ) from unknown


def _venue(entry: Mapping[str, object], *, path: Path, where: str) -> Venue:
    declared = _text(entry, "venue", path=path, where=where)
    try:
        return Venue(declared)
    except ValueError as unknown:
        raise FeedRequestError(
            f"{where} names venue {declared!r}; declared venues are "
            f"{sorted(member.value for member in Venue)}. Every one is a testnet, and adding "
            f"a production member is a change to the demo-only guarantee rather than to a "
            f"configuration file"
        ) from unknown


def _text(payload: Mapping[str, object], key: str, *, path: Path, where: str | None = None) -> str:
    found = _present(payload, key, path=path, where=where)
    if not isinstance(found, str) or not found.strip():
        raise FeedRequestError(f"{where or path} key {key!r} must be a non-empty string")
    return found


def _decimal(
    payload: Mapping[str, object], key: str, *, path: Path, where: str | None = None
) -> Decimal:
    """A decimal stated as a TOML *string*, never as a TOML float.

    TOML has a float type and `tick_size = 0.01` parses into one, at which point the value
    is already `0.01000000000000000020816681711721685...` and no later annotation recovers
    it. Requiring the quoted form is what makes the Decimal-from-str rule
    (`docs/rules/decimal-and-money.md`) hold across the file boundary rather than only
    inside the process.
    """
    found = _present(payload, key, path=path, where=where)
    if isinstance(found, float):
        raise FeedRequestError(
            f"{where or path} key {key!r} is a TOML float; quote it as a string. A float here "
            f"is already rounded before this process sees it, and the venue filters this value "
            f"becomes decide whether an order is an order at all"
        )
    if not isinstance(found, str):
        raise FeedRequestError(
            f"{where or path} key {key!r} must be a quoted decimal string, got "
            f"{type(found).__name__}"
        )
    try:
        parsed = Decimal(found)
    except InvalidOperation as invalid:
        raise FeedRequestError(
            f"{where or path} key {key!r} is not a decimal: {found!r}"
        ) from invalid
    if not parsed.is_finite():
        raise FeedRequestError(f"{where or path} key {key!r} must be finite; got {found!r}")
    return parsed


def _moment(payload: Mapping[str, object], key: str, *, path: Path) -> datetime:
    """An instant stated as an offset-carrying TOML datetime or ISO 8601 string.

    A bare TOML local date-time (`2025-01-02T00:00:00`, no offset) parses into a *naive*
    Python datetime, which the request refuses -- deliberately, and with a message naming
    the key, because a window boundary silently read as machine-local selects a different
    set of bars than the one it appears to name.
    """
    found = _present(payload, key, path=path, where=None)
    if isinstance(found, datetime):
        return found
    if not isinstance(found, str):
        raise FeedRequestError(
            f"{path} key {key!r} must be an instant with a UTC offset, got {type(found).__name__}"
        )
    try:
        return datetime.fromisoformat(found)
    except ValueError as invalid:
        raise FeedRequestError(
            f"{path} key {key!r} is not an ISO 8601 instant: {found!r}"
        ) from invalid


def _whole_number(payload: Mapping[str, object], key: str, *, path: Path) -> int:
    found = _present(payload, key, path=path, where=None)
    if not isinstance(found, int) or isinstance(found, bool):
        raise FeedRequestError(f"{path} key {key!r} must be an integer, got {found!r}")
    return found


def _present(payload: Mapping[str, object], key: str, *, path: Path, where: str | None) -> object:
    if key not in payload:
        raise FeedRequestError(
            f"{where or path} is missing the required key {key!r}. Nothing is defaulted here: "
            f"a window, an interval or a warm-up length filled in silently is a run whose "
            f"result answers a different question from the one in the file"
        )
    return payload[key]
