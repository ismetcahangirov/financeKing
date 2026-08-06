"""The gate that refuses a window the corpus cannot serve, and what it prints.

`BACKTEST_ENGINE.md` section 9 gives one line for missing bars -- *do not interpolate;
report coverage; narrow the window or refuse* -- and this file is the assertion that the
refusal happens and that the report is usable when it does. "Usable" means the *ranges*, not
a count: a reader deciding whether a hole overlaps the regime under test, or which range to
backfill, cannot get either from "99.2% covered", and the number that would tempt someone to
proceed is exactly the number nobody should be looking at.

Every corpus here is written from the recorded `data.binance.vision` archive with rows
omitted, never with rows edited. A partition that is short is what a truncated archive and an
interrupted backfill both produce; a partition with invented rows in it is not a thing that
happens, and testing against one would prove nothing about either.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from fking.backtest.feed import (
    EX_CONFIG,
    EX_DATAERR,
    CoverageRefusedError,
    MarketDataFeed,
    SeriesRequest,
    main,
)
from fking.backtest.feed import __main__ as entrypoint
from fking.data.format_resolver import Market
from fking.domain import Instrument, Venue
from tests.backtest import feed_support as fs

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# Minutes 30 through 40 inclusive: eleven consecutive bars inside the exposed span, which is
# one outage rather than eleven, and must be reported as one range.
GAP_MINUTES = tuple(range(30, 41))
GAP_BAR_COUNT = len(GAP_MINUTES)

# The window every test below shares: twenty warm-up bars and forty exposed ones.
WARMUP_BAR_COUNT = 20
EXPOSED_BAR_COUNT = 40
WINDOW_BAR_COUNT = WARMUP_BAR_COUNT + EXPOSED_BAR_COUNT

# The narrowed window that starts after the gap ends.
NARROWED_WARMUP_BAR_COUNT = 4
NARROWED_EXPOSED_BAR_COUNT = 45


def _feed(root: Path) -> MarketDataFeed:
    return MarketDataFeed(corpus_root=root, now_utc=fs.NOW_UTC)


def test_a_complete_window_is_servable_and_says_so(tmp_path: Path) -> None:
    fs.write_corpus(tmp_path)
    report = _feed(tmp_path).coverage(
        fs.request_for(exposed_minute=20, until_minute=60, warmup_bar_count=20)
    )

    assert report.is_servable
    assert report.incomplete == ()
    assert "no gaps" in report.render()
    assert (
        report.series[0].observed_bar_count
        == report.series[0].expected_bar_count
        == WINDOW_BAR_COUNT
    )


def test_an_injected_gap_refuses_the_run(tmp_path: Path) -> None:
    """The acceptance criterion, stated directly."""
    fs.write_corpus(tmp_path, omit_open_times=fs.minutes(*GAP_MINUTES))
    request = fs.request_for(exposed_minute=20, until_minute=60, warmup_bar_count=20)

    with pytest.raises(CoverageRefusedError) as refused:
        _feed(tmp_path).load(request)

    message = str(refused.value)
    assert "spot/BTCUSDT" in message
    assert "2025-01-02T00:30:00+00:00 .. 2025-01-02T00:41:00+00:00" in message
    assert f"({GAP_BAR_COUNT} bars)" in message


def test_the_report_names_the_gap_as_one_range_rather_than_eleven(tmp_path: Path) -> None:
    """Eleven consecutive absences were one outage. Reporting them singly is a wall of text."""
    fs.write_corpus(tmp_path, omit_open_times=fs.minutes(*GAP_MINUTES))
    report = _feed(tmp_path).coverage(
        fs.request_for(exposed_minute=20, until_minute=60, warmup_bar_count=20)
    )

    coverage = report.series[0]
    assert len(coverage.gaps) == 1
    assert coverage.gaps[0].missing_bar_count == GAP_BAR_COUNT
    assert coverage.observed_bar_count == WINDOW_BAR_COUNT - GAP_BAR_COUNT
    assert coverage.expected_bar_count == WINDOW_BAR_COUNT
    assert not report.is_servable


def test_two_separated_holes_are_two_ranges(tmp_path: Path) -> None:
    """One outage and two outages are different findings, and the report must distinguish."""
    fs.write_corpus(tmp_path, omit_open_times=fs.minutes(25, 26, 45))
    report = _feed(tmp_path).coverage(
        fs.request_for(exposed_minute=20, until_minute=60, warmup_bar_count=20)
    )

    gaps = report.series[0].gaps
    assert [gap.missing_bar_count for gap in gaps] == [2, 1]
    assert [gap.gap_start_utc for gap in gaps] == list(fs.minutes(25, 45))


def test_a_hole_in_the_warm_up_span_refuses_just_as_one_in_the_exposed_span_does(
    tmp_path: Path,
) -> None:
    """Warm-up is inside the covered span, not beside it.

    A feature warmed from fewer observations than it declared produces values no live run
    would ever have had, and they land in the sample looking exactly like real ones. The
    window below is clean everywhere the strategy can see.
    """
    fs.write_corpus(tmp_path, omit_open_times=fs.minutes(5, 6, 7))
    request = fs.request_for(exposed_minute=20, until_minute=60, warmup_bar_count=20)

    report = _feed(tmp_path).coverage(request)
    assert not report.is_servable
    assert report.series[0].gaps[0].gap_start_utc == fs.minutes(5)[0]
    with pytest.raises(CoverageRefusedError):
        _feed(tmp_path).load(request)


def test_a_series_with_no_partitions_at_all_is_one_gap_over_the_whole_window(
    tmp_path: Path,
) -> None:
    """ "Never ingested" and "ingested with a hole" both refuse, and read differently."""
    fs.write_corpus(tmp_path)
    request = fs.request_for(exposed_minute=20, until_minute=60, warmup_bar_count=20)
    ethusdt = SeriesRequest(
        market=Market.SPOT,
        instrument=Instrument(
            venue=Venue.BINANCE_SPOT_TESTNET,
            symbol="ETHUSDT",
            base_asset="ETH",
            quote_asset="USDT",
            tick_size=Decimal("0.01"),
            lot_step=Decimal("0.0001"),
            min_notional_quote=Decimal("10.00"),
        ),
    )
    widened = replace(request, series=(*request.series, ethusdt))

    report = _feed(tmp_path).coverage(widened)
    missing = next(entry for entry in report.series if entry.symbol == "ETHUSDT")
    assert missing.observed_bar_count == 0
    assert missing.first_open_time_utc is None
    assert [gap.missing_bar_count for gap in missing.gaps] == [WINDOW_BAR_COUNT]
    assert "(no partition read)" in "\n".join(missing.render())


def test_the_refusal_never_offers_a_way_to_proceed(tmp_path: Path) -> None:
    """There is no tolerance knob, and the message must not imply one.

    A configurable "acceptable missing bars" would be raised by whoever is in a hurry, and
    the run it admits produces a number nobody can distinguish from a real one.
    """
    fs.write_corpus(tmp_path, omit_open_times=fs.minutes(*GAP_MINUTES))
    report = _feed(tmp_path).coverage(
        fs.request_for(exposed_minute=20, until_minute=60, warmup_bar_count=20)
    )

    rendered = report.render()
    assert "Narrow the window or backfill" in rendered
    assert "interpolat" in rendered
    assert not hasattr(report, "tolerated_missing_bar_count")


def test_narrowing_the_window_past_the_gap_serves_the_run(tmp_path: Path) -> None:
    """The other half of the instruction: narrow, or refuse. Both must actually work."""
    fs.write_corpus(tmp_path, omit_open_times=fs.minutes(*GAP_MINUTES))
    narrowed = fs.request_for(exposed_minute=45, until_minute=90, warmup_bar_count=4)

    loaded = _feed(tmp_path).load(narrowed)
    assert loaded.coverage.is_servable
    # Four warm-up bars plus the forty-five the strategy is exposed to.
    assert (loaded.warmup_event_count, loaded.exposed_event_count) == (
        NARROWED_WARMUP_BAR_COUNT,
        NARROWED_EXPOSED_BAR_COUNT,
    )
    assert len(loaded.events) == NARROWED_WARMUP_BAR_COUNT + NARROWED_EXPOSED_BAR_COUNT


# ---------------------------------------------------------------------------
# `make backtest`
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, corpus_root: Path) -> Path:
    config = tmp_path / "backtest.toml"
    config.write_text(
        fs.config_toml(
            corpus_root=corpus_root,
            exposed_from_utc=fs.DAY_START_UTC + fs.MINUTE * 20,
            until_utc=fs.DAY_START_UTC + fs.MINUTE * 60,
            warmup_bar_count=20,
        ),
        encoding="utf-8",
    )
    return config


def test_the_command_refuses_a_gapped_window_and_prints_the_ranges(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """What `make backtest` does on a window containing an injected gap."""
    corpus = tmp_path / "corpus"
    fs.write_corpus(corpus, omit_open_times=fs.minutes(*GAP_MINUTES))
    config = _write_config(tmp_path, corpus)

    assert main([str(config)]) == EX_DATAERR

    printed = capsys.readouterr().out
    assert "spot/BTCUSDT" in printed
    assert (
        f"2025-01-02T00:30:00+00:00 .. 2025-01-02T00:41:00+00:00 ({GAP_BAR_COUNT} bars)" in printed
    )
    assert "REFUSED" in printed
    assert "digest" not in printed


def test_the_command_serves_a_clean_window_and_reports_the_stream(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = tmp_path / "corpus"
    fs.write_corpus(corpus)
    config = _write_config(tmp_path, corpus)

    assert main([str(config)]) == 0

    printed = capsys.readouterr().out
    assert "every requested bar is present" in printed
    assert "60 events from 60 archive bars (20 warm-up, 40 exposed)" in printed
    assert re.search(r"digest  [0-9a-f]{64}", printed) is not None
    # The command must not let a reader mistake a data gate for an executed strategy.
    assert "venue simulator, cost model and validation harness are not yet wired" in printed


def test_a_corpus_the_command_cannot_resolve_is_a_data_error_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A partition whose epoch unit is undeclared refuses before any coverage is printed.

    An operator should get an exit code and a sentence, not a stack trace: the two
    outcomes `make backtest` has are 'here is the report' and 'here is why there is no
    report', and a traceback is neither.
    """
    corpus = tmp_path / "corpus"
    fs.write_corpus(corpus)
    # A second partition for the same series, dated before the spot corpus exists.
    stray = (
        corpus
        / "market=spot"
        / "dataset=klines"
        / "symbol=BTCUSDT"
        / "interval=1m"
        / "year=2016"
        / "month=12"
    )
    stray.mkdir(parents=True)
    (stray / "part-2016-12.parquet").write_bytes(b"")
    config = tmp_path / "backtest.toml"
    config.write_text(
        fs.config_toml(
            corpus_root=corpus,
            exposed_from_utc=datetime(2016, 12, 2, tzinfo=UTC),
            until_utc=datetime(2016, 12, 2, 1, tzinfo=UTC),
            warmup_bar_count=0,
        ),
        encoding="utf-8",
    )

    assert main([str(config)]) == EX_DATAERR
    assert "no archive format is declared" in capsys.readouterr().err


