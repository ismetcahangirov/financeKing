"""Reading one day of open interest out of the archive's `metrics` file.

`data.binance.vision` files this dataset **daily only** -- measured 2026-08-05, every
`monthly/metrics/...` path 404s while the daily one answers 200, which is the exact
opposite of `fundingRate`. `fking.data.alt.registry.ARCHIVE_GRANULARITY` states each
dataset's granularity and every fetch passes it explicitly.

The layout, read from `BTCUSDT-metrics-2024-01-02` on 2026-08-05:

```text
create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio
2024-01-02 00:00:00,BTCUSDT,76608.79800000,3388422457.29960000,1.02072986,...
```
(the row is elided after the fourth field; `METRICS_COLUMNS` below is the whole layout)

Eight columns, a header row, one row every five minutes, and a `create_time` that is
**not an epoch** -- a naive datetime string with no offset (VF-029). That is the whole
reason `TimestampEncoding` exists, and the encoding is resolved from
`fking.data.format_resolver` per `(market, dataset, date)` rather than assumed here, so
this file has no opinion about how a timestamp is spelled.

**One of the eight columns is ingested: `sum_open_interest`.** That is the base quantity
`BINANCE_OPEN_INTEREST.unit` declares, and it is the only column in the file this project
has a hypothesis for. The other seven are dropped, deliberately and with the reasons
stated, because a column ingested without a reader is a maintenance surface that has to
be migrated, backfilled and reasoned about forever by people who cannot find out what it
was for:

- `symbol` is not a value, it is a key, and it is already in `AltSeriesRef`. It is read
  and checked rather than stored.
- `sum_open_interest_value` restates the same position in quote terms at a mark price the
  file does not carry, so it is `sum_open_interest` times an unstated number. Storing both
  invites somebody to divide one by the other and call the result a price.
- The four long/short ratios -- `count_toptrader_long_short_ratio`,
  `sum_toptrader_long_short_ratio`, `count_long_short_ratio` and
  `sum_taker_long_short_vol_ratio` -- are positioning statistics, not open interest. Each
  is a separate series with a separate meaning and would need its own `AltSourceSpec` with
  its own measured lag and unit; hanging them off this source's declaration would give
  four series one source's lag by accident. They are one registry entry each away, when
  something needs them (`.claude/rules/overfitting-defences.md`: a column nobody has a
  hypothesis for is a search nobody charged for).

**This file refuses rather than tallies**, for the same reason `funding.py` does: a day
holds 288 rows, and at that size one unparseable row is not background noise, it is the
layout moving. The file is the unit of failure and nothing partial is returned.

**The five-minute spacing is checked on every row rather than assumed.** For this source
the sampling period *is* the availability lag -- the archived series is the five-minute
aggregate, so the value stamped at T is only knowable once T's period closes, and
`BINANCE_OPEN_INTEREST.availability_lag` is that same five minutes. A file whose rows are
not five minutes apart is therefore not merely irregular: every `available_at_utc` derived
from it is wrong, in the look-ahead direction if the real period is longer. The declared
cadence and the observed spacing have to agree or the declaration is a fiction.

The known cost of that strictness is that a day with a genuinely missing sample -- a
Binance outage long enough to skip a bucket -- refuses whole. No such day has been
observed; if one is, the response is to record the finding and change the declaration, not
to loosen the comparison, because loosening it silently re-admits the wrong-lag case.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final

from fking.data.alt.registry import BINANCE_OPEN_INTEREST
from fking.data.alt.spec import AltObservation, require_utc
from fking.data.format_resolver import ArchiveFormat, TimestampEncoding, parse_naive_utc_datetime
from fking.data.loaders.source import split_rows
from fking.platform.errors import DataIntegrityError

__all__ = ["METRICS_COLUMNS", "OPEN_INTEREST_COLUMN", "parse_open_interest_archive"]

METRICS_COLUMNS: Final[tuple[str, ...]] = (
    "create_time",
    "symbol",
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)

OPEN_INTEREST_COLUMN: Final[str] = "sum_open_interest"

_CREATE_TIME: Final[int] = METRICS_COLUMNS.index("create_time")
_SYMBOL: Final[int] = METRICS_COLUMNS.index("symbol")
_OPEN_INTEREST: Final[int] = METRICS_COLUMNS.index(OPEN_INTEREST_COLUMN)

# Plain decimal notation only, and the same reasoning as `funding.py`: `Decimal(" 1 ")`
# strips whitespace, `Decimal("1_0")` is ten, and `Decimal("NaN")` constructs happily and
# then makes every comparison downstream False forever.
_DECIMAL_TOKEN: Final[re.Pattern[str]] = re.compile(r"\A[+-]?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?\Z")

# The declared sampling period, taken from the declaration rather than restated, so the
# two cannot disagree. Five minutes, and it is also this source's availability lag.
_SAMPLING_PERIOD: Final[timedelta] = BINANCE_OPEN_INTEREST.cadence


def parse_open_interest_archive(
    member_bytes: bytes,
    *,
    source: str,
    now_utc: datetime,
    archive_format: ArchiveFormat,
) -> tuple[AltObservation, ...]:
    """Every open-interest sample in one daily `metrics` member, in event order.

    `member_bytes` is the CSV inside the `.zip`; unwrap it with
    `fking.data.loaders.extract_single_member`, which refuses a multi-member archive.

    `now_utc` is the timestamp plausibility reference for the whole parse, fixed once. A
    bound re-read per row drifts mid-file, and then the same raw value can be accepted at
    the top of an archive and rejected at the bottom.

    `archive_format` is the resolved declaration for this `(market, dataset, date)`. It is
    a parameter rather than a lookup inside, because the date this file covers is the
    caller's fact and resolving it here would mean guessing it from the payload.

    Returns observations with no `published_at_utc`: Binance publishes no release calendar
    for the metrics series, so `available_at_utc` comes from the declared lag.

    Raises:
        DataIntegrityError: the declared format is not this dataset's, the header
            contradicts the declared layout, a row is unparseable, the symbol changes
            mid-file, the spacing is not the declared sampling period, or the series is
            not strictly increasing in `create_time`.
        HeaderExpectationError: a subclass of the above, raised by `split_rows` when the
            first row contradicts the declared header.
    """
    require_utc(now_utc, "now_utc")
    _require_declared_encoding(archive_format, source=source)
    rows = split_rows(
        member_bytes,
        source=source,
        has_header_row=archive_format.has_header_row,
        columns=METRICS_COLUMNS,
    )

    observations: list[AltObservation] = []
    previous_event_time_utc: datetime | None = None
    file_symbol: str | None = None
    for line_number, row in enumerate(rows, start=2):  # line 1 is the header
        where = f"{source} line {line_number}"
        if len(row) != len(METRICS_COLUMNS):
            raise DataIntegrityError(
                f"{where} holds {len(row)} columns, not {len(METRICS_COLUMNS)}; a row with "
                f"an extra or missing column means the layout moved upstream, and reading "
                f"the first three fields of eight would keep working while meaning "
                f"something else"
            )

        event_time_utc = _parse_sample_instant(row[_CREATE_TIME], where=where, now_utc=now_utc)
        file_symbol = _require_one_symbol(row[_SYMBOL], seen=file_symbol, where=where)
        open_interest_base_quantity = _parse_open_interest(row[_OPEN_INTEREST], where=where)

        if previous_event_time_utc is not None:
            _require_declared_spacing(
                event_time_utc, previous_event_time_utc=previous_event_time_utc, where=where
            )
        previous_event_time_utc = event_time_utc
        observations.append(
            AltObservation(
                event_time_utc=event_time_utc, observed_value=open_interest_base_quantity
            )
        )
    return tuple(observations)


def _require_declared_encoding(archive_format: ArchiveFormat, *, source: str) -> None:
    """The caller resolved a format; this checks it is the one this parser can read.

    Without it, a caller passing the kline declaration by mistake would have this parser
    read a millisecond epoch as a datetime string -- which raises on the first row, but
    with a message about a malformed timestamp rather than about the wrong declaration.
    """
    if archive_format.timestamp_encoding is not TimestampEncoding.NAIVE_UTC_DATETIME:
        raise DataIntegrityError(
            f"{source} was handed the format for market="
            f"{archive_format.market.value!r} dataset={archive_format.dataset.value!r}, "
            f"which stamps {archive_format.timestamp_encoding.value!r}. The metrics parser "
            f"reads {TimestampEncoding.NAIVE_UTC_DATETIME.value!r} and nothing else"
        )


def _parse_sample_instant(raw: str, *, where: str, now_utc: datetime) -> datetime:
    try:
        return parse_naive_utc_datetime(raw, now_utc=now_utc)
    except DataIntegrityError as unreadable:
        raise DataIntegrityError(f"{where}: create_time={raw!r} -- {unreadable}") from unreadable


def _require_one_symbol(raw: str, *, seen: str | None, where: str) -> str:
    """Every row names the same instrument, or the file is a merge of two series.

    The expected symbol is not a parameter: the parser protocol takes bytes and nothing
    that identifies the series, deliberately, so that a parser cannot quietly filter a
    payload down to what the caller asked for. What it *can* prove is internal
    consistency, and a `metrics` file holding two symbols would otherwise be written into
    one series under whichever id the caller supplied.
    """
    if not raw:
        raise DataIntegrityError(f"{where}: symbol is empty")
    if seen is not None and raw != seen:
        raise DataIntegrityError(
            f"{where}: symbol={raw!r} but earlier rows carry {seen!r}. One archive member "
            f"covers one instrument; a file holding two would be written into a single "
            f"series under the caller's id, silently interleaving them"
        )
    return raw


def _require_declared_spacing(
    event_time_utc: datetime, *, previous_event_time_utc: datetime, where: str
) -> None:
    if event_time_utc <= previous_event_time_utc:
        raise DataIntegrityError(
            f"{where} is stamped {event_time_utc.isoformat()}, not after the previous row's "
            f"{previous_event_time_utc.isoformat()}. Out-of-order or duplicate samples mean "
            f"a merged or re-generated file; the store keys on (series, event_time, "
            f"available_at), so a duplicate would silently collapse two samples into one"
        )
    spacing = event_time_utc - previous_event_time_utc
    if spacing != _SAMPLING_PERIOD:
        raise DataIntegrityError(
            f"{where} is {spacing} after the previous row, but "
            f"{BINANCE_OPEN_INTEREST.source_id} declares a sampling period of "
            f"{_SAMPLING_PERIOD}. For this source the sampling period *is* the "
            f"availability lag -- the value stamped at T is knowable once T's period "
            f"closes -- so a different spacing makes every available_at_utc derived from "
            f"this file wrong, in the look-ahead direction when the real period is longer. "
            f"Update the declared cadence rather than the comparison"
        )


def _parse_open_interest(raw: str, *, where: str) -> Decimal:
    """The summed open interest as an exact `Decimal`, in base quantity.

    Never via `float`: `Decimal(76608.798)` carries the double's rounding error before this
    code runs, and widening the type afterwards cannot undo it
    (`.claude/rules/decimal-and-money.md`).

    Non-negative is the only magnitude claim made. An upper bound would have to be
    per-instrument -- 76,608 is a plausible BTC figure and an absurdly small DOGE one -- so
    a single ceiling loose enough to admit DOGE would not catch a decimal point that moved
    in BTC, and would read as a check while checking nothing.
    """
    if not _DECIMAL_TOKEN.match(raw):
        raise DataIntegrityError(
            f"{where}: {OPEN_INTEREST_COLUMN}={raw!r} is not plain decimal notation"
        )
    try:
        parsed = Decimal(raw)
    except InvalidOperation as invalid:  # pragma: no cover - the pattern admits nothing else
        raise DataIntegrityError(
            f"{where}: {OPEN_INTEREST_COLUMN}={raw!r} is not a decimal"
        ) from invalid
    if not parsed.is_finite():  # pragma: no cover - the pattern excludes NaN and Infinity
        raise DataIntegrityError(f"{where}: {OPEN_INTEREST_COLUMN}={raw!r} is not finite")
    if parsed < 0:
        raise DataIntegrityError(
            f"{where}: {OPEN_INTEREST_COLUMN}={raw!r} is negative. Open interest is a count "
            f"of contracts outstanding and cannot be, so this is a sign or a column that "
            f"moved, not a market condition"
        )
    return parsed
