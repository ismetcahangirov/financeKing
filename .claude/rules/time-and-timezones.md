# Rule — Time and Timezones

## The rule

Every `datetime` in this system is timezone-aware and in UTC, rejected at construction if it is not. Nothing reads the wall clock implicitly: anything that needs the current time takes a `Clock` as a parameter, and in `strategy` and `risk` that is mandatory. Elapsed time is measured with `time.monotonic()`, never by subtracting wall clocks.

## Why

Crypto trades continuously. There is no market open, no settlement boundary, no weekend gap where an off-by-four-hours error announces itself. A timezone bug here does not produce a crash or an obviously empty session — it produces a backtest that is slightly, plausibly, better than reality.

The mechanism, concretely. A naive `datetime` in Python carries no offset, and `datetime.now()` returns local time. On a machine in UTC+4, a bar labelled `2026-03-14 12:00` naive is written to a `TIMESTAMP WITHOUT TIME ZONE` column, then read back and compared against a trade stream whose timestamps came from the exchange in UTC. The comparison does not raise — both sides are naive, both are valid, and the join succeeds. The feature computed "as of" bar 12:00 now includes four hours of trades that had not happened yet. Every feature is a little bit prophetic, every signal is a little bit early, and the backtest Sharpe rises. This is look-ahead bias arriving through the timezone, and `ARCHITECTURE.md` §6 names look-ahead as the most dangerous defect class in the project specifically because it does not fail — it makes bad strategies look excellent.

Mixing naive and aware is safer than mixing naive and naive: `aware - naive` raises `TypeError: can't subtract offset-naive and offset-aware datetimes`, and `aware < naive` raises too. The bug only survives when *everything* is naive. So the defence is not "be careful at the comparison", it is "no naive datetime ever exists in the process".

The clock injection half is about reproducibility rather than correctness. A strategy that calls `datetime.now(UTC)` produces a different decision on Tuesday than the same code produced on Monday against the same bar. That breaks backtest/live parity (`ARCHITECTURE.md` §4) at the only point where parity is checkable, it makes the strategy untestable without freezing the system clock, and it makes an evolution result irreproducible — which means the survival score is scoring noise. `CLAUDE.md` §4 requires purity in `strategy` and `risk`; the clock is the most commonly smuggled impurity because it does not look like I/O.

## Incorrect

```python
from datetime import datetime, timedelta
from decimal import Decimal


class MomentumStrategy:
    def evaluate(self, bars: list[Bar]) -> Signal | None:
        now = datetime.utcnow()                                   # naive, and deprecated in 3.12
        recent = [b for b in bars if b.open_time > now - timedelta(hours=4)]
        if not recent:
            return None
        return Signal(
            direction="long",
            conviction=Decimal("0.6"),
            horizon=timedelta(hours=8),
            invalidation=recent[0].low,
            rationale="4h momentum",
            decided_at=datetime.now(),                             # naive local time
        )
```

Two separate failures, neither of which raises. `datetime.utcnow()` returns a naive datetime whose value happens to be UTC — comparing it against `b.open_time` works only while the bars are also naive-UTC, and silently produces a four-hour window offset the moment any bar arrives aware or local. `datetime.now()` returns naive *local* time, so `decided_at` is written to the audit trail in the machine's timezone with nothing recording which timezone that was; six months later the trade is unreconstructable, which violates the governing observability requirement in `ARCHITECTURE.md` §11. And because the strategy reads the clock at all, replaying the same bars tomorrow yields a different signal.

## Correct

```python
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol


class Clock(Protocol):
    """The only sanctioned source of the current time."""

    def now(self) -> datetime:
        """Return an aware UTC datetime."""


@dataclass(frozen=True, slots=True)
class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Test and backtest clock. Mutable by design; it is infrastructure, not a domain type."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("FrozenClock requires an aware datetime")
        self._current = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> None:
        self._current += delta


class MomentumStrategy:
    def evaluate(self, bars: Sequence[Bar], clock: Clock) -> Signal | None:
        as_of = clock.now()
        window_start = as_of - timedelta(hours=4)
        recent = tuple(bar for bar in bars if window_start < bar.open_time_utc <= as_of)
        if not recent:
            return None
        return Signal(
            direction="long",
            conviction=Decimal("0.6"),
            horizon=timedelta(hours=8),
            invalidation=recent[0].low_quote_price,
            rationale="4h momentum",
            decided_at_utc=as_of,
        )
```

Note `bar.open_time_utc <= as_of` as well as the lower bound. Without the upper bound the strategy is correct in live — where no future bar exists — and leaks in backtest, where the whole history is in memory. That asymmetry is exactly the class of bug the shared code path is meant to expose, and it only gets exposed if the strategy filters against an injected `as_of` rather than against "now".

