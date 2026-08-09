"""Derive the corrupted-archive corpus in `tests/fixtures/corrupt/` from the recordings.

Every corrupt fixture is a **declared, deterministic mutation of a real recording**, never
an authored file. `docs/rules/testing-rules.md` bans hand-written fixtures, and a
corrupt corpus is where that ban matters most: the point of these files is to prove a gate
fires on the shape Binance actually emits, and a hand-typed CSV would encode what its
author believes a truncated archive or a lowercased boolean looks like.

So each output carries a `.corruption.json` sidecar naming the source recording, that
recording's digest, the mutation applied and the mutated file's own digest --
`tests/data/test_corrupt_fixture_integrity.py` re-derives every file from its pristine
source and requires byte equality. A hand-edit to a corrupt fixture therefore fails, and so
does an edit to the sidecar, because the derivation is what is checked rather than the
record of it.

Outputs are `.zip`, including the ones derived from the CSV-prefix recordings, because a
`.zip` is what ingestion actually receives -- gate 1 hashes the archive and gate 2 reads
the member out of it, and a corpus of loose CSVs could not exercise either.

Members are **stored, not deflated**, which makes each whole-day fixture roughly 220 KB
rather than 70 KB. That is deliberate: deflate output depends on the zlib build, and a
corpus whose committed bytes are only reproducible on the machine that wrote them turns the
integrity test into a source of cross-platform failures. Git packs the stored CSV back down,
so the repository pays little for it. The whole-day source is itself deliberate for gates 6
and 7 -- one bad row in 1,440 is 0.069%, which passes the file-wide 0.1% ceiling and fails
gate 6's 0.01% one, and a 32-row fragment could not demonstrate that the thresholds differ.

Run by hand when a mutation is added or a source recording is re-recorded:

    uv run python tools/corrupt_archive_fixture.py            # write the corpus
    uv run python tools/corrupt_archive_fixture.py --check    # verify without writing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Final

from fking.data.format_resolver import Dataset, Market

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
RECORDED_ROOT: Final[Path] = REPO_ROOT / "tests" / "fixtures" / "archives"
CORRUPT_ROOT: Final[Path] = REPO_ROOT / "tests" / "fixtures" / "corrupt"

# A fixed member timestamp and stored-not-deflated entries, so the same mutation produces
# the same bytes on every machine and in every Python version. A zip built with the
# default clock would make the integrity test fail one second after it was written.
_ZIP_MEMBER_TIME: Final[tuple[int, int, int, int, int, int]] = (2026, 1, 1, 0, 0, 0)

# `zipfile.ZipInfo.__init__` sets `create_system` to 0 on Windows and 3 everywhere else,
# and the field lands in the central directory at the *end* of the archive. Left at its
# default, a corpus generated on Windows and re-derived on Linux differs in one byte per
# member -- which presents as every fixture being stale at once, with the archive bytes
# looking fine right up to the footer. Pinned to Unix, arbitrarily but permanently.
_ZIP_CREATE_SYSTEM_UNIX: Final[int] = 3

# 0o644, shifted into the high half of external_attr where the zip format keeps Unix mode
# bits. `ZipInfo` leaves this at 0, which is already deterministic; it is set explicitly so
# that the archive is a normal readable file rather than one with no permissions at all.
_ZIP_EXTERNAL_ATTR: Final[int] = 0o644 << 16

_LINE_SEPARATOR: Final[str] = "\n"
_FIELD_SEPARATOR: Final[str] = ","
_ENCODING: Final[str] = "utf-8"

# Kline column indices. Named rather than inline because a mutation that writes to the
# wrong column produces a corrupt fixture that trips a different gate than its name claims,
# which is the one failure this corpus cannot detect in itself.
_OPEN_TIME: Final[int] = 0
_OPEN: Final[int] = 1
_HIGH: Final[int] = 2
_CLOSE: Final[int] = 4
_LOW: Final[int] = 3
_VOLUME: Final[int] = 5

# Binance kline archives write prices to eight decimal places.
_ARCHIVE_PRICE_STEP: Final[Decimal] = Decimal("0.00000001")

# exp(0.8), so the scaled bar sits exactly one 0.8 log return above its predecessor --
# comfortably past gate 9's 0.5 threshold and nowhere near any other gate's.
_LOG_RETURN_0_8_MULTIPLIER: Final[Decimal] = Decimal("2.22554092849246760458")

# Bars removed from the middle of a day for the cadence fixture. Ten rather than one so the
# gap's arithmetic is checkable: an off-by-one in the missing count is invisible at one.
_CADENCE_GAP_BARS: Final[int] = 10


@dataclass(frozen=True, slots=True)
class SourceRecording:
    """A pristine recording, addressed the way its provenance sidecar addresses it."""

    market: Market
    dataset: Dataset
    symbol: str
    interval: str | None
    archive_date: date

    @property
    def directory(self) -> Path:
        parts = [self.market.value, self.dataset.value, self.symbol]
        if self.interval is not None:
            parts.append(self.interval)
        return RECORDED_ROOT.joinpath(*parts)

    def member_bytes(self) -> tuple[str, bytes]:
        """The CSV member name and bytes, whether the recording is a `.zip` or a fragment."""
        slot = self.interval if self.interval is not None else self.dataset.value
        stem = f"{self.symbol}-{slot}-{self.archive_date.isoformat()}"
        whole = self.directory / f"{stem}.zip"
        if whole.is_file():
            with zipfile.ZipFile(BytesIO(whole.read_bytes())) as bundle:
                member = bundle.namelist()[0]
                return member, bundle.read(member)
        fragment = self.directory / f"{stem}.head32.csv"
        if fragment.is_file():
            return f"{stem}.csv", fragment.read_bytes()
        raise FileNotFoundError(
            f"no recording for {stem} under {self.directory}; record one with "
            f"tools/record_archive_fragment.py before deriving a corruption from it"
        )

    @property
    def label(self) -> str:
        slot = self.interval if self.interval is not None else self.dataset.value
        return f"{self.market.value}/{self.dataset.value}/{self.symbol}-{slot}"


@dataclass(frozen=True, slots=True)
class Corruption:
    """One named mutation, and the gate it exists to trip.

    `gate` is documentation rather than an import of `fking.data.quality.gates`: this tool
    must keep working while the gate it names is being rewritten, and a corpus that fails
    to generate because a production enum moved is a corpus nobody regenerates.
    """

    name: str
    source: SourceRecording
    gate: str
    rationale: str
    mutate: Callable[[bytes], bytes]

    @property
    def output_path(self) -> Path:
        return CORRUPT_ROOT / f"{self.name}.zip"


def _rewrite_member(payload: bytes, mutate: Callable[[list[str]], list[str]]) -> bytes:
    """Apply a line-level mutation, preserving the trailing newline the archives carry."""
    text = payload.decode(_ENCODING)
    trailing = _LINE_SEPARATOR if text.endswith(_LINE_SEPARATOR) else ""
    lines = mutate(text.splitlines())
    return (_LINE_SEPARATOR.join(lines) + trailing).encode(_ENCODING)


def _rewrite_field(line: str, *, index: int, value: str) -> str:
    fields = line.split(_FIELD_SEPARATOR)
    fields[index] = value
    return _FIELD_SEPARATOR.join(fields)


def truncate_to_half(payload: bytes) -> bytes:
    """Keep the first half of the file. Applied to the built `.zip`, not to the member."""
    return payload[: len(payload) // 2]


def prepend_a_header(columns: Sequence[str]) -> Callable[[bytes], bytes]:
    """Give a headerless spot file the futures header. Trap 2, in the direction that
    silently discards the first real bar of the day -- always 00:00 UTC."""

    def mutate(payload: bytes) -> bytes:
        return _rewrite_member(payload, lambda lines: [_FIELD_SEPARATOR.join(columns), *lines])

    return mutate


def strip_the_header(payload: bytes) -> bytes:
    """Remove a futures file's header. Trap 2, in the direction that files a bar at the
    Unix epoch by parsing the string `open_time` as a timestamp."""
    return _rewrite_member(payload, lambda lines: lines[1:])


def lowercase_the_booleans(payload: bytes) -> bytes:
    """`True`/`False` become `true`/`false`. Trap 3, exactly as it would recur upstream."""
    text = payload.decode(_ENCODING)
    return text.replace(",True", ",true").replace(",False", ",false").encode(_ENCODING)


def move_a_row_block_earlier(payload: bytes) -> bytes:
    """Move a later block of rows in front of an earlier one, as a merged file would."""

    def mutate(lines: list[str]) -> list[str]:
        cut = len(lines) // 2
        return [*lines[cut : cut + 4], *lines[:cut], *lines[cut + 4 :]]

    return _rewrite_member(payload, mutate)


def zero_the_first_epoch(payload: bytes) -> bytes:
    """Set the first data row's open time to 0, landing it in 1970."""

    def mutate(lines: list[str]) -> list[str]:
        return [_rewrite_field(lines[0], index=_OPEN_TIME, value="0"), *lines[1:]]

    return _rewrite_member(payload, mutate)


