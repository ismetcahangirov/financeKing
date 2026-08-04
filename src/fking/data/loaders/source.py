"""Getting rows out of the bytes: the zip member, the line split, and the header gate.

The header gate is trap 2, and it is the reason this module exists separately from the
dataset parsers. `has_header_row` is a *declared* field on the resolved format, never
sniffed from the file, and a first token that contradicts the declaration rejects the
**whole file**.

Both failure directions are silent, and they are not equally bad:

- Read a futures file as though it had no header and the literal string `open_time` is
  parsed as a timestamp. A strict parser raises; a lenient one coerces to 0 and files a
  bar at the Unix epoch, which then merges into a series whose high sits below its own
  open.
- Read a spot file as though it had a header and **the first real bar of the day is
  silently discarded.** 1,439 minutes instead of 1,440 looks like nothing at all, and it
  is always the same minute -- 00:00 UTC -- which correlates precisely with the
  daily-boundary behaviour strategies care about.

The second direction is why skipping a row is not an option and rejecting the file is. A
silent off-by-one-row load is discovered by a backtest months later. A failed load is
discovered in ninety seconds.

The gate does one more thing the issue does not require and that costs nothing here: when
a header *is* declared, its column names must equal the declared layout. An upstream
column reorder is otherwise perfectly silent -- every field parses, every type is right,
and `high` holds the low.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Sequence
from io import BytesIO
from typing import Final

from fking.platform.errors import DataIntegrityError

__all__ = [
    "ARCHIVE_ENCODING",
    "HeaderExpectationError",
    "extract_single_member",
    "split_rows",
]


class HeaderExpectationError(DataIntegrityError):
    """The file's first row contradicts `has_header_row`, in either direction.

    Named separately from every other way `split_rows` can refuse -- undecodable bytes, a
    zip holding two members -- because a caller adjudicating the quality gates
    (`DATA_PIPELINE.md` section 10, gate 2) has to report *which* gate refused, and a
    caller that catches `DataIntegrityError` to label it would file a decode failure under
    the header gate. A subclass rather than a sibling: existing handlers keep catching it.
    """


# Binance archive CSVs are ASCII. `strict` rather than `replace`: a replacement character
# in a numeric field would be caught by the decimal pattern, but one in a symbol or an id
# would pass through as a different string, and a silently substituted identifier is a
# join that stops matching for a reason nothing records.
ARCHIVE_ENCODING: Final[str] = "utf-8"

_FIELD_SEPARATOR: Final[str] = ","

_ASCII_INTEGER: Final[re.Pattern[str]] = re.compile(r"\A[+-]?[0-9]+\Z")


def extract_single_member(archive_bytes: bytes, *, source: str) -> bytes:
    """The bytes of the one member inside a Binance daily or monthly `.zip`.

    Exactly one member, or a refusal. A multi-member archive is a change in how Binance
    packages a dataset, and picking the first member would make that change invisible --
    right up to the release where the interesting file is second.

    Raises:
        DataIntegrityError: the bytes are not a zip, or hold other than one member.
    """
    try:
        with zipfile.ZipFile(BytesIO(archive_bytes)) as bundle:
            members = bundle.namelist()
            if len(members) != 1:
                raise DataIntegrityError(
                    f"archive {source} holds {len(members)} members {members!r}; a Binance "
                    f"archive holds exactly one CSV, and choosing among several would hide "
                    f"the packaging change that produced them"
                )
            return bundle.read(members[0])
    except zipfile.BadZipFile as malformed:
        raise DataIntegrityError(
            f"archive {source} is not a readable zip: {malformed}"
        ) from malformed


def split_rows(
    payload: bytes,
    *,
    source: str,
    has_header_row: bool,
    columns: Sequence[str],
) -> tuple[tuple[str, ...], ...]:
    """Decode, split, and apply the header gate. Returns data rows only.

    A blank trailing line is dropped -- every archive ends with a newline, and treating
    that as an empty row would produce one `field_count` rejection per file, which is
    exactly the kind of constant background noise that makes a rejection counter useless.
    A blank line *between* data rows is kept and will be rejected on its field count,
    because that is a genuine anomaly.

    Raises:
        DataIntegrityError: the payload is not decodable.
        HeaderExpectationError: the first row contradicts `has_header_row` in either
            direction, or a declared header does not match `columns`.
    """
    try:
        text = payload.decode(ARCHIVE_ENCODING)
    except UnicodeDecodeError as undecodable:
        raise DataIntegrityError(
            f"archive member {source} is not {ARCHIVE_ENCODING}: {undecodable}"
        ) from undecodable

    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        # An archive with no rows at all. Not an error: a symbol can print nothing for a
        # day. The caller reports rows_in=0 and no timestamps rather than inventing either.
        return ()

    first_row = tuple(lines[0].split(_FIELD_SEPARATOR))
    looks_like_header = _looks_like_header(first_row)

    if has_header_row and not looks_like_header:
        raise HeaderExpectationError(
            f"archive member {source} declares has_header_row=True but its first field "
            f"{first_row[0]!r} is numeric, so the file has no header. Reading it as though "
            f"it did would silently discard the first real row of the day -- always 00:00 "
            f"UTC. Resolve the format for this (market, dataset, date) instead"
        )
    if not has_header_row and looks_like_header:
        raise HeaderExpectationError(
            f"archive member {source} declares has_header_row=False but its first field "
            f"{first_row[0]!r} is not numeric, so the file has a header. Parsing it as data "
            f"would file a bar at the Unix epoch. Resolve the format for this "
            f"(market, dataset, date) instead"
        )
    if has_header_row and first_row != tuple(columns):
        raise HeaderExpectationError(
            f"archive member {source} has header {list(first_row)!r} but the declared layout "
            f"is {list(columns)!r}; a reordered or renamed column parses cleanly and means "
            f"something else, so the layout is compared rather than assumed"
        )

    body = lines[1:] if has_header_row else lines
    return tuple(tuple(line.split(_FIELD_SEPARATOR)) for line in body)


def _looks_like_header(row: Sequence[str]) -> bool:
    """Whether a row's first field is a column name rather than an ASCII integer.

    Only the first field, and only "is it an integer". Every archive row in every dataset
    opens with either an epoch or an id, both integers; every header opens with a name. A
    richer heuristic -- matching the expected names, counting alphabetic fields -- would be
    a *sniffer*, and a sniffer is what the declaration exists to replace. This decides
    nothing; it only reports what the file looks like so the declaration can be checked
    against it.

    An empty first field is not a header. It is a malformed row, and calling it a header
    would turn a one-line anomaly into a whole-file refusal with a misleading message; as
    data it is rejected on its field count, which is what it is.

    The pattern is `[0-9]`, not `str.isdigit()`: `isdigit()` is True for Arabic-Indic
    digits and `int()` accepts them, so a non-ASCII numeral would be classified as data
    and then parsed into a real integer nobody wrote.
    """
    return bool(row) and row[0] != "" and not _ASCII_INTEGER.match(row[0])