`SystemClock` is frozen and `FrozenClock` is not; that is deliberate and consistent with [`./immutability.md`](./immutability.md), which binds domain objects, not platform infrastructure.

## Exchange epoch units

`ARCHITECTURE.md` §6 records a verified ingestion trap: Binance spot data switched to microsecond timestamps from 2025-01-01 while futures stayed in milliseconds. A global `TIMESTAMP_UNIT` constant is wrong on one side of that split no matter which value it holds, and wrong by a factor of 1000 — which places 2026 data in 1970 or in the year 57000. Normalization is keyed on `(market, date)`:

```python
from datetime import UTC, date, datetime
from typing import Literal

Market = Literal["spot", "futures"]

# Binance spot archives switched to microseconds on this date; futures did not.
# ARCHITECTURE.md §6, DATA_PIPELINE.md, docs/adr/0013.
_SPOT_MICROSECOND_CUTOVER: Final = date(2025, 1, 1)
_PLAUSIBLE_RANGE_UTC: Final = (datetime(2015, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC))


def epoch_to_utc(raw: int, market: Market, archive_date: date) -> datetime:
    """Convert a Binance archive epoch to an aware UTC datetime.

    The unit is a property of (market, archive_date), never of the process.
    """
    divisor = 1_000_000 if market == "spot" and archive_date >= _SPOT_MICROSECOND_CUTOVER else 1_000
    moment = datetime.fromtimestamp(raw / divisor, tz=UTC)
    low, high = _PLAUSIBLE_RANGE_UTC
    if not low <= moment < high:
        raise DataIntegrityError(
            f"epoch {raw} for {market} on {archive_date} normalised to {moment.isoformat()}, "
            f"outside the plausible range; the unit mapping is wrong"
        )
    return moment
```

The range assertion is the important half. The mapping table will eventually be wrong again — Binance will change something else — and a magnitude check turns a silent 1000x error into a loud one at ingest, before the data reaches the feature store. `raw / divisor` is the single sanctioned float division in this path: it is a timestamp, not money, and the microsecond resolution it must preserve is well inside a double's 53 bits until the year 2255. See [`./decimal-and-money.md`](./decimal-and-money.md) for why that reasoning does not generalize to prices.

| Feed | Unit | Applies |
|---|---|---|
| Spot klines / trades, archive date ≥ 2025-01-01 | microseconds | keyed on `(spot, date)` |
| Spot klines / trades, archive date < 2025-01-01 | milliseconds | keyed on `(spot, date)` |
| Futures klines / trades / funding, all dates | milliseconds | keyed on `(futures, *)` |
| REST and WebSocket responses | as documented per endpoint, validated by range | never assumed from the archive mapping |

## Enforcement

**ruff `DTZ`** (flake8-datetimez), enabled repository-wide with no `DTZ` entry in `per-file-ignores`:

```toml
[tool.ruff.lint]
select = ["E", "F", "B", "N", "UP", "RUF", "TRY", "DTZ", "BLE", "FURB", "PL", "SIM", "ANN"]

[tool.ruff.lint.per-file-ignores]
# No DTZ exemption exists anywhere, including tests. The clock module needs none:
# `datetime.now(UTC)` and `datetime.fromtimestamp(x, tz=UTC)` are DTZ-clean by
# construction, so the correct spelling is also the only one that passes.
"tests/**" = ["S101"]
```

The rules that do the work: `DTZ001` (`datetime()` with no `tzinfo`), `DTZ002` (`datetime.today()`), `DTZ003` (`datetime.utcnow()`), `DTZ004` (`datetime.utcfromtimestamp()`), `DTZ005` (`datetime.now()` with no `tz`), `DTZ006` (`datetime.fromtimestamp()` with no `tz`), `DTZ007` (`strptime` without `%z` and without an explicit `tzinfo`), `DTZ011` (`date.today()`), `DTZ012` (`date.fromtimestamp()`).

**Domain construction guard.** `DTZ` catches the call sites it knows about; it cannot catch an aware-but-not-UTC datetime, or one deserialized from the database. The domain base type rejects both:

```python
from dataclasses import dataclass
from datetime import UTC, datetime


class DomainError(Exception):
    """Base for invariant violations in fking.domain."""


def require_utc(moment: datetime, field_name: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise DomainError(f"{field_name} must be timezone-aware; got naive {moment!r}")
    if moment.utcoffset() != UTC.utcoffset(None):
        raise DomainError(f"{field_name} must be UTC; got offset {moment.utcoffset()!r}")
    return moment


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: UUID
    event_time_utc: datetime
    quote_price: Decimal
    base_quantity: Decimal

    def __post_init__(self) -> None:
        require_utc(self.event_time_utc, "event_time_utc")
```

