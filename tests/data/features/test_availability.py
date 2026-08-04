"""The store refuses data the system does not have, and says what it does have.

The four refusals below are not four spellings of one check. Each closes a different way
a strategy ends up scored on a window it never saw:

1. An input no free source provides -- the LLM-authored request for
   `order_book_imbalance_top_10_levels`, which exists in the literature and not here.
2. A real dataset this symbol has no history for.
3. A window that opens before the corpus does, which otherwise returns a *short* series
   that reads downstream as "no signal" rather than as "no data".
4. A window running through a recorded hole, same failure, different cause.

`refuses_if_unavailable` is asserted at runtime as well as by `mypy --strict`, because the
construction that would carry `False` is the one built from an untyped mapping that no
type checker ever saw.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from fking.data.features.availability import (
    THE_CEILING,
    AvailabilityContract,
    AvailabilityDeclaration,
    AvailabilityGap,
    SeriesAddress,
)
from fking.data.features.registry import registered
from fking.data.features.spec import FeatureSpec
from fking.data.format_resolver import Dataset, Market
from fking.platform.errors import DataUnavailableError, FeatureContractError
from tests.support.availability import permitting

pytestmark = pytest.mark.unit

_EARLIEST = datetime(2020, 1, 1, tzinfo=UTC)
_LATEST = datetime(2026, 8, 1, tzinfo=UTC)
_KLINES = registered("trailing_return_fraction", 1)


def _feature_wanting(input_name: str) -> FeatureSpec:
    """A spec identical to a registered one except for the input it asks for.

    Never registered: the point is that the availability contract refuses it on the
    strength of its declaration alone, before anybody decides whether to register it.
    """
    return FeatureSpec(
        name="mean_reversion_candidate",
        version=1,
        compute=_KLINES.compute,
        inputs=frozenset({input_name}),
        lookback=timedelta(hours=1),
        availability_lag=timedelta(0),
        label_horizon=timedelta(hours=1),
        point_in_time_proof=(
            "Trailing window (t-1h, t] over closed observations; both endpoints existed at t."
        ),
        uses_trailing_statistics_only=True,
    )


def _require(
    contract: AvailabilityContract,
    spec: FeatureSpec,
    *,
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> None:
    contract.require(
        spec,
        market=Market.SPOT,
        symbol="BTCUSDT",
        window_start_utc=window_start_utc,
        window_end_utc=window_end_utc,
    )


# ---------------------------------------------------------------------------
# 1. The ceiling: inputs no free source provides
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input_name",
    [
        "order_book_imbalance_top_10_levels",
        "l2_book_snapshot",
        "orderbook_depth_5",
        "price_level_queue",
    ],
)
def test_an_l2_depth_input_is_refused_and_the_refusal_names_the_alternatives(
    input_name: str,
) -> None:
    """The refusal redirects the research rather than dead-ending it.

    `DATA_PIPELINE.md` section 9: free full-depth L2 order book history does not exist,
    and `bookDepth` is ten cumulative bands sampled about once a minute -- a shape
    statistic, not a book. A caller told only "unknown feature" goes looking for a bug in
    the registry.
    """
    contract = permitting(earliest_event_time_utc=_EARLIEST, latest_event_time_utc=_LATEST)
    with pytest.raises(DataUnavailableError) as refused:
        _require(
            contract,
            _feature_wanting(input_name),
            window_start_utc=_EARLIEST,
            window_end_utc=_LATEST,
        )
    message = str(refused.value)
    assert "full-depth L2 order book history does not exist" in message
    for alternative in ("tick trades", "bookTicker", "bookDepth", "once a minute", "klines"):
        assert alternative in message


@pytest.mark.parametrize("input_name", ["queue_position_estimate", "passive_fill_rate"])
def test_a_queue_or_passive_fill_input_names_the_l3_ceiling_specifically(input_name: str) -> None:
    """A different ceiling, and worth its own sentence.

    Depth bands at least exist; order-by-order data does not exist at any resolution here.
    `market-research` returns `None` for passive fill probability for this reason -- a
    fabricated number is worse than an admitted absence, because it propagates into
    sizing.
    """
    contract = permitting(earliest_event_time_utc=_EARLIEST, latest_event_time_utc=_LATEST)
    with pytest.raises(DataUnavailableError, match="order-by-order"):
        _require(
            contract,
            _feature_wanting(input_name),
            window_start_utc=_EARLIEST,
            window_end_utc=_LATEST,
        )


def test_an_input_that_names_no_dataset_at_all_still_lists_what_exists() -> None:
    """Not every bad input is a known ceiling. A typo gets the same courtesy."""
    contract = permitting(earliest_event_time_utc=_EARLIEST, latest_event_time_utc=_LATEST)
    with pytest.raises(DataUnavailableError) as refused:
        _require(
            contract,
            _feature_wanting("kline"),
            window_start_utc=_EARLIEST,
            window_end_utc=_LATEST,
        )
    assert "names no dataset this pipeline ingests" in str(refused.value)
    assert THE_CEILING in str(refused.value)


# ---------------------------------------------------------------------------
# 2. A real dataset this symbol has no history for
# ---------------------------------------------------------------------------


def test_a_real_dataset_the_corpus_has_not_ingested_is_refused_and_names_what_is_held() -> None:
    """The actionable answer here is a backfill, so the message carries the command."""
    contract = permitting(earliest_event_time_utc=_EARLIEST, latest_event_time_utc=_LATEST)
    with pytest.raises(DataUnavailableError) as refused:
        _require(
            contract,
            _feature_wanting(Dataset.TRADES.value),
            window_start_utc=_EARLIEST,
            window_end_utc=_LATEST,
        )
    message = str(refused.value)
    assert "does not hold for spot BTCUSDT" in message
    assert "spot BTCUSDT klines (1m)" in message
    assert "make ingest" in message


def test_a_symbol_the_corpus_holds_nothing_for_says_so_rather_than_listing_another_symbol() -> None:
    contract = permitting(earliest_event_time_utc=_EARLIEST, latest_event_time_utc=_LATEST)
    with pytest.raises(DataUnavailableError, match="nothing at all"):
        contract.require(
            _KLINES,
            market=Market.SPOT,
            symbol="SOMECOIN",
            window_start_utc=_EARLIEST,
            window_end_utc=_LATEST,
        )


# ---------------------------------------------------------------------------
# 3. Windows outside the declared range
# ---------------------------------------------------------------------------


def test_a_window_opening_before_the_declared_earliest_is_refused_and_names_the_date() -> None:
    """The date, not just the fact. "Move to 2020-01-01" is actionable; "too early" is not."""
    contract = permitting(earliest_event_time_utc=_EARLIEST, latest_event_time_utc=_LATEST)
    with pytest.raises(DataUnavailableError) as refused:
        _require(
            contract,
            _KLINES,
            window_start_utc=_EARLIEST - timedelta(days=1),
            window_end_utc=_EARLIEST + timedelta(days=1),
        )
    assert "begins at 2020-01-01" in str(refused.value)
    assert "no partial series is returned" in str(refused.value)


def test_a_window_closing_after_the_declared_latest_is_refused() -> None:
    """The other end, and the one that bites during live operation rather than research:
    the corpus stops at yesterday's archive and a run asked for today."""
    contract = permitting(earliest_event_time_utc=_EARLIEST, latest_event_time_utc=_LATEST)
    with pytest.raises(DataUnavailableError, match="ends at 2026-08-01"):
        _require(
            contract,
            _KLINES,
            window_start_utc=_LATEST - timedelta(days=1),
            window_end_utc=_LATEST + timedelta(days=1),
        )


