"""Stream frames to canonical records. The venue's JSON is hostile input.

Three rules from `.claude/rules/exchange-integration.md` are load-bearing here and each
has an obvious wrong answer:

**Parse into a model, never index into the dict.** `payload["k"]["c"]` raises `KeyError`
on an error envelope and, worse, succeeds against a frame whose meaning changed. A
model with declared types fails at the boundary and names the field.

**`extra="ignore"`, not `extra="forbid"`.** Binance adds fields to market-data frames
without notice -- `B` appeared on kline frames years after the stream shipped -- and
breaking the ingester on a field we do not read is a self-inflicted outage. The
asymmetry with LLM output, which is `extra="forbid"`, is deliberate: an extra field from
a venue is normal, and an extra field from a model means the schema was not respected.

**Every decimal arrives as a JSON string and a JSON number is a contract change.**
Binance serialises prices and quantities as strings precisely so they survive a parser
with no `Decimal` support. If one arrives as a number, `json.loads` has already turned
it into a float and the precision is gone before this module sees it -- so the honest
response is to refuse, not to `Decimal(str(value))` over the damage.

The output types are `KlineRecord` and `TradeRecord`, the same records the archive
loaders produce. That is the point of the module: `DATA_PIPELINE.md` section 5 opens
with "live and historical land in the same canonical schema", and a live-only record
type would be a second shape for one fact, diverging on the first field either side
added.

`ignored_field` on a live bar is `""` rather than the archive's `"0"`. The stream has no
such column, and inventing the archive's filler value would make a live bar
indistinguishable from an archived one in the one field whose only job is to show that
the source's column count was what we expected.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from fking.data.format_resolver import EpochUnit, epoch_to_utc
from fking.data.loaders.records import KlineRecord, TradeRecord
from fking.platform.errors import DataIntegrityError

__all__ = [
    "STREAM_EPOCH_UNIT",
    "AggTradeFrame",
    "BookTickerFrame",
    "KlineFrame",
    "LiveFrame",
    "MarkPriceFrame",
    "parse_frame",
]

# Both the spot and the futures WebSocket feeds emit milliseconds, on every date. The
# microsecond cutover in `format_resolver` is an *archive* fact -- spot CSVs switched on
# 2025-01-01 while the stream did not -- and reusing the archive's resolver here would
# make a live frame's unit a function of the calendar. Verified against the recordings
# in tests/fixtures/streams/, whose event times are 13 digits.
STREAM_EPOCH_UNIT: Final[EpochUnit] = EpochUnit.MILLISECONDS

# The stream has no equivalent of the archive's trailing always-zero column.
_NO_IGNORED_FIELD: Final[str] = ""


def _decimal_from_venue_string(raw_field: object) -> Decimal:
    if not isinstance(raw_field, str):
        # ValueError, not the TypeError ruff's TRY004 would prefer: Pydantic v2 converts
        # ValueError and AssertionError raised inside a validator into a
        # ValidationError, and lets a TypeError escape as itself. A TypeError here would
        # leave the boundary as an unhandled crash instead of the named field failure
        # `parse_frame` reports.
        raise ValueError(  # noqa: TRY004
            f"expected a string-encoded decimal, got {type(raw_field).__name__} "
            f"{raw_field!r}; a JSON number has already lost precision in the parser"
        )
    try:
        return Decimal(raw_field)
    except InvalidOperation as malformed:
        raise ValueError(f"{raw_field!r} is not a decimal") from malformed


VenueDecimal = Annotated[Decimal, BeforeValidator(_decimal_from_venue_string)]


class _VenueModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class _KlinePayload(_VenueModel):
    """The `k` object inside a kline frame."""

    open_time_ms: int = Field(alias="t")
    close_time_ms: int = Field(alias="T")
    symbol: str = Field(alias="s")
    interval: str = Field(alias="i")
    open_quote_price: VenueDecimal = Field(alias="o")
    close_quote_price: VenueDecimal = Field(alias="c")
    high_quote_price: VenueDecimal = Field(alias="h")
    low_quote_price: VenueDecimal = Field(alias="l")
    base_volume: VenueDecimal = Field(alias="v")
    trade_count: int = Field(alias="n")
    is_closed: bool = Field(alias="x")
    quote_volume: VenueDecimal = Field(alias="q")
    taker_buy_base_volume: VenueDecimal = Field(alias="V")
    taker_buy_quote_volume: VenueDecimal = Field(alias="Q")


class KlineFrame(_VenueModel):
    """A `<symbol>@kline_1m` frame, open or closed.

    Open frames are parsed rather than discarded at the JSON level so that the staleness
    monitor can see them (`DATA_PIPELINE.md` section 5). `to_record` is what refuses to
    turn one into a bar, which keeps "we saw the venue is alive" and "we have a bar"
    as two separate answers rather than one flag.
    """

    event_type: Literal["kline"] = Field(alias="e")
    event_time_ms: int = Field(alias="E")
    symbol: str = Field(alias="s")
    kline: _KlinePayload = Field(alias="k")

    @property
    def is_closed(self) -> bool:
        return self.kline.is_closed

    def to_record(self, *, now_utc: datetime) -> KlineRecord:
        """The canonical bar, or `DataIntegrityError` if this frame is still open.

        Raising rather than returning `None`: a caller that reaches this method has
        already decided the frame is a bar, and a `None` there would be checked at
        three call sites and forgotten at the fourth -- which persists a partial
        aggregate, the one failure this whole section exists to prevent.
        """
        if not self.kline.is_closed:
            raise DataIntegrityError(
                f"{self.symbol} {self.kline.interval} kline opening at "
                f"{self.kline.open_time_ms} is still open (k.x is false); an open kline "
                f"is a partial aggregate that will change, and storing one turns replay "
                f"into a lie"
            )
        return KlineRecord(
            open_time_utc=epoch_to_utc(
                self.kline.open_time_ms, unit=STREAM_EPOCH_UNIT, now_utc=now_utc
            ),
            close_time_utc=epoch_to_utc(
                self.kline.close_time_ms, unit=STREAM_EPOCH_UNIT, now_utc=now_utc
            ),
            open_quote_price=self.kline.open_quote_price,
            high_quote_price=self.kline.high_quote_price,
            low_quote_price=self.kline.low_quote_price,
            close_quote_price=self.kline.close_quote_price,
            base_volume=self.kline.base_volume,
            quote_volume=self.kline.quote_volume,
            trade_count=self.kline.trade_count,
            taker_buy_base_volume=self.kline.taker_buy_base_volume,
            taker_buy_quote_volume=self.kline.taker_buy_quote_volume,
            ignored_field=_NO_IGNORED_FIELD,
        )


class AggTradeFrame(_VenueModel):
    """A `<symbol>@aggTrade` frame.

    `aggregate_trade_id` stays an `int` here and becomes a string in the record. It is
    an integer *for the sequence detector*, which subtracts two of them to size a gap,
    and an identifier everywhere else -- and `TradeRecord.venue_trade_id` is a string
    because it is the seam key a REST backfill joins on, where the value that must match
    is the one the venue sent.
    """

    event_type: Literal["aggTrade"] = Field(alias="e")
    event_time_ms: int = Field(alias="E")
    symbol: str = Field(alias="s")
    aggregate_trade_id: int = Field(alias="a")
    quote_price: VenueDecimal = Field(alias="p")
    base_quantity: VenueDecimal = Field(alias="q")
    first_trade_id: int = Field(alias="f")
    last_trade_id: int = Field(alias="l")
    trade_time_ms: int = Field(alias="T")
    is_buyer_maker: bool = Field(alias="m")

    def to_record(self, *, now_utc: datetime) -> TradeRecord:
        """The canonical trade print.

        Keyed on `T` (trade time), not `E` (event time). `E` is when the matching engine
        published the frame and moves with the venue's own latency; `T` is when the
        trade happened, and it is the field the archive files. Keying on `E` would make
        a live print and its archived twin land in different minutes.

        `is_best_match` is `True` because the aggTrade stream carries no such flag and
        an aggregate print is by construction the best match for its own range. The
        archive's `aggTrades` dataset does not carry the column either.
        """
        return TradeRecord(
            venue_trade_id=str(self.aggregate_trade_id),
            event_time_utc=epoch_to_utc(
                self.trade_time_ms, unit=STREAM_EPOCH_UNIT, now_utc=now_utc
            ),
            quote_price=self.quote_price,
            base_quantity=self.base_quantity,
            quote_quantity=self.quote_price * self.base_quantity,
            is_buyer_maker=self.is_buyer_maker,
            is_best_match=True,
        )


class BookTickerFrame(_VenueModel):
    """A `<symbol>@bookTicker` frame: futures top of book.

    Not persisted to the corpus. It feeds the live staleness monitor and the execution
    path's view of the spread, both of which read the newest value and never a history.

    Subscribed on futures only. The spot venue serves a `@bookTicker` stream whose
    frames carry no `e` discriminator at all, so `parse_frame` could not route one --
    which is why `LiveStreamProfile` does not offer it there rather than this model
    accommodating a shape the session never asks for.
    """

    event_type: Literal["bookTicker"] = Field(alias="e")
    event_time_ms: int = Field(alias="E")
    symbol: str = Field(alias="s")
    best_bid_quote_price: VenueDecimal = Field(alias="b")
    best_bid_base_quantity: VenueDecimal = Field(alias="B")
    best_ask_quote_price: VenueDecimal = Field(alias="a")
    best_ask_base_quantity: VenueDecimal = Field(alias="A")


class MarkPriceFrame(_VenueModel):
    """A `<symbol>@markPrice@1s` frame: futures mark price and funding.

    `funding_rate_fraction` is a fraction, not a percentage and not basis points --
    `0.0001` is one basis point per funding interval. Named for the unit because
    `.claude/rules/naming.md` exists for exactly this field: a `funding_rate` read as
    percent by one caller and as a fraction by another is a 100x error in a number that
    is subtracted from PnL every eight hours.
    """

    event_type: Literal["markPriceUpdate"] = Field(alias="e")
    event_time_ms: int = Field(alias="E")
    symbol: str = Field(alias="s")
    mark_quote_price: VenueDecimal = Field(alias="p")
    index_quote_price: VenueDecimal = Field(alias="i")
    funding_rate_fraction: VenueDecimal = Field(alias="r")
    next_funding_time_ms: int = Field(alias="T")


LiveFrame = KlineFrame | AggTradeFrame | BookTickerFrame | MarkPriceFrame
"""Every frame shape a subscribed stream can produce."""

# Keyed on the venue's own `e` discriminator rather than on the `stream` envelope field,
# because the envelope is absent when a session subscribes to a single stream and the
# discriminator is present either way.
_FRAME_MODELS: Final[dict[str, type[BaseModel]]] = {
    "kline": KlineFrame,
    "aggTrade": AggTradeFrame,
    "bookTicker": BookTickerFrame,
    "markPriceUpdate": MarkPriceFrame,
}


def parse_frame(raw: str) -> LiveFrame:
    """One raw stream frame to a typed model, or `DataIntegrityError`.

    Unwraps the combined-stream envelope (`{"stream": ..., "data": {...}}`) when it is
    present. The envelope's `stream` field is deliberately discarded: it is the
    subscription name we asked for, and routing on it rather than on the payload's own
    `e` would trust our request over the venue's answer.

    An unrecognised event type is refused rather than ignored. A frame this system does
    not understand arriving on a stream it subscribed to means the subscription set and
    the parser disagree, and silently dropping it is how a feed goes quiet in one
    dataset while the process reports health.

    Raises:
        DataIntegrityError: the frame is not JSON, carries no usable event type, or
            fails its model's validation.
    """
    try:
        document: Any = json.loads(raw)
    except json.JSONDecodeError as malformed:
        raise DataIntegrityError(f"stream frame is not JSON: {raw[:200]!r}") from malformed

    if not isinstance(document, dict):
        raise DataIntegrityError(
            f"stream frame is a {type(document).__name__}, not a JSON object: {raw[:200]!r}"
        )
    payload = document.get("data", document)
    if not isinstance(payload, dict):
        raise DataIntegrityError(f"stream frame carries no payload object: {raw[:200]!r}")

    event_type = payload.get("e")
    model = _FRAME_MODELS.get(event_type) if isinstance(event_type, str) else None
    if model is None:
        raise DataIntegrityError(
            f"stream frame declares event type {event_type!r}, which this session did "
            f"not subscribe to; known types are {sorted(_FRAME_MODELS)}"
        )

    try:
        parsed = model.model_validate(payload)
    except ValidationError as invalid:
        raise DataIntegrityError(
            f"{event_type} frame failed validation: {invalid.errors(include_url=False)}"
        ) from invalid
    # The registry maps each discriminator to exactly one member of the union, and the
    # mapping is asserted exhaustive in tests/data/test_live_frames.py.
    return parsed  # type: ignore[return-value]