def test_a_missing_configuration_file_is_a_configuration_error_not_a_data_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """78 and 65 must stay distinguishable: "you configured this wrongly" is not "the
    corpus cannot serve it", and a deploy script should not have to parse stderr."""
    assert main([str(tmp_path / "absent.toml")]) == EX_CONFIG
    assert "no backtest configuration at" in capsys.readouterr().err


def test_the_make_target_runs_this_module() -> None:
    """The Makefile and the entrypoint must not drift.

    Every other test here calls `main` in process, which is what a coverage run can measure.
    This is the one assertion that the thing an operator types reaches that function.
    """
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = re.search(r"^backtest:.*?\n(?:\t.*\n)+", makefile, re.MULTILINE)
    assert recipe is not None, "the Makefile has no `backtest` target"
    assert "python -m fking.backtest.feed" in recipe.group(0)
    assert "$(BACKTEST_CONFIG)" in recipe.group(0)


def test_the_module_entrypoint_calls_the_function_every_other_test_exercises() -> None:
    """`python -m fking.backtest.feed` must reach the `main` asserted on above.

    Two tokens of glue, and the failure it closes is the one an in-process suite cannot
    see: a `__main__` that imports something else, or that has quietly grown a second code
    path, while every test here goes on passing.
    """
    assert entrypoint.main is main


@pytest.mark.slow
def test_python_dash_m_refuses_a_gapped_window_end_to_end(tmp_path: Path) -> None:
    """The `python -m` spelling itself, run once, as a subprocess.

    In-process calls cannot catch a broken `__main__.py`, an import that only resolves under
    the test runner's sys.path, or an exit code swallowed on the way out -- and those are the
    three ways a command that every test says works fails for the operator who types it.
    """
    corpus = tmp_path / "corpus"
    fs.write_corpus(corpus, omit_open_times=fs.minutes(*GAP_MINUTES))
    config = _write_config(tmp_path, corpus)

    completed = subprocess.run(  # noqa: S603 - sys.executable and paths this test wrote
        [sys.executable, "-m", "fking.backtest.feed", str(config)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )

    assert completed.returncode == EX_DATAERR, completed.stderr
    assert "2025-01-02T00:30:00+00:00 .. 2025-01-02T00:41:00+00:00" in completed.stdout
