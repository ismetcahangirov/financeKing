"""Which streams a venue publishes, and the one URL this system subscribes with.

The table is `DATA_PIPELINE.md` section 5 expressed as data. It is here rather than
inline in the session because the recorder, the supervisor and the fixture-integrity
test all have to agree about which streams a venue serves, and three places computing a
stream name is how a recording ends up proving a parser against a stream the running
system never subscribes to.

**Combined streams (`/stream?streams=a/b`), not one socket per stream.** A socket per
stream multiplies every property in this module by the subscription count: reconnects,
ping deadlines, and -- the one that matters -- the number of independent moments at
which a gap can open. One session means one gap per outage rather than four gaps that a
reader has to recognise as the same outage.

`bookTicker` and `markPrice@1s` are futures-only, and neither is persisted to the
corpus. They exist for the live risk path (mark price drives liquidation distance, and
top-of-book drives the staleness monitor), which is why `persisted_datasets` is a
separate, shorter tuple than `stream_suffixes`: subscribing to a stream and storing it
are different decisions, and collapsing them is how an unpersisted stream silently
acquires a coverage gap nobody can close.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from fking.data.format_resolver import Dataset, Market

__all__ = [
    "LIVE_BAR_INTERVAL",
    "LIVE_STREAM_PROFILES",
    "LiveStreamProfile",
    "combined_stream_url",
    "stream_names_for",
]

# The only interval consumed live. Every longer bar is an aggregate of these, and
# subscribing to `kline_5m` as well would give two sources for one fact that disagree
# at the boundary whenever one of them drops a frame.
LIVE_BAR_INTERVAL: Final[str] = "1m"


@dataclass(frozen=True, slots=True)
class LiveStreamProfile:
    """One venue's live market-data surface.

    `venue_id` matches the `venue` table's `CHECK`, so a gap or a bar recorded by this
    session names the same venue the reference data does. A second spelling here would
    be a join that silently returns nothing.
    """

    venue_id: str
    market: Market
    stream_base_url: str
    # The REST origin the gap backfill repairs an outage from (#28), and the path it asks
    # on. Two fields rather than one URL because spot and futures differ in the path
    # (`/api/v3/klines` against `/fapi/v1/klines`) as well as the host, and a single
    # concatenated string is one that gets built correctly in the profile and incorrectly
    # at the one call site that forgets the leading slash. Both hosts are in
    # `PERMITTED_HOSTS`; the backfill goes through `guarded_client` regardless, so a
    # profile edit cannot reach a production endpoint.
    rest_base_url: str
    klines_path: str
    stream_suffixes: tuple[str, ...]
    persisted_datasets: tuple[Dataset, ...]


SPOT_TESTNET: Final[LiveStreamProfile] = LiveStreamProfile(
    venue_id="binance-spot-testnet",
    market=Market.SPOT,
    stream_base_url="wss://stream.testnet.binance.vision",
    rest_base_url="https://testnet.binance.vision",
    klines_path="/api/v3/klines",
    stream_suffixes=(f"kline_{LIVE_BAR_INTERVAL}", "aggTrade"),
    persisted_datasets=(Dataset.KLINES, Dataset.AGG_TRADES),
)

FUTURES_TESTNET: Final[LiveStreamProfile] = LiveStreamProfile(
    venue_id="binance-futures-testnet",
    market=Market.FUTURES_UM,
    stream_base_url="wss://stream.binancefuture.com",
    rest_base_url="https://testnet.binancefuture.com",
    klines_path="/fapi/v1/klines",
    # markPrice@1s rather than the default 3s cadence: the mark price is what a
    # liquidation is computed against, and a three-second-old mark is three seconds of
    # unmeasured distance to it.
    stream_suffixes=(f"kline_{LIVE_BAR_INTERVAL}", "aggTrade", "bookTicker", "markPrice@1s"),
    persisted_datasets=(Dataset.KLINES, Dataset.AGG_TRADES),
)

LIVE_STREAM_PROFILES: Final[Mapping[str, LiveStreamProfile]] = MappingProxyType(
    {
        SPOT_TESTNET.venue_id: SPOT_TESTNET,
        FUTURES_TESTNET.venue_id: FUTURES_TESTNET,
    }
)


def stream_names_for(profile: LiveStreamProfile, symbol: str) -> tuple[str, ...]:
    """The stream names for one symbol, in the venue's own lowercase spelling.

    Binance rejects an uppercase stream name outright, so the case fold happens here
    once rather than at each call site -- a symbol arrives from the universe resolver
    uppercase and would otherwise be lowercased by whoever remembered to.
    """
    lowered = symbol.lower()
    return tuple(f"{lowered}@{suffix}" for suffix in profile.stream_suffixes)


def combined_stream_url(profile: LiveStreamProfile, symbols: Sequence[str]) -> str:
    """The combined-stream URL for every subscribed stream across `symbols`.

    Sorted, so the same subscription set always produces the same URL. That is not
    cosmetic: the URL is what a recorded fixture's provenance names, and an
    order-dependent URL would make two identical subscriptions look like two different
    recordings.
    """
    if not symbols:
        raise ValueError("a live session with no symbols would subscribe to nothing")
    names = sorted(name for symbol in symbols for name in stream_names_for(profile, symbol))
    return f"{profile.stream_base_url}/stream?streams={'/'.join(names)}"
