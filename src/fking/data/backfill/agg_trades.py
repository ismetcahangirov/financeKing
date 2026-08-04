"""The venue's public `aggTrades` endpoint, paged on `fromId` and on nothing else.

The kline repair next door walks a time window because a bar *is* a time window. A trade
tape is not, and paging this endpoint on time does not merely perform worse -- it cannot
address the gap at all. A `sequence` gap between two prints that share a millisecond has
one-millisecond bounds, and `startTime`/`endTime` have no finer resolution than the prints
they would have to separate. The aggregate trade id does: `a` is a monotone integer the
venue assigns, contiguous per symbol, so "the four prints we did not receive" is a range of
integers and the endpoint takes exactly that range as `fromId` plus `limit`.

Four parsing decisions, each with an obvious wrong answer:

**Rows are JSON objects here, not the positional arrays `/klines` returns.** So the field
count is not the schema and the keys are; a missing key is refused by name rather than
detected as a length change.

**Decimals come from the response text, never from a parsed number.** `json.loads` is given
`parse_float=Decimal` so a JSON number cannot reach a price as a float, *and* each decimal
field is still required to be a string -- Binance serialises them as strings precisely so
they survive a parser with no `Decimal` support, and one arriving as a number is a contract
change rather than something to paper over.

**Timestamps are milliseconds on both venues, on every date.** The microsecond cutover in
`fking.data.format_resolver` is an *archive* fact -- spot CSVs switched on 2025-01-01 while
neither the stream nor REST did -- so reusing the archive's resolver here would make a live
repair's unit a function of the calendar.

**`is_best_match` is set `True` and the response's `M` is not read.** Spot returns the field
and USDⓈ-M futures does not, so reading it would make the record shape depend on the venue;
and the stream sets it `True` by construction (`AggTradeFrame.to_record`), so a record built
from REST has to agree with the one built from the socket or the seam would escalate on a
field neither source really observed. It is outside `TRADE_SEAM_COMPARED_FIELDS` for that
reason.

No `httpx` import: the client comes from `fking.platform.safety.guarded_client`, which
validates the host on every request after URL merging. An `import-linter` contract forbids
`fking.data` from importing a transport directly.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import TracebackType
from typing import Final, Protocol

from fking.data.backfill.rest import REST_EPOCH_UNIT
from fking.data.format_resolver import epoch_to_utc
from fking.data.loaders.records import TradeRecord
from fking.platform.errors import DataIntegrityError, DataUnavailableError
from fking.platform.logging import get_logger
from fking.platform.safety import guarded_client

__all__ = [
    "MAX_AGG_TRADES_PER_PAGE",
    "AggTradeRestSource",
    "GuardedAggTradeRest",
    "parse_agg_trade_page",
]

_LOG: Final = get_logger(__name__)

# Binance's documented maximum for `limit` on `/api/v3/aggTrades` and `/fapi/v1/aggTrades`.
# Asking for more is rejected outright rather than truncated.
MAX_AGG_TRADES_PER_PAGE: Final[int] = 1000

_HTTP_OK: Final[int] = 200

# The keys this parser needs. `M` is deliberately absent; see the module docstring.
_AGGREGATE_ID_KEY: Final[str] = "a"
_TRADE_TIME_KEY: Final[str] = "T"
_QUOTE_PRICE_KEY: Final[str] = "p"
_BASE_QUANTITY_KEY: Final[str] = "q"
_BUYER_MAKER_KEY: Final[str] = "m"


class AggTradeRestSource(Protocol):
    """The `aggTrades` endpoint, as a trade-gap repair sees it.

    A Protocol so a test can replay recorded prints without a socket, and so the repair
    itself contains no transport code and can be reasoned about as a walk over id ranges.
    It is not a switch: `GuardedAggTradeRest` is the only implementation in `src/fking`.
    """

    async def agg_trades(
        self, *, symbol: str, from_id: int, print_count: int, now_utc: datetime
    ) -> tuple[TradeRecord, ...]:
        """Up to `print_count` prints whose aggregate id starts at `from_id`, oldest first.

        May return fewer. That is an ordinary answer -- the venue's retention on this
        endpoint is finite -- and the caller must treat the remainder as still missing
        rather than as filled.
        """


def parse_agg_trade_page(body: str, *, now_utc: datetime) -> tuple[TradeRecord, ...]:
    """One `/aggTrades` response body to canonical prints.

    Args:
        body: The raw response text. Parsed here rather than by the transport so that
            `parse_float=Decimal` is applied to the venue's own characters.
        now_utc: The ingestion instant, used only as the plausibility reference for epoch
            normalisation. A parameter rather than a clock read, so replaying a recorded
            response years later normalises the same integers the same way.

    Raises:
        DataIntegrityError: the body is not a JSON array of objects, a key is missing, a
            decimal field is not a string, or a timestamp normalises outside the
            plausible range.
    """
    try:
        document: object = json.loads(body, parse_float=Decimal)
    except json.JSONDecodeError as malformed:
        raise DataIntegrityError(f"aggTrades response is not JSON: {body[:200]!r}") from malformed

    if not isinstance(document, list):
        raise DataIntegrityError(
            f"aggTrades response is a {type(document).__name__}, not a JSON array. An error "
            f"envelope arrives as an object and must not be read as an empty page: "
            f"{body[:200]!r}"
        )
    return tuple(
        _parse_print(row, index=index, now_utc=now_utc) for index, row in enumerate(document)
    )


def _parse_print(row: object, *, index: int, now_utc: datetime) -> TradeRecord:
    if not isinstance(row, dict):
        raise DataIntegrityError(
            f"aggTrades row {index} is a {type(row).__name__}, not an object; the endpoint "
            f"returns keyed objects and there is nothing to read positionally if it does not"
        )
    quote_price = _decimal(row, _QUOTE_PRICE_KEY, index=index)
    base_quantity = _decimal(row, _BASE_QUANTITY_KEY, index=index)
    return TradeRecord(
        # A string in the record because it is an identifier and the seam joins on the
        # value the venue sent, and an int on the wire because the venue sends one.
        venue_trade_id=str(_integer(row, _AGGREGATE_ID_KEY, index=index)),
        event_time_utc=epoch_to_utc(
            _integer(row, _TRADE_TIME_KEY, index=index), unit=REST_EPOCH_UNIT, now_utc=now_utc
        ),
        quote_price=quote_price,
        base_quantity=base_quantity,
        # Derived, not read: the endpoint files no quote quantity for an aggregate print,
        # and `AggTradeFrame.to_record` derives it the same way from the same two fields.
        # Deriving it differently on the two sides is how one print becomes a seam
        # disagreement about a column neither source sent.
        quote_quantity=quote_price * base_quantity,
        is_buyer_maker=_boolean(row, _BUYER_MAKER_KEY, index=index),
        is_best_match=True,
    )


def _field(row: dict[str, object], key: str, *, index: int) -> object:
    if key not in row:
        raise DataIntegrityError(
            f"aggTrades row {index} carries no {key!r} key; present keys are "
            f"{sorted(row)}. The response is keyed, so a renamed field is a contract "
            f"change rather than a shifted column"
        )
    return row[key]


def _decimal(row: dict[str, object], key: str, *, index: int) -> Decimal:
    raw_field = _field(row, key, index=index)
    if not isinstance(raw_field, str):
        raise DataIntegrityError(
            f"aggTrades row {index} field {key!r} is a {type(raw_field).__name__}, not a "
            f"string-encoded decimal. Binance sends these as strings so they survive a "
            f"parser with no Decimal support; a number here has already lost precision"
        )
    try:
        return Decimal(raw_field)
    except InvalidOperation as malformed:
        raise DataIntegrityError(
            f"aggTrades row {index} field {key!r} is {raw_field!r}, which is not a decimal"
        ) from malformed


def _integer(row: dict[str, object], key: str, *, index: int) -> int:
    raw_field = _field(row, key, index=index)
    # `bool` first: it is a subclass of `int`, and a JSON `true` reaching a trade time
    # would normalise to 1970-01-01T00:00:00.001Z rather than failing.
    if isinstance(raw_field, bool) or not isinstance(raw_field, int):
        raise DataIntegrityError(
            f"aggTrades row {index} field {key!r} is {raw_field!r}, not an integer"
        )
    return raw_field


def _boolean(row: dict[str, object], key: str, *, index: int) -> bool:
    raw_field = _field(row, key, index=index)
    if not isinstance(raw_field, bool):
        raise DataIntegrityError(
            f"aggTrades row {index} field {key!r} is {raw_field!r}, not a JSON boolean. "
            f"`m` is the aggressor side inverted, and a truthy string read as a flag "
            f"would leave every other column of the print correct"
        )
    return raw_field


class GuardedAggTradeRest:
    """`AggTradeRestSource` over `guarded_client()`, paging on `fromId`.

    Owns one client for its lifetime and is an async context manager: a client per page
    loses connection reuse against an endpoint a repair hits once per missing thousand
    prints.
    """

    __slots__ = ("_agg_trades_path", "_client", "_max_pages", "_request_count")

    def __init__(
        self,
        *,
        base_url: str,
        agg_trades_path: str,
        max_pages: int = 8,
        timeout_seconds: float = 10.0,
    ) -> None:
        if max_pages < 1:
            raise ValueError(f"max_pages must be at least one, got {max_pages}")
        # Validated here as well as per-request, so a profile pointing somewhere it should
        # not fails at construction rather than on the first repair at 03:00.
        self._client = guarded_client(base_url=base_url, timeout_seconds=timeout_seconds)
        self._agg_trades_path = agg_trades_path
        # 8000 prints per call. A bound rather than an unbounded walk because the caller's
        # gap comes from a registry that can hold an arbitrarily large hole, and a repair
        # that silently turns into a full-history download through REST is what
        # DATA_PIPELINE.md section 2 forbids. Hitting the cap narrows the gap and leaves
        # the rest recorded.
        self._max_pages = max_pages
        self._request_count = 0

    @property
    def request_count(self) -> int:
        """Requests issued since construction.

        Exposed because "the walk stopped early" is otherwise unfalsifiable: a fetch that
        paged eight times and one that paged once return the same shape.
        """
        return self._request_count

    async def __aenter__(self) -> GuardedAggTradeRest:
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

    async def agg_trades(
        self, *, symbol: str, from_id: int, print_count: int, now_utc: datetime
    ) -> tuple[TradeRecord, ...]:
        if from_id < 0:
            raise DataIntegrityError(
                f"aggregate trade ids are non-negative; a repair asked for {from_id}, which "
                f"means the id it derived from the corpus is not the venue's"
            )
        collected: list[TradeRecord] = []
        cursor = from_id
        for page in range(self._max_pages):
            remaining = print_count - len(collected)
            if remaining <= 0:
                break
            prints = await self._page(
                symbol=symbol,
                from_id=cursor,
                page_limit=min(remaining, MAX_AGG_TRADES_PER_PAGE),
                now_utc=now_utc,
            )
            if not prints:
                break
            collected.extend(prints)
            # The venue assigns `a` contiguously per symbol, so the next unseen id is one
            # past the last returned. Deriving the cursor from the response rather than
            # from `from_id + page_limit` is what keeps a short page from skipping ids the
            # endpoint did have.
            cursor = int(prints[-1].venue_trade_id) + 1
            if len(prints) < min(remaining, MAX_AGG_TRADES_PER_PAGE):
                # A short page means the venue has nothing further in this range. Asking
                # again would return the same short page forever.
                break
            if page == self._max_pages - 1:
                _LOG.warning(
                    "backfill.agg_trade_page_cap_reached",
                    symbol=symbol,
                    pages=self._max_pages,
                    stopped_at_id=cursor,
                    requested_prints=print_count,
                    collected=len(collected),
                )
        return tuple(collected[:print_count])

    async def _page(
        self, *, symbol: str, from_id: int, page_limit: int, now_utc: datetime
    ) -> Sequence[TradeRecord]:
        self._request_count += 1
        response = await self._client.get(
            self._agg_trades_path,
            params={"symbol": symbol, "fromId": from_id, "limit": page_limit},
        )
        if response.status_code != _HTTP_OK:
            raise DataUnavailableError(
                f"GET {self._agg_trades_path} for {symbol} from id {from_id} returned HTTP "
                f"{response.status_code}: {response.text[:200]!r}. The gap stays open; a "
                f"repair that cannot reach the venue has recovered nothing"
            )
        return parse_agg_trade_page(response.text, now_utc=now_utc)