def high_below_close(payload: bytes) -> bytes:
    """Drop one bar's high below its own close, so the high no longer brackets the pair."""

    def mutate(lines: list[str]) -> list[str]:
        index = len(lines) // 2
        close = lines[index].split(_FIELD_SEPARATOR)[_CLOSE]
        # Decimal even here. The value is written back into a price column, and a float
        # round trip would put a corrupt fixture's price one ulp away from a round number
        # for a reason unrelated to the corruption being demonstrated.
        halved = str((Decimal(close) / 2).quantize(_ARCHIVE_PRICE_STEP))
        return [
            *lines[:index],
            _rewrite_field(lines[index], index=_HIGH, value=halved),
            *lines[index + 1 :],
        ]

    return _rewrite_member(payload, mutate)


def negative_volume(payload: bytes) -> bytes:
    """Negate one bar's base volume. No upstream change makes that a correct reading."""

    def mutate(lines: list[str]) -> list[str]:
        index = len(lines) // 3
        volume = lines[index].split(_FIELD_SEPARATOR)[_VOLUME]
        return [
            *lines[:index],
            _rewrite_field(lines[index], index=_VOLUME, value=f"-{volume}"),
            *lines[index + 1 :],
        ]

    return _rewrite_member(payload, mutate)


def scale_the_last_bar(payload: bytes) -> bytes:
    """Multiply the final bar's OHLC by exp(0.8), a real 0.8 log return.

    The whole bar rather than its close alone, so the row stays OHLC-coherent and reaches
    gate 9 instead of being rejected by gate 6. The *last* bar rather than an interior one,
    so the file holds exactly one discontinuity: scaling a middle bar produces a jump in and
    a jump back out, and two flags where the test means to assert one.
    """

    def mutate(lines: list[str]) -> list[str]:
        fields = lines[-1].split(_FIELD_SEPARATOR)
        for index in (_OPEN, _HIGH, _LOW, _CLOSE):
            scaled = Decimal(fields[index]) * _LOG_RETURN_0_8_MULTIPLIER
            fields[index] = str(scaled.quantize(_ARCHIVE_PRICE_STEP))
        return [*lines[:-1], _FIELD_SEPARATOR.join(fields)]

    return _rewrite_member(payload, mutate)


