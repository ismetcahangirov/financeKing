"""The backtest configuration file, and the four things it refuses to guess.

A configuration reader is where a project's non-negotiables leak out of the process. Every
rule inside `src/fking` about decimals, timezones and defaults is worth what the file
boundary is worth, and TOML is helpfully typed in exactly the two ways that hurt: it has a
float, and it has a local date-time with no offset. Both parse into something plausible and
neither announces itself.

So `tick_size = 0.01` is refused rather than accepted -- by the time Python has it, it is
`0.01000000000000000020816681711721685...` and no later annotation recovers the tenth of a
cent -- and a boundary with no offset is refused rather than localised, because a window
silently read as machine-local selects a different set of bars than the one it names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fking.backtest.feed import FeedRequestError, load_config
from fking.data.format_resolver import Market
from tests.backtest import feed_support as fs

pytestmark = pytest.mark.unit

WARMUP_BAR_COUNT = 20


def _write(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "backtest.toml"
    config.write_text(body, encoding="utf-8")
    return config


def _valid_body(tmp_path: Path) -> str:
    return fs.config_toml(
        corpus_root=tmp_path / "corpus",
        exposed_from_utc=fs.DAY_START_UTC + fs.MINUTE * 20,
        until_utc=fs.DAY_START_UTC + fs.MINUTE * 60,
        warmup_bar_count=WARMUP_BAR_COUNT,
    )


def test_a_well_formed_file_produces_the_request_it_states(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, _valid_body(tmp_path)))

    assert config.corpus_root == tmp_path / "corpus"
    assert config.now_utc == fs.NOW_UTC
    assert config.request.bar_interval == "1m"
    assert config.request.warmup_bar_count == WARMUP_BAR_COUNT
    assert config.request.warmup_start_utc == fs.DAY_START_UTC
    assert [entry.label for entry in config.request.series] == ["spot/BTCUSDT"]
    assert config.request.series[0].instrument.tick_size == fs.BTCUSDT_SPOT.tick_size


def test_a_decimal_written_as_a_toml_float_is_refused(tmp_path: Path) -> None:
    """The one that would pass review. `0.01` is already rounded before this process runs,
    and the value it becomes decides whether an order is an order at all."""
    body = _valid_body(tmp_path).replace('tick_size = "0.01"', "tick_size = 0.01")

    with pytest.raises(FeedRequestError, match="is a TOML float; quote it as a string"):
        load_config(_write(tmp_path, body))


def test_a_window_boundary_with_no_offset_is_refused(tmp_path: Path) -> None:
    """TOML's local date-time parses into a naive Python datetime, which is a window
    boundary read in whatever zone the container happens to run in."""
    body = _valid_body(tmp_path).replace(
        f'exposed_from_utc = "{(fs.DAY_START_UTC + fs.MINUTE * 20).isoformat()}"',
        "exposed_from_utc = 2025-01-02T00:20:00",
    )

    with pytest.raises(FeedRequestError, match="timezone-aware UTC"):
        load_config(_write(tmp_path, body))


@pytest.mark.parametrize(
    "key",
    ["corpus_root", "bar_interval", "exposed_from_utc", "until_utc", "warmup_bar_count", "now_utc"],
)
def test_every_top_level_key_is_required(tmp_path: Path, key: str) -> None:
    """Nothing is defaulted. A window, an interval or a warm-up length filled in silently is
    a run whose result answers a different question from the one in the file."""
    body = "\n".join(
        line for line in _valid_body(tmp_path).splitlines() if not line.startswith(f"{key} =")
    )

    with pytest.raises(FeedRequestError, match=f"missing the required key '{key}'"):
        load_config(_write(tmp_path, body))


def test_a_file_with_no_series_is_refused(tmp_path: Path) -> None:
    body = _valid_body(tmp_path).split("[[series]]")[0]

    with pytest.raises(FeedRequestError, match=r"\[\[series\]\]"):
        load_config(_write(tmp_path, body))


def test_an_unknown_market_names_the_declared_ones(tmp_path: Path) -> None:
    body = _valid_body(tmp_path).replace(f'market = "{Market.SPOT.value}"', 'market = "coinm"')

    with pytest.raises(FeedRequestError, match="declared markets are"):
        load_config(_write(tmp_path, body))


def test_an_unknown_venue_says_that_every_declared_one_is_a_testnet(tmp_path: Path) -> None:
    """The refusal is the place to say it: a production venue is a change to the demo-only
    guarantee, not a value a configuration file gets to introduce."""
    body = _valid_body(tmp_path).replace('venue = "binance-spot-testnet"', 'venue = "binance-spot"')

    with pytest.raises(FeedRequestError, match="Every one is a testnet"):
        load_config(_write(tmp_path, body))


def test_malformed_toml_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(FeedRequestError, match="is not readable as TOML"):
        load_config(_write(tmp_path, "corpus_root = [unclosed"))
