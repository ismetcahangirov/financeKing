"""Record real WebSocket frames from a Binance testnet market-data stream.

Run by hand, never in CI, for the same reason `record_archive_fragment.py` is:
`.claude/rules/testing-rules.md` bans hand-written fixtures outright. A hand-written
frame encodes what its author believes Binance emits, and the fields that actually
break a live ingester are the ones an author would never think to write down wrongly --
`k.x` false on every frame but the last of a minute, `E` and `T` differing by a
millisecond, `a` jumping when nothing is wrong because the venue aggregated two prints.

The connection is `fking.platform.safety.guarded_ws_connect`, not a socket this file
opens. That is not ceremony: a recorder is exactly the kind of one-off script that
would otherwise carry the first unguarded client in the tree, and the next thing that
needs a socket copies the nearest example.

Frames are written verbatim, one JSON document per line, in arrival order. Not
re-serialised: the fixture must be the bytes the venue sent, so that
`Decimal(frame["p"])` in a test is a claim about Binance's own formatting rather than
about `json.dumps`. The sidecar records a SHA-256 over the file, which is what
`tests/data/test_stream_fixture_integrity.py` re-derives to prove nobody hand-edited a
frame to make a test pass.

Recording stops when the session has captured enough closed klines *and* enough frames,
or when the deadline expires -- whichever comes first. A closed kline arrives once per
minute per symbol, so `--closed-klines 2` takes between one and two minutes and is the
smallest recording that can prove "the open frames for a minute were skipped and the
closed one for the same minute was kept".

Usage:

    uv run python tools/record_stream_frames.py --venue binance-spot-testnet
    uv run python tools/record_stream_frames.py --venue binance-futures-testnet
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from fking.data.live.streams import (
    LIVE_STREAM_PROFILES,
    LiveStreamProfile,
    combined_stream_url,
    stream_names_for,
)
from fking.platform.safety import guarded_ws_connect

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
FIXTURE_ROOT: Final[Path] = REPO_ROOT / "tests" / "fixtures" / "streams"

# Two closed klines, so a fixture can show a minute boundary rather than an endpoint,
# and 40 frames so the aggTrade sequence has somewhere to be contiguous.
DEFAULT_CLOSED_KLINES: Final[int] = 2
DEFAULT_MIN_FRAMES: Final[int] = 40
DEFAULT_DEADLINE_SECONDS: Final[float] = 300.0


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _is_closed_kline(frame: str) -> bool:
    """Whether this raw frame carries a closed kline, judged the way the parser does.

    Deliberately duck-typed rather than routed through `fking.data.live.frames`: the
    recorder must be able to capture a frame whose shape the parser would reject, since
    a shape change upstream is exactly the event a fresh recording is meant to surface.
    """
    document = json.loads(frame)
    payload = document.get("data", document)
    kline = payload.get("k") if isinstance(payload, dict) else None
    return isinstance(kline, dict) and kline.get("x") is True


async def _capture(
    profile: LiveStreamProfile,
    symbol: str,
    *,
    closed_klines: int,
    min_frames: int,
    deadline_seconds: float,
) -> tuple[str, list[str]]:
    url = combined_stream_url(profile, (symbol,))
    frames: list[str] = []
    closed_seen = 0
    loop = asyncio.get_running_loop()
    expires_at = loop.time() + deadline_seconds

    async with guarded_ws_connect(url) as connection:
        while closed_seen < closed_klines or len(frames) < min_frames:
            remaining = expires_at - loop.time()
            if remaining <= 0:
                break
            raw = await asyncio.wait_for(connection.recv(), timeout=remaining)
            frame = raw if isinstance(raw, str) else raw.decode("utf-8")
            frames.append(frame)
            if _is_closed_kline(frame):
                closed_seen += 1
    return url, frames


def record(
    *,
    venue_id: str,
    symbol: str,
    closed_klines: int,
    min_frames: int,
    deadline_seconds: float,
) -> None:
    profile = LIVE_STREAM_PROFILES[venue_id]
    url, frames = asyncio.run(
        _capture(
            profile,
            symbol,
            closed_klines=closed_klines,
            min_frames=min_frames,
            deadline_seconds=deadline_seconds,
        )
    )
    if not frames:
        raise SystemExit(f"captured no frames from {url}")

    directory = FIXTURE_ROOT / venue_id
    directory.mkdir(parents=True, exist_ok=True)
    fixture_path = directory / f"{symbol.lower()}.jsonl"
    # "\n".join plus a trailing newline, written binary: the sidecar digest must
    # describe the bytes on disk on every platform, and text mode on Windows would
    # translate every separator into CRLF after the digest was taken.
    payload = ("\n".join(frames) + "\n").encode("utf-8")
    fixture_path.write_bytes(payload)

    closed = sum(1 for frame in frames if _is_closed_kline(frame))
    provenance = {
        "venue_id": venue_id,
        "symbol": symbol,
        "streams": list(stream_names_for(profile, symbol)),
        "source_url": url,
        "frame_count": len(frames),
        "closed_kline_count": closed,
        "frames_sha256": _sha256_hex(payload),
        "recorded_at_utc": datetime.now(UTC).isoformat(),
    }
    sidecar = fixture_path.with_suffix(".provenance.json")
    sidecar.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"wrote {fixture_path.relative_to(REPO_ROOT)}  frames={len(frames)} closed={closed}")
    print(f"wrote {sidecar.relative_to(REPO_ROOT)}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", required=True, choices=sorted(LIVE_STREAM_PROFILES))
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--closed-klines", type=int, default=DEFAULT_CLOSED_KLINES)
    parser.add_argument("--min-frames", type=int, default=DEFAULT_MIN_FRAMES)
    parser.add_argument("--deadline-seconds", type=float, default=DEFAULT_DEADLINE_SECONDS)
    arguments = parser.parse_args(argv)

    record(
        venue_id=arguments.venue,
        symbol=arguments.symbol,
        closed_klines=arguments.closed_klines,
        min_frames=arguments.min_frames,
        deadline_seconds=arguments.deadline_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