Rejecting a non-UTC aware datetime rather than converting it is deliberate. `astimezone(UTC)` would silently accept a value whose offset was guessed wrong upstream; raising forces the guess to be made — and reviewed — where the data enters.

**AST check** at `tools/checks/clock_isolation.py`, wired into the `checks` target described in [`./decimal-and-money.md`](./decimal-and-money.md):

```python
"""No module under strategy/, risk/ or backtest/ may read the wall clock."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PURE_PACKAGES: tuple[str, ...] = ("strategy", "risk", "backtest")
BANNED_ATTRIBUTES: frozenset[str] = frozenset({"now", "today", "utcnow", "utcfromtimestamp", "fromtimestamp"})
BANNED_CALLS: frozenset[str] = frozenset({"time", "time_ns", "monotonic", "perf_counter"})
CLOCK_MODULE = Path("platform/clock.py")


def offending_nodes(tree: ast.AST) -> list[ast.expr]:
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in BANNED_ATTRIBUTES:
            found.append(node)
        elif isinstance(func, ast.Attribute) and func.attr in BANNED_CALLS:
            found.append(node)
    return found


def main(root: Path) -> int:
    failures: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.relative_to(root).parts[0] not in PURE_PACKAGES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in offending_nodes(tree):
            failures.append(
                f"{path}:{node.lineno} reads the clock directly; accept a Clock parameter instead"
            )
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
```

`backtest` is in that set for a sharper version of the same reason as the other two. Its clock is *simulated* — the event loop advances it to each event's instant — and a result is reproducible only while that is the sole source of time in the run. One `datetime.now(UTC)` inside the engine and two runs of the same `config_hash` disagree, which per `../../BACKTEST_ENGINE.md` §5 voids not only that run but every result the engine has produced: nothing distinguishes the ones that happened to agree.

**Tests** that the guard is real, not decorative:

```python
import pytest
from datetime import UTC, datetime, timedelta, timezone


def test_naive_event_time_is_rejected_at_construction() -> None:
    with pytest.raises(DomainError, match="timezone-aware"):
        Fill(
            fill_id=uuid4(),
            event_time_utc=datetime(2026, 3, 14, 12, 0),
            quote_price=Decimal("64000.00"),
            base_quantity=Decimal("0.01"),
        )


def test_aware_but_non_utc_event_time_is_rejected_rather_than_converted() -> None:
    baku = timezone(timedelta(hours=4))
    with pytest.raises(DomainError, match="must be UTC"):
        Fill(
            fill_id=uuid4(),
            event_time_utc=datetime(2026, 3, 14, 16, 0, tzinfo=baku),
            quote_price=Decimal("64000.00"),
            base_quantity=Decimal("0.01"),
        )
```

**Storage.** Every temporal column is `TIMESTAMPTZ`, including the hypertable time dimension, and the session timezone is pinned so that nothing depends on the server's locale:

```sql
ALTER DATABASE fking SET timezone TO 'UTC';

CREATE TABLE bar (
    symbol          TEXT        NOT NULL,
    open_time_utc   TIMESTAMPTZ NOT NULL,
    close_quote_price NUMERIC(38, 18) NOT NULL,
    PRIMARY KEY (symbol, open_time_utc)
);

SELECT create_hypertable('bar', by_range('open_time_utc'));
```

`TIMESTAMPTZ` stores an absolute instant and renders it in the session timezone; `TIMESTAMP` stores wall-clock digits and discards the offset on insert. That discard is irreversible and silent — which is why the column-type test in [`./decimal-and-money.md`](./decimal-and-money.md) has a sibling asserting that no `_utc`-suffixed column is `timestamp without time zone`.

**Serialization** is `isoformat()`, producing `2026-03-14T12:00:00+00:00`. Never `str()`, never `strftime` with a hand-written pattern, never an epoch integer on the wire — an integer forces every consumer to re-derive the unit, which is the same mistake as the global epoch constant one layer up.

## The one exception

`time.monotonic()` and `time.perf_counter()` are not datetimes and are the correct tool for elapsed time — request latency, retry backoff, timeout budgets, span durations.

```python
import time

started = time.monotonic()
response = await guarded_client().get(url)
latency_seconds = time.monotonic() - started
```

Wall clock subtraction is wrong for this, not merely inelegant: an NTP step correction during the measurement produces a negative latency, which lands in a Prometheus histogram as an underflow and in a p99 calculation as garbage. `monotonic()` cannot go backwards. It also cannot be formatted, stored, or compared across processes — which is the point. It never enters a domain object, an audit record, or a database column; a monotonic reading is a duration, and a duration is named `_seconds` per [`./naming.md`](./naming.md).

Note that the clock-isolation check bans `monotonic()` inside `strategy` and `risk` as well. Pure functions have no business measuring their own runtime, and a strategy that branches on how long it took to compute is no longer deterministically replayable.