def remove_a_bar_block(payload: bytes) -> bytes:
    """Delete a block of consecutive bars from the middle of a day.

    What an exchange outage, a maintenance window or a genuine archive hole looks like. The
    surviving rows are all valid, which is why this is reported rather than refused -- and
    why nothing fills it.
    """

    def mutate(lines: list[str]) -> list[str]:
        cut = len(lines) // 2
        return [*lines[:cut], *lines[cut + _CADENCE_GAP_BARS :]]

    return _rewrite_member(payload, mutate)


_FUTURES_KLINE_HEADER: Final[tuple[str, ...]] = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
)

SPOT_KLINES_2025: Final = SourceRecording(
    market=Market.SPOT,
    dataset=Dataset.KLINES,
    symbol="BTCUSDT",
    interval="1m",
    archive_date=date(2025, 1, 2),
)
FUTURES_KLINES_2025: Final = SourceRecording(
    market=Market.FUTURES_UM,
    dataset=Dataset.KLINES,
    symbol="BTCUSDT",
    interval="1m",
    archive_date=date(2025, 1, 2),
)
SPOT_TRADES_2025: Final = SourceRecording(
    market=Market.SPOT,
    dataset=Dataset.TRADES,
    symbol="BTCUSDT",
    interval=None,
    archive_date=date(2025, 1, 2),
)

