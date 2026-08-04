"""Where a REST backfill meets what the corpus already holds, and what happens there.

`DATA_PIPELINE.md` section 5 states the constraint this module exists for:

> **Reconcile the seam on exchange trade id, never on timestamp.**

Timestamps at a seam are the least reliable field in the record. The live stream carries
the exchange's event time, the REST view carries the archive's, clocks differ, and for
spot ranges after 2025-01-01 a microsecond/millisecond boundary can sit inside the window
(VF-015). Deduplicating on `(symbol, timestamp)` there either drops a real print or admits
a duplicate, and both are invisible afterwards.

**Klines have no id, so the seam key is `open_time_utc` plus a full-field equality check
on the overlapping bars.** That is the whole difference from the trade case: an id lets
two records be recognised as one fact regardless of what their other fields say, and a
timestamp does not -- so a shared open time is a *claim* that has to be verified rather
than a join that can be trusted.

**Trades have an id, so `reconcile_trades` joins on `venue_trade_id` and leaves
`event_time_utc` out of the comparison entirely.** That inversion is the point of the
rule quoted above and it cuts both ways. Two records carrying the same id and event times
a few milliseconds apart are one print filed twice, and exactly one survives -- the stream
stamps `T` from the frame it received and a REST view of the same aggregate can re-derive
it, so a timestamp difference there says something about two clocks and nothing about two
trades. Two records carrying the same millisecond and different ids are two prints, and
both survive -- which is the case a `(symbol, timestamp)` dedupe loses silently, and it is
not rare: a single aggressive order fills against several resting ones inside one
millisecond routinely.

**A disagreement escalates. It is not merged, and nothing is written.** A closed bar is
immutable, so two sources disagreeing about one means at least one of them is wrong about
a period that is already final. Resolving it by preferring the stream, or the REST view,
or the newer arrival, records one view as fact and destroys the only evidence that they
ever differed -- and the next disagreement then looks like the first one nobody caught.
The whole batch is refused, including the bars that agreed, because a seam that contains a
contradiction has not been shown to be a seam at all.

**Two fields are deliberately outside the comparison, and each for a stated reason.**

- `close_time_utc` is a derived boundary, not an observation: Binance files it as the last
  representable instant inside the interval, which is `.999` on a millisecond source and
  `.999999` on a microsecond one. Comparing it would escalate every seam that spans the
  spot epoch cutover -- the exact trap the reconciliation is supposed to survive -- while
  detecting nothing about the market. The bar's identity is its open time, and its
  duration is the interval.
- `ignored_field` is Binance's trailing always-zero CSV column. It is `"0"` from an
  archive, `""` from the stream, and absent from the `bar` table entirely. It carries no
  market information; its only job is to make a parser's field count the file's field
  count, which is a claim about a CSV and not about a bar.

Everything that describes the market -- the four prices, both volumes, both taker-buy
volumes and the trade count -- is compared exactly, as `Decimal`, with no tolerance. A
tolerance here would be a threshold below which two sources are allowed to disagree, and
nobody can say what that number should be for a volume field that spans eight orders of
magnitude across symbols.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from fking.data.loaders.records import KlineRecord, TradeRecord
from fking.platform.errors import DataIntegrityError, SeamDisagreementError

__all__ = [
    "SEAM_COMPARED_FIELDS",
    "TRADE_SEAM_COMPARED_FIELDS",
    "KlineSeam",
    "TradeSeam",
    "reconcile_klines",
    "reconcile_trades",
]

# Every field that describes the market. Named explicitly rather than derived by
# subtracting exclusions from `dataclasses.fields`, so that a field added to
# `KlineRecord` is absent from the comparison until somebody decides it belongs there --
# the opposite default would silently start escalating on a field nobody had considered.
SEAM_COMPARED_FIELDS: Final[tuple[str, ...]] = (
    "open_quote_price",
    "high_quote_price",
    "low_quote_price",
    "close_quote_price",
    "base_volume",
    "quote_volume",
    "trade_count",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
)

# Everything about a print that two sources must agree on once they agree it is the same
# print. Two fields of `TradeRecord` are deliberately absent, and neither omission is a
# tolerance:
#
# - `event_time_utc` is the field the id exists to replace. `DATA_PIPELINE.md` section 5
#   makes it the least reliable one at a seam, so comparing it would escalate on exactly
#   the disagreement the join key was chosen to survive.
# - `is_best_match` is not an observation on the live side. The `aggTrade` stream carries
#   no such flag and `AggTradeFrame.to_record` sets it `True` by construction, so
#   comparing it would compare our own constant against whatever the other source filed
#   -- an escalation about a field the stream never saw.
TRADE_SEAM_COMPARED_FIELDS: Final[tuple[str, ...]] = (
    "quote_price",
    "base_quantity",
    "quote_quantity",
    "is_buyer_maker",
)


@dataclass(frozen=True, slots=True)
class KlineSeam:
    """The result of merging a fetched page into what the corpus already holds.

    `agreed` and `recovered` are separate counts rather than one total because they
    answer different questions. `recovered` is how much of the hole was actually filled,
    which is what decides whether a gap closes. `agreed` is how much overlap the fetch
    bought -- and an overlap of zero means the seam was never tested, which is a weaker
    result than it looks and worth being able to state.
    """

    merged: tuple[KlineRecord, ...]
    agreed: int
    recovered: int


def reconcile_klines(held: Sequence[KlineRecord], fetched: Sequence[KlineRecord]) -> KlineSeam:
    """Merge `fetched` into `held` on open time, refusing any disagreement.

    Args:
        held: Bars the corpus already holds over the fetch window, in any order.
        fetched: Bars the venue's REST endpoint returned, in any order.

    Returns:
        The union, ordered by open time, with the counts of overlapping bars that agreed
        and of bars only the fetch supplied.

    Raises:
        SeamDisagreementError: two records share an open time and differ in a compared
            field, or one source contains two records for the same open time. The second
            case is the same failure one step earlier: a source that filed one minute
            twice cannot be reconciled against anything until that is understood.
    """
    held_by_open_time = _index_by_open_time(held, source_name="corpus")
    fetched_by_open_time = _index_by_open_time(fetched, source_name="REST")

    agreed = 0
    for open_time_utc, held_record in held_by_open_time.items():
        fetched_record = fetched_by_open_time.get(open_time_utc)
        if fetched_record is None:
            continue
        disagreement = _first_disagreement(held_record, fetched_record)
        if disagreement is not None:
            field_name, held_value, fetched_value = disagreement
            raise SeamDisagreementError(
                f"the corpus and the venue's REST view disagree about the closed bar "
                f"opening at {open_time_utc.isoformat()}: {field_name} is {held_value} "
                f"held and {fetched_value} fetched. A closed bar is immutable, so one of "
                f"these is wrong about a final period -- nothing from this seam is "
                f"written, and choosing a winner would leave no record that they differed"
            )
        agreed += 1

    merged = {**fetched_by_open_time, **held_by_open_time}
    return KlineSeam(
        merged=tuple(merged[key] for key in sorted(merged)),
        agreed=agreed,
        recovered=len(fetched_by_open_time) - agreed,
    )


def _index_by_open_time(
    records: Sequence[KlineRecord], *, source_name: str
) -> Mapping[datetime, KlineRecord]:
    indexed: dict[datetime, KlineRecord] = {}
    for record in records:
        previous = indexed.get(record.open_time_utc)
        if previous is not None and _first_disagreement(previous, record) is not None:
            raise SeamDisagreementError(
                f"the {source_name} view contains two different bars opening at "
                f"{record.open_time_utc.isoformat()}; one minute has one closed bar, and "
                f"a source that filed it twice cannot be reconciled against anything"
            )
        indexed[record.open_time_utc] = record
    return indexed


@dataclass(frozen=True, slots=True)
class TradeSeam:
    """The result of merging fetched prints into the prints the corpus already holds.

    `merged` is ordered by `(event_time_utc, aggregate id)` rather than by event time
    alone, because event time is not a total order over a tape -- many prints share a
    millisecond -- and an order that depends on which source a print came from would make
    the Parquet content digest a function of the merge rather than of the content.
    `fking.data.parquet.write_records` sorts stably by event time, so this order survives
    the write.
    """

    merged: tuple[TradeRecord, ...]
    agreed: int
    recovered: int


def reconcile_trades(held: Sequence[TradeRecord], fetched: Sequence[TradeRecord]) -> TradeSeam:
    """Merge `fetched` into `held` on `venue_trade_id`, refusing any disagreement.

    Passing an empty `held` is the deduplication case rather than a degenerate one: the
    live corpus writer hands its whole spool in as `fetched` so that a print delivered
    twice across a reconnect becomes one row, and gets the same contradiction check for
    free.

    Where two sources agree that a print is the same print but stamp it differently, the
    **held** record is kept. Not because the corpus is more trustworthy -- neither is, and
    that is why the field is outside the comparison -- but because keeping it makes the
    merge stable: a repair that preferred the fetch would rewrite the partition, and its
    content digest, on every pass over a range that had already been reconciled.

    Args:
        held: Prints the corpus already holds over the window, in any order.
        fetched: Prints the other source supplied, in any order.

    Returns:
        The union ordered by `(event_time_utc, aggregate id)`, with the counts of prints
        both sources carried and of prints only the fetch supplied.

    Raises:
        SeamDisagreementError: two records share a `venue_trade_id` and differ in a
            compared field. One trade cannot have had two prices.
        DataIntegrityError: a `venue_trade_id` is not a decimal integer. The whole seam
            rests on the venue's id being the monotone integer `DATA_PIPELINE.md`
            section 5 says it is; an id that is not one cannot order the tape, and
            ordering it by its characters would put print 9 after print 10.
    """
    held_by_id = _index_by_trade_id(held, source_name="corpus")
    fetched_by_id = _index_by_trade_id(fetched, source_name="REST")

    agreed = 0
    for venue_trade_id, held_record in held_by_id.items():
        fetched_record = fetched_by_id.get(venue_trade_id)
        if fetched_record is None:
            continue
        disagreement = _first_trade_disagreement(held_record, fetched_record)
        if disagreement is not None:
            field_name, held_value, fetched_value = disagreement
            raise SeamDisagreementError(
                f"the corpus and the venue disagree about trade {venue_trade_id}: "
                f"{field_name} is {held_value} held and {fetched_value} fetched. The id "
                f"is the venue's own and identifies one execution, so nothing from this "
                f"seam is written -- choosing a winner would leave no record that two "
                f"sources described one trade differently"
            )
        agreed += 1

    merged = {**fetched_by_id, **held_by_id}
    return TradeSeam(
        merged=tuple(
            sorted(
                merged.values(),
                key=lambda record: (record.event_time_utc, _aggregate_id(record)),
            )
        ),
        agreed=agreed,
        recovered=len(fetched_by_id) - agreed,
    )


def _index_by_trade_id(
    records: Sequence[TradeRecord], *, source_name: str
) -> Mapping[str, TradeRecord]:
    """One record per id, refusing a source that filed one id two different ways.

    A *repeated* id whose fields all agree is deduplicated silently, because that is what
    a reconnect produces and it is the case this function exists to absorb. A repeated id
    whose fields differ is the same contradiction as the cross-source one, one step
    earlier, and it is not something a merge can resolve.
    """
    indexed: dict[str, TradeRecord] = {}
    for record in records:
        _aggregate_id(record)  # refuses a non-integer id before it reaches the sort
        previous = indexed.get(record.venue_trade_id)
        if previous is not None and _first_trade_disagreement(previous, record) is not None:
            raise SeamDisagreementError(
                f"the {source_name} view contains two different trades under id "
                f"{record.venue_trade_id}; the venue assigns one id to one execution, and "
                f"a source that filed it twice cannot be reconciled against anything"
            )
        indexed[record.venue_trade_id] = record
    return indexed


def _aggregate_id(record: TradeRecord) -> int:
    """The venue's id as the integer it is, for ordering only.

    The string stays the record's identity -- the value that must match is the value the
    venue sent -- and this is the one place it is read as a number, because a tape sorted
    by the characters of its ids puts print 9 after print 10.
    """
    if not record.venue_trade_id.isdigit():
        raise DataIntegrityError(
            f"trade id {record.venue_trade_id!r} is not a decimal integer. The seam joins "
            f"and orders on the venue's monotone id; an id that is not one cannot order "
            f"the tape, and a lexical order would file print 9 after print 10"
        )
    return int(record.venue_trade_id)


def _first_trade_disagreement(
    held: TradeRecord, fetched: TradeRecord
) -> tuple[str, object, object] | None:
    for field_name in TRADE_SEAM_COMPARED_FIELDS:
        held_value = getattr(held, field_name)
        fetched_value = getattr(fetched, field_name)
        if held_value != fetched_value:
            return field_name, held_value, fetched_value
    return None


def _first_disagreement(
    held: KlineRecord, fetched: KlineRecord
) -> tuple[str, object, object] | None:
    """The first compared field on which two records for one minute differ.

    First rather than all of them: the message names one field and its two values, which
    is what an operator needs to decide whether this is a venue correction or a parser
    bug. A list of nine differences reads as "everything is wrong" and says less.
    """
    for field_name in SEAM_COMPARED_FIELDS:
        held_value = getattr(held, field_name)
        fetched_value = getattr(fetched, field_name)
        if held_value != fetched_value:
            return field_name, held_value, fetched_value
    return None
