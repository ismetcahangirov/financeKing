"""REST kline pages built from recorded archive bytes, never hand-authored.

`/api/v3/klines` returns a JSON array whose elements are the *same twelve positional
fields, in the same order, with the same string encoding* as a row of a futures kline
archive CSV. So a page can be assembled from bytes this repository already recorded from
`data.binance.vision` -- checksum-verified, provenance-carrying, and re-verified on every
run by `tests/data/test_archive_fixture_integrity.py` -- instead of from a JSON file
somebody typed.

That distinction is the point of `.claude/rules/testing-rules.md`'s ban on hand-written
fixtures. The two fields an author would get wrong are the two that matter here: the
decimals are strings rather than numbers, and the epochs are integers whose unit decides
whether a bar lands in 2025 or in 1970. Transcribing recorded rows into the wire shape
keeps both from the venue's own characters; inventing a page would encode this module's
beliefs about them and every parser assertion downstream would be checking those beliefs.

**The futures fixture, deliberately.** Futures archives are in milliseconds and so is the
REST endpoint on both venues; the spot fixture for 2025-01-02 is in microseconds because
of the archive-only cutover (VF-015), and reusing it here would build a page whose unit
the endpoint never emits.

`shift_to` re-bases the recorded minutes onto a test's own window. It moves the timestamps
and nothing else, so the prices, volumes and trade counts stay exactly as recorded -- which
is what makes a seam comparison in a test a comparison of real venue numbers.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Sequence
from datetime import date, datetime
from io import BytesIO
from typing import Final

from fking.data.format_resolver import Dataset, Market
from tests.support import archive_fixtures

_OPEN_TIME_INDEX: Final[int] = 0
_CLOSE_TIME_INDEX: Final[int] = 6

# The three positions the endpoint serialises as JSON *numbers* while the CSV -- which
# has no types at all -- writes as bare digits: the two epochs and the trade count. Every
# other field is a JSON string on the wire, which is the encoding the parser insists on
# for the decimals and the reason a page cannot simply be `json.dumps` of split CSV text.
_INTEGER_INDICES: Final[tuple[int, ...]] = (_OPEN_TIME_INDEX, _CLOSE_TIME_INDEX, 8)

# The recorded whole futures archive for 2025-01-02: 1440 one-minute bars in
# milliseconds, checksum-verified. See the module docstring for why not the spot one. The
# whole `.zip` rather than the 31-line fragment because a partial-backfill test needs a
# window longer than half an hour to be about anything.
_SOURCE_MARKET: Final[Market] = Market.FUTURES_UM
_SOURCE_DATE: Final[date] = date(2025, 1, 2)


def recorded_rows() -> tuple[list[object], ...]:
    """Every recorded bar as a twelve-element positional row, oldest first.

    Epochs stay `int` and every other field stays the exact string the archive filed, so
    `json.dumps` of these rows is byte-for-byte the shape the endpoint returns.
    """
    recorded = archive_fixtures.find(
        market=_SOURCE_MARKET, dataset=Dataset.KLINES, archive_date=_SOURCE_DATE, whole=True
    )
    with zipfile.ZipFile(BytesIO(recorded.read())) as archive:
        member = archive.namelist()[0]
        payload = archive.read(member).decode("utf-8")
    lines = payload.splitlines()
    data_lines = lines[1:] if recorded.has_header_row else lines
    rows: list[list[object]] = []
    for line in data_lines:
        if not line.strip():
            continue
        fields: list[object] = list(line.split(","))
        for position in _INTEGER_INDICES:
            fields[position] = int(str(fields[position]))
        rows.append(fields)
    return tuple(rows)


def shift_to(
    rows: Sequence[Sequence[object]], *, first_open_utc: datetime
) -> tuple[list[object], ...]:
    """The same rows with their minute grid re-based so the first bar opens at `first_open_utc`.

    Only the two epoch fields move. A test that also perturbed a price would no longer be
    asserting against recorded venue numbers, which is the property this module exists for.
    """
    if not rows:
        return ()
    origin_ms = int(str(rows[0][_OPEN_TIME_INDEX]))
    target_ms = _to_epoch_ms(first_open_utc)
    delta_ms = target_ms - origin_ms
    shifted: list[list[object]] = []
    for row in rows:
        moved: list[object] = list(row)
        moved[_OPEN_TIME_INDEX] = int(str(row[_OPEN_TIME_INDEX])) + delta_ms
        moved[_CLOSE_TIME_INDEX] = int(str(row[_CLOSE_TIME_INDEX])) + delta_ms
        shifted.append(moved)
    return tuple(shifted)


def page(rows: Sequence[Sequence[object]]) -> str:
    """The rows as a response body, exactly as the endpoint serialises one."""
    return json.dumps([list(row) for row in rows], separators=(",", ":"))


def _to_epoch_ms(moment: datetime) -> int:
    return (int(moment.timestamp()) * 1000) + (moment.microsecond // 1000)
