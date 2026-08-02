"""Construction is the boundary. Nothing downstream re-checks these values.

CONFIGURATION.md section 3: the process refuses to start on invalid configuration --
not "logs an error and continues with defaults". A typo in a limit that falls back to a
default is a system operating under a limit nobody chose, and it will not announce
itself.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from fking.platform.config import ConfigError, Settings, load_settings

pytestmark = pytest.mark.unit


def test_a_bare_process_starts_on_defaults_alone() -> None:
    """No .env, no environment. CONFIGURATION.md section 2: the process that runs
    without a configuration file is the one that matters."""
    settings = load_settings(env_file=None)
    assert settings.risk.max_portfolio_notional_usd == Decimal("25000")
    assert settings.agents.enabled is False
    assert settings.exchange.enabled is False


def test_an_unparsable_limit_names_the_exact_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FKING_RISK__MAX_PORTFOLIO_NOTIONAL_USD", "twenty thousand")
    with pytest.raises(ConfigError) as raised:
        load_settings(env_file=None)
    assert "max_portfolio_notional_usd" in str(raised.value)


def test_an_unknown_setting_is_rejected_rather_than_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silently ignored key is a setting somebody believes is in effect."""
    monkeypatch.setenv("FKING_RISK__MAX_PORTFOLIO_NOTINAL_USD", "1000")
    with pytest.raises(ConfigError) as raised:
        load_settings(env_file=None)
    assert "max_portfolio_notinal_usd" in str(raised.value).lower()


def test_an_unprefixed_variable_does_not_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    """SECURITY.md section 2 vector 2 is a copied environment file. The FKING_ prefix
    makes most of that copying inert."""
    monkeypatch.setenv("MAX_LEVERAGE", "3")
    assert load_settings(env_file=None).risk.max_leverage == Decimal("2")


