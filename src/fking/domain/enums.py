"""The closed vocabularies. Every one of these is a `StrEnum`.

`StrEnum` rather than `Enum` because these values cross a wire in both directions --
into JSON payloads, audit rows, log fields and stream keys -- and a plain `Enum`
serialises as `Direction.LONG` unless every call site remembers `.value`. One that
forgets writes a row that no query will ever match again, and the audit log is
append-only, so it stays wrong.

`Literal` unions were the alternative and were rejected: a `Literal` cannot carry the
behaviour that `Side.signed_multiplier` needs, and reversing a side then becomes a
free function that some caller will reimplement inline with the sign flipped.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final

_LONG_MULTIPLIER: Final = Decimal("1")
_SHORT_MULTIPLIER: Final = Decimal("-1")


class Venue(StrEnum):
    """A trading venue.

    Every member is a testnet. There is no production member, and adding one would be
    a change to the demo-only guarantee rather than an addition to an enum -- the
    compiled-in host allowlist in `fking.platform.safety` is the mechanism, and this
    type is not a second place to widen it. See `.claude/rules/safety-kernel.md`.
    """

    BINANCE_SPOT_TESTNET = "binance-spot-testnet"
    BINANCE_FUTURES_TESTNET = "binance-futures-testnet"
    BYBIT_TESTNET = "bybit-testnet"


class Direction(StrEnum):
    """Which way a belief or a position points.

    Distinct from `Side`: a `Side` is what an order does to the book, a `Direction` is
    what a position or a thesis is. Selling can open a short or close a long, and
    collapsing the two types is how that distinction gets lost.
    """

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class Side(StrEnum):
    """The side of an order or a fill."""

    BUY = "buy"
    SELL = "sell"

    @property
    def signed_multiplier(self) -> Decimal:
        """`+1` for a buy, `-1` for a sell.

        `Decimal` rather than `int` so it can multiply a quantity without the caller
        reaching for `Decimal(...)` at the call site -- which is where somebody
        eventually writes `Decimal(-1.0)`.
        """
        return _LONG_MULTIPLIER if self is Side.BUY else _SHORT_MULTIPLIER


class OrderType(StrEnum):
    """How the venue should price the order.

    Only the two types this system actually sends. Stop and take-profit orders are
    absent because the risk engine holds the invalidation level and acts on it
    (`RISK_PHILOSOPHY.md`); a resting stop at the venue is a decision the venue makes
    on our behalf while the kill switch is not looking.
    """

    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(StrEnum):
    """How long the venue should keep the order working."""

    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class RiskVerdict(StrEnum):
    """The risk engine's answer to a signal.

    `REJECTED` is an audited decision, not the absence of one. A signal the risk engine
    refused must leave the same kind of record as one it approved, or the audit log
    cannot answer why no order exists.
    """

    APPROVED = "approved"
    REJECTED = "rejected"
