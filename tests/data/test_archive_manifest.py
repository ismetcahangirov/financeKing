"""Which symbols the archive actually holds verified history for.

The manifest reads the corpus the ingestion path writes, so the fixture here is built
with `partition_path` rather than by hand-spelling Hive directories: a layout change
that broke the manifest would otherwise pass this test and fail in production.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from fking.data import ArchiveCoordinate, Dataset, Market, ParquetArchiveManifest
from fking.data.parquet import partition_path

pytestmark = pytest.mark.unit


def _write_partition(root: Path, *, market: Market, symbol: str, archive_date: date) -> None:
    path = partition_path(
        ArchiveCoordinate(
            market=market,
            dataset=Dataset.KLINES,
            symbol=symbol,
            archive_date=archive_date,
            interval="1m",
        ),
        root=root,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PAR1")


def test_a_symbol_with_a_written_partition_has_history(tmp_path: Path) -> None:
    _write_partition(tmp_path, market=Market.SPOT, symbol="BTCUSDT", archive_date=date(2025, 1, 2))
    manifest = ParquetArchiveManifest(corpus_root=tmp_path)
    assert manifest.symbols_with_history(market=Market.SPOT, dataset=Dataset.KLINES) == frozenset(
        {"BTCUSDT"}
    )


def test_a_symbol_directory_with_no_partition_file_is_not_history(tmp_path: Path) -> None:
    """What an interrupted write leaves behind. Admitting it would put a symbol into the
    tradable universe that no backtest can read a single bar for."""
    _write_partition(tmp_path, market=Market.SPOT, symbol="BTCUSDT", archive_date=date(2025, 1, 2))
    (tmp_path / "market=spot" / "dataset=klines" / "symbol=ETHUSDT" / "interval=1m").mkdir(
        parents=True
    )
    manifest = ParquetArchiveManifest(corpus_root=tmp_path)
    assert manifest.symbols_with_history(market=Market.SPOT, dataset=Dataset.KLINES) == frozenset(
        {"BTCUSDT"}
    )


def test_markets_do_not_leak_into_each_other(tmp_path: Path) -> None:
    """Spot and futures are separate corpora with separate format histories; a symbol
    listed on one says nothing about the other."""
    _write_partition(tmp_path, market=Market.SPOT, symbol="BTCUSDT", archive_date=date(2025, 1, 2))
    _write_partition(
        tmp_path, market=Market.FUTURES_UM, symbol="ETHUSDT", archive_date=date(2025, 1, 2)
    )
    manifest = ParquetArchiveManifest(corpus_root=tmp_path)
    assert manifest.symbols_with_history(
        market=Market.FUTURES_UM, dataset=Dataset.KLINES
    ) == frozenset({"ETHUSDT"})


def test_an_absent_corpus_reports_no_history_rather_than_raising(tmp_path: Path) -> None:
    """The empty set is the truthful answer -- nothing has been ingested -- and it fails
    loudly one layer up, where the universe resolver names every requested symbol as
    absent from the archive side."""
    manifest = ParquetArchiveManifest(corpus_root=tmp_path / "never-ingested")
    assert manifest.symbols_with_history(market=Market.SPOT, dataset=Dataset.KLINES) == frozenset()


def test_a_symbol_keeps_its_exact_code_points(tmp_path: Path) -> None:
    """Classification belongs to `fking.execution.symbols`, where the reason can be
    reported; the manifest must not decide by silently omitting a name."""
    unicode_symbol = "这是测试币456"
    _write_partition(
        tmp_path, market=Market.SPOT, symbol=unicode_symbol, archive_date=date(2025, 1, 2)
    )
    manifest = ParquetArchiveManifest(corpus_root=tmp_path)
    assert manifest.symbols_with_history(market=Market.SPOT, dataset=Dataset.KLINES) == frozenset(
        {unicode_symbol}
    )
