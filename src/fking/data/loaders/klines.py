"""The kline row parser. One layout, two markets, two epoch units, two header conventions.

Spot and USDⓈ-M futures kline archives share a column layout exactly -- verified against
the recorded fragments under `tests/fixtures/archives/`, both dated 2025-01-02:

    spot     1735776000000000,94591.78000000,...,0     no header, microseconds
    futures  open_time,open,high,low,close,...         header,    milliseconds
             1735776000000,94580.90,...,0

What differs is everything the format resolver declares and nothing this file decides. The
column names below are the futures header verbatim; they are compared against the file's
own header when one is declared, so an upstream rename or reorder is a refusal rather than
a silent remapping.

`ignore` is Binance's trailing always-zero column. It is parsed as an opaque string and
kept, so the field count is the file's field count -- see `KlineRecord.ignored_field`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from fking.data.loaders._fields import (
    RowRejected,
    parse_epoch,
    parse_non_negative_decimal,
    parse_non_negative_int,
    parse_positive_decimal,
    require_field_count,
)
from fking.data.loaders.outcome import RejectionReason
from fking.data.loaders.records import KlineRecord
from fking.data.loaders.spec import IngestionSpec

__all__ = ["KLINE_COLUMNS", "parse_kline_row"]

# The USDⓈ-M futures kline header, verbatim. `count` and `taker_buy_volume` are Binance's
# names; the record renames them to `trade_count`, `taker_buy_base_volume` and so on,
# because a bare `count` is ambiguous once three kinds of count exist in one system
# (.claude/rules/naming.md). The translation belongs at this boundary, which is the only
# place both spellings are simultaneously correct.
KLINE_COLUMNS: Final[tuple[str, ...]] = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
)

_OPEN_TIME: Final[int] = 0
_OPEN: Final[int] = 1
_HIGH: Final[int] = 2
_LOW: Final[int] = 3
_CLOSE: Final[int] = 4
_VOLUME: Final[int] = 5
_CLOSE_TIME: Final[int] = 6
_QUOTE_VOLUME: Final[int] = 7
_TRADE_COUNT: Final[int] = 8
_TAKER_BUY_BASE: Final[int] = 9
_TAKER_BUY_QUOTE: Final[int] = 10
_IGNORE: Final[int] = 11


def parse_kline_row(row: Sequence[str], spec: IngestionSpec) -> KlineRecord:
    """One CSV row to one `KlineRecord`, or a `RowRejected` carrying the reason.

    The OHLC bracket check is the cheapest data-quality gate in the system and it fires on
    real corruption rather than on a hypothetical: a mis-declared epoch unit puts a 2026
    bar beside a 1970 one, and whatever merges them produces a high below its own open.
    Checked here rather than left to a later gate because the row is the only place the
    raw fields are still available to name in the rejection.
    """
    require_field_count(row, expected=len(KLINE_COLUMNS))
    unit = spec.archive_format.epoch_unit
    now_utc = spec.now_utc

    open_time_utc = parse_epoch(row[_OPEN_TIME], column="open_time", unit=unit, now_utc=now_utc)
    close_time_utc = parse_epoch(row[_CLOSE_TIME], column="close_time", unit=unit, now_utc=now_utc)
    if close_time_utc <= open_time_utc:
        raise RowRejected(
            RejectionReason.INTERVAL_NOT_FORWARD,
            f"close_time {close_time_utc.isoformat()} does not follow "
            f"open_time {open_time_utc.isoformat()}",
        )

    open_quote_price = parse_positive_decimal(row[_OPEN], column="open")
    high_quote_price = parse_positive_decimal(row[_HIGH], column="high")
    low_quote_price = parse_positive_decimal(row[_LOW], column="low")
    close_quote_price = parse_positive_decimal(row[_CLOSE], column="close")
    extremes = (open_quote_price, close_quote_price)
    if high_quote_price < max(extremes) or low_quote_price > min(extremes):
        raise RowRejected(
            RejectionReason.OHLC_NOT_BRACKETING,
            f"high {high_quote_price} / low {low_quote_price} do not bracket "
            f"open {open_quote_price} and close {close_quote_price}",
        )

    return KlineRecord(
        open_time_utc=open_time_utc,
        close_time_utc=close_time_utc,
        open_quote_price=open_quote_price,
        high_quote_price=high_quote_price,
        low_quote_price=low_quote_price,
        close_quote_price=close_quote_price,
        base_volume=parse_non_negative_decimal(row[_VOLUME], column="volume"),
        quote_volume=parse_non_negative_decimal(row[_QUOTE_VOLUME], column="quote_volume"),
        trade_count=parse_non_negative_int(row[_TRADE_COUNT], column="count"),
        taker_buy_base_volume=parse_non_negative_decimal(
            row[_TAKER_BUY_BASE], column="taker_buy_volume"
        ),
        taker_buy_quote_volume=parse_non_negative_decimal(
            row[_TAKER_BUY_QUOTE], column="taker_buy_quote_volume"
        ),
        ignored_field=row[_IGNORE],
    )