def test_a_window_wholly_inside_the_declared_range_is_permitted() -> None:
    """The other half. A contract that refuses everything is not a contract, it is an
    outage nobody notices until a backtest reports no trades."""
    contract = permitting(earliest_event_time_utc=_EARLIEST, latest_event_time_utc=_LATEST)
    _require(
        contract,
        _KLINES,
        window_start_utc=_EARLIEST + timedelta(days=1),
        window_end_utc=_LATEST - timedelta(days=1),
    )


# ---------------------------------------------------------------------------
# 4. Known gaps
# ---------------------------------------------------------------------------


_GAP = AvailabilityGap(
    gap_start_utc=datetime(2021, 5, 3, tzinfo=UTC),
    gap_end_utc=datetime(2021, 5, 4, tzinfo=UTC),
    gap_kind="cadence",
    bar_interval="1m",
    missing_bar_count=1440,
)


@pytest.mark.parametrize(
    ("window_start_utc", "window_end_utc"),
    [
        # Straddling the whole gap.
        (datetime(2021, 5, 1, tzinfo=UTC), datetime(2021, 5, 6, tzinfo=UTC)),
        # Ending one hour inside it: a window short by one hour is still short.
        (datetime(2021, 5, 1, tzinfo=UTC), datetime(2021, 5, 3, 1, tzinfo=UTC)),
        # Beginning one hour before it ends.
        (datetime(2021, 5, 3, 23, tzinfo=UTC), datetime(2021, 5, 6, tzinfo=UTC)),
    ],
)
def test_any_intersection_with_a_recorded_gap_is_refused(
    window_start_utc: datetime, window_end_utc: datetime
) -> None:
    """Overlap, not containment.

    A window running one bar into a hole comes back one bar short, and a short series is
    the failure this whole check exists for -- it reads as an absence of signal rather
    than as an absence of data.
    """
    contract = permitting(
        earliest_event_time_utc=_EARLIEST,
        latest_event_time_utc=_LATEST,
        known_gaps=[_GAP],
    )
    with pytest.raises(DataUnavailableError) as refused:
        _require(
            contract, _KLINES, window_start_utc=window_start_utc, window_end_utc=window_end_utc
        )
    message = str(refused.value)
    assert "2021-05-03T00:00:00+00:00" in message
    assert "2021-05-04T00:00:00+00:00" in message
    assert "1440 bars" in message
    assert "at 1m" in message


