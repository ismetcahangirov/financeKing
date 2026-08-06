"""The single seam through which every order, query and stream reaches a venue.

One interface, so that the backtest engine and the demo account differ by which object
is passed and by nothing else. If a strategy could behave differently across that seam,
every backtest result would be unfalsifiable (CLAUDE.md section 2), which is why the
seam is a Protocol with a stated shape rather than a base class somebody can widen from
inside one implementation.

`UserDataSource` is separate from `ExecutionVenue` and has four methods rather than two
because spot and futures have **opposite session lifetimes**, and the interface has to be
able to express both:

- Spot authenticates the *socket* with an Ed25519 `session.logon` -- `POST
  /api/v3/userDataStream` is 410 Gone -- so `refresh()` is a no-op, the session dies
  with the connection, and `reconnect()` re-runs the handshake and then reconciles,
  because the gap between drop and resubscribe is a hole in the event stream.
- Futures authenticates with a `listenKey`, a server-side resource that outlives the
  socket and expires on a missed keepalive. `refresh()` is a `PUT`, and `reconnect()`
  reuses the existing key rather than minting one.

Collapsing those into "connect, then subscribe with a flag" leaks a futures key on every
spot reconnect, or -- worse, because it is silent -- lets the keepalive die with the
socket's task group and stops fills arriving with no error anywhere.

The implementations land in #62 and #63. What this module owes them is an interface that
does not make either mechanism awkward, which is why `refresh` exists at all: an
interface with only `connect`/`reconnect` would force the futures keepalive to live
inside whatever owns the socket, which is the exact bug.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Protocol, runtime_checkable

from fking.domain import Order
from fking.execution.models import (
    VenueBalance,
    VenueExchangeInfo,
    VenueOrder,
    VenuePosition,
    VenueTrade,
)
from fking.execution.venue_profile import VenueProfile

__all__ = ["ExecutionVenue", "UserDataSource"]


@runtime_checkable
class ExecutionVenue(Protocol):
    """Every request this system makes of a venue.

    Read methods return the venue's own view, parsed but not reconciled. That is
    deliberate: exchange state is the source of truth and local state converges to it
    (ARCHITECTURE.md section 7), so a method that merged the two would hide the
    divergence a reconciler exists to find.
    """

    @property
    def profile(self) -> VenueProfile:
        """The venue's compiled configuration. Everything venue-varying lives here."""

    async def exchange_info(self) -> VenueExchangeInfo:
        """Symbol universe and per-symbol filters, as the venue states them now."""

    async def fetch_balances(self) -> tuple[VenueBalance, ...]:
        """Every asset balance, free and locked separately."""

    async def fetch_positions(self) -> tuple[VenuePosition, ...]:
        """Open positions. Empty on a spot venue, which has none."""

    async def fetch_open_orders(self, *, symbol: str | None = None) -> tuple[VenueOrder, ...]:
        """Orders the venue still considers live. The reconciler's primary input."""

    async def fetch_my_trades(
        self, *, symbol: str, since_ms: int | None = None
    ) -> tuple[VenueTrade, ...]:
        """Fills, deduplicated downstream on the venue's own trade id."""

    async def submit(self, order: Order) -> VenueOrder:
        """Place `order`, using its `client_order_id` as the venue-side idempotency key."""

    async def cancel(self, *, symbol: str, client_order_id: str) -> VenueOrder:
        """Cancel by client order id, never by the venue's id.

        The client id is ours and is derivable from the decision; the venue's id is only
        knowable from a response we may never have received, which is exactly the case a
        cancel after a timeout has to handle.
        """

    async def cancel_replace(
        self, *, replacement: Order, cancel_client_order_id: str
    ) -> VenueOrder:
        """Atomically cancel one order and place another in its place."""

    async def aclose(self) -> None:
        """Release the transport. Safe to call more than once."""


@runtime_checkable
class UserDataSource(Protocol):
    """Fill and balance events. Two mechanisms, one interface -- see the module docstring."""

    @property
    def profile(self) -> VenueProfile:
        """The venue's compiled configuration."""

    async def connect(self) -> None:
        """Establish the authenticated stream. Idempotent."""

    async def refresh(self) -> None:
        """Renew the server-side credential, if the venue has one.

        A no-op where authentication is bound to the socket. Present so that a caller
        can own the keepalive timer *outside* the socket's lifetime, which is what stops
        a reconnect from silently cancelling it.
        """

    async def reconnect(self) -> None:
        """Re-establish after a drop, re-authenticating if the venue requires it."""

    def events(self) -> AsyncIterator[Mapping[str, object]]:
        """Decoded user-data events, in arrival order.

        Consumers are idempotent by design: a reconnect replays, and delivery is
        at-least-once (CLAUDE.md section 2).
        """

    async def aclose(self) -> None:
        """Release the socket and any keepalive it owns."""
