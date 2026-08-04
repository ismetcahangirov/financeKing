"""`python -m fking.data.backfill` -- the two commands an operator runs.

In process rather than by subprocess, for the reason
`tests/platform/persistence/test_entrypoint.py` gives from the other side: a coverage run
can measure what an in-process call executed, and what is asserted here is the exit code and
the printed output rather than process mechanics.

Every test that could otherwise reach `data.binance.vision` is arranged to fail before the
first request. A unit test that quietly downloads an archive is a unit test that is green
until the day the host is slow.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from fking.data.backfill.__main__ import _symbols, main
from fking.data.backfill.report import BackfillReport, SymbolReport
from fking.data.format_resolver import Dataset, Market
from fking.platform.config import EX_CONFIG

pytestmark = pytest.mark.unit


def test_an_empty_symbol_list_is_refused_rather_than_reporting_a_clean_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A backfill of no symbols reports a complete run having fetched nothing."""
    monkeypatch.chdir(tmp_path)  # no .env in reach; see test_entrypoint.py
    assert main(["ingest", "--symbols", " , "]) == EX_CONFIG
    assert "--symbols is empty" in capsys.readouterr().err


def test_a_malformed_through_date_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["ingest", "--symbols", "BTCUSDT", "--through", "last-tuesday"]) == EX_CONFIG
    assert "invalid argument" in capsys.readouterr().err


def test_invalid_configuration_exits_78(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FKING_RISK__MAX_LEVERAGE", "99")
    assert main(["coverage"]) == EX_CONFIG
    assert "max_leverage" in capsys.readouterr().err


def test_symbols_are_normalised_and_deduplicated() -> None:
    assert _symbols(" btcusdt , ETHUSDT ,BTCUSDT") == ("BTCUSDT", "ETHUSDT")


def test_an_unpublished_symbol_renders_as_a_sentence_not_a_blank_row() -> None:
    """A symbol with no archive must not read as a symbol with no data left to fetch."""
    report = BackfillReport(
        market=Market.SPOT,
        dataset=Dataset.KLINES,
        interval="1m",
        through_date=date(2025, 1, 6),
        symbols=(
            SymbolReport(
                symbol="BTCUSDT",
                earliest_archive_date=None,
                partitions_written=0,
                partitions_resumed=0,
                archives_ingested=0,
                archives_absent=0,
                rows_in=0,
                rows_out=0,
                rows_rejected=0,
                rejection_reasons={},
                gaps_recorded=0,
                gaps_newly_discovered=0,
                gapped_duration=timedelta(0),
                first_event_time_utc=None,
                last_event_time_utc=None,
            ),
        ),
    )

    rendered = report.render()
    assert "no published archive" in rendered
    assert "no archive published in the searched range for ['BTCUSDT']" in rendered
    assert report.shortest_history_start is None


def test_rejections_are_named_per_reason_not_totalled() -> None:
    """DATA_PIPELINE.md section 4: a run reporting only rows_out has hidden its rejections."""
    symbol = SymbolReport(
        symbol="BTCUSDT",
        earliest_archive_date=date(2025, 1, 2),
        partitions_written=1,
        partitions_resumed=0,
        archives_ingested=1,
        archives_absent=0,
        rows_in=1440,
        rows_out=1437,
        rows_rejected=3,
        rejection_reasons={"boolean_unrecognised": 2, "ohlc_not_bracketing": 1},
        gaps_recorded=0,
        gaps_newly_discovered=0,
        gapped_duration=timedelta(0),
        first_event_time_utc=None,
        last_event_time_utc=None,
    )
    assert symbol.describe_rejections() == "boolean_unrecognised=2, ohlc_not_bracketing=1"

    report = BackfillReport(
        market=Market.SPOT,
        dataset=Dataset.KLINES,
        interval="1m",
        through_date=date(2025, 1, 6),
        symbols=(symbol, symbol),
    )
    assert report.rows_rejected == 6  # noqa: PLR2004 - two symbols, three rejections each
    assert report.describe_rejections() == "boolean_unrecognised=4, ohlc_not_bracketing=2"


@pytest.mark.integration
@pytest.mark.slow
def test_the_coverage_command_reports_an_empty_corpus_plainly(
    migrated_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """An empty corpus prints a sentence, not an empty table that reads like a failure."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FKING_DATABASE__DSN", migrated_dsn)

    assert main(["coverage"]) == 0
    assert "no ingested partitions" in capsys.readouterr().out
