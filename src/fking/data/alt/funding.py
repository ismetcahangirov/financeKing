"""Reading one month of realised funding rates out of the archive.

`data.binance.vision` files this dataset **monthly only** -- measured 2026-08-05, every
`daily/fundingRate/...` path 404s while the monthly one answers 200, which is the exact
opposite of `metrics`. `resolve_granularity` answers by distance from today and is
therefore wrong for both, so `fking.data.alt.registry.ARCHIVE_GRANULARITY` states each
dataset's granularity and every fetch passes it explicitly.

The layout, read from `BTCUSDT-fundingRate-2020-01` on 2026-08-05:

```text
calc_time,funding_interval_hours,last_funding_rate
1704067200000,8,0.00037409
1704096000000,8,0.00027213
```

A header row, a millisecond epoch, and a rate that is a signed dimensionless fraction --
negative when shorts pay longs, which is not an anomaly and is not rejected.

**This file refuses rather than tallies, and that is a deliberate departure from
`fking.data.loaders.driver`.** That driver rejects a bad row and counts it, because one
malformed print in 3.5 million must not stop a backfill. A funding month holds about
ninety rows. At that size a single unparseable row is not background noise, it is a
signal that the layout moved, and the rejection-fraction gate would need a ceiling of one
row in ninety -- 1.1% -- which is eleven times looser than the corpus ceiling and would
admit exactly the drift it exists to catch. So the file is the unit of failure here and
nothing partial is returned.

`funding_interval_hours` is checked against the declared cadence on every row rather than
ignored as a constant. Binance has changed the settlement interval on individual
perpetuals, and when it does, every rate in the file means something different -- a rate
per four hours summed as though it were per eight hours understates carry by half, in a
series whose whole purpose is to measure carry.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final

from fking.data.alt.registry import BINANCE_FUNDING_RATE
from fking.data.alt.spec import AltObservation, require_utc
from fking.data.format_resolver import EpochUnit, epoch_to_utc
from fking.data.loaders.source import split_rows
from fking.platform.errors import DataIntegrityError

__all__ = ["FUNDING_RATE_COLUMNS", "parse_funding_rate_archive"]

FUNDING_RATE_COLUMNS: Final[tuple[str, ...]] = (
    "calc_time",
    "funding_interval_hours",
    "last_funding_rate",
)

# Plain decimal notation only. `Decimal(" 1 ")` strips whitespace and `Decimal("1_0")` is
# ten, so a field that arrived malformed would silently become a different number; and
# `Decimal("NaN")` constructs happily and then makes every comparison downstream False
# forever. Same reasoning, same pattern shape, as `fking.data.loaders._fields`.
_DECIMAL_TOKEN: Final[re.Pattern[str]] = re.compile(r"\A[+-]?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?\Z")
_UNSIGNED_INTEGER_TOKEN: Final[re.Pattern[str]] = re.compile(r"\A[0-9]+\Z")
_SIGNED_INTEGER_TOKEN: Final[re.Pattern[str]] = re.compile(r"\A[+-]?[0-9]+\Z")

# An absurdity bound, not a plausibility model. Binance caps funding per settlement well
# below this on every perpetual it lists; one whole turn of the notional in a single
# settlement is arithmetically possible to express and impossible to mean, so it catches a
# decimal point that moved without pretending to know the venue's real cap.
_ABSURD_RATE_MAGNITUDE: Final[Decimal] = Decimal("1")

_HOURS_PER_SETTLEMENT: Final[int] = int(
    BINANCE_FUNDING_RATE.cadence // timedelta(hours=1)  # 8, from the declaration itself
)


def parse_funding_rate_archive(
    member_bytes: bytes, *, source: str, now_utc: datetime
) -> tuple[AltObservation, ...]:
    """Every realised funding settlement in one archive member, in event order.

    `member_bytes` is the CSV inside the `.zip`; unwrap it with
    `fking.data.loaders.extract_single_member`, which refuses a multi-member archive.

    `now_utc` is the plausibility reference for every timestamp in the file, fixed once
    for the whole parse. A bound re-read per row drifts mid-file, and then the same raw
    integer can be accepted at the top of an archive and rejected at the bottom.

    Returns observations with no `published_at_utc`: Binance publishes no release calendar
    for funding, so `available_at_utc` comes from the declared lag.

    Raises:
        DataIntegrityError: the header contradicts the declared layout, a row is
            unparseable, the settlement interval is not the declared cadence, or the
            series is not strictly increasing in `calc_time`.
        HeaderExpectationError: a subclass of the above, raised by `split_rows` when the
            first row is data rather than the declared header.
    """
    require_utc(now_utc, "now_utc")
    rows = split_rows(
        member_bytes,
        source=source,
        has_header_row=True,
        columns=FUNDING_RATE_COLUMNS,
    )

    observations: list[AltObservation] = []
    previous_event_time_utc: datetime | None = None
    for line_number, row in enumerate(rows, start=2):  # line 1 is the header
        where = f"{source} line {line_number}"
        if len(row) != len(FUNDING_RATE_COLUMNS):
            raise DataIntegrityError(
                f"{where} holds {len(row)} columns, not {len(FUNDING_RATE_COLUMNS)}; a row "
                f"with an extra or missing column means the layout moved upstream, and "
                f"reading the first three fields of four would keep working while meaning "
                f"something else"
            )
        raw_calc_time, raw_interval_hours, raw_last_funding_rate = row

        event_time_utc = _parse_settlement_instant(raw_calc_time, where=where, now_utc=now_utc)
        _require_declared_settlement_interval(raw_interval_hours, where=where)
        funding_rate_fraction = _parse_signed_rate(raw_last_funding_rate, where=where)

        if previous_event_time_utc is not None and event_time_utc <= previous_event_time_utc:
            raise DataIntegrityError(
                f"{where} settles at {event_time_utc.isoformat()}, not after the previous "
                f"row's {previous_event_time_utc.isoformat()}. Out-of-order or duplicate "
                f"settlements mean a merged or re-generated file; the store keys on "
                f"(series, event_time, available_at), so a duplicate would silently "
                f"collapse two settlements into one"
            )
        previous_event_time_utc = event_time_utc
        observations.append(
            AltObservation(event_time_utc=event_time_utc, observed_value=funding_rate_fraction)
        )
    return tuple(observations)


def _parse_settlement_instant(raw: str, *, where: str, now_utc: datetime) -> datetime:
    if not _SIGNED_INTEGER_TOKEN.match(raw):
        raise DataIntegrityError(f"{where}: calc_time={raw!r} is not a base-10 integer")
    try:
        return epoch_to_utc(int(raw), unit=EpochUnit.MILLISECONDS, now_utc=now_utc)
    except DataIntegrityError as out_of_range:
        raise DataIntegrityError(
            f"{where}: calc_time={raw!r} read as milliseconds is implausible -- {out_of_range}"
        ) from out_of_range


def _require_declared_settlement_interval(raw: str, *, where: str) -> None:
    if not _UNSIGNED_INTEGER_TOKEN.match(raw):
        raise DataIntegrityError(
            f"{where}: funding_interval_hours={raw!r} is not a non-negative base-10 integer"
        )
    if int(raw) != _HOURS_PER_SETTLEMENT:
        raise DataIntegrityError(
            f"{where}: funding_interval_hours={raw!r} but {BINANCE_FUNDING_RATE.source_id} "
            f"declares a cadence of {_HOURS_PER_SETTLEMENT} hours. Binance has changed the "
            f"settlement interval on individual perpetuals, and when it does every rate in "
            f"the file means something else -- carry summed at the wrong interval is wrong "
            f"by that ratio. Update the declared cadence rather than the comparison"
        )


def _parse_signed_rate(raw: str, *, where: str) -> Decimal:
    """The realised rate as an exact `Decimal`, negative when shorts pay longs.

    Never via `float`: `Decimal(0.00037409)` carries the double's rounding error before
    this code runs, and widening the type afterwards cannot undo it
    (`.claude/rules/decimal-and-money.md`).
    """
    if not _DECIMAL_TOKEN.match(raw):
        raise DataIntegrityError(
            f"{where}: last_funding_rate={raw!r} is not plain decimal notation"
        )
    try:
        parsed = Decimal(raw)
    except InvalidOperation as invalid:  # pragma: no cover - the pattern admits nothing else
        raise DataIntegrityError(
            f"{where}: last_funding_rate={raw!r} is not a decimal"
        ) from invalid
    if not parsed.is_finite():  # pragma: no cover - the pattern excludes NaN and Infinity
        raise DataIntegrityError(f"{where}: last_funding_rate={raw!r} is not finite")
    if abs(parsed) > _ABSURD_RATE_MAGNITUDE:
        raise DataIntegrityError(
            f"{where}: last_funding_rate={raw!r} is a whole turn of the notional in one "
            f"settlement, which no venue charges; a decimal point has moved"
        )
    return parsed
