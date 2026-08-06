"""Deriving the venue-side idempotency key.

`Order.client_order_id` is held by the domain and derived here, because the derivation
needs the venue's charset and length limits and `fking.domain` imports nothing but the
standard library.

Deterministic, never random and never a counter. Binance rejects a duplicate
`newClientOrderId` while the original order is live, so a submission retried after a
timeout -- the case where the venue may or may not have received the first attempt -- is
recognised by the venue as the same order rather than filled twice. A `uuid4()` here
would be a fresh key per attempt, which is the same as having no key at all.

The quantity is quantized onto the venue's lattice *before* it is hashed.
`Decimal("0.100")` and `Decimal("0.1")` are the same order to the venue, and an id that
distinguished them would let one order be placed twice by two callers that formatted it
differently.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fking.domain import Instrument, OrderType, Side, TimeInForce
from fking.execution._errors import VenueProfileError
from fking.execution.venue_profile import VenueProfile

__all__ = ["assert_client_order_id_acceptable", "derive_client_order_id"]


# PLR0913: the parameters are the decision's identity, one for one. Bundling them into
# an intent object would mean constructing a second object to construct the first, and
# every field here is load-bearing -- dropping one makes two different decisions share an
# id, which is a duplicate order rather than an untidy signature.
def derive_client_order_id(  # noqa: PLR0913
    *,
    profile: VenueProfile,
    correlation_id: UUID,
    strategy_id: str,
    instrument: Instrument,
    side: Side,
    base_quantity: Decimal,
    order_type: OrderType,
    time_in_force: TimeInForce,
    decided_at_utc: datetime,
) -> str:
    """Return the deterministic client order id for exactly this decision."""
    quantized = instrument.quantize_base_quantity(base_quantity)
    material = "|".join(
        (
            str(correlation_id),
            strategy_id,
            instrument.symbol,
            side.value,
            format(quantized, "f"),
            order_type.value,
            time_in_force.value,
            decided_at_utc.isoformat(),
        )
    )
    # blake2b with an explicit digest size rather than a truncated sha256: the length is
    # a parameter of the function instead of a slice somebody can widen, and the budget
    # is tight -- Binance caps newClientOrderId at 36 characters and the prefix spends
    # three of them.
    room = profile.client_order_id_max_len - len(profile.client_order_id_prefix)
    digest_size = min(20, room // 2)
    digest = hashlib.blake2b(material.encode("utf-8"), digest_size=digest_size).hexdigest()
    return assert_client_order_id_acceptable(
        f"{profile.client_order_id_prefix}{digest}", profile=profile
    )


def assert_client_order_id_acceptable(client_order_id: str, *, profile: VenueProfile) -> str:
    """Return `client_order_id`, or refuse it before the venue does.

    A locally-detected rejection costs a stack trace; the same rejection from the venue
    costs a round trip in the order path and arrives as a `-1100` naming a parameter
    rather than the id.
    """
    if len(client_order_id) > profile.client_order_id_max_len:
        raise VenueProfileError(
            f"client order id {client_order_id!r} is {len(client_order_id)} characters "
            f"and {profile.venue_id} accepts {profile.client_order_id_max_len}"
        )
    outside = sorted(set(client_order_id) - set(profile.client_order_id_charset))
    if outside:
        raise VenueProfileError(
            f"client order id {client_order_id!r} contains {outside}, which "
            f"{profile.venue_id} does not accept"
        )
    return client_order_id
