"""Archive format, resolved per `(market, dataset, date)` and never assumed.

`DATA_PIPELINE.md` §3 states the design rule this module exists to make structural:

> **Format is a property of `(market, dataset, date)`, never of the codebase.**

Four verified traps generalise to it, and they share a shape. Each is a format
assumption a competent engineer would make *correctly* for one combination and
*incorrectly* for another, and none of them raises:

1. Binance **spot** archives emit **microsecond** epochs from 2025-01-01. USDⓈ-M
   **futures** archives stayed on **milliseconds**, then and now. No field, header or
   filename announces this (VF-015, `docs/adr/0013`).
2. Futures kline CSVs open with a header row. Spot kline CSVs open with data (VF-016).
   Read a spot file as though it had a header and the first real bar of the day is
   silently discarded -- always 00:00 UTC, always exactly one row.
3. Spot trade archives serialise booleans Python-style, `True`/`False` (F-005). A
   lowercase comparison returns `False` for every row in the file, so row counts,
   prices and volumes are all right and only the trade *side* is wrong -- uniformly.
4. The USDⓈ-M `metrics` archive does not stamp an epoch at all: `create_time` is
   `2024-01-02 00:00:00`, a naive datetime string with no offset (VF-029). Nothing in
   the header, the filename or the neighbouring datasets says so, and the failure is the
   quiet one -- `datetime.fromisoformat` accepts it and returns a **naive** datetime,
   which compares equal to nothing and joins against nothing, or gets `astimezone`'d in
   a process whose local zone is not UTC and lands hours away from where it belongs.

So this module holds a lookup table and two pure functions, and **it has no default**.
An undeclared `(market, dataset, date)` raises. Defaulting is not a lenient version of
this design; defaulting is trap 1's exact mechanism, because the whole failure is a
value that was inferred rather than known. A date range that crosses the cutover is
two ingestion specs, not one with a conditional inside the row loop.

The table is deliberately small. A combination is declared when an archive has been
observed, not when it seems likely -- see the note above `DECLARED_FORMATS`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Final

from fking.platform.errors import DataIntegrityError

__all__ = [
    "ALT_DATASETS",
    "ArchiveFormat",
    "BooleanEncoding",
    "Dataset",
    "EpochUnit",
    "Market",
    "TimestampEncoding",
    "epoch_to_utc",
    "parse_naive_utc_datetime",
    "resolve_archive_format",
]


class Market(StrEnum):
    """Which `data.binance.vision` corpus a file belongs to.

    Spot and USDⓈ-M futures are separate archives with separate format histories, which
    is the entire reason this type exists rather than a `symbol` string: the traps are
    keyed on the corpus, and a symbol does not tell you which corpus you are in.
    """

    SPOT = "spot"
    FUTURES_UM = "futures_um"


class Dataset(StrEnum):
    """A dataset within a corpus.

    Values match the path segment `data.binance.vision` uses, including `aggTrades`'
    camel case, so a declaration and a URL cannot disagree about which file is meant.
    """

    KLINES = "klines"
    TRADES = "trades"
    AGG_TRADES = "aggTrades"
    BOOK_DEPTH = "bookDepth"
    BOOK_TICKER = "bookTicker"
    # The two alternative series (#32, #155). Their formats are declared here like every
    # other, and they are deliberately absent from the corpus tables -- `_PARSERS`,
    # `DATASET_PARTITION_GRAIN` and the Parquet schemas: neither produces a bar or a
    # print, neither has a partition grain, and neither enters the corpus.
    # `ALT_DATASETS` is what states that, and `fking.data.alt` owns their layout, their
    # availability lag and their readers.
    FUNDING_RATE = "fundingRate"
    METRICS = "metrics"


class EpochUnit(StrEnum):
    """The unit a raw integer timestamp is expressed in.

    Kept distinct from `TimestampEncoding` because it answers a narrower question that
    also arises where there is no archive at all: the REST backfill and the live stream
    both receive integer epochs and neither has a `(market, dataset, date)` to resolve
    (`fking.data.backfill.rest`, `fking.data.live.frames`). This is also the value the
    ingest registry records as `epoch_unit_applied`, and widening it to cover a datetime
    string would put a value in that column that no epoch was ever read in.
    """

    MILLISECONDS = "ms"
    MICROSECONDS = "us"


class TimestampEncoding(StrEnum):
    """How an archive CSV spells the instant in its first column.

    Three members rather than two-plus-a-flag, because the third is not a variation on
    an epoch -- it is a different kind of value with a different failure mode. An epoch
    read in the wrong unit lands in 1970 or the year 56,000 and the plausibility window
    catches it on the first row. A naive datetime string read with `fromisoformat` and
    filed as-is is *correct to the second* and merely has no timezone, so nothing about
    it looks wrong until a join returns nothing months later.

    `NAIVE_UTC_DATETIME` names both halves of what is being asserted: the field carries
    no offset, **and** the publisher's own convention is that it means UTC. The second
    half is a claim about Binance, sourced in VF-029, not an inference from the first --
    a naive string is equally consistent with exchange-local time, and `parse_naive_utc_
    datetime` attaching UTC is only sound because the claim has been checked.
    """

    EPOCH_MILLISECONDS = "epoch_ms"
    EPOCH_MICROSECONDS = "epoch_us"
    NAIVE_UTC_DATETIME = "naive_utc_datetime"


class BooleanEncoding(StrEnum):
    """How a boolean column is serialised in a CSV.

    `PYTHON` is `True`/`False`, `JSON` is `true`/`false`, `NUMERIC` is `1`/`0`. Three
    members rather than a `bool` flag because the failure is not "we got it backwards"
    -- it is a parser treating an unrecognised token as falsy, which needs the expected
    spelling to be stated rather than sniffed.
    """

    PYTHON = "python"
    JSON = "json"
    NUMERIC = "numeric"


# Which encodings are epochs, and in what unit. A mapping rather than a `str.startswith`
# on the member value: the two enums are allowed to drift apart, and a prefix convention
# is the kind of coupling that survives a rename in only one of the two files.
_EPOCH_UNIT_OF: Final[Mapping[TimestampEncoding, EpochUnit]] = {
    TimestampEncoding.EPOCH_MILLISECONDS: EpochUnit.MILLISECONDS,
    TimestampEncoding.EPOCH_MICROSECONDS: EpochUnit.MICROSECONDS,
}


@dataclass(frozen=True, slots=True)
class ArchiveFormat:
    """Every format decision for one `(market, dataset)` over one date range.

    Dates are UTC calendar days, matching the day a `data.binance.vision` daily archive
    is filed under. The range is half-open `[declared_from_date, declared_until_date)`;
    `declared_until_date is None` means the segment is the current one.

    Immutable and slotted so that a caller holding a resolved format cannot adjust it
    for one awkward file and hand the adjusted object to the next reader.
    """

    market: Market
    dataset: Dataset
    timestamp_encoding: TimestampEncoding
    has_header_row: bool
    boolean_encoding: BooleanEncoding | None
    boolean_columns: tuple[str, ...]
    declared_from_date: date
    declared_until_date: date | None

    def covers(self, archive_date: date) -> bool:
        """True when this segment declares the format for `archive_date`."""
        if archive_date < self.declared_from_date:
            return False
        return self.declared_until_date is None or archive_date < self.declared_until_date

    def require_epoch_unit(self) -> EpochUnit:
        """The unit to read this format's integer timestamps in, or a refusal.

        A method rather than an optional field, so a caller cannot reach for a `None` and
        pick a unit for it. The corpus loaders all read epochs and all call this; the one
        format that has no epoch (`metrics`) is read by `fking.data.alt`, which asks for
        the encoding instead.

        Raises:
            DataIntegrityError: this format's timestamps are not epochs at all.
        """
        unit = _EPOCH_UNIT_OF.get(self.timestamp_encoding)
        if unit is None:
            raise DataIntegrityError(
                f"market={self.market.value!r} dataset={self.dataset.value!r} stamps "
                f"{self.timestamp_encoding.value!r}, which is not an epoch, so there is no "
                f"unit to read it in. Use fking.data.format_resolver.parse_naive_utc_datetime"
            )
        return unit


# Binance spot archives switched to microsecond epochs on this date; USDⓈ-M futures
# archives did not, and still have not. VF-015, and docs/adr/0013 for the argument.
SPOT_MICROSECOND_CUTOVER: Final[date] = date(2025, 1, 1)

# Corpus genesis. Below these dates there is no archive to have a format, so a request
# is a bad request rather than a format question with an obvious backwards answer.
# BTCUSDT spot 1m klines begin 2017-08-17 (VF-013); USDⓈ-M futures launched 2019-09-08.
# Per-symbol coverage starts later and varies -- that is discovered by probing and
# recorded in the availability declaration, and it is not this module's question.
SPOT_ARCHIVE_GENESIS: Final[date] = date(2017, 8, 17)
FUTURES_UM_ARCHIVE_GENESIS: Final[date] = date(2019, 9, 8)

# Spot trade archives carry exactly these two boolean columns (F-005).
SPOT_TRADE_BOOLEAN_COLUMNS: Final[tuple[str, ...]] = ("is_buyer_maker", "is_best_match")

ALT_DATASETS: Final[frozenset[Dataset]] = frozenset({Dataset.FUNDING_RATE, Dataset.METRICS})
"""Datasets whose format is declared here but which never enter the corpus.

