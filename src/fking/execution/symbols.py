"""Symbol classification: Unicode-safe, round-trip exact, quarantining rather than dropping.

Binance spot testnet's `exchangeInfo` deliberately carries non-ASCII symbols -- five of
them as of 2026-08-05, including `这是测试币456` and four variants
of `币安人生`. They are there to break parsers, and the two shapes they
break are exactly the two a reviewer waves through:

- `symbol.isalnum()` returns `True` for many non-ASCII strings and `False` for others,
  so a filter built on it drops some of them silently. The universe is then quietly
  wrong and nobody knows which symbol went missing.
- `unicodedata.normalize("NFKC", symbol)` changes the code points, and the venue expects
  the ones it sent. A normalised symbol is a different symbol.

So nothing here coerces. A symbol that cannot be traded is returned marked untradable
with its offending code points named, and `classify(raw).symbol == raw` holds byte for
byte for every input.

The reason to name the code points rather than log "bad symbol": a `UnicodeEncodeError`
inside a symbol-universe load is a startup crash on a Windows console's default
codepage, and `U+8FD9` is printable in cp1252 while the character is not.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

__all__ = ["SymbolClassification", "classify_symbol", "tradable_symbols"]

# Binance symbols are uppercase alphanumeric. The bound is 2..32 rather than open-ended
# so that a payload field which is a symbol-shaped sentence fails classification instead
# of entering the universe.
_TRADABLE_SYMBOL: Final[re.Pattern[str]] = re.compile(r"\A[A-Z0-9]{2,32}\Z")

# The last ASCII code point. Anything above it is what the deliberate testnet symbols are
# made of, and naming it keeps the boundary readable next to the `ord()` that tests it.
_MAX_ASCII_CODEPOINT: Final[int] = 0x7F


@dataclass(frozen=True, slots=True)
class SymbolClassification:
    """One venue symbol, judged. `symbol` is always what the venue sent."""

    symbol: str
    is_tradable: bool
    # None when tradable. When not, it names *why* in terms a log sink can render.
    reason: str | None


def _describe_non_ascii(raw: str) -> str:
    named = " ".join(
        f"U+{ord(character):04X}({unicodedata.name(character, 'UNNAMED')})"
        for character in raw
        if ord(character) > _MAX_ASCII_CODEPOINT
    )
    return named or "no non-ascii code points"


def classify_symbol(raw: str) -> SymbolClassification:
    """Classify one venue symbol without altering it."""
    if _TRADABLE_SYMBOL.match(raw):
        return SymbolClassification(symbol=raw, is_tradable=True, reason=None)
    if raw.isascii():
        return SymbolClassification(
            symbol=raw,
            is_tradable=False,
            reason=(
                f"ascii symbol {raw!r} of length {len(raw)} is outside {_TRADABLE_SYMBOL.pattern}"
            ),
        )
    return SymbolClassification(
        symbol=raw,
        is_tradable=False,
        reason=f"non-ascii code points {_describe_non_ascii(raw)}",
    )


def tradable_symbols(raws: Iterable[str]) -> frozenset[str]:
    """The tradable subset, preserving each symbol's exact code points."""
    return frozenset(
        classification.symbol
        for classification in map(classify_symbol, raws)
        if classification.is_tradable
    )