# One corruption per gate that a file can trip, plus both directions of the header gate.
# Gates 8 and 9 are absent because they report rather than refuse, and gates 10 and 11 are
# absent because neither is a property of a single file.
CORRUPTIONS: Final[tuple[Corruption, ...]] = (
    Corruption(
        name="spot_klines_truncated_archive",
        source=SPOT_KLINES_2025,
        gate="1 - checksum",
        rationale=(
            "A truncated archive parses cleanly for the rows it still holds and then ends, "
            "producing a day with a plausible row count, a plausible price range and hours "
            "missing that nobody notices until a backtest shows an implausibly clean edge "
            "in that window."
        ),
        mutate=truncate_to_half,
    ),
    Corruption(
        name="spot_klines_header_prepended",
        source=SPOT_KLINES_2025,
        gate="2 - header expectation",
        rationale=(
            "Spot kline archives carry no header. Reading one as though it did silently "
            "discards the first real bar of the day -- always 00:00 UTC, which correlates "
            "precisely with the daily-boundary behaviour strategies care about."
        ),
        mutate=prepend_a_header(_FUTURES_KLINE_HEADER),
    ),
    Corruption(
        name="futures_klines_header_stripped",
        source=FUTURES_KLINES_2025,
        gate="2 - header expectation",
        rationale=(
            "Futures kline archives carry a header. Reading one as though it did not parses "
            "the string 'open_time' as a timestamp, which a lenient parser coerces to a bar "
            "at the Unix epoch."
        ),
        mutate=strip_the_header,
    ),
    Corruption(
        name="spot_klines_first_epoch_zeroed",
        source=SPOT_KLINES_2025,
        gate="3 - first timestamp plausible",
        rationale=(
            "The magnitude a wrong epoch unit produces, in the direction that lands in "
            "1970. Caught on row one rather than after every row has been rejected "
            "identically."
        ),
        mutate=zero_the_first_epoch,
    ),
    Corruption(
        name="spot_klines_rows_out_of_order",
        source=SPOT_KLINES_2025,
        gate="4 - monotone timestamps",
        rationale=(
            "What two files merged upstream, or one epoch unit applied to half a file, "
            "looks like on disk. Every row is individually valid."
        ),
        mutate=move_a_row_block_earlier,
    ),
    Corruption(
        name="spot_trades_booleans_lowercased",
        source=SPOT_TRADES_2025,
        gate="5 - boolean tokens",
        rationale=(
            "Trap 3 recurring. Read under a lowercase-only comparison the aggressor side "
            "would be False on every row, with counts, prices and volumes all correct."
        ),
        mutate=lowercase_the_booleans,
    ),
    Corruption(
        name="spot_klines_high_below_close",
        source=SPOT_KLINES_2025,
        gate="6 - OHLC coherence",
        rationale=(
            "A bar describing a price path that did not happen. The range computed from it "
            "is wrong in the direction that makes a strategy look better."
        ),
        mutate=high_below_close,
    ),
    Corruption(
        name="spot_klines_negative_volume",
        source=SPOT_KLINES_2025,
        gate="7 - non-negative volume",
        rationale=(
            "A corrupt byte or a column that moved. A moved column means the neighbouring "
            "values are also being read as something they are not."
        ),
        mutate=negative_volume,
    ),
    Corruption(
        name="spot_klines_gapped_bar_block",
        source=SPOT_KLINES_2025,
        gate="8 - bar cadence (reported, never refused)",
        rationale=(
            "A gap is information about the world -- an outage, a maintenance window, a "
            "delisting -- and filling it manufactures a price path that never traded. A "
            "synthesised bar has zero realised volatility and perfect mean reversion, which "
            "is catnip to exactly the strategies this system is trying to reject."
        ),
        mutate=remove_a_bar_block,
    ),
    Corruption(
        name="spot_klines_08_log_return",
        source=SPOT_KLINES_2025,
        gate="9 - price continuity (flagged, never refused)",
        rationale=(
            "A 50% single-minute move on a thin altcoin is a real event. Rejecting it "
            "removes exactly the tail the risk engine most needs to have seen, and gates "
            "that discard unusual-but-real data bias every volatility estimate toward calm."
        ),
        mutate=scale_the_last_bar,
    ),
)


