"""Every alternative source this project has measured, and what each one is late by.

The register is here rather than in a configuration file for the same reason the archive
format table is: these are *findings*, each with a date and a method, and a finding that
lives in configuration is a finding somebody edits to make a run go through.
`SOURCES.md` is the human-facing register and carries the seven columns; this is the one
the code is written against, and `tests/data/test_alt_sources.py` asserts the two agree on
every source id.

**Nothing here is calibrated from testnet.** Futures testnet showed a 7.5bp spread against
production's 0.16bp with roughly 10x inflated volume (VF-008), so a testnet funding rate
or open interest is an execution-plumbing artefact and not a statistic.

**Two of the five are reachable today and three are not**, and the split is not a
sequencing accident:

- `binance.fundingRate` and `binance.openInterest` are files on `data.binance.vision`,
  which already has an egress path (`docs/adr/0017`). No allowlist changes.
- `alternative.me.fearGreed` and `cryptopanic.posts` are on hosts that are in no
  allowlist. Reaching either widens `ARCHIVE_HOSTS`, which is a `safety:critical` pull
  request and a decision on its own.
- `stlouisfed.fredReleases` cannot be made reachable that way *at all*. ADR-0017's own
  verification block says the decision is refuted if "ARCHIVE_HOSTS acquires a host with
  an authenticated endpoint", and FRED requires an API key. It needs a superseding ADR
  introducing a third egress class for credentialed data sources.

Their measurements are kept regardless, because the measurement is the expensive part and
`Delivery.EGRESS_NOT_PROVISIONED` says exactly what is missing without pretending
otherwise.

**A parser is registered separately from a source**, in `fking.data.alt.ingest`. "Is this
source declared" and "can this source's payload be read" are different questions -- the
same split `fking.data.loaders` keeps between `DECLARED_FORMATS` and its parser table --
and a source can acquire the first without the second. `binance.openInterest` is exactly
that case today: its archive is fetchable and probeable, and its first column is
`create_time,2024-01-02 00:00:00`, a naive datetime string that `ArchiveFormat.epoch_unit`
has no member for.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType
from typing import Final

from fking.data.alt.spec import AltSourceSpec, Delivery, Revision
from fking.data.archive import Granularity
from fking.data.format_resolver import Dataset
from fking.platform.errors import DataUnavailableError

__all__ = [
    "ALT_SOURCES",
    "ARCHIVE_DATASET",
    "ARCHIVE_GRANULARITY",
    "BINANCE_FUNDING_RATE",
    "BINANCE_OPEN_INTEREST",
    "CRYPTOPANIC_POSTS",
    "FEAR_AND_GREED",
    "FRED_RELEASES",
    "registered",
]

# The realised rate for a settlement is broadcast at that settlement over
# `<symbol>@markPrice@1s` (DATA_PIPELINE.md section 5). One minute is the envelope for
# stream delivery and a reconnect, and it is 1/480 of a settlement interval -- large
# enough that the boundary is real, small enough that it changes no feature's semantics.
# It is deliberately not the archive's own publication delay: `available_at_utc` is the
# earliest instant this system *could* have known the value, and a live system does not
# wait for a monthly zip.
_FUNDING_PUBLICATION_LAG: Final[timedelta] = timedelta(minutes=1)

# The archived open-interest series is the five-minute aggregate, so the value stamped at
# T is only in the series once T's period has closed.
_OPEN_INTEREST_SAMPLING_PERIOD: Final[timedelta] = timedelta(minutes=5)

# Measured 2026-08-05T06:05Z: the value stamped 2026-08-05T00:00Z carried
# `time_until_update` = 64,448 s, i.e. it is refreshed at the *end* of the day it is
# stamped with. Treating it as knowable at the start of that day is look-ahead of up to a
# full day, so the value stamped for day D is declared knowable from D+1 00:00Z.
_FEAR_AND_GREED_LAG: Final[timedelta] = timedelta(hours=24)

# A floor, not an estimate. No macro release is published before its observation period
# ends, and every FRED series carries its real release instant in `release/dates`, which
# `AltObservation.published_at_utc` is for. This value exists only to refuse a release
# instant that contradicts the observation period.
_FRED_MINIMUM_RELEASE_LAG: Final[timedelta] = timedelta(days=1)

# CryptoPanic timestamps a *republication*, not a first print. Five minutes is the polling
# envelope on top of that. The number is not the problem with this source; the seam is,
# and it is stated in `terms_position` and in SOURCES.md.
_CRYPTOPANIC_POLL_LAG: Final[timedelta] = timedelta(minutes=5)


BINANCE_FUNDING_RATE: Final = AltSourceSpec(
    source_id="binance.fundingRate",
    delivery=Delivery.ARCHIVE,
    availability_lag=_FUNDING_PUBLICATION_LAG,
    cadence=timedelta(hours=8),
    unit="dimensionless funding rate fraction applied at one settlement",
    requires_credential=False,
    revision=Revision.FINAL,
    terms_position=(
        "data.binance.vision is a public bulk archive served without authentication and "
        "automated download is the intended use; checked 2026-08-05"
    ),
    provenance=(
        "cadence and layout read from BTCUSDT-fundingRate-2020-01 on 2026-08-05; the lag "
        "is the markPrice stream delivery envelope, not the archive publication delay"
    ),
)

BINANCE_OPEN_INTEREST: Final = AltSourceSpec(
    source_id="binance.openInterest",
    delivery=Delivery.ARCHIVE,
    availability_lag=_OPEN_INTEREST_SAMPLING_PERIOD,
    cadence=_OPEN_INTEREST_SAMPLING_PERIOD,
    unit="open interest in base quantity, summed across the perpetual",
    requires_credential=False,
    revision=Revision.FINAL,
    terms_position=(
        "data.binance.vision is a public bulk archive served without authentication and "
        "automated download is the intended use; checked 2026-08-05"
    ),
    provenance=(
        "cadence read from BTCUSDT-metrics-2024-01-02 on 2026-08-05: rows five minutes "
        "apart, stamped create_time as a naive datetime string rather than an epoch"
    ),
)

FEAR_AND_GREED: Final = AltSourceSpec(
    source_id="alternative.me.fearGreed",
    delivery=Delivery.EGRESS_NOT_PROVISIONED,
    availability_lag=_FEAR_AND_GREED_LAG,
    cadence=timedelta(hours=24),
    unit="index in [0, 100], 0 extreme fear and 100 extreme greed",
    requires_credential=False,
    revision=Revision.FINAL,
    terms_position=(
        "public API with no stated restriction on automated use; measured HTTP 200 in "
        "0.6 s without credentials on 2026-08-03 and again on 2026-08-05"
    ),
    provenance=(
        "lag derived from the response's own time_until_update field, measured "
        "2026-08-05T06:05Z: 64,448 s remaining on the value stamped that day at 00:00Z"
    ),
)

CRYPTOPANIC_POSTS: Final = AltSourceSpec(
    source_id="cryptopanic.posts",
    delivery=Delivery.EGRESS_NOT_PROVISIONED,
    availability_lag=_CRYPTOPANIC_POLL_LAG,
    cadence=timedelta(minutes=1),
    unit="count of posts matching a filter within one polling window",
    requires_credential=True,
    revision=Revision.FINAL,
    terms_position=(
        "API key required and the free-tier terms have not been read; measured HTTP 404 "
        "on developer/v2 without a key and HTTP 403 from Cloudflare on v1 with a "
        "placeholder token, 2026-08-03. Unusable until the terms are read"
    ),
    provenance=(
        "an aggregator: its timestamp is the republication instant, not the first print, "
        "so this lag is a polling envelope and not an information lag. Clustering "
        "republications to one event with a first-print timestamp is required before this "
        "can be a feature at all"
    ),
)

FRED_RELEASES: Final = AltSourceSpec(
    source_id="stlouisfed.fredReleases",
    delivery=Delivery.EGRESS_NOT_PROVISIONED,
    availability_lag=_FRED_MINIMUM_RELEASE_LAG,
    cadence=timedelta(days=1),
    unit="series-dependent; the release calendar is what this source contributes",
    requires_credential=True,
    revision=Revision.REVISED,
    terms_position=(
        "free API key by registration, attribution required, automated use permitted; "
        "measured HTTP 400 without a key on 2026-08-03. The per-key rate limit has not "
        "been obtained (OQ-001)"
    ),
    provenance=(
        "fred/release/dates returns the release calendar as a first-class resource, so "
        "available_at is published rather than inferred; the declared lag is only the "
        "floor that refuses a release instant predating its own observation period"
    ),
)

ALT_SOURCES: Final[Mapping[str, AltSourceSpec]] = MappingProxyType(
    {
        source.source_id: source
        for source in (
            BINANCE_FUNDING_RATE,
            BINANCE_OPEN_INTEREST,
            FEAR_AND_GREED,
            CRYPTOPANIC_POSTS,
            FRED_RELEASES,
        )
    }
)

# Which `data.binance.vision` dataset backs each archive-delivered source, and at which
# granularity. The granularity is a property of the dataset and not of the calendar:
# measured 2026-08-05, `monthly/fundingRate/...` answers 200 while `daily/fundingRate/...`
# 404s, and `daily/metrics/...` answers 200 while `monthly/metrics/...` 404s. So
# `resolve_granularity`, which chooses by distance from today, is wrong for both of them
# in opposite directions, and every fetch here states its granularity explicitly.
ARCHIVE_DATASET: Final[Mapping[str, Dataset]] = MappingProxyType(
    {
        BINANCE_FUNDING_RATE.source_id: Dataset.FUNDING_RATE,
        BINANCE_OPEN_INTEREST.source_id: Dataset.METRICS,
    }
)

ARCHIVE_GRANULARITY: Final[Mapping[str, Granularity]] = MappingProxyType(
    {
        BINANCE_FUNDING_RATE.source_id: Granularity.MONTHLY,
        BINANCE_OPEN_INTEREST.source_id: Granularity.DAILY,
    }
)


def registered(source_id: str) -> AltSourceSpec:
    """The declaration for one source, or a refusal naming the ones that exist.

    Raises:
        DataUnavailableError: no source is registered under that id.
    """
    source = ALT_SOURCES.get(source_id)
    if source is None:
        raise DataUnavailableError(
            f"no alternative source is registered as {source_id!r}; registered sources are "
            f"{sorted(ALT_SOURCES)}. A source is registered with its measured availability "
            f"lag, cadence and terms position, never before them (SOURCES.md)"
        )
    return source
