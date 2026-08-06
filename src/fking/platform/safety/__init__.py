"""The safety kernel. Knows which hosts this process is permitted to contact.

**This system never trades real money.** Not in development, not in testing, not
"just once to verify", not behind a flag. There is no configuration value,
environment variable, command-line argument or feature flag that enables trading
against a production exchange. Enabling it would require editing
`_allowlist.py` and merging a pull request labelled `safety:critical`.

That friction is not an obstacle to work around. It is the single most important
property of this system. CLAUDE.md section 0.

Everything not listed in `__all__` is private and may change without notice.
"""

from fking.platform.safety._allowlist import PERMITTED_HOSTS
from fking.platform.safety._errors import SafetyViolation
from fking.platform.safety.client import (
    TRANSPORT_ERRORS,
    assert_host_permitted,
    guarded_client,
    guarded_ws_connect,
    verify_endpoints_or_abort,
)
from fking.platform.safety.exchange import (
    GuardedExchange,
    UnknownVenueEndpointError,
    VenueTransportError,
    assert_sandbox_urls_permitted,
    guarded_aiohttp_session,
    guarded_ccxt,
)

# `fking.platform.safety.archive` is deliberately absent: an importer has to name that
# module, which is what makes the contract forbidding `fking.execution` from reaching it
# enforceable. The exchange path is the opposite case -- `fking.execution` is supposed
# to reach it, and is supposed to have no other way of obtaining an exchange object.
__all__ = [
    "PERMITTED_HOSTS",
    "TRANSPORT_ERRORS",
    "GuardedExchange",
    "SafetyViolation",
    "UnknownVenueEndpointError",
    "VenueTransportError",
    "assert_host_permitted",
    "assert_sandbox_urls_permitted",
    "guarded_aiohttp_session",
    "guarded_ccxt",
    "guarded_client",
    "guarded_ws_connect",
    "verify_endpoints_or_abort",
]
