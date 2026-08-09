"""The venue's failure vocabulary.

Leaves of `FkingError` rather than siblings of it, so a handler meaning "any failure
this codebase raises on purpose" catches them. They live here rather than in
`fking.platform.errors` for the reason `docs/rules/module-boundaries.md` gives:
`platform` holds mechanism and gets no trading vocabulary, and a class describing a
venue's rejection of an order is vocabulary that belongs to `execution`.

The split that carries weight is transient versus permanent, and it is a *classification*
rather than a description. Retry acts on it, so getting it wrong is not cosmetic:
retrying a bad signature or a filter rejection burns the rate-limit budget and delays the
real failure by the whole backoff schedule, while treating a socket reset as permanent
stops a system that would have recovered on the next attempt.

`UniverseUnavailableError` is neither. It is a startup condition -- a requested symbol
that the venue does not list -- and no number of attempts changes it.
"""

from __future__ import annotations

from fking.platform.errors import FkingError

__all__ = [
    "ExchangeError",
    "PermanentExchangeError",
    "TransientExchangeError",
    "UniverseUnavailableError",
    "VenueProfileError",
]


class ExchangeError(FkingError):
    """The venue rejected or failed a request.

    Carries the venue's own vocabulary -- HTTP status and the numeric code Binance
    returns in its error envelope -- because that is what a runbook and an audit row are
    written against. Mapping them into a house taxonomy and discarding the original is
    how an incident investigation loses the one identifier the venue's documentation is
    indexed by.
    """

    def __init__(
        self,
        message: str,
        *,
        venue_id: str,
        http_status: int | None,
        venue_code: int | None,
    ) -> None:
        super().__init__(message)
        self.venue_id = venue_id
        self.http_status = http_status
        self.venue_code = venue_code


class TransientExchangeError(ExchangeError):
    """Retryable: timeout, 5xx, rate limit, connection reset.

    "Retryable" means the identical request may succeed, which is only safe because
    order submission carries a deterministic `clientOrderId` -- a retried placement that
    already succeeded is recognised by the venue rather than duplicated.
    """


class PermanentExchangeError(ExchangeError):
    """Not retryable: bad signature, unknown symbol, filter rejection, insufficient balance.

    Also the default for a code this system has never seen. Defaulting to transient
    would mean a novel failure is retried with backoff on every request forever, turning
    an unknown condition into a self-inflicted outage; defaulting to permanent surfaces
    it immediately, and promoting a code afterwards is a one-line change with a comment
    naming where the code came from.
    """


class UniverseUnavailableError(FkingError):
    """A requested symbol is not in the venue's tradable set.

    Fatal at startup rather than skipped. Spot testnet is missing 79 symbols that exist
    on spot production and futures testnet is missing 189 (verified 2026-08-01), so a
    strategy validated on production archives and pointed at a symbol testnet does not
    list produces zero fills -- and zero fills score as "no edge", which retires a good
    strategy for an infrastructure reason.
    """


class VenueProfileError(FkingError):
    """A venue profile is internally inconsistent.

    Distinct from `SafetyViolation`, which a profile naming a non-allowlisted host
    raises instead: that one is a statement about where the process may connect and has
    no handler anywhere. This one is a statement about a profile's own fields --
    a `listenKey` keepalive declared for a venue that authenticates its socket, a
    `clientOrderId` prefix that does not fit the venue's charset -- and it is a
    programming error to be fixed, not a safety event.
    """
