"""Building an hour-of-day spread profile from production `bookTicker` observations.

The provenance check runs *here* as well as on `CostModel`, and the duplication is
deliberate. The model's validator stops a testnet-sourced profile from being *used*; this
one stops it from being *built*, which is several hours earlier and is where the person
who would have to undo the work still remembers where the samples came from.

Quantiles are selected by **nearest rank** rather than interpolated between order
statistics. Interpolation needs a fractional weight, and a fractional weight applied to a
`Decimal` order statistic is the one place in this subpackage where a float would have
had to appear -- in the field that decides whether a strategy is profitable. Nearest rank
is exact, deterministic across platforms, and at p99 it rounds *up* to the next observed
spread rather than averaging toward the cheaper neighbour.

Every UTC hour must be observed. An hour with no samples cannot be filled from the daily
median without reintroducing exactly the funding-hour subsidy the profile exists to
remove, so it is refused instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, field_validator

from fking.backtest.costs._errors import CostModelConfigError
from fking.backtest.costs._provenance import require_production_provenance
from fking.backtest.costs._spread import HOURS_IN_DAY, SpreadQuantiles, SymbolSpreadProfile
from fking.backtest.costs._units import NonNegativeBps

_NO_OFFSET: Final = timedelta(0)
_PERCENT: Final = 100
_P50_RANK: Final = 50
_P99_RANK: Final = 99


class SpreadObservation(BaseModel):
    """One production `bookTicker` sample: a quoted spread at an instant."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    observed_at_utc: datetime
    spread_bps: NonNegativeBps

    @field_validator("observed_at_utc")
    @classmethod
    def _observation_time_is_utc(cls, candidate: datetime) -> datetime:
        if candidate.tzinfo is None or candidate.utcoffset() is None:
            raise ValueError(f"observed_at_utc must be timezone-aware; got naive {candidate!r}")
        if candidate.utcoffset() != _NO_OFFSET:
            raise ValueError(
                f"observed_at_utc must be UTC; got offset {candidate.utcoffset()!r}; an "
                f"offset here shifts every sample into the wrong hour bucket"
            )
        return candidate


def _nearest_rank(ordered: Sequence[Decimal], rank_pct: int) -> Decimal:
    """The nearest-rank order statistic: element ceil(rank_pct/100 * n), one-indexed."""
    position = -(-rank_pct * len(ordered) // _PERCENT)
    return ordered[position - 1]


def calibrate_spread_profile(
    observations: Sequence[SpreadObservation], *, calibration_source: str
) -> SymbolSpreadProfile:
    """Bucket `observations` by UTC hour and take p50 and p99 of each bucket.

    Raises `CalibrationProvenanceError` when `calibration_source` names testnet, and
    `CostModelConfigError` when any UTC hour is unobserved.
    """
    require_production_provenance(calibration_source, "calibration_source")

    buckets: dict[int, list[Decimal]] = {hour: [] for hour in range(HOURS_IN_DAY)}
    for observation in observations:
        buckets[observation.observed_at_utc.hour].append(observation.spread_bps)

    unobserved = sorted(hour for hour, samples in buckets.items() if not samples)
    if unobserved:
        raise CostModelConfigError(
            f"UTC hours {unobserved} carry no spread observation in {calibration_source!r}; "
            f"filling them from the daily median would restore the funding-hour subsidy "
            f"the hour-of-day profile exists to remove"
        )

    hourly = {}
    for hour, samples in buckets.items():
        ordered = sorted(samples)
        hourly[hour] = SpreadQuantiles(
            p50_bps=_nearest_rank(ordered, _P50_RANK),
            p99_bps=_nearest_rank(ordered, _P99_RANK),
        )
    return SymbolSpreadProfile(hourly=hourly)
