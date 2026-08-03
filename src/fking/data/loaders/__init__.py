"""Pure archive parsers: `(bytes, IngestionSpec) -> (records, NormalizationResult)`.

Pure in the strict sense -- no clock, no network, no filesystem, no randomness -- which is
what makes every parser testable against the recorded archive fragments under
`tests/fixtures/archives/` rather than against a live download. The reference instant for
timestamp plausibility arrives on the spec, so the same bytes produce the same records
forever.

`DATA_PIPELINE.md` section 3 names three verified traps, and all three are structural here
rather than remembered:

1. **Epoch unit** comes from the resolved format, per `(market, dataset, date)`. This
   package has no divisor of its own. A wrong declaration rejects every row and the
   rejection-fraction gate refuses the file.
2. **Header presence** is declared, never sniffed, and a first token that contradicts the
   declaration rejects the whole file in either direction -- see `source.split_rows` for
   why skipping a row instead is worse than failing.
3. **Boolean encoding** is declared, and the token table is exact and case-sensitive with
   no falsy default. An unrecognised token is counted, and 0.1% of them fails the run.

**Only the datasets whose format is declared have a parser.** `resolve_archive_format`
refuses `aggTrades`, `bookDepth`, `bookTicker` and every futures dataset other than
`klines`, because declaring a format requires having read a real archive rather than
inferring one from a neighbouring dataset. `parse_archive` refuses them for the same
reason and with the same shape: a parser written against a column layout recalled from
memory, tested against a hand-written fixture, would be two of this repository's rules
broken at once. Adding one is one resolver entry, one recorded fragment and one row parser.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from fking.data.format_resolver import Dataset
from fking.data.loaders.driver import DatasetParser, parse_rows
from fking.data.loaders.klines import KLINE_COLUMNS, parse_kline_row
from fking.data.loaders.outcome import NormalizationResult, RejectionReason
from fking.data.loaders.records import ArchiveRecord, KlineRecord, TradeRecord
from fking.data.loaders.source import extract_single_member
from fking.data.loaders.spec import DEFAULT_MAX_REJECTION_FRACTION, IngestionSpec
from fking.data.loaders.trades import TRADE_COLUMNS, parse_trade_row
from fking.platform.errors import DataIntegrityError

__all__ = [
    "DEFAULT_MAX_REJECTION_FRACTION",
    "IMPLEMENTED_DATASETS",
    "KLINE_COLUMNS",
    "TRADE_COLUMNS",
    "ArchiveRecord",
    "IngestionSpec",
    "KlineRecord",
    "NormalizationResult",
    "RejectionReason",
    "TradeRecord",
    "extract_single_member",
    "parse_archive",
    "parse_klines",
    "parse_trades",
]

KLINE_PARSER = DatasetParser(columns=KLINE_COLUMNS, parse_row=parse_kline_row)
TRADE_PARSER = DatasetParser(columns=TRADE_COLUMNS, parse_row=parse_trade_row)

# Exactly the datasets `fking.data.format_resolver.DECLARED_FORMATS` can resolve. Kept as a
# separate table rather than derived from it, because the two answer different questions --
# "is this file's format known" and "can this file's layout be read" -- and a dataset can
# acquire the first without the second. A test asserts the two stay in step, so the
# duplication cannot drift silently.
_PARSERS: Mapping[Dataset, DatasetParser[KlineRecord] | DatasetParser[TradeRecord]] = {
    Dataset.KLINES: KLINE_PARSER,
    Dataset.TRADES: TRADE_PARSER,
}

IMPLEMENTED_DATASETS: Final[frozenset[Dataset]] = frozenset(_PARSERS)
"""Datasets `parse_archive` can read. Everything else raises, naming what is missing."""


def parse_klines(
    payload: bytes, spec: IngestionSpec, *, source: str
) -> tuple[tuple[KlineRecord, ...], NormalizationResult]:
    """Parse kline CSV bytes. Spot or futures -- the difference is entirely in the spec."""
    return parse_rows(payload, spec, KLINE_PARSER, source=source)


def parse_trades(
    payload: bytes, spec: IngestionSpec, *, source: str
) -> tuple[tuple[TradeRecord, ...], NormalizationResult]:
    """Parse trades CSV bytes."""
    return parse_rows(payload, spec, TRADE_PARSER, source=source)


def parse_archive(
    payload: bytes, spec: IngestionSpec, *, source: str
) -> tuple[tuple[ArchiveRecord, ...], NormalizationResult]:
    """Parse CSV bytes for whichever dataset the spec declares.

    The entry point a backfill uses, because a backfill iterates coordinates and does not
    know the record type at the call site. Callers that do know it should prefer
    `parse_klines` or `parse_trades`, which return a precise type instead of a union.

    Raises:
        DataIntegrityError: no parser exists for the declared dataset, or any file-level
            fault named by `parse_rows`.
    """
    parser = _PARSERS.get(spec.archive_format.dataset)
    if parser is None:
        raise DataIntegrityError(
            f"no parser is implemented for dataset {spec.archive_format.dataset.value!r}; "
            f"implemented datasets are "
            f"{sorted(dataset.value for dataset in _PARSERS)}. A parser arrives with a "
            f"recorded archive fragment and a declared format, never before them -- a "
            f"column layout written from memory and checked against a hand-written fixture "
            f"is a loader that passes its tests and mis-reads the corpus"
        )
    return parse_rows(payload, spec, parser, source=source)
