"""Record real Binance testnet responses into the fixture corpus.

Run by hand, never in CI, for the same reason `record_stream_frames.py` is:
`.claude/rules/testing-rules.md` bans hand-written fixtures outright. A hand-written
exchange response encodes what its author believes Binance returns, and the two things
nobody would ever hand-write wrongly are exactly the two that break a venue adapter --
the precise string encoding of a decimal field (`"0.00001000"`, not `1e-5`), and the
deliberate non-ASCII symbols testnet serves in `exchangeInfo`.

Every request goes through `fking.platform.safety.guarded_client`. That is not ceremony:
a recorder is exactly the sort of one-off script that would otherwise carry the first
unguarded client in the tree, and the next thing needing a socket copies the nearest
example.

**No credentials are used or accepted.** Two consequences, both deliberate:

- The public endpoints (`exchangeInfo`, `time`) are recorded as successes.
- The authenticated endpoints are recorded as the venue's own *rejection* envelopes.
  Those are real responses, not synthesised ones, and they are what the adapter's error
  path is tested against -- `{"code":-2014,...}` has no `orderId`, which is the whole
  reason `response["orderId"]` is a bug. Recording their success payloads needs a
  testnet key pair and lands with the user-data streams in #62/#63.

`exchangeInfo` is recorded as a *declared subset*: the full spot response is 2.5 MB and
1,373 symbols, which is not a reviewable diff. The envelope is verbatim and the symbol
array is narrowed to a fixed, stated set -- two liquid pairs plus every non-ASCII symbol
the venue served -- with the full response's digest and symbol count recorded alongside,
so the derivation is checkable rather than asserted. Same pattern as the `headN` archive
fragments under tests/fixtures/archives/.

Usage:

    uv run python tools/record_exchange.py --endpoint exchangeInfo
    uv run python tools/record_exchange.py --endpoint exchangeInfo --venue binance-futures-testnet
    uv run python tools/record_exchange.py --endpoint all
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import yaml

from fking.domain import Venue
from fking.execution import VENUE_PROFILES, VenueProfile, classify_symbol
from fking.platform.safety import guarded_client

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
FIXTURE_ROOT: Final[Path] = REPO_ROOT / "tests" / "fixtures" / "recorded"

# Two liquid pairs so a filter test has a realistic tick and step, plus every non-ASCII
# symbol the venue serves. The Unicode ones are the point of keeping a subset at all: a
# parser that has never seen one is a parser that crashes on a Windows console codepage
# the first time it logs the universe.
_KEEP_SYMBOLS: Final[frozenset[str]] = frozenset({"BTCUSDT", "ETHUSDT"})


class _Endpoint:
    """One recordable request. Named rather than a tuple so the YAML keys have a source."""

    def __init__(self, name: str, method: str, path: str) -> None:
        self.name = name
        self.method = method
        self.path = path


# `name` becomes the fixture directory. The rejection endpoints are recorded unsigned on
# purpose -- see the module docstring.
_ENDPOINTS: Final[Mapping[Venue, tuple[_Endpoint, ...]]] = {
    Venue.BINANCE_SPOT_TESTNET: (
        _Endpoint("exchangeInfo", "GET", "/api/v3/exchangeInfo"),
        _Endpoint("serverTime", "GET", "/api/v3/time"),
        _Endpoint("account_rejected", "GET", "/api/v3/account"),
        _Endpoint("openOrders_rejected", "GET", "/api/v3/openOrders"),
        _Endpoint("myTrades_rejected", "GET", "/api/v3/myTrades"),
        _Endpoint("order_rejected", "POST", "/api/v3/order"),
        _Endpoint("orderCancel_rejected", "DELETE", "/api/v3/order"),
        _Endpoint("orderCancelReplace_rejected", "POST", "/api/v3/order/cancelReplace"),
    ),
    Venue.BINANCE_FUTURES_TESTNET: (
        _Endpoint("exchangeInfo", "GET", "/fapi/v1/exchangeInfo"),
        _Endpoint("serverTime", "GET", "/fapi/v1/time"),
        _Endpoint("balance_rejected", "GET", "/fapi/v3/balance"),
        _Endpoint("positionRisk_rejected", "GET", "/fapi/v3/positionRisk"),
        _Endpoint("openOrders_rejected", "GET", "/fapi/v1/openOrders"),
        _Endpoint("userTrades_rejected", "GET", "/fapi/v1/userTrades"),
        _Endpoint("order_rejected", "POST", "/fapi/v1/order"),
        _Endpoint("orderCancel_rejected", "DELETE", "/fapi/v1/order"),
        _Endpoint("orderAmend_rejected", "PUT", "/fapi/v1/order"),
    ),
}


def sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def narrow_exchange_info(body: str) -> tuple[str, dict[str, object]]:
    """Return a subset body and the derivation record that explains it.

    The envelope, rate limits and every retained symbol are byte-identical to what the
    venue sent, because the subset is produced by dropping array entries and
    re-serialising -- never by editing a field. `separators` reproduces Binance's own
    compact spacing so the retained entries read the same as the source.
    """
    document = json.loads(body)
    symbols = document["symbols"]
    kept = [
        entry
        for entry in symbols
        if entry["symbol"] in _KEEP_SYMBOLS or not classify_symbol(entry["symbol"]).is_tradable
    ]
    narrowed = {**document, "symbols": kept}
    subset_body = json.dumps(narrowed, separators=(",", ":"), ensure_ascii=True)
    derivation = {
        "kind": "symbol_subset",
        "source_body_sha256": sha256_hex(body),
        "source_symbol_count": len(symbols),
        "kept_symbol_count": len(kept),
        "rule": (
            "every symbol in {BTCUSDT, ETHUSDT} plus every symbol classify_symbol() "
            "marks untradable, which is how the deliberate non-ascii symbols are kept"
        ),
    }
    return subset_body, derivation


async def _fetch(profile: VenueProfile, endpoint: _Endpoint) -> tuple[int, str]:
    async with guarded_client(base_url=profile.rest_url, timeout_seconds=30) as client:
        response = await client.request(endpoint.method, endpoint.path)
        return response.status_code, response.text


def record_one(profile: VenueProfile, endpoint: _Endpoint) -> Path:
    http_status, body = asyncio.run(_fetch(profile, endpoint))

    # Testnet returns a CloudFront/nginx 502 from time to time, and its body is HTML.
    # Refusing to record it matters more than it looks: a fixture holding a gateway
    # error still parses as "a response the venue sent", and every test replaying it
    # would then be asserting against an outage rather than against Binance.
    if not body.lstrip().startswith(("{", "[")):
        raise SystemExit(
            f"{endpoint.method} {endpoint.path} returned HTTP {http_status} with a "
            f"non-JSON body ({body[:80]!r}); refusing to record it. Retry when the venue "
            f"is answering."
        )

    derivation: dict[str, object] | None = None
    if endpoint.name == "exchangeInfo":
        body, derivation = narrow_exchange_info(body)

    captured_at = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    document = {
        "_recording": {
            "venue": str(profile.venue_id),
            "endpoint": f"{endpoint.method} {endpoint.path}",
            "request_url": f"{profile.rest_url}{endpoint.path}",
            "http_status": http_status,
            "captured_at_utc": captured_at,
            "body_sha256": sha256_hex(body),
            "derivation": derivation,
        },
        "body": body,
    }

    directory = FIXTURE_ROOT / str(profile.venue_id) / endpoint.name
    directory.mkdir(parents=True, exist_ok=True)
    fixture_path = directory / f"{captured_at}.yaml"
    fixture_path.write_text(
        yaml.safe_dump(document, sort_keys=True, width=1_000_000_000, allow_unicode=False),
        encoding="utf-8",
    )
    return fixture_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--venue",
        default=str(Venue.BINANCE_SPOT_TESTNET),
        choices=sorted(str(venue) for venue in _ENDPOINTS),
    )
    parser.add_argument("--endpoint", default="exchangeInfo")
    arguments = parser.parse_args(argv)

    profile = VENUE_PROFILES[Venue(arguments.venue)]
    endpoints = _ENDPOINTS[profile.venue_id]
    if arguments.endpoint != "all":
        endpoints = tuple(entry for entry in endpoints if entry.name == arguments.endpoint)
        if not endpoints:
            parser.error(
                f"{arguments.venue} has no recordable endpoint named "
                f"{arguments.endpoint!r}; choose from "
                f"{sorted(entry.name for entry in _ENDPOINTS[profile.venue_id])} or 'all'"
            )

    for endpoint in endpoints:
        path = record_one(profile, endpoint)
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
