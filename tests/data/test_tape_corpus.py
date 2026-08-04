"""The live tape's landing site: a spool that survives a process, then a whole partition.

The properties asserted here are the ones that fail silently. A spool that lost its tail
produces a partition missing prints nothing recorded as missing. A seal keyed on the
clock rather than on the print's own event time files the last second of a day under the
next one, and every partition-filtered query agrees with it. A rewrite that narrows a
partition deletes prints while reporting a successful write.

Real files throughout, and a real `write_records`. There is no schema, trigger or grant
here for a double to be wrong about -- the thing under test *is* the bytes on disk -- so
mocking the filesystem would assert that the mock accumulates a day.

Prints come from `tests/support/tape_prints`, which parses frames captured from a live
testnet socket.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from fking.data.backfill.registry import NO_INTERVAL, SeriesKey
from fking.data.format_resolver import Dataset, Market
from fking.data.live.router import LiveTrade
from fking.data.live.tape import SEAL_GRACE, TapeCorpusWriter, spool_path
from fking.data.loaders.records import TradeRecord
from fking.data.parquet.schema import RecordSource
from fking.platform.errors import DataIntegrityError, SeamDisagreementError
from tests.support import tape_prints

pytestmark = pytest.mark.unit

SYMBOL = "BTCUSDT"
SERIES = SeriesKey(
    market=Market.SPOT, dataset=Dataset.AGG_TRADES, symbol=SYMBOL, bar_interval=NO_INTERVAL
)

TAPE_DAY = date(2026, 8, 3)
TAPE_START = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
# The plausibility reference for epoch normalisation. Fixed, so the tests do not move
# their own boundaries as the real clock advances.
NOW_UTC = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

MIDNIGHT_AFTER = datetime(2026, 8, 4, tzinfo=UTC)
SEALABLE_AT = MIDNIGHT_AFTER + SEAL_GRACE + timedelta(minutes=1)
INSIDE_THE_GRACE = MIDNIGHT_AFTER + SEAL_GRACE - timedelta(minutes=1)


@pytest.fixture(name="roots")
def roots_fixture(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "parquet", tmp_path / "spool"


@pytest.fixture(autouse=True)
def _close_writers() -> Iterator[None]:
    """Close every writer a test constructed, whatever the test did.

    Not tidiness: a spool handle left open holds a lock on Windows, and the next
    assertion about the file would fail for a reason that has nothing to do with the code
    under test.
    """
    _OPENED.clear()
    yield
    for writer in _OPENED:
        writer.close()
    _OPENED.clear()


_OPENED: list[TapeCorpusWriter] = []


def _writer(roots: tuple[Path, Path]) -> TapeCorpusWriter:
    corpus_root, spool_root = roots
    opened = TapeCorpusWriter(corpus_root=corpus_root, spool_root=spool_root)
    _OPENED.append(opened)
    return opened


def _prints(
    count: int, *, offset: int = 0, first_event_utc: datetime = TAPE_START
) -> tuple[TradeRecord, ...]:
    return tape_prints.prints(
        count, first_event_utc=first_event_utc, now_utc=NOW_UTC, offset=offset
    )


def _live(records: Sequence[TradeRecord]) -> tuple[LiveTrade, ...]:
    return tuple(LiveTrade(SERIES, record) for record in records)


def _partition_ids(path: Path) -> list[str]:
    return [str(value) for value in pq.read_table(path).column("venue_trade_id").to_pylist()]


def test_a_sealed_day_holds_every_print_the_session_received(
    roots: tuple[Path, Path],
) -> None:
    """The criterion the whole spool exists for. A partition is the day, not a sample."""
    recorded = _prints(40)
    writer = _writer(roots)
    writer.append(_live(recorded))

    sealed = writer.seal_elapsed_days(now_utc=SEALABLE_AT)

    assert len(sealed) == 1
    partition = sealed[0]
    assert partition.day == TAPE_DAY
    assert partition.prints_written == len(recorded)
    assert partition.was_rewritten is True
    assert _partition_ids(partition.path) == [record.venue_trade_id for record in recorded]

    table = pq.read_table(partition.path)
    assert set(table.column("source").to_pylist()) == {RecordSource.STREAM.value}
    # The prices are the venue's, to the digit, through a spool round trip and a
    # decimal128(38, 18) column.
    assert table.column("quote_price").to_pylist()[0] == recorded[0].quote_price


def test_the_partition_lands_under_the_datasets_own_daily_path(
    roots: tuple[Path, Path],
) -> None:
    """`aggTrades` is a separate partition tree from `trades`. One aggregate print covers
    a range of raw ones, so a glob that spanned both would double-count volume."""
    corpus_root, _ = roots
    writer = _writer(roots)
    writer.append(_live(_prints(3)))

    sealed = writer.seal_elapsed_days(now_utc=SEALABLE_AT)

    assert sealed[0].path == (
        corpus_root
        / "market=spot"
        / "dataset=aggTrades"
        / "symbol=BTCUSDT"
        / "year=2026"
        / "month=08"
        / "day=03"
        / "part-2026-08-03.parquet"
    )


def test_re_running_the_rollover_writes_nothing_new(roots: tuple[Path, Path]) -> None:
    """Idempotent by content digest, which is what makes a re-run of a session safe.

    The second seal is given the same prints again -- the shape a restarted session that
    re-consumed the same window produces -- and must recognise the file it already wrote
    rather than replacing its bytes.
    """
    recorded = _prints(20)
    first = _writer(roots)
    first.append(_live(recorded))
    original = first.seal_elapsed_days(now_utc=SEALABLE_AT)[0]
    written_bytes = original.path.read_bytes()

    second = _writer(roots)
    second.append(_live(recorded))
    repeated = second.seal_elapsed_days(now_utc=SEALABLE_AT)[0]

    assert repeated.content_digest_hex == original.content_digest_hex
    assert repeated.was_rewritten is False
    assert repeated.path.read_bytes() == written_bytes


def test_a_day_that_has_not_ended_is_not_sealed(roots: tuple[Path, Path]) -> None:
    """A partition is written whole. Sealing at noon would file half a day as the day."""
    writer = _writer(roots)
    writer.append(_live(_prints(5)))

    assert writer.seal_elapsed_days(now_utc=datetime(2026, 8, 3, 23, 59, tzinfo=UTC)) == ()
    assert spool_path(SERIES, TAPE_DAY, spool_root=roots[1]).is_file()


def test_a_day_inside_the_grace_window_is_not_sealed(roots: tuple[Path, Path]) -> None:
    """A print stamped 23:59:59.999 can arrive after the clock has passed midnight, and
    sealing on the date change would race it into a file that had just been written."""
    writer = _writer(roots)
    writer.append(_live(_prints(5)))

    assert writer.seal_elapsed_days(now_utc=INSIDE_THE_GRACE) == ()
    assert writer.seal_elapsed_days(now_utc=SEALABLE_AT) != ()


def test_a_print_is_filed_by_its_own_event_time_not_by_the_clock(
    roots: tuple[Path, Path],
) -> None:
    """The partition key is read from the path, so a print filed under the wrong day is
    a print every filtered query agrees is in a period it is not in.

    One print, stamped a tenth of a second before midnight. Whatever the clock says when
    it is handed over, it belongs to the day it happened on.
    """
    last_of_the_day = _prints(
        1, first_event_utc=datetime(2026, 8, 3, 23, 59, 59, 900_000, tzinfo=UTC)
    )
    writer = _writer(roots)
    writer.append(_live(last_of_the_day))

    assert spool_path(SERIES, TAPE_DAY, spool_root=roots[1]).is_file()
    assert not spool_path(SERIES, date(2026, 8, 4), spool_root=roots[1]).exists()


def test_prints_spanning_midnight_are_split_into_two_days(
    roots: tuple[Path, Path],
) -> None:
    """One `append` call can straddle a boundary, and the split is per print."""
    straddling = _prints(6, first_event_utc=datetime(2026, 8, 3, 23, 59, 59, 900_000, tzinfo=UTC))
    writer = _writer(roots)
    writer.append(_live(straddling))

    spooled_days = {record.event_time_utc.date() for record in straddling}
    assert spooled_days == {TAPE_DAY, date(2026, 8, 4)}
    for day in spooled_days:
        assert spool_path(SERIES, day, spool_root=roots[1]).is_file()


def test_a_spool_left_by_a_dead_process_is_sealed_by_the_next_one(
    roots: tuple[Path, Path],
) -> None:
    """`seal_elapsed_days` reads the spool directory, not this instance's memory. A
    session that died at 23:00 must not cost the corpus its day."""
    abandoned_prints = _prints(12)
    abandoned = _writer(roots)
    abandoned.append(_live(abandoned_prints))
    abandoned.close()

    sealed = _writer(roots).seal_elapsed_days(now_utc=SEALABLE_AT)

    assert len(sealed) == 1
    assert sealed[0].prints_written == len(abandoned_prints)


def test_a_reconnect_that_replays_prints_writes_each_of_them_once(
    roots: tuple[Path, Path],
) -> None:
    """The spool is append-only and a socket may resend, so the seal deduplicates -- and
    reports how much overlap there was rather than swallowing it."""
    recorded = _prints(9)
    replayed = recorded[-3:]
    writer = _writer(roots)
    writer.append(_live(recorded))
    writer.append(_live(replayed))

    sealed = writer.seal_elapsed_days(now_utc=SEALABLE_AT)[0]

    assert sealed.prints_written == len(recorded)
    assert sealed.duplicates_dropped == len(replayed)
    assert _partition_ids(sealed.path) == [record.venue_trade_id for record in recorded]


def test_two_prints_under_one_id_stop_the_seal(roots: tuple[Path, Path]) -> None:
    """A spool holding one id twice with different prices is a contradiction, and the
    seal escalates it exactly as a REST repair would rather than picking one."""
    recorded = _prints(3)
    contradicted = _prints(3, offset=40)
    relabelled = tuple(
        LiveTrade(SERIES, replace(other, venue_trade_id=original.venue_trade_id))
        for original, other in zip(recorded, contradicted, strict=True)
    )
    writer = _writer(roots)
    writer.append(_live(recorded))
    writer.append(relabelled)

    with pytest.raises(SeamDisagreementError, match="two different trades under id"):
        writer.seal_elapsed_days(now_utc=SEALABLE_AT)


def test_a_seal_that_would_shrink_a_partition_is_refused(
    roots: tuple[Path, Path],
) -> None:
    """A partition is written whole, so a rewrite from a partial spool deletes prints the
    corpus holds -- and reports a successful write while doing it."""
    writer = _writer(roots)
    writer.append(_live(_prints(20)))
    writer.seal_elapsed_days(now_utc=SEALABLE_AT)

    late = _writer(roots)
    late.append(_live(_prints(1, offset=25)))

    with pytest.raises(DataIntegrityError, match="already holds 20"):
        late.seal_elapsed_days(now_utc=SEALABLE_AT)


def test_a_torn_spool_line_stops_the_seal_rather_than_being_skipped(
    roots: tuple[Path, Path],
) -> None:
    """Dropping it would lose prints the sequence detector never reported missing -- an
    absence with no gap row, which is the one hole the coverage registry cannot describe."""
    writer = _writer(roots)
    writer.append(_live(_prints(4)))
    writer.close()
    spool = spool_path(SERIES, TAPE_DAY, spool_root=roots[1])
    with spool.open("a", encoding="utf-8") as handle:
        handle.write('{"venue_trade_id":"9999","quote_pri\n')

    with pytest.raises(DataIntegrityError, match="is not JSON"):
        _writer(roots).seal_elapsed_days(now_utc=SEALABLE_AT)


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        pytest.param(
            {"is_buyer_maker": "false"},
            "not a JSON boolean",
            id="a truthy string where the aggressor side belongs",
        ),
        pytest.param(
            {"quote_price": "not-a-number"},
            "not a decimal",
            id="a price that is not a decimal",
        ),
        pytest.param(
            {"event_time_utc": "2026-08-03T10:00:00"},
            "must be timezone-aware UTC",
            id="a naive instant, which moves prints across partition boundaries",
        ),
        pytest.param({}, "carries no 'quote_price'", id="a field that is simply absent"),
    ],
)
def test_a_spool_line_that_cannot_be_trusted_stops_the_seal(
    roots: tuple[Path, Path], edit: dict[str, object], expected: str
) -> None:
    """Each of these would otherwise write a print that is wrong in one field and correct
    in every other -- `is_buyer_maker` most of all, which is the aggressor side inverted."""
    writer = _writer(roots)
    writer.append(_live(_prints(1)))
    writer.close()
    spool = spool_path(SERIES, TAPE_DAY, spool_root=roots[1])
    document = json.loads(spool.read_text(encoding="utf-8").strip())
    document.update(edit)
    if not edit:
        del document["quote_price"]
    spool.write_text(json.dumps(document) + "\n", encoding="utf-8")

    with pytest.raises(DataIntegrityError, match=expected):
        _writer(roots).seal_elapsed_days(now_utc=SEALABLE_AT)


def test_closing_a_session_seals_nothing(roots: tuple[Path, Path]) -> None:
    """Shutdown is not a day boundary. The half-day stays a spool and the next session
    continues the same file."""
    corpus_root, spool_root = roots
    writer = _writer(roots)
    writer.append(_live(_prints(7)))

    writer.close()

    assert spool_path(SERIES, TAPE_DAY, spool_root=spool_root).is_file()
    assert not corpus_root.exists()


def test_an_empty_spool_is_discarded_rather_than_written_as_a_zero_row_file(
    roots: tuple[Path, Path],
) -> None:
    """A zero-row Parquet file is indistinguishable from a gap to every later scan, which
    reads it as "we have this period"."""
    _, spool_root = roots
    spool = spool_path(SERIES, TAPE_DAY, spool_root=spool_root)
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.touch()

    sealed = _writer(roots).seal_elapsed_days(now_utc=SEALABLE_AT)

    assert len(sealed) == 1
    assert sealed[0].prints_written == 0
    assert not sealed[0].path.exists()
    assert not spool.exists()


def test_a_spool_path_outside_the_declared_layout_is_refused(
    roots: tuple[Path, Path],
) -> None:
    """The parse back out of the path is anchored on the Hive keys, so a stray file is
    refused rather than read as whichever series its depth lined up with."""
    _, spool_root = roots
    stray = spool_root / "market=spot" / "not-a-key" / "symbol=BTCUSDT" / "2026-08-03.jsonl"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("", encoding="utf-8")

    with pytest.raises(DataIntegrityError, match="is not a 'dataset='"):
        _writer(roots).seal_elapsed_days(now_utc=SEALABLE_AT)