def test_a_window_clear_of_the_gap_is_permitted() -> None:
    """The gap bounds are half-open, so a window ending exactly at `gap_start` is clear."""
    contract = permitting(
        earliest_event_time_utc=_EARLIEST,
        latest_event_time_utc=_LATEST,
        known_gaps=[_GAP],
    )
    _require(
        contract,
        _KLINES,
        window_start_utc=datetime(2021, 5, 1, tzinfo=UTC),
        window_end_utc=_GAP.gap_start_utc,
    )


def test_a_gap_with_no_bar_count_says_so_rather_than_reporting_zero() -> None:
    """A trades archive that was never published says nothing about how many prints are
    missing, and a zero there would read as "none" -- a stronger claim than the evidence
    supports."""
    contract = permitting(
        earliest_event_time_utc=_EARLIEST,
        latest_event_time_utc=_LATEST,
        known_gaps=[
            AvailabilityGap(
                gap_start_utc=datetime(2021, 5, 3, tzinfo=UTC),
                gap_end_utc=datetime(2021, 5, 4, tzinfo=UTC),
                gap_kind="absent_archive",
                bar_interval="1m",
                missing_bar_count=None,
            )
        ],
    )
    with pytest.raises(DataUnavailableError, match="an unknown number of observations"):
        _require(
            contract,
            _KLINES,
            window_start_utc=datetime(2021, 5, 1, tzinfo=UTC),
            window_end_utc=datetime(2021, 5, 6, tzinfo=UTC),
        )


# ---------------------------------------------------------------------------
# The declaration itself
# ---------------------------------------------------------------------------


def _permissive_flag() -> Literal[True]:
    """A `False` wearing the annotation `Literal[True]`.

    The ignore is the whole point of the test: it reproduces a value reaching the
    constructor with its annotation never checked against the value -- from a mapping, a
    config file or an agent response -- which is the only way a permissive declaration
    could ever be built.
    """
    return False  # type: ignore[return-value]


def test_refuses_if_unavailable_cannot_be_false_at_runtime_either() -> None:
    """`mypy --strict` rejects the literal `False`; this rejects the value that arrived
    from somewhere no type checker ever saw."""
    with pytest.raises(FeatureContractError, match="no permissive mode"):
        AvailabilityDeclaration(
            address=SeriesAddress(market=Market.SPOT, symbol="BTCUSDT", dataset=Dataset.KLINES),
            resolutions=("1m",),
            earliest_event_time_utc=_EARLIEST,
            latest_event_time_utc=_LATEST,
            known_gaps=(),
            refuses_if_unavailable=_permissive_flag(),
        )


def test_a_declaration_whose_latest_precedes_its_earliest_is_refused() -> None:
    with pytest.raises(FeatureContractError, match="before earliest"):
        AvailabilityDeclaration(
            address=SeriesAddress(market=Market.SPOT, symbol="BTCUSDT", dataset=Dataset.KLINES),
            resolutions=("1m",),
            earliest_event_time_utc=_LATEST,
            latest_event_time_utc=_EARLIEST,
            known_gaps=(),
            refuses_if_unavailable=True,
        )


def test_the_declaration_mapping_cannot_be_mutated_through_the_reference_it_was_built_from() -> (
    None
):
    """`frozen=True` protects the binding, not the mapping bound to it.

    A caller keeping its own reference to the dict it passed in could otherwise add a
    series after the snapshot was taken, which is precisely the "the contract changed
    mid-run" behaviour the snapshot exists to prevent.
    """
    address = SeriesAddress(market=Market.SPOT, symbol="BTCUSDT", dataset=Dataset.KLINES)
    declaration = AvailabilityDeclaration(
        address=address,
        resolutions=("1m",),
        earliest_event_time_utc=_EARLIEST,
        latest_event_time_utc=_LATEST,
        known_gaps=(),
        refuses_if_unavailable=True,
    )
    mutable = {address: declaration}
    contract = AvailabilityContract(declarations=mutable)
    mutable.clear()
    assert contract.declaration(address) is declaration
