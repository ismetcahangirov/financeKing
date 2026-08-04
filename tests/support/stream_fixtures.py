"""Loading the recorded WebSocket frames under `tests/fixtures/streams/`.

Every file here was captured from a live testnet socket by
`tools/record_stream_frames.py` and is committed verbatim, one JSON document per line,
in arrival order. Nothing in this module parses a frame: the point of a recording is
that the parser under test is the only thing that has ever interpreted it.

The sidecar carries a SHA-256 over the exact bytes on disk, which
`tests/data/test_stream_fixture_integrity.py` re-derives. Without that, "recorded from
Binance" is a claim in a docstring and a hand-edited frame -- most likely edited in the
one field a failing test was complaining about -- would be indistinguishable from a real
capture.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
STREAM_FIXTURE_ROOT: Final[Path] = REPO_ROOT / "tests" / "fixtures" / "streams"

__all__ = ["STREAM_FIXTURE_ROOT", "RecordedStream", "recorded_streams"]


@dataclass(frozen=True, slots=True)
class RecordedStream:
    """One recorded session, and the provenance sidecar beside it."""

    path: Path
    venue_id: str
    symbol: str
    streams: tuple[str, ...]
    source_url: str
    frame_count: int
    closed_kline_count: int
    frames_sha256: str
    recorded_at_utc: str

    @property
    def label(self) -> str:
        return f"{self.venue_id}/{self.symbol}"

    def read_bytes(self) -> bytes:
        """The fixture exactly as written, for the digest check."""
        return self.path.read_bytes()

    def frames(self) -> tuple[str, ...]:
        """The raw frames, in arrival order, undecoded beyond the line split."""
        text = self.path.read_text(encoding="utf-8")
        return tuple(line for line in text.split("\n") if line)


def recorded_streams() -> tuple[RecordedStream, ...]:
    """Every recorded session, ordered by path so parametrised ids are stable."""
    recordings: list[RecordedStream] = []
    for sidecar in sorted(STREAM_FIXTURE_ROOT.rglob("*.provenance.json")):
        provenance = json.loads(sidecar.read_text(encoding="utf-8"))
        recordings.append(
            RecordedStream(
                path=sidecar.with_name(sidecar.name.removesuffix(".provenance.json") + ".jsonl"),
                venue_id=str(provenance["venue_id"]),
                symbol=str(provenance["symbol"]),
                streams=tuple(str(name) for name in provenance["streams"]),
                source_url=str(provenance["source_url"]),
                frame_count=int(provenance["frame_count"]),
                closed_kline_count=int(provenance["closed_kline_count"]),
                frames_sha256=str(provenance["frames_sha256"]),
                recorded_at_utc=str(provenance["recorded_at_utc"]),
            )
        )
    return tuple(recordings)
