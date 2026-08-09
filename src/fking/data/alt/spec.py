"""What an alternative source must declare before a single value of it is stored.

A bar is late by the length of its own interval and no more. Everything in this module is
late by something a *publisher* chose: a funding rate settles and is broadcast, an index
is stamped with a day and refreshed at the end of that day, a statistic has an observation
period in June and a release at 08:30 on 26 August. `docs/rules/no-lookahead.md` opens
on the resulting defect and it is the reason this file exists rather than a `dict` of
URLs: **joining any of these on `event_time` produces a backtest that knew the number
before it was published**, the gap is larger here than anywhere else in the corpus, and it
is visible in the source, which makes it easy to shrug off.

Three shapes carry the guarantee.

**`availability_lag` is required and must be strictly positive.** Zero is a legal
declaration for a value derived only from a bar that has closed (`FeatureSpec` permits
it), and it is a lie for everything here -- every one of these values is published by
somebody after the instant it is stamped with. A field with no default cannot be
forgotten; a field that rejects zero cannot be filled in with the permissive answer by
somebody who has not looked up the number.

**`available_at_utc` is derived, never supplied.** `AltPoint` has no public constructor
path from a caller: `AltSourceSpec.point()` is the only way to make one, and it computes
`event_time_utc + availability_lag` itself. A writer that could pass its own
`available_at_utc` could claim a value was knowable earlier than the declaration says, and
the declaration is the only thing standing between this data and a backtest that reads the
future.

**`terms_position` is a required, non-blank sentence, and it is recorded before
ingestion.** `SOURCES.md` states the rule; this is where the code refuses to hold a source
that has not satisfied it. The honest outcomes include "this source is not usable", which
is a result worth writing down once rather than rediscovering, and `Delivery` is how a
source says so without pretending to be fetchable.

The unit of `observed_value` is a property of the source, not of a row, so it is declared
once here as `unit` and never carried per observation. That is why the store's column can
be generically named: a funding rate is a dimensionless fraction, an open interest is a
base quantity, and the Fear & Greed index is an integer between 0 and 100, and a column
that tried to name all three would name none of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from fking.platform.errors import DataIntegrityError, FeatureContractError

__all__ = [
    "GLOBAL_SERIES",
    "AltObservation",
    "AltPoint",
    "AltSeriesRef",
    "AltSourceSpec",
    "Delivery",
    "Revision",
]

_UTC_OFFSET: Final[timedelta] = timedelta(0)

# A proof shorter than this states nothing checkable. Same bound and same reasoning as
# `FeatureSpec.point_in_time_proof`: the check is against an empty or placeholder string,
# not an attempt to grade prose.
_MINIMUM_STATEMENT_CHARACTERS: Final[int] = 20

# The series id for a source that publishes one worldwide series rather than one per
# instrument. A literal rather than an empty string, because an empty string is what a
# missing value looks like and this is a present one.
GLOBAL_SERIES: Final[str] = "GLOBAL"


class Delivery(StrEnum):
    """How -- and whether -- this system can currently reach a source.

    `EGRESS_NOT_PROVISIONED` is a first-class member rather than an omission. A source
    whose lag, cadence and terms have been measured is knowledge worth keeping even when
    the host is in no allowlist, and the alternative shapes are both worse: leaving it out
    of the registry loses the measurement, and pretending it is fetchable produces a
    stub that looks finished.
    """

    ARCHIVE = "archive"
    EGRESS_NOT_PROVISIONED = "egress_not_provisioned"


class Revision(StrEnum):
    """Whether a published value is ever restated.

    `REVISED` is not a hint. It is the difference between a series whose `event_time` is
    a sufficient key and one where it is not: a revised value arrives as a *new row* with
    a later `available_at_utc`, both survive, and an `as_of` before the revision returns
    the print that was actually believed then. A source declared `FINAL` that turns out to
    revise would silently collide on the store's primary key and lose the correction.
    """

    FINAL = "final"
    REVISED = "revised"


@dataclass(frozen=True, slots=True)
class AltSeriesRef:
    """One series of one source: which publisher, which instrument or release.

    `series_id` is a symbol for a per-instrument source and `GLOBAL_SERIES` for a
    worldwide one. It is deliberately not `symbol`: an economic release has no
    instrument, and a field named `symbol` holding `"GDP"` is a field that will be joined
    against the venue's symbol table by somebody reading the column name.
    """

    source_id: str
    series_id: str

    def describe(self) -> str:
        return f"{self.source_id}:{self.series_id}"


@dataclass(frozen=True, slots=True)
class AltObservation:
    """One value as its publisher stamped it, before this system decides when it knew it.

    `published_at_utc` is the source's *own* statement of when it released the value, and
    it is `None` for every source that does not make one. This is the distinction the
    macro sources force: FRED publishes `release/dates` and BEA a forward-dated schedule,
    so for those series `available_at` is **published rather than inferred** -- Q2 GDP has
    an event time in June and an availability time of 08:30 on 26 August, and no fixed lag
    can express that. A feature keyed on the observation period is look-ahead by weeks.

    When it is `None`, `AltSourceSpec.point()` falls back to the declared lag. When it is
    set it wins, and it is checked against the declared lag as a floor -- a release
    claiming to predate the source's own minimum publication delay is a contradiction, not
    a fast source.
    """

    event_time_utc: datetime
    observed_value: Decimal
    published_at_utc: datetime | None = None


@dataclass(frozen=True, slots=True)
class AltPoint:
    """One observation with the instant this system could first have known it.

    Construct only through `AltSourceSpec.point()`. Building one directly is possible in
    Python and is a defect: it is the single step at which a declared lag can be bypassed,
    and `tests/data/test_alt_sources.py` asserts the derivation rather than trusting it.
    """

    series: AltSeriesRef
    event_time_utc: datetime
    available_at_utc: datetime
    observed_value: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class AltSourceSpec:
    """A registrable alternative source.

    Keyword-only and fully required. The fields somebody would default are
    `availability_lag=0` and `revises=FINAL`, and both are the permissive answer.
    """

    source_id: str
    delivery: Delivery
    #: `event_time_utc` -> `available_at_utc`. Strictly positive; see the module docstring.
    availability_lag: timedelta
    #: How often the publisher emits a value. Sizes the gap detector, not the lag.
    cadence: timedelta
    unit: str
    requires_credential: bool
    revision: Revision
    #: The terms-of-service clause that permits or forbids automated retrieval, quoted or
    #: paraphrased with its date. Recorded before ingestion, per `SOURCES.md`.
    terms_position: str
    #: Where `availability_lag` and `cadence` came from: a measurement, a header the
    #: response carries, or a published schedule. A lag with no provenance is a lag that
    #: will be "corrected" downward by the next person who finds it inconvenient.
    provenance: str

    def __post_init__(self) -> None:
        _require_text(self.source_id, "source_id")
        _require_text(self.unit, "unit")
        if self.availability_lag <= timedelta(0):
            raise FeatureContractError(
                f"{self.source_id} declares availability_lag={self.availability_lag}; every "
                f"alternative source is published after the instant it stamps, so a "
                f"non-positive lag is a claim that the value was knowable before it existed "
                f"(docs/rules/no-lookahead.md)"
            )
        if self.cadence <= timedelta(0):
            raise FeatureContractError(
                f"{self.source_id} declares cadence={self.cadence}; a source that never "
                f"publishes has no series"
            )
        for field_name, statement in (
            ("terms_position", self.terms_position),
            ("provenance", self.provenance),
        ):
            text = _require_text(statement, field_name)
            if len(text) < _MINIMUM_STATEMENT_CHARACTERS:
                raise FeatureContractError(
                    f"{field_name} for {self.source_id} is {len(text)} characters and states "
                    f"nothing checkable; SOURCES.md requires the position on automated use "
                    f"and the origin of the declared numbers, before ingestion rather than "
                    f"after"
                )

    def point(self, series: AltSeriesRef, observation: AltObservation) -> AltPoint:
        """The observation plus the instant this system could first have known it.

        The only construction path for an `AltPoint`. `available_at_utc` is computed here
        from the declaration, so no caller anywhere can assert an earlier one.

        Raises:
            DataIntegrityError: the series belongs to a different source, a timestamp is
                not aware UTC, or a declared release instant predates the declared lag.
        """
        if series.source_id != self.source_id:
            raise DataIntegrityError(
                f"series {series.describe()} does not belong to source {self.source_id!r}; "
                f"a point built from another source's declaration would carry that "
                f"source's lag"
            )
        event_time_utc = require_utc(observation.event_time_utc, "event_time_utc")
        earliest_knowable_utc = event_time_utc + self.availability_lag
        available_at_utc = earliest_knowable_utc
        if observation.published_at_utc is not None:
            available_at_utc = require_utc(observation.published_at_utc, "published_at_utc")
            if available_at_utc < earliest_knowable_utc:
                raise DataIntegrityError(
                    f"{series.describe()} declares a release at "
                    f"{available_at_utc.isoformat()} for an event at "
                    f"{event_time_utc.isoformat()}, which is inside the source's own "
                    f"declared minimum publication delay of {self.availability_lag}. One of "
                    f"the two is wrong, and taking the earlier one would move the whole "
                    f"series forward in time"
                )
        return AltPoint(
            series=series,
            event_time_utc=event_time_utc,
            available_at_utc=available_at_utc,
            observed_value=observation.observed_value,
        )

    def series(self, series_id: str) -> AltSeriesRef:
        """A reference to one of this source's series, so the two ids cannot be mismatched."""
        return AltSeriesRef(source_id=self.source_id, series_id=series_id)


def require_utc(candidate: datetime, field_name: str) -> datetime:
    """Aware, and exactly UTC. Rejected rather than converted.

    `astimezone(UTC)` would launder an offset guessed wrong upstream into a confident
    value, and in a 24/7 market there is no session boundary to make the resulting shift
    visible.
    """
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise DataIntegrityError(f"{field_name} must be timezone-aware; got naive {candidate!r}")
    if candidate.utcoffset() != _UTC_OFFSET:
        raise DataIntegrityError(
            f"{field_name} must be UTC; got offset {candidate.utcoffset()!r} in {candidate!r}"
        )
    return candidate


def _require_text(candidate: str, field_name: str) -> str:
    if not candidate.strip():
        raise FeatureContractError(f"{field_name} must not be blank")
    return candidate
