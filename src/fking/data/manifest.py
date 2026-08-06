"""What the production archive actually has history for.

The universe a strategy may trade is the intersection of two sets that neither contains
the other (VF-006): what the venue lists *now*, and what the production archive holds
history for. This module answers the second half, and it answers it from the local
Parquet corpus rather than from a listing request against `data.binance.vision`.

That choice is the point rather than a shortcut. A symbol directory exists in the corpus
only because an archive for it passed checksum verification and was written whole
(`fking.data.archive`, `DATA_PIPELINE.md` section 2), so "has history" means "we hold
verified bytes" instead of "the vendor's index mentions it". The difference bites
exactly where it matters: an upstream index entry whose archive is truncated, renamed or
404 would enter the universe under a listing-based manifest, and the backtest that
followed would be over data the system cannot actually read.

What this module deliberately does **not** provide is any function that answers "which
symbols existed at time *t*". Selecting a historical universe from today's corpus is
survivorship bias wearing a safety check as a disguise -- a 2021 backtest would run over
symbols chosen for having survived to 2026. That question is answered by the
point-in-time `universe_as_of(venue, as_of)` query against listing and delisting
timestamps (`.claude/rules/no-lookahead.md`), and the absence of a lookalike here is
what stops the two being confused.

Symbol names are read from directory names and are never normalised, lowered, or
filtered by shape. A corpus written from a venue payload carrying a non-ASCII symbol
must round-trip that symbol's exact code points, and classification -- deciding whether
it is tradable -- belongs to `fking.execution.symbols`, one layer up, where the reason
can be reported rather than implied by a symbol's absence.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from fking.data.format_resolver import Dataset, Market

__all__ = ["ArchiveManifest", "ParquetArchiveManifest"]

# The Hive key/value prefixes `fking.data.parquet.layout` writes. Matched rather than
# assumed positionally: a corpus root that happens to contain an unrelated directory
# tree must contribute no symbols instead of contributing its directory names.
_MARKET_PREFIX: Final[str] = "market="
_DATASET_PREFIX: Final[str] = "dataset="
_SYMBOL_PREFIX: Final[str] = "symbol="


@runtime_checkable
class ArchiveManifest(Protocol):
    """The symbols the production archive holds verified history for."""

    def symbols_with_history(self, *, market: Market, dataset: Dataset) -> frozenset[str]:
        """Every symbol with at least one readable partition in `(market, dataset)`."""


@dataclass(frozen=True, slots=True)
class ParquetArchiveManifest:
    """`ArchiveManifest` over the local Parquet corpus.

    A missing corpus root yields an empty set rather than raising. The empty set is the
    truthful answer -- nothing has been ingested -- and it fails loudly one layer up,
    where `resolve_universe` reports every requested symbol as absent from the archive
    side with the counts that say so. Raising here would instead report a filesystem
    error at startup, which sends the reader to the wrong problem.
    """

    corpus_root: Path

    def symbols_with_history(self, *, market: Market, dataset: Dataset) -> frozenset[str]:
        corpus = (
            self.corpus_root
            / f"{_MARKET_PREFIX}{market.value}"
            / (f"{_DATASET_PREFIX}{dataset.value}")
        )
        if not corpus.is_dir():
            return frozenset()
        return frozenset(self._symbols_under(corpus))

    def _symbols_under(self, corpus: Path) -> Iterator[str]:
        for entry in sorted(corpus.iterdir()):
            if not entry.is_dir() or not entry.name.startswith(_SYMBOL_PREFIX):
                continue
            # A directory with no partition file is not history. It is what a failed or
            # interrupted write leaves behind, and admitting it would put a symbol into
            # the tradable universe that no backtest can read a single bar for.
            if next(entry.rglob("*.parquet"), None) is None:
                continue
            yield entry.name.removeprefix(_SYMBOL_PREFIX)
