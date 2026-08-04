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

from fking.data.loaders.records import KlineRecord
from fking.platform.errors import SeamDisagreementError

__all__ = [
    "SEAM_COMPARED_FIELDS",
    "KlineSeam",
    "reconcile_klines",
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
