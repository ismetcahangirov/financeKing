"""Where a symbol's alternative history actually starts, discovered rather than assumed.

The question this module answers is the one OQ-006 asks: how far back does funding and
open-interest history go for a given perpetual? The tempting answer is "to the contract's
listing", and it is wrong for every symbol measured so far. On 2026-08-05, against
`data.binance.vision`:

| Series | Corpus genesis | First archive |
|---|---|---|
| USDⓈ-M perpetuals | 2019-09-08 | -- |
| `fundingRate` BTCUSDT | | **2020-01** (2019-09 through 2019-12 all 404) |
| `fundingRate` ETHUSDT | | **2020-01** (2019-11 404) |
| `metrics` BTCUSDT | | **2020-09-01** (2020-08-31 404) |

Funding starts four months after the corpus, open interest almost a year after it. A
backtest that assumed either reached the listing would run a window whose first months are
empty, and an empty window reads downstream as "no signal in this period" rather than as
"no data in this period" -- a strategy scored on history it never saw. So the start date is
probed per `(source, symbol)`, recorded in an `AltAvailability`, and a request that opens
before it is refused rather than answered short. That is the same refusal, for the same
reason, as `fking.data.features.availability`.

**The probe is a binary search over the archive's period index, and it assumes existence
is monotone** -- that once a symbol's series starts it does not stop. That assumption is
load-bearing and is checked rather than trusted: the period after the discovered boundary
is probed too, so a single stranded island of history is refused instead of being reported
as the start of a continuous series. A hole in the *middle* of a series is a gap, which is
the ingestion registry's question and not this one's.

It probes the `.CHECKSUM` sibling, never the archive. The sibling is about eighty bytes
against a `.zip` that can be tens of megabytes, and it exists exactly when the archive
does -- so the cheap request answers the same question. Roughly seven requests settle a
monthly series and twelve a daily one, which is why this can run per symbol at startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

import structlog

from fking.data.alt.registry import ARCHIVE_DATASET, ARCHIVE_GRANULARITY, registered
from fking.data.alt.spec import AltSeriesRef, Delivery, require_utc
from fking.data.archive import ArchiveCoordinate, Granularity, archive_url
from fking.data.format_resolver import FUTURES_UM_ARCHIVE_GENESIS, Market
from fking.platform.errors import DataUnavailableError
from fking.platform.safety.archive import ArchiveEgress, ArchiveUnavailableError

__all__ = ["ALT_MARKET", "AltAvailability", "probe_earliest_archive_date"]

_LOG: Final = structlog.get_logger(__name__)

# Both alternative datasets live in the USDⓈ-M futures corpus and only there: spot has no
# funding and no open interest to publish. Named rather than inlined so that the day a
# spot alternative series appears, the thing to change is one binding with a reader.
ALT_MARKET: Final[Market] = Market.FUTURES_UM


@dataclass(frozen=True, slots=True, kw_only=True)
class AltAvailability:
    """The earliest period the archive holds for one series, and the probe that found it.

    Keyword-only and fully required, including `probe_request_count`. The count is not
    diagnostics: it is the difference between a declaration derived from evidence and one
    somebody typed, and a declaration with a count of zero could be either.
    """

    series: AltSeriesRef
    earliest_archive_date: date
    granularity: Granularity
    probed_at_utc: datetime
    probe_request_count: int

    def require_window(self, *, window_start_utc: datetime, window_end_utc: datetime) -> None:
        """Refuse unless the window opens at or after the earliest archived period.

        Returns `None` on success and raises otherwise; there is no boolean to forget to
        check.

        Raises:
            DataUnavailableError: the window opens before this series begins, or closes
                before it opens.
        """
        require_utc(window_start_utc, "window_start_utc")
        require_utc(window_end_utc, "window_end_utc")
        if window_end_utc <= window_start_utc:
            raise DataUnavailableError(
                f"{self.series.describe()} was asked for the empty window "
                f"({window_start_utc.isoformat()}, {window_end_utc.isoformat()}]"
            )
        if window_start_utc.date() < self.earliest_archive_date:
            raise DataUnavailableError(
                f"{self.series.describe()} begins at "
                f"{self.earliest_archive_date.isoformat()}, probed against the archive at "
                f"{self.probed_at_utc.isoformat()} in {self.probe_request_count} requests, "
                f"and the window opens at {window_start_utc.isoformat()}. This is earlier "
                f"than the series exists, not merely earlier than it has been ingested: no "
                f"partial series is returned, because a short series reads downstream as no "
                f"signal rather than as no data"
            )


async def probe_earliest_archive_date(
    egress: ArchiveEgress,
    *,
    source_id: str,
    symbol: str,
    today_utc: date,
    now_utc: datetime,
) -> AltAvailability:
    """Find the first archive period this symbol's series exists for.

    `today_utc` bounds the search and `now_utc` stamps the declaration; both are
    parameters rather than clock reads, so a probe replayed against a recorded egress
    resolves the same URLs it resolved the first time.

    Raises:
        DataUnavailableError: the source is unregistered, is not archive-delivered, or
            has no archive at all for this symbol within the corpus.
    """
    require_utc(now_utc, "now_utc")
    source = registered(source_id)
    if source.delivery is not Delivery.ARCHIVE:
        raise DataUnavailableError(
            f"{source_id} is declared {source.delivery.value}, so there is no archive to "
            f"probe. Its measured lag, cadence and terms are registered; reaching it is a "
            f"separate egress decision (docs/adr/0017)"
        )

    dataset = ARCHIVE_DATASET[source_id]
    granularity = ARCHIVE_GRANULARITY[source_id]
    series = source.series(symbol)
    periods = _periods(granularity, today_utc=today_utc)
    if not periods:  # pragma: no cover - the corpus genesis is always in the past
        raise DataUnavailableError(f"{series.describe()}: no archive period precedes {today_utc}")

    probe_count = 0

    async def exists(period: date) -> bool:
        nonlocal probe_count
        probe_count += 1
        url = archive_url(
            ArchiveCoordinate(
                market=ALT_MARKET, dataset=dataset, symbol=symbol, archive_date=period
            ),
            granularity,
        )
        try:
            await egress.get_text(f"{url}.CHECKSUM")
        except ArchiveUnavailableError:
            return False
        return True

    if not await exists(periods[-1]):
        raise DataUnavailableError(
            f"{series.describe()} has no archive at the most recent period "
            f"{periods[-1].isoformat()}, so its history cannot be bracketed. Either the "
            f"symbol is not listed on this market or the dataset was renamed upstream; "
            f"probing earlier periods would report a start date for a series that has "
            f"stopped"
        )

    # Invariant: `absent` never exists and `present` always does. The loop narrows the gap
    # between them, so it terminates at the boundary rather than at a period that merely
    # answered 200.
    absent = -1
    present = len(periods) - 1
    while present - absent > 1:
        midpoint = (absent + present) // 2
        if await exists(periods[midpoint]):
            present = midpoint
        else:
            absent = midpoint

    earliest = periods[present]
    # The monotonicity check. One extra request refuses a stranded island of history,
    # which a binary search would otherwise report as a start date.
    if present + 1 < len(periods) and not await exists(periods[present + 1]):
        raise DataUnavailableError(
            f"{series.describe()} answers at {earliest.isoformat()} but not at the period "
            f"immediately after it, so archive existence is not monotone for this series "
            f"and a binary search cannot name its start. Enumerate the periods before "
            f"declaring availability"
        )

    _LOG.info(
        "alt.earliest_archive_probed",
        source_id=source_id,
        symbol=symbol,
        earliest_archive_date=earliest.isoformat(),
        granularity=granularity.value,
        probe_request_count=probe_count,
    )
    return AltAvailability(
        series=series,
        earliest_archive_date=earliest,
        granularity=granularity,
        probed_at_utc=now_utc,
        probe_request_count=probe_count,
    )


def _periods(granularity: Granularity, *, today_utc: date) -> tuple[date, ...]:
    """Every archive period from the corpus genesis to the last one that can be published.

    The tail stops one period short of today: today's daily archive does not exist until
    the day is over, and this month's monthly archive does not exist until the month is.
    Including either would put a guaranteed 404 at the high end of the search, which is
    the one position the bracket cannot tolerate.
    """
    if granularity is Granularity.MONTHLY:
        months: list[date] = []
        cursor = FUTURES_UM_ARCHIVE_GENESIS.replace(day=1)
        last = today_utc.replace(day=1)
        while cursor < last:
            months.append(cursor)
            cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        return tuple(months)

    one_day = timedelta(days=1)
    days: list[date] = []
    cursor = FUTURES_UM_ARCHIVE_GENESIS
    while cursor < today_utc:
        days.append(cursor)
        cursor += one_day
    return tuple(days)
