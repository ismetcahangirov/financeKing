"""Record a real archive fragment from `data.binance.vision` into `tests/fixtures/archives/`.

Run by hand, never in CI. It is the only way a parser fixture enters this repository,
because `docs/rules/testing-rules.md` bans hand-written fixtures outright: a
hand-written CSV encodes what its author believes Binance emits, and the two things that
actually break a loader -- `True`/`False` where every other exchange writes `true`, and
microsecond epochs on spot but not futures -- are precisely the two things an author
would never think to write down wrongly.

Every byte kept here passed a SHA-256 check against the archive's `.CHECKSUM` sibling
first, because the fetcher is `fking.data.archive.ArchiveFetcher` rather than a client
this file constructs. That matters beyond hygiene: a fixture recorded from a truncated
transfer is a fixture that teaches a parser the wrong row count, and it would be
indistinguishable from a real short day.

`today_utc` is passed as the archive's own date, so `resolve_granularity` answers DAILY.
That is not a fake clock -- it is asking the production rule which granularity was
available on the day the archive covers, and the daily archive is both the smallest
verifiable unit and the one that never stops existing. A monthly 1m kline file is
thirty times larger for the same handful of rows.

Usage:

    uv run python tools/record_archive_fragment.py \
        --market spot --dataset klines --symbol BTCUSDT --interval 1m \
        --date 2025-01-02 --rows 32

    uv run python tools/record_archive_fragment.py \
        --market spot --dataset klines --symbol BTCUSDT --interval 1m \
        --date 2025-01-02 --keep-archive
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import tempfile
import zipfile
from collections.abc import Sequence
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from typing import Final

from fking.data.archive import (
    ArchiveCoordinate,
    ArchiveFetcher,
    Granularity,
    archive_filename,
    resolve_granularity,
)
from fking.data.format_resolver import ALT_DATASETS, Dataset, Market
from fking.platform.safety.archive import GuardedArchiveEgress

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
FIXTURE_ROOT: Final[Path] = REPO_ROOT / "tests" / "fixtures" / "archives"
# Alternative series (#32) -- funding rates, open interest. They never become bars or
# prints, so they are kept apart from the corpus fixtures; see `_fixture_directory`.
ALT_FIXTURE_ROOT: Final[Path] = REPO_ROOT / "tests" / "fixtures" / "alt"

# Enough rows that a fragment exercises real variety -- a zero-volume minute, a price
# that moves, an eight-decimal quantity -- and few enough that a reviewer can read the
# whole file in a diff. A fixture nobody reads is a fixture nobody notices going stale.
DEFAULT_FRAGMENT_ROWS: Final[int] = 32


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _single_member_name(archive_bytes: bytes) -> str:
    with zipfile.ZipFile(BytesIO(archive_bytes)) as bundle:
        names = bundle.namelist()
    if len(names) != 1:
        raise SystemExit(f"expected exactly one member in the archive, found {names!r}")
    return names[0]


def _read_member(archive_bytes: bytes, member: str) -> bytes:
    with zipfile.ZipFile(BytesIO(archive_bytes)) as bundle:
        return bundle.read(member)


def _fragment_of(member_bytes: bytes, *, rows: int) -> tuple[bytes, int, int]:
    """First `rows` physical lines of `member_bytes`, with the member's total line count.

    Physical lines, not parsed records: the fragment must be a byte-exact prefix of the
    real file so that `Decimal(raw) == parsed` is a claim about Binance's own formatting
    rather than about a re-serialisation performed here.
    """
    lines = member_bytes.splitlines(keepends=True)
    kept = lines[:rows]
    return b"".join(kept), len(lines), len(kept)


async def _download(
    coordinate: ArchiveCoordinate, cache_root: Path, granularity: Granularity
) -> tuple[bytes, str, str]:
    async with GuardedArchiveEgress() as egress:
        fetcher = ArchiveFetcher(egress=egress, cache_root=cache_root)
        fetched = await fetcher.fetch(
            coordinate, today_utc=coordinate.archive_date, granularity=granularity
        )
    return fetched.path.read_bytes(), fetched.sha256_hex, fetched.url


def _fixture_directory(coordinate: ArchiveCoordinate) -> Path:
    """Where this recording belongs: the corpus fixtures, or the alternative ones.

    Decided by `ALT_DATASETS`, which is the statement of which datasets never become bars
    or prints. It used to be decided by catching the refusal from `resolve_archive_format`
    -- true only while the alternative datasets had no declaration, and silently wrong the
    moment `metrics` acquired one (#155), at which point a `metrics` recording would have
    landed under the corpus root and made the whole corpus suite raise on collection.
    Inferring a fact that is stated three imports away is the failure the format table
    exists to prevent, so it is not a shape to keep in the tool that populates it.
    """
    root = ALT_FIXTURE_ROOT if coordinate.dataset in ALT_DATASETS else FIXTURE_ROOT
    parts = [coordinate.market.value, coordinate.dataset.value, coordinate.symbol]
    if coordinate.interval is not None:
        parts.append(coordinate.interval)
    return root.joinpath(*parts)


def record(
    *,
    market: Market,
    dataset: Dataset,
    symbol: str,
    archive_date: date,
    interval: str | None,
    rows: int,
    keep_archive: bool,
    granularity: Granularity | None = None,
) -> None:
    coordinate = ArchiveCoordinate(
        market=market,
        dataset=dataset,
        symbol=symbol,
        archive_date=archive_date,
        interval=interval,
    )
    if granularity is None:
        # today_utc = the archive's own date, so the production rule resolves DAILY.
        granularity = resolve_granularity(archive_date=archive_date, today_utc=archive_date)
        if granularity is not Granularity.DAILY:  # pragma: no cover - the rule cannot say else
            raise SystemExit(f"expected a daily archive, resolved {granularity}")

    with tempfile.TemporaryDirectory(prefix="fking-record-") as scratch:
        archive_bytes, archive_sha256, url = asyncio.run(
            _download(coordinate, Path(scratch), granularity)
        )

    member = _single_member_name(archive_bytes)
    member_bytes = _read_member(archive_bytes, member)
    fragment, member_rows, fragment_rows = _fragment_of(member_bytes, rows=rows)

    destination = _fixture_directory(coordinate)
    destination.mkdir(parents=True, exist_ok=True)
    stem = Path(member).stem
    fragment_path = destination / f"{stem}.head{fragment_rows}.csv"
    fragment_path.write_bytes(fragment)

    provenance: dict[str, str | int | bool | None] = {
        "source_url": url,
        "archive_filename": archive_filename(coordinate, granularity),
        "archive_sha256": archive_sha256,
        "member_filename": member,
        "member_sha256": _sha256_hex(member_bytes),
        "member_line_count": member_rows,
        "fragment_line_count": fragment_rows,
        "fragment_sha256": _sha256_hex(fragment),
        "market": market.value,
        "dataset": dataset.value,
        "symbol": symbol,
        "interval": interval,
        "archive_date": archive_date.isoformat(),
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "whole_archive_committed": keep_archive,
    }
    (destination / f"{fragment_path.name}.provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    if keep_archive:
        # The verified `.zip` itself, so one test can exercise the real path end to end:
        # unzip, parse, and assert a full day's row count. A fragment cannot prove that
        # 1,440 minutes arrived.
        archive_path = destination / archive_filename(coordinate, granularity)
        archive_path.write_bytes(archive_bytes)
        (destination / f"{archive_path.name}.provenance.json").write_text(
            json.dumps(
                {**provenance, "fragment_line_count": None, "fragment_sha256": None},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"wrote {archive_path.relative_to(REPO_ROOT)} ({len(archive_bytes)} bytes)")

    print(
        f"wrote {fragment_path.relative_to(REPO_ROOT)} "
        f"({fragment_rows} of {member_rows} lines, {len(fragment)} bytes) from {url}"
    )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", required=True, choices=[member.value for member in Market])
    parser.add_argument("--dataset", required=True, choices=[member.value for member in Dataset])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--date", required=True, help="UTC calendar day, YYYY-MM-DD")
    parser.add_argument("--interval", default=None, help="klines only, e.g. 1m")
    parser.add_argument("--rows", type=int, default=DEFAULT_FRAGMENT_ROWS)
    parser.add_argument(
        "--granularity",
        default=None,
        choices=[member.value for member in Granularity],
        help=(
            "override the daily/monthly choice. Required for the alternative datasets, "
            "whose granularity is a property of the dataset rather than of the calendar: "
            "fundingRate is published monthly only and metrics daily only"
        ),
    )
    parser.add_argument(
        "--keep-archive",
        action="store_true",
        help="also commit the whole verified .zip; only for archives of a few tens of KB",
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str]) -> int:
    if shutil.which("git") is None:  # pragma: no cover - a sanity check on the environment
        print("no git on PATH; run this from a checkout")
        return 1
    parsed = _parse_args(argv)
    record(
        market=Market(parsed.market),
        dataset=Dataset(parsed.dataset),
        symbol=parsed.symbol,
        archive_date=date.fromisoformat(parsed.date),
        interval=parsed.interval,
        rows=parsed.rows,
        keep_archive=parsed.keep_archive,
        granularity=None if parsed.granularity is None else Granularity(parsed.granularity),
    )
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
