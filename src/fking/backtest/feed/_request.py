"""What a caller asks the feed for, and the lattice that request implies.

Three parts of the request are load-bearing and each has an obvious wrong reading.

**The window is stated on bar *open* times, half-open `[exposed_from_utc, until_utc)`.**
The corpus keys a bar on its open (`fking.data.loaders.records.KlineRecord.event_time_utc`),
so stating the window any other way means the request and the corpus disagree about which
bar a boundary refers to -- and the disagreement is exactly one bar, at each end, forever.
The event the loop dispatches is still stamped at the bar's *close*, because that is the
first instant its high, low and close are facts; the two are different questions and this
type answers the first.

**Warm-up is stated in bars, not in a duration.** A feature needs a number of observations,
not an elapsed time, and the two differ the moment a series has a gap in it -- which is
precisely the condition the coverage gate exists for. `warmup_start_utc` is derived from the
count and the interval, so the warm-up span is part of the coverage requirement rather than
something checked afterwards.

**The window must be aligned to the interval lattice.** Binance anchors bars at the Unix
epoch, so a 1h window opening at 09:30 names no bar the archive contains. Left unchecked,
every bar in the window reads as missing and the coverage report blames the corpus for the
request.

The guards here raise `FeedRequestError` rather than reusing `fking.backtest._guards`,
whose failures are `RunConfigError`. The distinction is worth four duplicated lines: a
malformed *run identity* and a malformed *data request* are answered by different people --
one edits the strategy config, the other narrows the window or backfills -- and collapsing
them would put both under an error class whose name names neither.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from fking.backtest.feed._errors import FeedRequestError
from fking.backtest.feed._intervals import interval_duration
from fking.data.format_resolver import Market
from fking.domain import Instrument

__all__ = ["FeedRequest", "SeriesRequest"]

# Binance anchors every kline boundary at the Unix epoch, so lattice alignment is measured
# from here rather than from the window's own start.
_LATTICE_ANCHOR_UTC: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)
_ZERO: Final[timedelta] = timedelta(0)


def _require_utc(candidate: datetime, field_name: str) -> None:
    if candidate.tzinfo is None or candidate.utcoffset() != _ZERO:
        raise FeedRequestError(
            f"{field_name} must be timezone-aware UTC; got {candidate!r}. An offset window "
            f"boundary selects a different set of bars than the one it appears to name"
        )


@dataclass(frozen=True, slots=True)
class SeriesRequest:
    """One `(market, instrument)` the run subscribes to.

    The `Instrument` rather than a bare symbol, because a `Bar` carries one and no archive
    file contains the venue filters it holds. A feed that invented a `lot_step` would let a
    backtest fill quantities the venue would have refused, which is the failure
    `fking.domain.Instrument` exists to prevent -- so the filters arrive from whatever holds
    the instrument definitions, and the feed refuses to guess them.

    `market` is separate from `instrument.venue` on purpose. A venue is where an order goes;
    a market is which `data.binance.vision` corpus a bar came out of, and the two are
    different facts that happen to correlate today. The epoch-unit split is keyed on the
    second (`fking.data.format_resolver`), so reading it off the first would be an
    inference standing in for a declaration.
    """

    market: Market
    instrument: Instrument

    @property
    def symbol(self) -> str:
        """The instrument's symbol, as the corpus partitions it."""
        return self.instrument.symbol

    @property
    def label(self) -> str:
        """`market/symbol`, the key this series is reported and ordered under."""
        return f"{self.market.value}/{self.symbol}"


@dataclass(frozen=True, slots=True)
class FeedRequest:
    """One window of one interval over a set of series, with its warm-up span.

    `warmup_bar_count` of zero is permitted and means the strategy is exposed from the
    first bar. It is not the default: a feature with a lookback and no warm-up computes its
    first values from a partially-filled window, and those values land in the sample as
    though they were real, inflating the early part of every equity curve in the same
    direction.
    """

    series: tuple[SeriesRequest, ...]
    bar_interval: str
    exposed_from_utc: datetime
    until_utc: datetime
    warmup_bar_count: int

    def __post_init__(self) -> None:
        if not self.bar_interval.strip():
            raise FeedRequestError("bar_interval must not be blank")
        _require_utc(self.exposed_from_utc, "exposed_from_utc")
        _require_utc(self.until_utc, "until_utc")
        if not isinstance(self.warmup_bar_count, int) or isinstance(self.warmup_bar_count, bool):
            raise FeedRequestError(
                f"warmup_bar_count must be an int, got {self.warmup_bar_count!r}"
            )
        if self.warmup_bar_count < 0:
            raise FeedRequestError(
                f"warmup_bar_count must not be negative; got {self.warmup_bar_count}"
            )
        if not self.series:
            raise FeedRequestError("a feed request must name at least one series")

        keys = tuple((entry.market, entry.symbol) for entry in self.series)
        if len(set(keys)) != len(keys):
            # Not merely untidy: a duplicated series is read twice, so every bar is
            # dispatched twice and every count derived from the run doubles for it alone.
            raise FeedRequestError(
                f"series names the same (market, symbol) twice: "
                f"{sorted((market.value, symbol) for market, symbol in keys)}"
            )

        duration = interval_duration(self.bar_interval)
        if self.until_utc <= self.exposed_from_utc:
            raise FeedRequestError(
                f"until_utc {self.until_utc.isoformat()} must follow exposed_from_utc "
                f"{self.exposed_from_utc.isoformat()}"
            )
        for name, moment in (
            ("exposed_from_utc", self.exposed_from_utc),
            ("until_utc", self.until_utc),
        ):
            if (moment - _LATTICE_ANCHOR_UTC) % duration != _ZERO:
                raise FeedRequestError(
                    f"{name} {moment.isoformat()} is not on the {self.bar_interval} lattice, "
                    f"which Binance anchors at {_LATTICE_ANCHOR_UTC.isoformat()}. An unaligned "
                    f"boundary names no bar the archive contains, so every bar in the window "
                    f"would be reported missing and the coverage report would blame the corpus "
                    f"for the request"
                )

    @property
    def duration(self) -> timedelta:
        """How long one bar of this request's interval lasts."""
        return interval_duration(self.bar_interval)

    @property
    def warmup_start_utc(self) -> datetime:
        """The open time of the first warm-up bar, which is where the run's data begins."""
        return self.exposed_from_utc - self.duration * self.warmup_bar_count

    @property
    def expected_bar_count(self) -> int:
        """How many bars each series must hold across warm-up and exposure together."""
        return self.warmup_bar_count + (self.until_utc - self.exposed_from_utc) // self.duration

    def lattice(self) -> Iterator[datetime]:
        """Every open time the corpus must hold, warm-up first, in ascending order."""
        duration = self.duration
        candidate = self.warmup_start_utc
        while candidate < self.until_utc:
            yield candidate
            candidate += duration

    def is_warmup(self, open_time_utc: datetime) -> bool:
        """Whether a bar opening at this instant belongs to the warm-up phase."""
        return open_time_utc < self.exposed_from_utc
