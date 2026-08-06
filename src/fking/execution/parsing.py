"""The one place money enters this system, and it enters from text.

`ccxt` decodes JSON with the standard library's decoder, which turns
`"quantity": "0.00050000"` into the float `0.0005` before any of our code runs. That
value is not repairable afterwards: `Decimal(0.0005)` is
`Decimal('0.000500000000000000010408340855980918...')`, and widening the type does not
undo a rounding the parser already performed. So the adapter never reads ccxt's parsed
structure -- it re-parses the recorded response *body* with `parse_float=Decimal`, which
hands `Decimal` the venue's original characters.

The second half of this module is classification. An exchange response is hostile input:
`response["orderId"]` is a bug, because an error envelope has no `orderId` at all and
the `KeyError` surfaces three frames away from the rejection that caused it. Every
payload passes `raise_for_venue_error` before a model sees it, so a rejection becomes a
typed refusal at the boundary rather than a missing key somewhere downstream.

Unknown codes classify as **permanent**. Defaulting to transient would retry a novel
failure five times with backoff on every request forever, converting an unknown
condition into a self-inflicted outage.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Final

from fking.execution._errors import (
    ExchangeError,
    PermanentExchangeError,
    TransientExchangeError,
)

__all__ = [
    "RETRYABLE_HTTP_STATUSES",
    "RETRYABLE_VENUE_CODES",
    "classify_venue_failure",
    "parse_venue_payload",
    "raise_for_venue_error",
]

# 418 is deliberately absent. Binance returns it for an IP ban after repeated 429s, and
# retrying into a ban extends it -- the correct response is a hard stop, not a backoff.
RETRYABLE_HTTP_STATUSES: Final[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})

# Binance error codes whose documented meaning is "the same request may succeed".
# Source: Binance spot API error-code reference, checked 2026-08-05.
RETRYABLE_VENUE_CODES: Final[frozenset[int]] = frozenset(
    {
        -1001,  # DISCONNECTED: internal error, request not processed
        -1003,  # TOO_MANY_REQUESTS: rate limit; back off and retry
        -1007,  # TIMEOUT: send status unknown. Safe to retry only because
        #         clientOrderId is derived deterministically, so a submission that
        #         did land is recognised by the venue rather than duplicated.
        -1016,  # SERVICE_SHUTTING_DOWN
        -1099,  # NOT_FOUND: transient internal routing failure, not a missing order
    }
)

# -1021 is deliberately *not* retryable. It means the request timestamp fell outside
# recvWindow, which is a statement about our clock rather than about the venue; retrying
# re-signs against the same wrong clock and fails identically, and the fix is a drift
# check, never a wider window.


def parse_venue_payload(body: str) -> object:
    """Decode a venue response body without ever materialising a float.

    `json`'s `parse_float` hook receives the original source substring rather than a
    parsed double, so every `Decimal` is constructed from the exact characters the venue
    sent. Returns `object` on purpose: the shape is a venue's claim, and the caller
    validates it into a model rather than indexing it.
    """
    return json.loads(body, parse_float=Decimal)


def _venue_error_fields(payload: object) -> tuple[int, str] | None:
    """Extract `(code, msg)` if this payload is a Binance error envelope.

    Binance error codes are negative integers; a success payload with a `code` field
    exists (the futures order response has none, but nothing forbids one arriving), so
    the sign is checked rather than the key's presence alone.
    """
    if not isinstance(payload, Mapping):
        return None
    code = payload.get("code")
    message = payload.get("msg")
    if not isinstance(code, int) or isinstance(code, bool) or code >= 0:
        return None
    return code, message if isinstance(message, str) else ""


def classify_venue_failure(
    *, venue_id: str, http_status: int | None, venue_code: int | None, message: str
) -> ExchangeError:
    """Map a venue failure onto the taxonomy. Classification happens here, once.

    In the adapter rather than at the call site, because this is where the venue's
    vocabulary is understood -- a call site that classified `-1021` would be guessing at
    a number it has no way to look up.
    """
    retryable = (http_status in RETRYABLE_HTTP_STATUSES) or (venue_code in RETRYABLE_VENUE_CODES)
    failure_type = TransientExchangeError if retryable else PermanentExchangeError
    return failure_type(
        message or f"{venue_id} failed with http_status={http_status} code={venue_code}",
        venue_id=venue_id,
        http_status=http_status,
        venue_code=venue_code,
    )


def raise_for_venue_error(
    payload: object, *, venue_id: str, http_status: int | None = None
) -> object:
    """Return `payload`, or raise the classified failure it describes.

    Called before every model validation, so a rejection never reaches a model that
    would report it as a missing field.
    """
    fields = _venue_error_fields(payload)
    if fields is None:
        return payload
    venue_code, message = fields
    raise classify_venue_failure(
        venue_id=venue_id,
        http_status=http_status,
        venue_code=venue_code,
        message=f"{venue_id} rejected the request: [{venue_code}] {message}",
    )
