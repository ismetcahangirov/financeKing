"""The spot trades row parser. Where trap 3 lives.

Verified against the recorded fragment `tests/fixtures/archives/spot/trades/BTCUSDT/`,
dated 2025-01-02 -- seven columns, no header, microsecond epochs, and:

    4361451942,94591.78000000,0.00015000,14.18876700,1735776000113701,True,True
    4361451944,94591.79000000,0.00092000,87.02444680,1735776000539055,False,True

`True` and `False`, Python-style. Not `true`/`false`, not `1`/`0`. A `value == "true"`
comparison, a `json.loads`, or any parser treating an unrecognised token as falsy returns
`False` for **every row in the file**. Row counts stay right, prices stay right, volumes
stay right, and only the trade side is wrong -- uniformly, on every trade. An order-flow
imbalance feature built on that is not noisy, it is sign-inverted, and it backtests
beautifully in one direction. F-005; `DATA_PIPELINE.md` section 3.

So the encoding is read off the declared format and the token table is exact and
case-sensitive, with no default branch. An unrecognised token is the only evidence that an
upstream encoding has drifted, and the rejection-fraction gate is what turns that evidence
into a refusal.

There is no header row on any spot trades archive, so `TRADE_COLUMNS` is never compared
against a file's own header -- it names the layout for a reader and supplies the field
count. That is why the names here are this file's own rather than Binance's: there is no
header to disagree with.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from fking.data.loaders._fields import (
    RowRejected,
    parse_boolean,
    parse_epoch,
    parse_positive_decimal,
    require_field_count,
)
from fking.data.loaders.outcome import RejectionReason
from fking.data.loaders.records import TradeRecord
from fking.data.loaders.spec import IngestionSpec
from fking.platform.errors import DataIntegrityError

__all__ = ["TRADE_COLUMNS", "parse_trade_row"]

TRADE_COLUMNS: Final[tuple[str, ...]] = (
    "trade_id",
    "price",
    "qty",
    "quote_qty",
    "time",
    "is_buyer_maker",
    "is_best_match",
)

_TRADE_ID: Final[int] = 0
_PRICE: Final[int] = 1
_BASE_QUANTITY: Final[int] = 2
_QUOTE_QUANTITY: Final[int] = 3
_EVENT_TIME: Final[int] = 4
_IS_BUYER_MAKER: Final[int] = 5
_IS_BEST_MATCH: Final[int] = 6


def parse_trade_row(row: Sequence[str], spec: IngestionSpec) -> TradeRecord:
    """One CSV row to one `TradeRecord`, or a `RowRejected` carrying the reason.

    Raises:
        RowRejected: any field is malformed, or a boolean token is not defined by the
            declared encoding.
        DataIntegrityError: the declared format names no boolean encoding for a dataset
            that has boolean columns. That is a file-level fault, not a row fault: it means
            the declaration is incomplete, and every row would fail identically.
    """
    require_field_count(row, expected=len(TRADE_COLUMNS))
    encoding = spec.archive_format.boolean_encoding
    if encoding is None:
        raise DataIntegrityError(
            f"the declared format for {spec.archive_format.market.value}/"
            f"{spec.archive_format.dataset.value} names no boolean_encoding, but the dataset "
            f"has boolean columns. Declaring one requires reading a real archive rather than "
            f"assuming the neighbouring dataset's encoding -- that assumption is trap 3"
        )

    trade_id = row[_TRADE_ID]
    if not trade_id.strip():
        # Kept as a string rather than parsed to an int, so the emptiness has to be
        # checked explicitly: `int("")` raises, but a blank id that survived would become
        # the join key a backfill seam reconciles on, and every seam would match nothing.
        raise RowRejected(RejectionReason.IDENTIFIER_BLANK, "trade_id is blank")

    return TradeRecord(
        venue_trade_id=trade_id,
        event_time_utc=parse_epoch(
            row[_EVENT_TIME],
            column="time",
            unit=spec.archive_format.require_epoch_unit(),
            now_utc=spec.now_utc,
        ),
        quote_price=parse_positive_decimal(row[_PRICE], column="price"),
        base_quantity=parse_positive_decimal(row[_BASE_QUANTITY], column="qty"),
        quote_quantity=parse_positive_decimal(row[_QUOTE_QUANTITY], column="quote_qty"),
        is_buyer_maker=parse_boolean(
            row[_IS_BUYER_MAKER], column="is_buyer_maker", encoding=encoding
        ),
        is_best_match=parse_boolean(row[_IS_BEST_MATCH], column="is_best_match", encoding=encoding),
    )
