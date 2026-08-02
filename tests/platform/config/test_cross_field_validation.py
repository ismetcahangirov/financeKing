"""Cross-field rules -- the validation class that catches a *combination* nobody chose.

Each of these is individually valid and jointly incoherent. They are worth refusing at
boot rather than discovering later, because every one of them produces a system that
runs and behaves in a way its operator did not ask for: a backoff schedule that shrinks,
a total drawdown limit that can never be reached, an order limit that can breach the
position limit it sits under.

CONFIGURATION.md section 3, validation class "cross-field".
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fking.platform.config import AgentBudget, ProviderSettings, Settings

pytestmark = pytest.mark.unit

VALID_PROVIDER = {
    "api_key": "not-a-real-key",
    "base_url": "https://generativelanguage.googleapis.com",
    "model": "gemini-2.5-flash-002",
    "daily_token_budget": 800_000,
    "requests_per_minute": 4,
}


def test_a_backoff_cap_below_its_base_is_refused() -> None:
    """A cap under the base makes each reconnect attempt wait *less* than the last,
    which is a retry storm wearing a backoff's name."""
    with pytest.raises(ValidationError, match="ws_reconnect_cap_seconds"):
        Settings.model_validate(
            {"data": {"ws_reconnect_base_seconds": 10.0, "ws_reconnect_cap_seconds": 1.0}}
        )


def test_a_cpcv_split_with_no_training_groups_is_refused() -> None:
    with pytest.raises(ValidationError, match="cpcv_test_group_size"):
        Settings.model_validate({"backtest": {"cpcv_groups": 4, "cpcv_test_group_size": 4}})


def test_a_held_out_window_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(ValidationError, match="held_out_end"):
        Settings.model_validate(
            {"backtest": {"held_out_start": "2026-08-01", "held_out_end": "2026-06-01"}}
        )


def test_an_order_limit_above_the_position_limit_is_refused() -> None:
    """Both are inside their ceilings. Together they let one order open a position the
    position limit forbids, which makes the position limit advisory."""
    with pytest.raises(ValidationError, match="max_single_order_notional_usd"):
        Settings.model_validate(
            {
                "risk": {
                    "max_position_notional_usd": "1000",
                    "max_single_order_notional_usd": "2000",
                }
            }
        )


def test_a_daily_drawdown_limit_above_the_total_is_refused() -> None:
    """The total limit becomes unreachable, and an unreachable limit is decorative."""
    with pytest.raises(ValidationError, match="max_daily_drawdown_ratio"):
        Settings.model_validate(
            {"risk": {"max_daily_drawdown_ratio": "0.04", "max_total_drawdown_ratio": "0.02"}}
        )


def test_a_connection_pool_whose_minimum_exceeds_its_maximum_is_refused() -> None:
    with pytest.raises(ValidationError, match="pool_min_size"):
        Settings.model_validate({"database": {"pool_min_size": 20, "pool_max_size": 5}})


def test_a_population_above_its_own_maximum_is_refused() -> None:
    with pytest.raises(ValidationError, match="population_size"):
        Settings.model_validate({"evolution": {"population_size": 60, "max_population_size": 50}})


def test_a_provider_inside_its_ceilings_is_accepted() -> None:
    provider = ProviderSettings.model_validate(VALID_PROVIDER)
    assert provider.daily_token_budget == VALID_PROVIDER["daily_token_budget"]
    assert provider.api_key.get_secret_value() == VALID_PROVIDER["api_key"]


def test_a_request_rate_above_its_ceiling_is_refused() -> None:
    with pytest.raises(ValidationError, match="requests_per_minute"):
        ProviderSettings.model_validate({**VALID_PROVIDER, "requests_per_minute": 10_000})


def test_an_agent_budget_inside_its_ceiling_is_accepted() -> None:
    budget = AgentBudget.model_validate(
        {
            "agent_id": "quant",
            "token_budget": 45_000,
            "timeout_seconds": 900.0,
            "daily_invocations": 5,
        }
    )
    assert budget.agent_id == "quant"


def test_an_agent_invocation_ceiling_cannot_be_exceeded() -> None:
    """An invocation ceiling is a cost limit, and cost limits get raised at 1am."""
    with pytest.raises(ValidationError, match="daily_invocations"):
        AgentBudget.model_validate(
            {
                "agent_id": "quant",
                "token_budget": 45_000,
                "timeout_seconds": 900.0,
                "daily_invocations": 5_000,
            }
        )


def test_two_budgets_for_the_same_agent_are_refused() -> None:
    """Which record wins would depend on iteration order, and the loser would be a
    budget somebody believes is in force."""
    budget = {
        "agent_id": "quant",
        "token_budget": 45_000,
        "timeout_seconds": 900.0,
        "daily_invocations": 5,
    }
    with pytest.raises(ValidationError, match="quant"):
        Settings.model_validate({"agents": {"budgets": [budget, budget]}})


def test_an_enabled_agent_runtime_with_a_provider_is_accepted() -> None:
    settings = Settings.model_validate(
        {"agents": {"enabled": True, "primary": VALID_PROVIDER, "fallback": VALID_PROVIDER}}
    )
    assert settings.agents.primary is not None
    assert settings.agents.fallback is not None


def test_the_shipped_cost_model_default_is_itself_validated() -> None:
    """`validate_default=True` on calibration_source: a validator that only runs on
    supplied values leaves the default unchecked, and the default is the value most
    likely to be edited without a test."""
    assert "testnet" not in Settings().backtest.cost_model.calibration_source
