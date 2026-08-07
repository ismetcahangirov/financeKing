"""Venues, the order management system and reconciliation. Knows about order types
and venue protocols.

Exchange state is the source of truth; local state converges to it. Every response
crossing this boundary is hostile input -- parsed into a typed model, never indexed
into optimistically.

What exists today is the seam every order, query and stream crosses: the
`ExecutionVenue` and `UserDataSource` interfaces, the `VenueProfile` carrying everything
that differs between spot, futures and the fallback venue, the Binance adapter over a
guarded `ccxt` transport, and the models that turn a venue's response *text* into values
whose prices are `Decimal`. The two user-data stream implementations (#62, #63), the OMS
and the reconciler hang off these.

Two properties here are structural rather than conventional, and both are `import-linter`
contracts rather than review items:

- **This package constructs neither a network client nor a `ccxt` exchange.** The only
  way it obtains one is `fking.platform.safety.guarded_ccxt`, whose transport validates
  the resolved host on *every* request -- including requests ccxt issues internally,
  which no call-site check could reach. `set_sandbox_mode(True)` is applied and is not
  the safety mechanism: it is a convenience supplied by a dependency, and dependencies
  change base URLs in minor bumps.
- **Money enters the system here, and it enters from text.** `GuardedExchange.call`
  returns the venue's raw body rather than ccxt's parsed structure, because ccxt decodes
  JSON with the standard library's float parser and a value that has been through a
  float is not repairable by widening the type afterwards.

Everything not in `__all__` is private and may change without notice.
"""

from fking.execution._errors import (
    ExchangeError,
    PermanentExchangeError,
    TransientExchangeError,
    UniverseUnavailableError,
    VenueProfileError,
)
from fking.execution.binance import BinanceVenue, open_binance_venue
from fking.execution.models import (
    SymbolFilters,
    VenueBalance,
    VenueExchangeInfo,
    VenueOrder,
    VenuePosition,
    VenueSymbol,
    VenueSymbolFilter,
    VenueTrade,
    venue_epoch_to_utc,
)
from fking.execution.order_id import assert_client_order_id_acceptable, derive_client_order_id
from fking.execution.parsing import (
    RETRYABLE_HTTP_STATUSES,
    RETRYABLE_VENUE_CODES,
    classify_venue_failure,
    parse_venue_payload,
    raise_for_venue_error,
)
from fking.execution.symbols import SymbolClassification, classify_symbol, tradable_symbols
from fking.execution.universe import (
    IntersectionBaseline,
    IntersectionDrift,
    SymbolUniverse,
    check_intersection_drift,
    resolve_universe,
)
from fking.execution.venue import ExecutionVenue, UserDataSource
from fking.execution.venue_profile import (
    BINANCE_FUTURES_TESTNET,
    BINANCE_SPOT_TESTNET,
    BYBIT_TESTNET,
    VENUE_PROFILES,
    UserDataMechanism,
    VenueProfile,
)

__all__: tuple[str, ...] = (
    "BINANCE_FUTURES_TESTNET",
    "BINANCE_SPOT_TESTNET",
    "BYBIT_TESTNET",
    "RETRYABLE_HTTP_STATUSES",
    "RETRYABLE_VENUE_CODES",
    "VENUE_PROFILES",
    "BinanceVenue",
    "ExchangeError",
    "ExecutionVenue",
    "IntersectionBaseline",
    "IntersectionDrift",
    "PermanentExchangeError",
    "SymbolClassification",
    "SymbolFilters",
    "SymbolUniverse",
    "TransientExchangeError",
    "UniverseUnavailableError",
    "UserDataMechanism",
    "UserDataSource",
    "VenueBalance",
    "VenueExchangeInfo",
    "VenueOrder",
    "VenuePosition",
    "VenueProfile",
    "VenueProfileError",
    "VenueSymbol",
    "VenueSymbolFilter",
    "VenueTrade",
    "assert_client_order_id_acceptable",
    "check_intersection_drift",
    "classify_symbol",
    "classify_venue_failure",
    "derive_client_order_id",
    "open_binance_venue",
    "parse_venue_payload",
    "raise_for_venue_error",
    "resolve_universe",
    "tradable_symbols",
    "venue_epoch_to_utc",
)
