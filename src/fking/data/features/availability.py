"""What the corpus holds, what it can never hold, and the refusal that says so.

A strategy cannot request data this system does not have. The store refuses rather than
substituting something adjacent, and the refusal names what *does* exist
(`DATA_PIPELINE.md` section 8).

Three things here are structural rather than documentary.

**`refuses_if_unavailable` is `Literal[True]`.** There is no permissive mode, because the
permissive mode is where a strategy gets a forward-filled approximation of depth and does
not know it. `mypy --strict` rejects `False` at the call site and `__post_init__` rejects
it at runtime, because the construction that would carry `False` is the one built from an
untyped mapping that no type checker ever saw.

**Declarations are derived from the ingestion registry, never written by hand.** A
hand-maintained list of what exists is a list that is wrong the first time a backfill runs
and stays wrong until somebody notices -- and the direction it is wrong in is the
optimistic one, because nobody removes a symbol from a list. `AvailabilityContract.snapshot`
reads `data_coverage` and `coverage_gap`, so ingesting a new dataset changes what the
contract permits with no code edit.

**The refusal names the alternatives.** "No L2 depth history exists for free; you have tick
trades, futures top-of-book, and ~1-minute aggregated depth bands" redirects the research.
A bare `KeyError` sends somebody looking for a bug. This matters most for LLM-authored
strategies: an agent asked for a mean-reversion strategy will cheerfully request
`order_book_imbalance_top_10_levels`, because that feature exists in the literature it was
trained on. The refusal is what keeps the strategy population inside the data this system
actually possesses (`ARCHITECTURE.md` section 6).

**One declaration per `(market, symbol, dataset)`, with intervals collapsed into it.**
`DATA_PIPELINE.md` section 8 spells `resolution` in the singular; the registry keys klines
by interval, so a symbol can hold 1m and 1h series that begin on different days. The
declaration therefore carries `resolutions` -- every interval held -- while `earliest` is
the earliest across them and `known_gaps` is the union across them, each gap naming its own
interval. The union is the conservative direction: a window intersecting a hole in the 1h
series is refused even for a feature that would have read 1m, and the message says which
interval has the hole, so the answer is to backfill it rather than to widen the check.
Features declare datasets and not yet resolutions (`FeatureSpec.inputs`), and when they do
the check narrows to the declared one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Final, Literal

from fking.data.backfill.registry import NO_INTERVAL, IngestRegistry
from fking.data.features.spec import FeatureSpec
from fking.data.format_resolver import Dataset, Market
from fking.platform.errors import DataUnavailableError, FeatureContractError

__all__ = [
    "THE_CEILING",
    "UNOBTAINABLE_INPUTS",
    "AvailabilityContract",
    "AvailabilityDeclaration",
    "AvailabilityGap",
    "SeriesAddress",
]

# The sentence every refusal ends with. One string rather than one per call site, because
# the ceiling is a property of the corpus and not of whichever check happened to fire, and
# two divergent spellings of it would read as two different ceilings.
THE_CEILING: Final[str] = (
    "what this corpus can hold: tick trades with side and exchange trade id "
    "(trades, aggTrades); futures top-of-book (bookTicker); cumulative depth bands at "
    "+/-1..5% from mid sampled roughly once a minute (bookDepth); funding rate, open "
    "interest and mark price; and klines at every standard interval "
    "(DATA_PIPELINE.md section 9)"
)

# Substrings of an input name that identify a request no free source can satisfy, and the
# reason it cannot. Matched as substrings rather than as exact names on purpose: the
# caller most likely to trip this is a language model naming a feature after the
# literature, and `order_book_imbalance_top_10_levels`, `l2_book_pressure` and
# `queue_position_estimate` are three spellings of two ceilings.
#
# Only reached for an input that is not a real `Dataset`, so `bookDepth` and `bookTicker`
# -- which do contain "book" -- never arrive here.
_L2_CEILING: Final[str] = (
    "free full-depth L2 order book history does not exist, on data.binance.vision or on "
    "any free mirror; bookDepth is ten cumulative bands sampled about once a minute, not "
    "a book, and it carries no price level, no per-level quantity and no ordering"
)
_L3_CEILING: Final[str] = (
    "queue position and passive fill probability require order-by-order (L3/MBO) data, "
    "which no free source publishes at all; a number produced without it would be "
    "fabricated, and a fabricated number propagates into sizing"
)

UNOBTAINABLE_INPUTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "order_book": _L2_CEILING,
        "orderbook": _L2_CEILING,
        "book_imbalance": _L2_CEILING,
        "book_pressure": _L2_CEILING,
        "book_event": _L2_CEILING,
        "depth_level": _L2_CEILING,
        "price_level": _L2_CEILING,
        "level2": _L2_CEILING,
        "l2_": _L2_CEILING,
        "l3": _L3_CEILING,
        "mbo": _L3_CEILING,
        "queue_position": _L3_CEILING,
        "passive_fill": _L3_CEILING,
        "fill_probability": _L3_CEILING,
    }
)


@dataclass(frozen=True, slots=True)
class SeriesAddress:
    """The three coordinates a declaration is keyed by.

    Not the four the registry uses: the interval is inside the declaration rather than in
    its key, for the reason the module docstring gives.
    """

    market: Market
    symbol: str
    dataset: Dataset


@dataclass(frozen=True, slots=True)
class AvailabilityGap:
    """A hole the corpus is known to have, in half-open event time.

    `bar_interval` travels with it because the gap belongs to one ingested resolution and
    the refusal has to say which -- "the 1h series is missing 2021-05-03" is a backfill
    instruction, and "this symbol has a hole" is not.
    """

    gap_start_utc: datetime
    gap_end_utc: datetime
    gap_kind: str
    bar_interval: str
    missing_bar_count: int | None

    def intersects(self, *, window_start_utc: datetime, window_end_utc: datetime) -> bool:
        """Whether this gap overlaps `(window_start_utc, window_end_utc]` at all.

        Any overlap at all, rather than a containment test: a window that runs one bar
        into a hole is a window whose series is short by one bar, and a short series reads
        downstream as "no signal here" rather than as "no data here".
        """
        return self.gap_start_utc < window_end_utc and self.gap_end_utc > window_start_utc


@dataclass(frozen=True, slots=True, kw_only=True)
class AvailabilityDeclaration:
    """What the corpus holds for one `(market, symbol, dataset)`.

    Keyword-only and fully required. The fields somebody would default are
    `known_gaps=()` and an optimistic `earliest`, and both defaults answer the question
    this type exists to ask in the permissive direction.
    """

    address: SeriesAddress
    resolutions: tuple[str, ...]
    earliest_event_time_utc: datetime
    latest_event_time_utc: datetime
    known_gaps: tuple[AvailabilityGap, ...]
    refuses_if_unavailable: Literal[True]

    def __post_init__(self) -> None:
        # `mypy --strict` already rejects `False` here. This catches the construction it
        # cannot see: a declaration built from a mapping, a config file or an LLM
        # response, where the annotation was never checked against a real value.
        if self.refuses_if_unavailable is not True:
            raise FeatureContractError(
                "refuses_if_unavailable must be True; there is no permissive mode, "
                "because the permissive mode is where a strategy gets a forward-filled "
                "approximation and does not know it (DATA_PIPELINE.md section 8)"
            )
        if self.latest_event_time_utc < self.earliest_event_time_utc:
            raise FeatureContractError(
                f"{self.describe()} declares latest {self.latest_event_time_utc.isoformat()} "
                f"before earliest {self.earliest_event_time_utc.isoformat()}"
            )

    @property
    def earliest_date(self) -> date:
        """The calendar day the corpus begins on, which is what a refusal quotes."""
        return self.earliest_event_time_utc.date()

    @property
    def latest_date(self) -> date:
        return self.latest_event_time_utc.date()

    def describe(self) -> str:
        """`spot BTCUSDT klines (1m)` -- the phrase every refusal opens with."""
        held = ", ".join(interval for interval in self.resolutions if interval != NO_INTERVAL)
        suffix = f" ({held})" if held else ""
        return (
            f"{self.address.market.value} {self.address.symbol} "
            f"{self.address.dataset.value}{suffix}"
        )


@dataclass(frozen=True, slots=True)
class AvailabilityContract:
    """An immutable snapshot of what the corpus held at the instant it was taken.

    A snapshot rather than a live query, and deliberately so: a per-read lookup would make
    a refusal depend on whether a backfill happened to be mid-partition, so the same
    backtest would refuse and then not refuse with nothing in the code having changed. A
    run holds one snapshot and is reproducible against it.
    """

    declarations: Mapping[SeriesAddress, AvailabilityDeclaration]

    def __post_init__(self) -> None:
        # `frozen=True` protects the binding, not the mapping bound to it. Copying and
        # proxying is what makes the immutability real rather than nominal.
        object.__setattr__(self, "declarations", MappingProxyType(dict(self.declarations)))

    @classmethod
    def of(cls, declarations: Iterable[AvailabilityDeclaration]) -> AvailabilityContract:
        """Build from declarations directly. The registry-derived path is `snapshot`."""
        return cls(declarations={declaration.address: declaration for declaration in declarations})

    @classmethod
    async def snapshot(cls, registry: IngestRegistry) -> AvailabilityContract:
        """Derive the whole contract from the ingestion registry.

        Two reads rather than one: `data_coverage` carries the first and last event time
        per series and deliberately aggregates gaps into a count, and a window check needs
        the bounds that aggregate throws away.
        """
        rows = await registry.coverage()
        gaps = await registry.recorded_gaps()

        gaps_by_address: dict[SeriesAddress, list[AvailabilityGap]] = {}
        for series_gap in gaps:
            address = SeriesAddress(
                market=Market(series_gap.market),
                symbol=series_gap.symbol,
                dataset=Dataset(series_gap.dataset),
            )
            gaps_by_address.setdefault(address, []).append(
                AvailabilityGap(
                    gap_start_utc=series_gap.gap.gap_start_utc,
                    gap_end_utc=series_gap.gap.gap_end_utc,
                    gap_kind=series_gap.gap.gap_kind.value,
                    bar_interval=series_gap.bar_interval,
                    missing_bar_count=series_gap.gap.missing_bar_count,
                )
            )

        collapsed: dict[SeriesAddress, AvailabilityDeclaration] = {}
        for row in rows:
            address = SeriesAddress(
                market=Market(row.market), symbol=row.symbol, dataset=Dataset(row.dataset)
            )
            existing = collapsed.get(address)
            resolutions = (() if row.bar_interval == NO_INTERVAL else (row.bar_interval,)) + (
                existing.resolutions if existing is not None else ()
            )
            collapsed[address] = AvailabilityDeclaration(
                address=address,
                resolutions=tuple(sorted(set(resolutions))),
                earliest_event_time_utc=(
                    row.first_event_time_utc
                    if existing is None
                    else min(existing.earliest_event_time_utc, row.first_event_time_utc)
                ),
                latest_event_time_utc=(
                    row.last_event_time_utc
                    if existing is None
                    else max(existing.latest_event_time_utc, row.last_event_time_utc)
                ),
                # Attached once per address rather than per interval row, so a symbol
                # holding 1m and 1h does not carry each gap twice.
                known_gaps=tuple(gaps_by_address.get(address, ())),
                refuses_if_unavailable=True,
            )
        return cls(declarations=collapsed)

    def declaration(self, address: SeriesAddress) -> AvailabilityDeclaration | None:
        """What is declared for one series, or `None` if the corpus holds nothing."""
        return self.declarations.get(address)

    def require(
        self,
        spec: FeatureSpec,
        *,
        market: Market,
        symbol: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> None:
        """Refuse unless every input of `spec` covers `(window_start_utc, window_end_utc]`.

        Returns `None` on success and raises otherwise; there is no boolean to forget to
        check. The inputs are visited in sorted order so that a feature failing on two of
        them reports the same one on every run -- a refusal that alternates trains people
        to re-run it.
        """
        for input_name in sorted(spec.inputs):
            dataset = _as_dataset(input_name)
            if dataset is None:
                raise DataUnavailableError(_unobtainable_message(spec, input_name))
            declaration = self.declarations.get(
                SeriesAddress(market=market, symbol=symbol, dataset=dataset)
            )
            if declaration is None:
                raise DataUnavailableError(_absent_message(spec, market, symbol, dataset, self))
            _require_window_inside(spec, declaration, window_start_utc, window_end_utc)


def _as_dataset(input_name: str) -> Dataset | None:
    """The `Dataset` an input names, or `None` if no dataset is spelled that way."""
    try:
        return Dataset(input_name)
    except ValueError:
        return None


def _unobtainable_message(spec: FeatureSpec, input_name: str) -> str:
    """Why an unrecognised input can never be satisfied, or that it simply is not one."""
    lowered = input_name.lower()
    for token, ceiling in UNOBTAINABLE_INPUTS.items():
        if token in lowered:
            return (
                f"{spec.name} v{spec.version} declares input {input_name!r}, which no free "
                f"source provides: {ceiling}. Instead, {THE_CEILING}."
            )
    return (
        f"{spec.name} v{spec.version} declares input {input_name!r}, which names no dataset "
        f"this pipeline ingests. Datasets are {sorted(member.value for member in Dataset)}; "
        f"{THE_CEILING}."
    )


def _absent_message(
    spec: FeatureSpec,
    market: Market,
    symbol: str,
    dataset: Dataset,
    contract: AvailabilityContract,
) -> str:
    """A real dataset that this symbol has no ingested history for.

    Lists what the symbol *does* hold, because the actionable answer is almost always a
    backfill of one more dataset rather than a different feature.
    """
    held = sorted(
        declaration.describe()
        for address, declaration in contract.declarations.items()
        if address.market is market and address.symbol == symbol
    )
    holding = ", ".join(held) if held else "nothing at all"
    return (
        f"{spec.name} v{spec.version} declares input {dataset.value!r}, which the corpus "
        f"does not hold for {market.value} {symbol}. It holds {holding}. Backfill it with "
        f"`make ingest MARKET={market.value} DATASET={dataset.value} SYMBOLS={symbol}` or "
        f"change the feature's inputs; no partial series is returned."
    )


def _require_window_inside(
    spec: FeatureSpec,
    declaration: AvailabilityDeclaration,
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> None:
    """The three ways a real series still fails to answer a real window."""
    if window_start_utc < declaration.earliest_event_time_utc:
        raise DataUnavailableError(
            f"{declaration.describe()} begins at {declaration.earliest_date.isoformat()} "
            f"({declaration.earliest_event_time_utc.isoformat()}), and {spec.name} "
            f"v{spec.version} was asked for a window opening at "
            f"{window_start_utc.isoformat()}. The corpus holds "
            f"{declaration.earliest_date.isoformat()} to "
            f"{declaration.latest_date.isoformat()}; no partial series is returned, "
            f"because a short series reads downstream as no signal rather than as no data."
        )
    if window_end_utc > declaration.latest_event_time_utc:
        raise DataUnavailableError(
            f"{declaration.describe()} ends at {declaration.latest_date.isoformat()} "
            f"({declaration.latest_event_time_utc.isoformat()}), and {spec.name} "
            f"v{spec.version} was asked for a window closing at "
            f"{window_end_utc.isoformat()}. Ingest the remaining archives or move the "
            f"window back; no partial series is returned."
        )
    for gap in declaration.known_gaps:
        if not gap.intersects(window_start_utc=window_start_utc, window_end_utc=window_end_utc):
            continue
        missing = (
            "an unknown number of observations"
            if gap.missing_bar_count is None
            else f"{gap.missing_bar_count} bars"
        )
        interval = f" at {gap.bar_interval}" if gap.bar_interval != NO_INTERVAL else ""
        raise DataUnavailableError(
            f"{declaration.describe()} has a recorded {gap.gap_kind} gap"
            f"{interval} covering [{gap.gap_start_utc.isoformat()}, "
            f"{gap.gap_end_utc.isoformat()}), which intersects the window "
            f"({window_start_utc.isoformat()}, {window_end_utc.isoformat()}] that "
            f"{spec.name} v{spec.version} asked for; {missing} are absent there. Narrow "
            f"the window or backfill the gap -- the corpus never fills one, because a "
            f"synthesised bar has zero realised volatility and perfect mean reversion."
        )
