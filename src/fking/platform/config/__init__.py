"""Typed configuration. Knows how a value is parsed, validated and reported.

Mechanism, not policy: this package knows that `max_leverage` is a `Decimal` bounded
above by a compiled-in ceiling. It has no opinion about what leverage is for. That is
what keeps it importable from every layer without smuggling a decision across a
boundary (docs/rules/module-boundaries.md).

Four properties carry everything else:

- **The process refuses to start on invalid configuration.** Not "logs an error and
  continues with defaults" -- a typo that falls back to a default is a system operating
  under a limit nobody chose, and it will not announce itself.
- **Configuration is read once, at startup, and never at call time.** Settings are
  injected as constructor arguments. There is no hot reload, because a hot reload makes
  the boot log wrong for part of the run.
- **The full effective config is logged at boot, redacted by type.** Every credential is
  `SecretStr`, so a credential added tomorrow is redacted tomorrow.
- **Risk limits are configuration bounded in both directions by compiled-in constants.**
  Ceilings above, floors below. Configuration can only make this system more
  conservative.

Everything not listed in `__all__` is private and may change without notice.

CONFIGURATION.md, SECURITY.md section 4.
"""

from fking.platform.config._ceilings import (
    AGENT_HARD_CEILINGS,
    HARD_CEILINGS,
    HARD_FLOORS,
)
from fking.platform.config._errors import EX_CONFIG, ConfigError
from fking.platform.config.boot import (
    DEFAULT_ENV_FILE,
    JsonValue,
    bootstrap,
    config_hash,
    effective_config,
    load_settings,
    venue_endpoints,
)
from fking.platform.config.settings import (
    AgentBudget,
    AgentSettings,
    ApiSettings,
    BacktestSettings,
    BinanceSettings,
    BusSettings,
    BybitSettings,
    CostModelSettings,
    DatabaseSettings,
    DataSettings,
    EvolutionSettings,
    ExchangeSettings,
    ExecutionSettings,
    FeeSettings,
    ProviderSettings,
    RiskSettings,
    SchedulerSettings,
    Settings,
    SymbolSpec,
    TelemetrySettings,
    environment_variable_names,
)

__all__ = [
    "AGENT_HARD_CEILINGS",
    "DEFAULT_ENV_FILE",
    "EX_CONFIG",
    "HARD_CEILINGS",
    "HARD_FLOORS",
    "AgentBudget",
    "AgentSettings",
    "ApiSettings",
    "BacktestSettings",
    "BinanceSettings",
    "BusSettings",
    "BybitSettings",
    "ConfigError",
    "CostModelSettings",
    "DataSettings",
    "DatabaseSettings",
    "EvolutionSettings",
    "ExchangeSettings",
    "ExecutionSettings",
    "FeeSettings",
    "JsonValue",
    "ProviderSettings",
    "RiskSettings",
    "SchedulerSettings",
    "Settings",
    "SymbolSpec",
    "TelemetrySettings",
    "bootstrap",
    "config_hash",
    "effective_config",
    "environment_variable_names",
    "load_settings",
    "venue_endpoints",
]