def test_a_monetary_setting_parses_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decimal("0.1") is exactly one tenth; float 0.1 is not, and the difference
    compounds across thousands of fills."""
    monkeypatch.setenv("FKING_RISK__MAX_DAILY_DRAWDOWN_RATIO", "0.025")
    ratio = load_settings(env_file=None).risk.max_daily_drawdown_ratio
    assert ratio == Decimal("0.025")
    assert str(ratio) == "0.025"


def test_the_environment_outranks_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from_file, from_environment = 4, 3
    env_file = tmp_path / "dotenv"
    env_file.write_text(f"FKING_RISK__MAX_OPEN_POSITIONS={from_file}\n", encoding="utf-8")
    assert load_settings(env_file=env_file).risk.max_open_positions == from_file

    monkeypatch.setenv("FKING_RISK__MAX_OPEN_POSITIONS", str(from_environment))
    assert load_settings(env_file=env_file).risk.max_open_positions == from_environment


def test_the_env_file_outranks_the_code_default(tmp_path: Path) -> None:
    from_file = 750
    env_file = tmp_path / "dotenv"
    env_file.write_text(f"FKING_BACKTEST__WARMUP_BARS={from_file}\n", encoding="utf-8")
    assert load_settings(env_file=env_file).backtest.warmup_bars == from_file


def test_settings_are_frozen() -> None:
    """CONFIGURATION.md section 1 principle 3: a running process's behaviour cannot
    change under it. There is no hot reload."""
    settings = load_settings(env_file=None)
    with pytest.raises(ValidationError):
        settings.risk.max_leverage = Decimal("1")


def test_a_cost_model_calibrated_on_testnet_is_refused() -> None:
    """Futures testnet showed a 7.5bp spread against production's 0.16bp. A cost model
    built from that looks conservative and is fiction."""
    with pytest.raises(ValidationError, match="testnet"):
        Settings.model_validate(
            {"backtest": {"cost_model": {"calibration_source": "binance_um_testnet_2026-07"}}}
        )


# Every property that must not become configurable, and an attempt to change it.
# `Literal[...]` with one member is the pattern: the field stays visible in the tree and
# in the boot log so its value is auditable, while the type admits nothing else.
FIXED_PROPERTIES: Final[tuple[tuple[str, dict[str, object]], ...]] = (
    ("data.verify_checksums", {"data": {"verify_checksums": False}}),
    ("data.refuse_undeclared_features", {"data": {"refuse_undeclared_features": False}}),
    (
        "exchange.execution.reconcile_on_startup",
        {"exchange": {"execution": {"reconcile_on_startup": False}}},
    ),
    (
        "exchange.execution.retry_after_ambiguous_response",
        {"exchange": {"execution": {"retry_after_ambiguous_response": True}}},
    ),
    ("backtest.held_out_burned", {"backtest": {"held_out_burned": True}}),
    (
        "backtest.cost_model.funding_enabled",
        {"backtest": {"cost_model": {"funding_enabled": False}}},
    ),
    ("risk.kill_switch_enabled", {"risk": {"kill_switch_enabled": False}}),
    ("risk.require_invalidation_level", {"risk": {"require_invalidation_level": False}}),
    (
        "agents.degrade_to_deterministic_on_quota_exhaustion",
        {"agents": {"degrade_to_deterministic_on_quota_exhaustion": False}},
    ),
    ("telemetry.log_format", {"telemetry": {"log_format": "console"}}),
    ("telemetry.order_path_sample_ratio", {"telemetry": {"order_path_sample_ratio": 0}}),
    ("telemetry.metric_prefix", {"telemetry": {"metric_prefix": "fk_"}}),
    ("bus.require_correlation_id", {"bus": {"require_correlation_id": False}}),
    ("api.host", {"api": {"host": "0.0.0.0"}}),  # noqa: S104 - the value under refusal
    ("scheduler.timezone", {"scheduler": {"timezone": "Asia/Baku"}}),
    (
        "evolution.promotion_requires_forward_outperformance",
        {"evolution": {"promotion_requires_forward_outperformance": False}},
    ),
    (
        "evolution.trial_ledger_counts_failed_runs",
        {"evolution": {"trial_ledger_counts_failed_runs": False}},
    ),
)


@pytest.mark.parametrize(
    ("field_path", "payload"),
    FIXED_PROPERTIES,
    ids=[field_path for field_path, _ in FIXED_PROPERTIES],
)
def test_a_fixed_property_cannot_be_changed_by_configuration(
    field_path: str, payload: dict[str, object]
) -> None:
    """Omitting these fields instead of fixing them would invite someone to add the
    flag. `api.host` is the sharpest one: this stack holds exchange credentials, and a
    service bound to all interfaces is exposed to every device on the local network."""
    with pytest.raises(ValidationError, match=field_path.rsplit(".", maxsplit=1)[-1]):
        Settings.model_validate(payload)


def test_reask_attempts_cannot_be_raised() -> None:
    """PROMPT_LIBRARY.md section 3. A retry loop over a stochastic generator searches
    for a response that passes validation, not one that is correct."""
    with pytest.raises(ValidationError):
        Settings.model_validate({"agents": {"max_reask_attempts": 1}})


def test_enabling_agents_without_a_provider_names_the_missing_field() -> None:
    with pytest.raises(ValidationError, match=r"agents\.primary"):
        Settings.model_validate({"agents": {"enabled": True}})


def test_enabling_the_exchange_without_credentials_names_the_missing_fields() -> None:
    with pytest.raises(ValidationError) as raised:
        Settings.model_validate({"exchange": {"enabled": True}})
    rendered = str(raised.value)
    assert "futures_api_key" in rendered
    assert "futures_api_secret" in rendered


def test_a_provider_token_budget_above_its_ceiling_is_refused() -> None:
    """An agent's token budget is a cost limit, and cost limits are the ones that get
    raised at 1am. CONFIGURATION.md section 9."""
    with pytest.raises(ValidationError, match="daily_token_budget"):
        Settings.model_validate(
            {
                "agents": {
                    "enabled": True,
                    "primary": {
                        "api_key": "k",
                        "base_url": "https://generativelanguage.googleapis.com",
                        "model": "gemini-2.5-flash",
                        "daily_token_budget": 10_000_000_000,
                        "requests_per_minute": 4,
                    },
                }
            }
        )


def test_a_plaintext_endpoint_is_refused() -> None:
    """A downgrade to http puts the API key on the wire, and a host check alone accepts
    it because the host is right."""
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {"exchange": {"binance": {"spot_rest_url": "http://testnet.binance.vision"}}}
        )
