""".env.example is the only documentation of the configuration surface that cannot rot.

It is derived from the model here rather than reviewed by eye, in both directions: a
new setting with no entry fails, and an entry for a setting that no longer exists fails.
A one-directional check leaves the file accumulating keys nobody reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from fking.platform.config import Settings, environment_variable_names

pytestmark = pytest.mark.unit

ENV_EXAMPLE: Final[Path] = Path(__file__).resolve().parents[3] / ".env.example"

# SECURITY.md section 4.3: no real hosts, no real keys, no production URLs -- not even
# commented out, because "one uncomment from live" is a real sequence.
PRODUCTION_HOST_FRAGMENTS: Final[tuple[str, ...]] = (
    "api.binance.com",
    "fapi.binance.com",
    "dapi.binance.com",
    "stream.binance.com",
    "fstream.binance.com",
    "api.bybit.com",
    "stream.bybit.com",
)


def _declared_keys() -> dict[str, str]:
    declared: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, _, configured = stripped.partition("=")
        declared[name.strip()] = configured.strip()
    return declared


def test_env_example_exists() -> None:
    assert ENV_EXAMPLE.is_file()


def test_every_setting_appears_in_env_example() -> None:
    missing = sorted(set(environment_variable_names(Settings)) - set(_declared_keys()))
    assert missing == [], f"add these to .env.example: {missing}"


def test_env_example_declares_nothing_the_application_does_not_read() -> None:
    stale = sorted(set(_declared_keys()) - set(environment_variable_names(Settings)))
    assert stale == [], f"remove these from .env.example: {stale}"


def test_every_value_is_blank() -> None:
    """A placeholder that looks like a credential is a credential somebody pastes over
    with a real one and then commits. Blank means the file cannot be mistaken for a
    working configuration."""
    populated = sorted(name for name, configured in _declared_keys().items() if configured)
    assert populated == []


@pytest.mark.parametrize("fragment", PRODUCTION_HOST_FRAGMENTS)
def test_env_example_names_no_production_host(fragment: str) -> None:
    assert fragment not in ENV_EXAMPLE.read_text(encoding="utf-8")


def test_generated_names_use_the_documented_shape() -> None:
    names = environment_variable_names(Settings)
    assert "FKING_RISK__MAX_PORTFOLIO_NOTIONAL_USD" in names
    assert "FKING_EXCHANGE__BINANCE__FUTURES_API_KEY" in names
    assert all(name.startswith("FKING_") for name in names)