def build(corruption: Corruption) -> tuple[bytes, dict[str, object]]:
    """The corrupted archive bytes and the sidecar describing how they were derived."""
    member_name, member = corruption.source.member_bytes()
    pristine_zip = _zip(member_name, member)

    if corruption.mutate is truncate_to_half:
        # The only mutation that operates on the archive rather than on its member: a
        # truncation that left a valid zip behind would test nothing about gate 1.
        corrupted = corruption.mutate(pristine_zip)
    else:
        corrupted = _zip(member_name, corruption.mutate(member))

    sidecar: dict[str, object] = {
        "name": corruption.name,
        "gate": corruption.gate,
        "rationale": corruption.rationale,
        "mutation": corruption.mutate.__name__,
        "source_recording": corruption.source.label,
        "source_member_sha256": hashlib.sha256(member).hexdigest(),
        "pristine_archive_sha256": hashlib.sha256(pristine_zip).hexdigest(),
        "corrupt_archive_sha256": hashlib.sha256(corrupted).hexdigest(),
        "member_name": member_name,
    }
    return corrupted, sidecar


def _zip(member_name: str, member: bytes) -> bytes:
    """A deterministic single-member zip: fixed member time, stored, no extra metadata."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_STORED) as bundle:
        info = zipfile.ZipInfo(filename=member_name, date_time=_ZIP_MEMBER_TIME)
        info.create_system = _ZIP_CREATE_SYSTEM_UNIX
        info.external_attr = _ZIP_EXTERNAL_ATTR
        bundle.writestr(info, member)
    return buffer.getvalue()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed corpus matches the derivation instead of writing it",
    )
    arguments = parser.parse_args(argv)

    CORRUPT_ROOT.mkdir(parents=True, exist_ok=True)
    stale = 0
    for corruption in CORRUPTIONS:
        corrupted, sidecar = build(corruption)
        archive_path = corruption.output_path
        sidecar_path = archive_path.with_suffix(".zip.corruption.json")
        rendered = json.dumps(sidecar, indent=2, sort_keys=True) + "\n"

        if arguments.check:
            if not archive_path.is_file() or archive_path.read_bytes() != corrupted:
                print(f"STALE {archive_path.relative_to(REPO_ROOT)}")
                stale += 1
            if not sidecar_path.is_file() or sidecar_path.read_text(encoding=_ENCODING) != rendered:
                print(f"STALE {sidecar_path.relative_to(REPO_ROOT)}")
                stale += 1
            continue

        archive_path.write_bytes(corrupted)
        sidecar_path.write_text(rendered, encoding=_ENCODING)
        print(f"wrote {archive_path.relative_to(REPO_ROOT)} (gate {corruption.gate})")

    if stale:
        print(f"{stale} file(s) do not match their declared derivation; run without --check")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
