"""The venue's public kline endpoint, for gap repair and for nothing else.

`DATA_PIPELINE.md` section 2 puts REST in one row of the source table with one purpose:
**gap backfill only.** It is not a second ingestion path and not a substitute for the
archives. Its 1000-bar page size and its request weights make a full-history walk a slow
way to fetch data that already exists as a checksummed archive, and the two views would
then have to be reconciled against each other forever.

Three parsing decisions, each with an obvious wrong answer:

**The page is parsed positionally with an exact field count.** Binance returns klines as
an array of twelve-element arrays with no keys, so there is nothing to validate against by
name -- which makes the length check the entire schema. Accepting eleven elements would
mean a column was removed upstream and every field after it silently shifted by one; that
is a change worth failing on, not one to tolerate.

**Decimals come from the response text, never from a parsed number.** `json.loads` is
given `parse_float=Decimal` so that a JSON number cannot reach a price as a float, *and*
each decimal field is still required to be a string -- because Binance serialises them as
strings precisely so they survive a parser without `Decimal` support, and one arriving as
a number is a contract change rather than something to paper over.

**Timestamps are milliseconds on both venues, on every date.** The microsecond cutover in
`fking.data.format_resolver` is an *archive* fact -- spot CSVs switched on 2025-01-01
while neither the stream nor the REST endpoint did -- so reusing the archive's resolver
here would make a live repair's unit a function of the calendar. `epoch_to_utc` still
range-checks the result, which is what turns a wrong unit into a loud failure at the
boundary rather than a bar filed in 1970.

**The fetcher never invents a bar it did not receive.** A page that returns fewer bars
than the window spans ends the walk; the caller narrows its gap by exactly what arrived.
A partially backfilled gap recorded as closed is worse than an open one, because the
coverage report then tells `backtest` it may run.

No `httpx` import: the client comes from `fking.platform.safety.guarded_client`, which is
the only sanctioned constructor in this process and validates the host on every request
after URL merging. An `import-linter` contract forbids `fking.data` from importing a
transport directly.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import TracebackType
from typing import Final, Protocol

from fking.data.format_resolver import EpochUnit, epoch_to_utc
from fking.data.loaders.records import KlineRecord
from fking.platform.errors import DataIntegrityError, DataUnavailableError
from fking.platform.logging import get_logger
from fking.platform.safety import guarded_client

__all__ = [
    "KLINE_FIELD_COUNT",
    "MAX_KLINES_PER_PAGE",
    "REST_EPOCH_UNIT",
    "GuardedKlineRest",
    "KlineRestSource",
    "parse_kline_page",
]

_LOG: Final = get_logger(__name__)

# Both venues emit milliseconds on the REST kline endpoint, on every date. See the module
# docstring for why this is not `format_resolver`'s (market, date) resolver.
REST_EPOCH_UNIT: Final[EpochUnit] = EpochUnit.MILLISECONDS

# Binance's documented maximum for `limit` on both `/api/v3/klines` and `/fapi/v1/klines`.
# Asking for more is rejected outright rather than truncated.
MAX_KLINES_PER_PAGE: Final[int] = 1000

# openTime, open, high, low, close, volume, closeTime, quoteVolume, tradeCount,
# takerBuyBaseVolume, takerBuyQuoteVolume, ignore.
KLINE_FIELD_COUNT: Final[int] = 12

_HTTP_OK: Final[int] = 200

# The trailing always-zero element, kept so that the record's field count is the
# response's field count. `""` rather than the element's own text would lose that.
_IGNORED_FIELD_INDEX: Final[int] = 11

_DECIMAL_INDICES: Final[tuple[int, ...]] = (1, 2, 3, 4, 5, 7, 9, 10)
_EPOCH_INDICES: Final[tuple[int, ...]] = (0, 6)
_TRADE_COUNT_INDEX: Final[int] = 8


class KlineRestSource(Protocol):
    """The kline endpoint, as the gap backfill sees it.

    A Protocol so that a test can replay recorded venue rows without a socket, which is
    the seam `.claude/rules/testing-rules.md` requires -- and so that the backfiller
    itself contains no transport code and can be reasoned about as a pure walk over
    windows. It is not a switch: `GuardedKlineRest` is the only implementation in
    `src/fking`.
    """

    async def klines(
        self,
        *,
        symbol: str,
        interval: str,
        from_utc: datetime,
        until_utc: datetime,
        now_utc: datetime,
    ) -> tuple[KlineRecord, ...]:
        """Closed bars whose open time lies in `[from_utc, until_utc)`, oldest first.

        May return fewer than the window spans. That is an ordinary answer -- the venue
        does not hold every minute either -- and the caller must treat the remainder as
        still missing rather than as filled.
        """


def parse_kline_page(body: str, *, now_utc: datetime) -> tuple[KlineRecord, ...]:
    """One `/klines` response body to canonical records.

    Args:
        body: The raw response text. Parsed here rather than by the transport so that
            `parse_float=Decimal` is applied to the venue's own characters.
        now_utc: The ingestion instant, used only as the plausibility reference for epoch
            normalisation. A parameter rather than a clock read, so replaying a recorded
            response years later normalises the same integers the same way.

    Raises:
        DataIntegrityError: the body is not a JSON array of twelve-element arrays, a
            decimal field is not a string, or a timestamp normalises outside the
            plausible range.
    """
    try:
        document: object = json.loads(body, parse_float=Decimal)
    except json.JSONDecodeError as malformed:
        raise DataIntegrityError(f"kline response is not JSON: {body[:200]!r}") from malformed

    if not isinstance(document, list):
        raise DataIntegrityError(
            f"kline response is a {type(document).__name__}, not a JSON array. An error "
            f"envelope arrives as an object and must not be read as an empty page: "
            f"{body[:200]!r}"
        )
    return tuple(
        _parse_row(row, index=index, now_utc=now_utc) for index, row in enumerate(document)
    )


def _parse_row(row: object, *, index: int, now_utc: datetime) -> KlineRecord:
    if not isinstance(row, list):
        raise DataIntegrityError(
            f"kline row {index} is a {type(row).__name__}, not an array; the endpoint "
            f"returns positional arrays and there is nothing to key on if it does not"
        )
    if len(row) != KLINE_FIELD_COUNT:
        raise DataIntegrityError(
            f"kline row {index} has {len(row)} fields, expected {KLINE_FIELD_COUNT}. The "
            f"array is positional, so a changed field count shifts every value after the "
            f"change and no individual field looks wrong"
        )

    epochs = {
        position: _epoch(row[position], index=index, position=position)
        for position in _EPOCH_INDICES
    }
    decimals = {
        position: _decimal(row[position], index=index, position=position)
        for position in _DECIMAL_INDICES
    }
    return KlineRecord(
        open_time_utc=epoch_to_utc(epochs[0], unit=REST_EPOCH_UNIT, now_utc=now_utc),
        close_time_utc=epoch_to_utc(epochs[6], unit=REST_EPOCH_UNIT, now_utc=now_utc),
        open_quote_price=decimals[1],
        high_quote_price=decimals[2],
        low_quote_price=decimals[3],
        close_quote_price=decimals[4],
        base_volume=decimals[5],
        quote_volume=decimals[7],
        trade_count=_trade_count(row[_TRADE_COUNT_INDEX], index=index),
        taker_buy_base_volume=decimals[9],
        taker_buy_quote_volume=decimals[10],
        ignored_field=str(row[_IGNORED_FIELD_INDEX]),
    )


def _decimal(raw_field: object, *, index: int, position: int) -> Decimal:
    if not isinstance(raw_field, str):
        raise DataIntegrityError(
            f"kline row {index} field {position} is a {type(raw_field).__name__}, not a "
            f"string-encoded decimal. Binance sends these as strings so they survive a "
            f"parser with no Decimal support; a number here has already lost precision"
        )
    try:
        return Decimal(raw_field)
    except InvalidOperation as malformed:
        raise DataIntegrityError(
            f"kline row {index} field {position} is {raw_field!r}, which is not a decimal"
        ) from malformed


def _epoch(raw_field: object, *, index: int, position: int) -> int:
    # `bool` first: it is a subclass of `int`, and a JSON `true` reaching a timestamp
    # would normalise to 1970-01-01T00:00:00.001Z rather than failing.
    if isinstance(raw_field, bool) or not isinstance(raw_field, int):
        raise DataIntegrityError(
            f"kline row {index} field {position} is {raw_field!r}, not an integer epoch"
        )
    return raw_field


def _trade_count(raw_field: object, *, index: int) -> int:
    if isinstance(raw_field, bool) or not isinstance(raw_field, int):
        raise DataIntegrityError(
            f"kline row {index} field {_TRADE_COUNT_INDEX} is {raw_field!r}, not an "
            f"integer trade count"
        )
    if raw_field < 0:
        raise DataIntegrityError(f"kline row {index} reports {raw_field} trades")
    return raw_field


class GuardedKlineRest:
    """`KlineRestSource` over `guarded_client()`, paging until the window is covered.

    Owns one client for its lifetime and is an async context manager: a client per page
    loses connection reuse against an endpoint a repair hits once per missing thousand
    minutes.
    """

    __slots__ = ("_bar_interval", "_client", "_klines_path", "_max_pages", "_request_count")

    def __init__(
        self,
        *,
        base_url: str,
        klines_path: str,
        bar_interval: timedelta,
        max_pages: int = 8,
        timeout_seconds: float = 10.0,
    ) -> None:
        if max_pages < 1:
            raise ValueError(f"max_pages must be at least one, got {max_pages}")
        # Validated here as well as per-request, so a profile pointing somewhere it should
        # not fails at construction rather than on the first repair at 03:00.
        self._client = guarded_client(base_url=base_url, timeout_seconds=timeout_seconds)
        self._klines_path = klines_path
        self._bar_interval = bar_interval
        # 8000 bars per call, which is five and a half days of one-minute data. A bound
        # rather than an unbounded walk because the caller's gap comes from a registry
        # that can hold an arbitrarily old hole, and a repair that silently turns into a
        # full-history download through REST is the thing DATA_PIPELINE.md section 2
        # forbids. Hitting the cap narrows the gap and leaves the rest recorded.
        self._max_pages = max_pages
        self._request_count = 0

    @property
    def request_count(self) -> int:
        """Requests issued since construction.

        Exposed because "the walk stopped early" is otherwise unfalsifiable: a fetch that
        paged eight times and one that paged once return the same shape.
        """
        return self._request_count

    async def __aenter__(self) -> GuardedKlineRest:
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def klines(
        self,
        *,
        symbol: str,
        interval: str,
        from_utc: datetime,
        until_utc: datetime,
        now_utc: datetime,
    ) -> tuple[KlineRecord, ...]:
        collected: list[KlineRecord] = []
        cursor = from_utc
        for page in range(self._max_pages):
            if cursor >= until_utc:
                break
            rows = await self._page(
                symbol=symbol,
                interval=interval,
                from_utc=cursor,
                until_utc=until_utc,
                now_utc=now_utc,
            )
            if not rows:
                break
            collected.extend(rows)
            cursor = rows[-1].open_time_utc + self._bar_interval
            if len(rows) < MAX_KLINES_PER_PAGE:
                # A short page means the venue has no more bars in this window. Asking
                # again would return the same short page forever.
                break
            if page == self._max_pages - 1:
                _LOG.warning(
                    "backfill.rest_page_cap_reached",
                    symbol=symbol,
                    interval=interval,
                    pages=self._max_pages,
                    stopped_at_utc=cursor.isoformat(),
                    requested_until_utc=until_utc.isoformat(),
                )
        return tuple(record for record in collected if from_utc <= record.open_time_utc < until_utc)

    async def _page(
        self,
        *,
        symbol: str,
        interval: str,
        from_utc: datetime,
        until_utc: datetime,
        now_utc: datetime,
    ) -> Sequence[KlineRecord]:
        self._request_count += 1
        response = await self._client.get(
            self._klines_path,
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": _to_epoch_ms(from_utc),
                # Inclusive on the venue's side, and the window is half-open on ours, so
                # the last millisecond before `until_utc` is the last one asked for.
                "endTime": _to_epoch_ms(until_utc) - 1,
                "limit": MAX_KLINES_PER_PAGE,
            },
        )
        if response.status_code != _HTTP_OK:
            raise DataUnavailableError(
                f"GET {self._klines_path} for {symbol} {interval} over "
                f"[{from_utc.isoformat()}, {until_utc.isoformat()}) returned HTTP "
                f"{response.status_code}: {response.text[:200]!r}. The gap stays open; a "
                f"repair that cannot reach the venue has recovered nothing"
            )
        return parse_kline_page(response.text, now_utc=now_utc)


def _to_epoch_ms(moment: datetime) -> int:
    """An aware UTC instant as integer milliseconds.

    `//` rather than `int()` on a float: the endpoint takes an integer parameter, and a
    float intermediate at millisecond resolution is a rounding step in the one value that
    decides which bars come back.
    """
    if moment.tzinfo is None or moment.utcoffset() != UTC.utcoffset(None):
        raise DataIntegrityError(
            f"kline window bound {moment!r} is not timezone-aware UTC; a naive bound is "
            f"sent to the venue as though it were UTC and returns the wrong minutes"
        )
    return (int(moment.timestamp()) * 1000) + (moment.microsecond // 1000)