They are read by `fking.data.alt`, not by `fking.data.loaders`: neither becomes a bar or
a print, neither has a partition grain or a Parquet schema, and both are late by a
publisher's choice rather than by the length of their own interval.

Stated as a set rather than left implicit, because two tests key on it and would
otherwise have to name the datasets inline and drift apart:
`tests/data/test_parsers.py` asserts every *corpus* declared dataset has a corpus parser,
and `tools/record_archive_fragment.py` decides which fixture root a recording belongs in.
Before #155 that second decision was made by catching the refusal from
`resolve_archive_format`, which stopped working the moment these two acquired a
declaration -- an inference standing in for a fact, which is the failure this whole
module is about.
"""

# Every combination this project has evidence for, and no others.
#
# `(spot, aggTrades)` is absent on purpose, and it is the omission most worth arguing
# with. Its epoch unit *is* verified -- VF-015 names klines, trades and aggTrades
# together -- but its boolean encoding is not. It is almost certainly `PYTHON`, because
# the same generator writes it. "Almost certainly" is trap 3's exact mechanism: an
# unrecognised token read as falsy inverts the sign of every trade in the file while
# every other column stays correct, and an order-flow imbalance feature built on that
# data is not noisy, it is sign-inverted, and it backtests beautifully in one
# direction. So it raises until an archive has been read, at which point the fix is one
# entry here. The same applies to bookDepth, bookTicker, and every futures dataset
# other than klines.
#
# Segments under one key must be ordered, contiguous and open-ended at the tail. That
# is asserted in tests/data/test_format_resolver.py rather than checked at import: the
# table is static, so a hole in it is a defect in this file, and the place to fail is
# the test suite rather than the first process that happens to import the module.
DECLARED_FORMATS: Final[Mapping[tuple[Market, Dataset], tuple[ArchiveFormat, ...]]] = {
    (Market.SPOT, Dataset.KLINES): (
        ArchiveFormat(
            market=Market.SPOT,
            dataset=Dataset.KLINES,
            timestamp_encoding=TimestampEncoding.EPOCH_MILLISECONDS,
            has_header_row=False,
            boolean_encoding=None,
            boolean_columns=(),
            declared_from_date=SPOT_ARCHIVE_GENESIS,
            declared_until_date=SPOT_MICROSECOND_CUTOVER,
        ),
        ArchiveFormat(
            market=Market.SPOT,
            dataset=Dataset.KLINES,
            timestamp_encoding=TimestampEncoding.EPOCH_MICROSECONDS,
            has_header_row=False,
            boolean_encoding=None,
            boolean_columns=(),
            declared_from_date=SPOT_MICROSECOND_CUTOVER,
            declared_until_date=None,
        ),
    ),
    (Market.SPOT, Dataset.TRADES): (
        ArchiveFormat(
            market=Market.SPOT,
            dataset=Dataset.TRADES,
            timestamp_encoding=TimestampEncoding.EPOCH_MILLISECONDS,
            has_header_row=False,
            boolean_encoding=BooleanEncoding.PYTHON,
            boolean_columns=SPOT_TRADE_BOOLEAN_COLUMNS,
            declared_from_date=SPOT_ARCHIVE_GENESIS,
            declared_until_date=SPOT_MICROSECOND_CUTOVER,
        ),
        ArchiveFormat(
            market=Market.SPOT,
            dataset=Dataset.TRADES,
            timestamp_encoding=TimestampEncoding.EPOCH_MICROSECONDS,
            has_header_row=False,
            boolean_encoding=BooleanEncoding.PYTHON,
            boolean_columns=SPOT_TRADE_BOOLEAN_COLUMNS,
            declared_from_date=SPOT_MICROSECOND_CUTOVER,
            declared_until_date=None,
        ),
    ),
    (Market.FUTURES_UM, Dataset.KLINES): (
        ArchiveFormat(
            market=Market.FUTURES_UM,
            dataset=Dataset.KLINES,
            timestamp_encoding=TimestampEncoding.EPOCH_MILLISECONDS,
            has_header_row=True,
            boolean_encoding=None,
            boolean_columns=(),
            declared_from_date=FUTURES_UM_ARCHIVE_GENESIS,
            declared_until_date=None,
        ),
    ),
    # The two alternative datasets. Their `declared_from_date` is the corpus genesis and
    # not the date each series really begins -- funding starts 2020-01 and open interest
    # 2020-09-01 for BTCUSDT, four and twelve months later, and both boundaries move per
    # symbol (VF-028). That is an availability question, answered per `(source, symbol)`
    # by `fking.data.alt.probe`; this table answers only "how is a file of this dataset
    # spelled", and it has been spelled this way for every archive read.
    (Market.FUTURES_UM, Dataset.FUNDING_RATE): (
        ArchiveFormat(
            market=Market.FUTURES_UM,
            dataset=Dataset.FUNDING_RATE,
            timestamp_encoding=TimestampEncoding.EPOCH_MILLISECONDS,
            has_header_row=True,
            boolean_encoding=None,
            boolean_columns=(),
            declared_from_date=FUTURES_UM_ARCHIVE_GENESIS,
            declared_until_date=None,
        ),
    ),
    (Market.FUTURES_UM, Dataset.METRICS): (
        ArchiveFormat(
            market=Market.FUTURES_UM,
            dataset=Dataset.METRICS,
            # The fourth trap, and the reason `TimestampEncoding` exists (VF-029).
            timestamp_encoding=TimestampEncoding.NAIVE_UTC_DATETIME,
            has_header_row=True,
            boolean_encoding=None,
            boolean_columns=(),
            declared_from_date=FUTURES_UM_ARCHIVE_GENESIS,
            declared_until_date=None,
        ),
    ),
}


def resolve_archive_format(
    *, market: Market, dataset: Dataset, archive_date: date
) -> ArchiveFormat:
    """The declared format for one archive, or a refusal.

    There is no fallback branch and no inferred answer. An unknown combination is an
    escalation (`DATA_PIPELINE.md` §11), because the alternative -- picking the
    neighbouring segment's format, or the other market's -- is how a corpus acquires a
    region of silently wrong timestamps that nothing downstream can detect.

    Raises:
        DataIntegrityError: the pair is undeclared, or the date falls outside every
            declared segment for it.
    """
    segments = DECLARED_FORMATS.get((market, dataset))
    if segments is None:
        raise DataIntegrityError(
            f"no archive format is declared for market={market.value!r} "
            f"dataset={dataset.value!r}; declaring one requires reading a real archive, "
            f"not inferring from a neighbouring dataset -- see docs/adr/0013"
        )
    for segment in segments:
        if segment.covers(archive_date):
            return segment
    raise DataIntegrityError(
        f"archive format for market={market.value!r} dataset={dataset.value!r} is "
        f"declared only from {segments[0].declared_from_date.isoformat()}; "
        f"{archive_date.isoformat()} falls outside every declared segment"
    )


# The one sanctioned float division in the parsing path. A timestamp is not money:
# microsecond epochs stay inside a double's 53 bits until the year 2255, and the
# reasoning does not generalise to prices, which is why no price appears in this
# module. docs/rules/time-and-timezones.md carries the argument.
_DIVISOR: Final[Mapping[EpochUnit, int]] = {
    EpochUnit.MILLISECONDS: 1_000,
    EpochUnit.MICROSECONDS: 1_000_000,
}

# Below this, a timestamp predates every archive Binance serves, so it is a unit error
# rather than old data. Milliseconds read as microseconds land in 1970; the window
# catches that on the first row, every time.
EARLIEST_PLAUSIBLE_UTC: Final[datetime] = datetime(2010, 1, 1, tzinfo=UTC)

# One day of slack above the caller's reference instant. An archive written seconds ago
# carries a timestamp fractionally ahead of a slightly skewed local clock, and a tight
# bound would reject that on every live run -- a false positive on the healthy path,
# which is how a guard gets widened until it stops guarding.
_FUTURE_TOLERANCE: Final[timedelta] = timedelta(days=1)


# `2024-01-02 00:00:00` and nothing else. Not `datetime.fromisoformat`, which since 3.11
# also accepts `2024-01-02T00:00:00`, a `Z` suffix, an offset and fractional seconds --
# and an offset is precisely what must not be silently absorbed here. If Binance ever
# starts stating one, that is a format change worth seeing on the first row rather than a
# case to handle: the declaration would move to a new segment, and the two segments would
# be two ingestion specs rather than one with a conditional in the row loop.
_NAIVE_UTC_DATETIME_TOKEN: Final[re.Pattern[str]] = re.compile(
    r"\A[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\Z"
)
_NAIVE_UTC_DATETIME_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"


def _require_aware_utc_reference(now_utc: datetime) -> None:
    if now_utc.tzinfo is None or now_utc.utcoffset() != timedelta(0):
        raise DataIntegrityError(
            f"now_utc must be timezone-aware UTC; got {now_utc!r}. A naive or offset "
            f"reference instant moves the plausibility window by its offset"
        )


def _describe(raw_epoch: int, divisor: int) -> str:
    """Render an out-of-range epoch for the error message, or say why it cannot be.

    The magnitude check runs on integers precisely so it cannot overflow; this renders
    for a human afterwards, and a value far enough out cannot be a `datetime` at all.
    """
    try:
        return datetime.fromtimestamp(raw_epoch / divisor, tz=UTC).isoformat()
    except (OSError, OverflowError, ValueError):
        return "an instant no datetime can represent"


def epoch_to_utc(raw_epoch: int, *, unit: EpochUnit, now_utc: datetime) -> datetime:
    """Normalise a raw archive epoch to an aware UTC datetime, or refuse.

    `now_utc` is a parameter rather than a `datetime.now(UTC)` call inside, for two
    reasons. The clock stays injectable, so this function is deterministically
    replayable. More importantly the upper bound must be **one fixed instant for a
    whole ingestion run**: a bound re-read per row drifts mid-file, and then the same
    raw integer can be accepted at the top of a file and rejected at the bottom.

    The plausibility window is the cheapest detector of a wrong unit that exists, and
    it works because both failure directions are absurd rather than subtle -- 1970 in
    one direction, roughly the year 56,000 in the other. It cannot catch a *mixed*
    series whose halves are each individually plausible; only a per-`(market, dataset,
    date)` declaration can, which is what `resolve_archive_format` is for.

    Raises:
        DataIntegrityError: the value is not an integer, the reference instant is not
            aware UTC, or the result falls outside `[2010-01-01, now_utc + 1 day)`.
    """
    # bool is a subclass of int, so an isinstance check alone accepts True and
    # normalises it to 1970-01-01T00:00:00.001Z without a word.
    if isinstance(raw_epoch, bool) or not isinstance(raw_epoch, int):
        raise DataIntegrityError(
            f"raw epoch must be an int, got {type(raw_epoch).__name__} {raw_epoch!r}"
        )
    _require_aware_utc_reference(now_utc)

    divisor = _DIVISOR[unit]
    # Compared as integers in the raw unit rather than as datetimes: an absurd value is
    # exactly the input this guard exists for, and converting first would raise
    # OverflowError from the float division instead of the message that names the unit.
    lower_raw = int(EARLIEST_PLAUSIBLE_UTC.timestamp()) * divisor
    upper_raw = int((now_utc + _FUTURE_TOLERANCE).timestamp()) * divisor
    if not lower_raw <= raw_epoch < upper_raw:
        raise DataIntegrityError(
            f"epoch {raw_epoch} read as {unit.value!r} normalises to "
            f"{_describe(raw_epoch, divisor)}, outside the plausible window "
            f"[{EARLIEST_PLAUSIBLE_UTC.isoformat()}, "
            f"{(now_utc + _FUTURE_TOLERANCE).isoformat()}); the unit is wrong, not the "
            f"data -- fix the declaration in fking.data.format_resolver"
        )
    return datetime.fromtimestamp(raw_epoch / divisor, tz=UTC)


def parse_naive_utc_datetime(raw: str, *, now_utc: datetime) -> datetime:
    """Read a `TimestampEncoding.NAIVE_UTC_DATETIME` field as an aware UTC datetime.

    The counterpart of `epoch_to_utc` for the one declared format whose first column is
    not a number. There is **no code path here that can return a naive datetime**: the
    only construction site attaches `UTC` in the same expression, which is what makes
    that a property of the function rather than a convention its callers follow.

    Attaching UTC rather than the process's local zone is the load-bearing choice, and it
    rests on VF-029 rather than on the string's shape: `2024-01-02 00:00:00` is equally
    consistent with an exchange-local stamp, and `astimezone` on a naive value would
    silently apply whatever zone the container happens to run in. The archive was read
    against the same instrument's millisecond-epoch funding series to confirm the two
    agree, and the encoding is declared per `(market, dataset, date)` so that a publisher
    who changes convention changes a segment rather than a row.

    The plausibility window is the same one `epoch_to_utc` applies, for a weaker but
    still worthwhile reason: a datetime string cannot be off by a factor of a thousand,
    so the window catches a corrupt year or a file from an unrelated era rather than a
    unit error. Applying it in both places keeps one definition of "an instant this
    corpus could contain".

    Raises:
        DataIntegrityError: the field is not exactly `YYYY-MM-DD HH:MM:SS`, is not a real
            calendar instant, the reference instant is not aware UTC, or the result falls
            outside `[2010-01-01, now_utc + 1 day)`.
    """
    _require_aware_utc_reference(now_utc)
    if not _NAIVE_UTC_DATETIME_TOKEN.match(raw):
        raise DataIntegrityError(
            f"timestamp {raw!r} is not {_NAIVE_UTC_DATETIME_FORMAT!r}. An offset, a 'Z', a "
            f"'T' separator or fractional seconds all mean the publisher changed how it "
            f"spells this column, which is a new declared segment rather than a row to "
            f"absorb -- see docs/adr/0013"
        )
    try:
        moment = datetime.strptime(raw, _NAIVE_UTC_DATETIME_FORMAT).replace(tzinfo=UTC)
    except ValueError as impossible:
        # The pattern admits 2024-13-45 32:99:99; the calendar does not.
        raise DataIntegrityError(
            f"timestamp {raw!r} matches the declared shape but is not a real instant: {impossible}"
        ) from impossible

    latest_plausible_utc = now_utc + _FUTURE_TOLERANCE
    if not EARLIEST_PLAUSIBLE_UTC <= moment < latest_plausible_utc:
        raise DataIntegrityError(
            f"timestamp {raw!r} reads as {moment.isoformat()}, outside the plausible "
            f"window [{EARLIEST_PLAUSIBLE_UTC.isoformat()}, "
            f"{latest_plausible_utc.isoformat()}); no archive Binance serves covers it"
        )
    return moment
