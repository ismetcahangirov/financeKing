"""The settings tree. One model, constructed once, frozen, injected.

Three layers, highest precedence first: environment variables, then the `.env` file,
then the code defaults on the model. There is no fourth layer -- no config table, no
remote config service, no runtime override endpoint. Each of those would reintroduce
the property that CONFIGURATION.md section 1 principle 3 exists to eliminate: a running
process whose behaviour can change under it, which makes the boot log wrong for part of
the run and the audit trail unreconstructable.

Code defaults are the *safe* baseline, not a convenient one. A process started with no
`.env` and no environment variables runs with the tightest limits, testnet venues,
agents off and the smallest symbol set. A missing configuration file must never produce
a less safe system than a present one, because someone will eventually run the process
in a context where the file did not mount.

Several fields are typed `Literal[...]` with a single admissible member. That is the
pattern for a property that must not become configurable: it stays visible in the tree
and in the boot log, so its value is auditable, while `mypy --strict` and pydantic
between them reject any attempt to change it. Making it visible-but-fixed beats
omitting it, because omission invites someone to add the flag.

CONFIGURATION.md sections 2 and 5 through 12.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import UnionType
from typing import Annotated, Final, Literal, Self, Union, get_args, get_origin

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    PostgresDsn,
    RedisDsn,
    SecretStr,
    UrlConstraints,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from fking.platform.config._ceilings import AGENT_HARD_CEILINGS, HARD_CEILINGS

ENV_PREFIX: Final[str] = "FKING_"
ENV_NESTED_DELIMITER: Final[str] = "__"

# TLS only, enforced by the type rather than by review. A downgrade to plaintext puts
# the API key on the wire, and the safety kernel's host check would accept it because
# the host is right.
HttpsUrl = Annotated[AnyUrl, UrlConstraints(allowed_schemes=["https"])]
WebSocketUrl = Annotated[AnyUrl, UrlConstraints(allowed_schemes=["wss"])]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


def _assert_within_ceilings(
    model: BaseModel, ceilings: Mapping[str, Decimal | int], *, scope: str
) -> None:
    """Refuse any limit above its compiled-in ceiling, reporting every breach at once.

    Refusing rather than clamping: clamping runs the system under a limit nobody chose,
    announced only by a warning, and in an unattended process a loud warning is a quiet
    one. CONFIGURATION.md section 1 principle 2.

    Every breach is collected before raising. Fixing one and being told about the next
    on the following boot turns a misconfiguration into three restarts.
    """
    breaches = [
        f"{scope}.{name}={Decimal(str(getattr(model, name)))} exceeds the "
        f"compiled-in hard ceiling {ceiling}"
        for name, ceiling in ceilings.items()
        if Decimal(str(getattr(model, name))) > Decimal(str(ceiling))
    ]
    if breaches:
        raise ValueError(
            "; ".join(breaches)
            + ". Configuration may only make this system more conservative. Raising a "
            "ceiling requires a source edit and a pull request labelled safety:critical."
        )


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


class SymbolSpec(BaseModel):
    """One tradable instrument in the configured universe."""

    model_config = _FROZEN

    symbol: str
    market: Literal["spot", "futures_um"]


class DataSettings(BaseModel):
    """Ingestion, storage and feature-store configuration."""

    model_config = _FROZEN

    # data.binance.vision is a public archive host, deliberately absent from the
    # trading allowlist and deliberately not validated against it at boot. It is
    # reached over the separate data-host egress path (#22); putting it in the trading
    # allowlist to make one download work would widen the allowlist permanently.
    archive_base_url: HttpsUrl = AnyUrl("https://data.binance.vision")
    # Not configurable: an unverified archive is untrusted data, and a flag to skip
    # verification is a flag that gets set when a download is slow.
    verify_checksums: Literal[True] = True
    archive_download_concurrency: int = Field(default=4, ge=1, le=16)
    archive_retry_attempts: int = Field(default=2, ge=0, le=10)

    universe: tuple[SymbolSpec, ...] = (SymbolSpec(symbol="BTCUSDT", market="futures_um"),)
    bar_intervals: tuple[str, ...] = ("1m",)
    history_start: date = date(2023, 1, 1)

    parquet_root: Path = Path("data/parquet")
    parquet_compression: Literal["zstd", "snappy"] = "zstd"
    parquet_compression_level: int = Field(default=3, ge=1, le=22)
    parquet_target_file_mb: int = Field(default=256, ge=1)
    bar_partition_granularity: Literal["month"] = "month"
    trade_partition_granularity: Literal["day"] = "day"

    hypertable_chunk_interval: timedelta = timedelta(days=1)
    operational_bar_window: timedelta = timedelta(days=90)

    ws_reconnect_base_seconds: float = Field(default=1.0, gt=0)
    ws_reconnect_cap_seconds: float = Field(default=60.0, gt=0)
    kline_cadence_grace_seconds: int = Field(default=90, gt=0)
    staleness_threshold_seconds: int = Field(default=120, gt=0)
    backfill_from_rest: bool = True

    max_row_rejection_ratio: Decimal = Field(default=Decimal("0.001"), ge=0, le=1)
    max_ohlc_incoherence_ratio: Decimal = Field(default=Decimal("0.0001"), ge=0, le=1)

    feature_cache_size_mb: int = Field(default=512, ge=1)
    # Not configurable: a permissive mode is exactly where a strategy silently receives
    # an approximation of data that does not exist. DATA_PIPELINE.md section 8.
    refuse_undeclared_features: Literal[True] = True

    @model_validator(mode="after")
    def _reconnect_cap_is_not_below_the_base(self) -> Self:
        if self.ws_reconnect_cap_seconds < self.ws_reconnect_base_seconds:
            raise ValueError(
                f"ws_reconnect_cap_seconds={self.ws_reconnect_cap_seconds} is below "
                f"ws_reconnect_base_seconds={self.ws_reconnect_base_seconds}, which "
                f"makes the backoff schedule shrink with each attempt"
            )
        return self


# ---------------------------------------------------------------------------
# exchange
# ---------------------------------------------------------------------------


class BinanceSettings(BaseModel):
    """Binance testnet endpoints and credentials.

    The URL fields are configurable, and that is not a hole in the safety kernel -- it
    is why the kernel validates hosts rather than URLs. A configured URL pointing at a
    non-allowlisted host aborts startup, and even one that passes startup is
    re-validated on every request. Configuration selects *among* allowlisted venues; it
    cannot add one.
    """

    model_config = _FROZEN

    spot_rest_url: HttpsUrl = AnyUrl("https://testnet.binance.vision")
    spot_ws_url: WebSocketUrl = AnyUrl("wss://ws-api.testnet.binance.vision/ws-api/v3")
    futures_rest_url: HttpsUrl = AnyUrl("https://testnet.binancefuture.com")
    futures_ws_url: WebSocketUrl = AnyUrl("wss://stream.binancefuture.com/ws")

    spot_api_key: SecretStr | None = None
    # A path, not the key material. Environment variables are inherited by child
    # processes, appear in `docker inspect` and in /proc/<pid>/environ, and are
    # trivially dumped by anything running in the process. A file has permissions,
    # and those permissions are checked at boot. SECURITY.md section 4.5.
    spot_ed25519_key_path: Path | None = None
    futures_api_key: SecretStr | None = None
    futures_api_secret: SecretStr | None = None

    recv_window_ms: int = Field(default=5000, gt=0, le=60_000)
    request_timeout_seconds: float = Field(default=10.0, gt=0)
    # 0.5 means the system plans to use at most half the venue's stated budget in
    # steady state. A rate-limit hit in normal operation is an architectural finding,
    # not something to back off from.
    rate_limit_headroom_ratio: Decimal = Field(default=Decimal("0.5"), gt=0, le=1)


class BybitSettings(BaseModel):
    """Fallback venue. ARCHITECTURE.md section 7."""

    model_config = _FROZEN

    rest_url: HttpsUrl = AnyUrl("https://api-testnet.bybit.com")
    ws_url: WebSocketUrl = AnyUrl("wss://stream-testnet.bybit.com/v5/public/linear")
    api_key: SecretStr | None = None
    api_secret: SecretStr | None = None
    request_timeout_seconds: float = Field(default=10.0, gt=0)


class FeeSettings(BaseModel):
    """Binance VIP-0 *production* rates. Assuming a better tier manufactures edge."""

    model_config = _FROZEN

    spot_maker_bp: Decimal = Field(default=Decimal("10.0"), ge=0)
    spot_taker_bp: Decimal = Field(default=Decimal("10.0"), ge=0)
    futures_maker_bp: Decimal = Field(default=Decimal("2.0"), ge=0)
    futures_taker_bp: Decimal = Field(default=Decimal("5.0"), ge=0)
    bnb_discount_applied: bool = False


class ExecutionSettings(BaseModel):
    """Order working style and reconciliation cadence."""

    model_config = _FROZEN

    default_urgency: Literal["passive", "normal"] = "normal"
    max_order_duration_seconds: int = Field(default=300, gt=0)
    reconcile_on_startup: Literal[True] = True
    reconcile_interval_seconds: int = Field(default=60, gt=0)
    max_reconciliation_age_seconds: int = Field(default=120, gt=0)
    # A timeout is not a rejection. Retrying a possibly-filled order is how a system
    # doubles a position; reconciliation is the only correct response.
    retry_after_ambiguous_response: Literal[False] = False
    client_order_id_prefix: str = Field(default="fk", min_length=1, max_length=8)


class ExchangeSettings(BaseModel):
    """Venues and credentials.

    `enabled` defaults to False so a fresh clone with no credentials boots and every
    offline path -- backtest, validation, the whole evolution loop -- runs. The moment
    it is switched on, the credentials the venue adapters need become mandatory, and
    the failure names the exact missing field rather than surfacing as a 401 later.
    """

    model_config = _FROZEN

    enabled: bool = False
    binance: BinanceSettings = Field(default_factory=BinanceSettings)
    bybit: BybitSettings | None = None
    fees: FeeSettings = Field(default_factory=FeeSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)

    @model_validator(mode="after")
    def _credentials_present_when_enabled(self) -> Self:
        if not self.enabled:
            return self
        missing = [
            f"exchange.binance.{name}"
            for name in ("futures_api_key", "futures_api_secret")
            if getattr(self.binance, name) is None
        ]
        if missing:
            raise ValueError(
                f"exchange.enabled is true but {', '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} unset; a missing exchange "
                f"credential is a startup failure, not an empty string that fails "
                f"later with a confusing 401"
            )
        return self


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------


class CostModelSettings(BaseModel):
    """Cost model provenance and parameters. Calibrated on production data only."""

    model_config = _FROZEN

    version: str = "2026-05-production-v3"
    # validate_default=True: a field_validator runs only on supplied values by default,
    # which would leave the shipped default unchecked -- and the default is exactly the
    # value that gets edited during a calibration refresh.
    calibration_source: str = Field(
        default="binance_um_production_2026-03..2026-05", validate_default=True
    )
    calibration_method: str = "bookTicker p50/p99 by hour-of-day; sqrt impact prior"
    calibrated_at: date = date(2026, 5, 31)

    spread_profile: Literal["p50", "p99"] = "p50"
    impact_coefficient: Decimal = Field(default=Decimal("0.35"), ge=0)
    impact_exponent: Decimal = Field(default=Decimal("0.5"), gt=0)  # square-root prior
    passive_markout_bp: Decimal = Field(default=Decimal("3.2"), ge=0)
    latency_ack_ms: int = Field(default=180, ge=0)
    latency_fill_ms: int = Field(default=95, ge=0)
    funding_enabled: Literal[True] = True
    reject_orders_exceeding_band_depth: Literal[True] = True

    @field_validator("calibration_source")
    @classmethod
    def _not_calibrated_on_testnet(cls, source: str) -> str:
        # Measured: futures testnet shows a 7.5bp spread against production's 0.16bp
        # and roughly 10x inflated volume. A model built from that looks conservative
        # and is fiction.
        if "testnet" in source.lower():
            raise ValueError(
                f"calibration_source={source!r} names a testnet; cost model parameters "
                f"are calibrated from production market data only"
            )
        return source


class BacktestSettings(BaseModel):
    """Engine parameters and the credibility gates."""

    model_config = _FROZEN

    initial_equity_usd: Decimal = Field(default=Decimal("10000"), gt=0)
    warmup_bars: int = Field(default=500, ge=0)
    min_credible_trade_count: int = Field(default=200, ge=1)
    min_trades_per_cpcv_fold: int = Field(default=30, ge=1)
    min_edge_to_cost_ratio: Decimal = Field(default=Decimal("2.0"), gt=0)
    max_pbo: Decimal = Field(default=Decimal("0.30"), ge=0, le=1)

    cpcv_groups: int = Field(default=8, ge=2)
    cpcv_test_group_size: int = Field(default=2, ge=1)
    monte_carlo_paths: int = Field(default=1000, ge=1)
    parameter_perturbation_ratio: Decimal = Field(default=Decimal("0.10"), gt=0, le=1)

    run_seed: int = 20260101
    # Mandatory rather than optional: DuckDB takes most of available memory by default
    # and is the single most likely thing to trigger an OOM kill on Postgres, which the
    # kernel selects by score rather than by importance. DEPLOYMENT.md section 4.
    duckdb_memory_limit_gb: Decimal = Field(default=Decimal("3"), gt=0)
    duckdb_threads: int = Field(default=4, ge=1)

    held_out_start: date = date(2026, 6, 1)
    held_out_end: date = date(2026, 8, 1)
    # Burning the held-out period requires a source edit, not an environment variable --
    # the same friction pattern as the allowlist, for the same reason. It is burned on
    # read, and burning it is a decision taken once, by the user.
    held_out_burned: Literal[False] = False

    cost_model: CostModelSettings = Field(default_factory=CostModelSettings)

    @model_validator(mode="after")
    def _cpcv_split_is_constructible(self) -> Self:
        if self.cpcv_test_group_size >= self.cpcv_groups:
            raise ValueError(
                f"cpcv_test_group_size={self.cpcv_test_group_size} leaves no training "
                f"groups out of cpcv_groups={self.cpcv_groups}"
            )
        return self

    @model_validator(mode="after")
    def _held_out_window_is_ordered(self) -> Self:
        if self.held_out_end <= self.held_out_start:
            raise ValueError(
                f"held_out_end={self.held_out_end} is not after "
                f"held_out_start={self.held_out_start}"
            )
        return self


# ---------------------------------------------------------------------------
# risk
# ---------------------------------------------------------------------------


class RiskSettings(BaseModel):
    """Limits, bounded above by `HARD_CEILINGS`.

    Tightening any of these is free. Loosening one past its ceiling is impossible from
    configuration; it requires a source edit and a `safety:critical` pull request.
    """

    model_config = _FROZEN

    max_portfolio_notional_usd: Decimal = Field(default=Decimal("25000"), gt=0)
    max_position_notional_usd: Decimal = Field(default=Decimal("5000"), gt=0)
    max_leverage: Decimal = Field(default=Decimal("2"), gt=0)
    max_daily_drawdown_ratio: Decimal = Field(default=Decimal("0.03"), gt=0, le=1)
    max_total_drawdown_ratio: Decimal = Field(default=Decimal("0.10"), gt=0, le=1)
    max_open_positions: int = Field(default=5, ge=1)
    max_orders_per_minute: int = Field(default=10, ge=1)
    max_single_order_notional_usd: Decimal = Field(default=Decimal("2000"), gt=0)
    max_correlated_exposure_ratio: Decimal = Field(default=Decimal("0.25"), gt=0, le=1)

    # CLAUDE.md section 11 names the anti-pattern: adding a config flag to bypass a
    # gate. Gates exist because someone will be in a hurry later, and that someone is
    # you.
    kill_switch_enabled: Literal[True] = True
    kill_switch_daily_loss_ratio: Decimal = Field(default=Decimal("0.03"), gt=0, le=1)
    conviction_floor: Decimal = Field(default=Decimal("0.15"), ge=0, le=1)
    require_invalidation_level: Literal[True] = True

    @model_validator(mode="after")
    def _within_ceilings(self) -> Self:
        _assert_within_ceilings(self, HARD_CEILINGS, scope="risk")
        return self

    @model_validator(mode="after")
    def _single_order_fits_inside_a_position(self) -> Self:
        if self.max_single_order_notional_usd > self.max_position_notional_usd:
            raise ValueError(
                f"max_single_order_notional_usd={self.max_single_order_notional_usd} "
                f"exceeds max_position_notional_usd={self.max_position_notional_usd}, "
                f"so one order could open a position the position limit forbids"
            )
        return self

    @model_validator(mode="after")
    def _daily_drawdown_is_not_above_total(self) -> Self:
        if self.max_daily_drawdown_ratio > self.max_total_drawdown_ratio:
            raise ValueError(
                f"max_daily_drawdown_ratio={self.max_daily_drawdown_ratio} exceeds "
                f"max_total_drawdown_ratio={self.max_total_drawdown_ratio}, which makes "
                f"the total limit unreachable and therefore decorative"
            )
        return self


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------


class ProviderSettings(BaseModel):
    """One LLM provider. Budgets are bounded by `AGENT_HARD_CEILINGS`."""

    model_config = _FROZEN

    api_key: SecretStr
    base_url: HttpsUrl
    # A pinned model id, never a floating alias: a provider silently rolling
    # `*-latest` forward changes every result in the project with no diff, and the
    # reconstructability requirement then cannot be met for anything before the roll.
    model: str
    daily_token_budget: int = Field(gt=0)
    requests_per_minute: int = Field(gt=0)
    timeout_seconds: float = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def _within_ceilings(self) -> Self:
        bounded = ("daily_token_budget", "requests_per_minute")
        _assert_within_ceilings(
            self,
            {name: AGENT_HARD_CEILINGS[name] for name in bounded},
            scope="agents.provider",
        )
        return self


class AgentBudget(BaseModel):
    """Per-agent budget, timeout and invocation ceiling.

    One record per agent rather than three parallel mappings keyed by agent id: three
    mappings can disagree about which agents exist, and the disagreement is silent.
    """

    model_config = _FROZEN

    agent_id: str
    token_budget: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    daily_invocations: int = Field(gt=0)

    @model_validator(mode="after")
    def _within_ceilings(self) -> Self:
        _assert_within_ceilings(
            self,
            {"daily_invocations": AGENT_HARD_CEILINGS["daily_invocations"]},
            scope=f"agents.budget[{self.agent_id}]",
        )
        return self


class AgentSettings(BaseModel):
    """LLM providers, quotas and per-agent budgets.

    `enabled` is False by default. The system must be fully functional with every agent
    disabled -- that is the degraded mode, and a degraded mode that has never been the
    default is a degraded mode that does not work.
    """

    model_config = _FROZEN

    enabled: bool = False
    primary: ProviderSettings | None = None
    fallback: ProviderSettings | None = None
    prompt_root: Path = Path("prompts")
    require_prompt_hash_match: Literal[True] = True
    # An unparseable response is a failure, not something to retry with "please output
    # valid JSON". A retry loop over a stochastic generator searches for a response
    # that passes validation, not one that is correct -- and it spends the quota the
    # golden set needs. PROMPT_LIBRARY.md section 3.
    max_reask_attempts: Literal[0] = 0
    # Fixed True because the alternative -- stalling -- is worse in an unattended
    # system. ARCHITECTURE.md section 9.
    degrade_to_deterministic_on_quota_exhaustion: Literal[True] = True
    budgets: tuple[AgentBudget, ...] = ()

    @model_validator(mode="after")
    def _provider_present_when_enabled(self) -> Self:
        if self.enabled and self.primary is None:
            raise ValueError(
                "agents.enabled is true but agents.primary is unset; an enabled agent "
                "runtime with no provider fails on its first invocation rather than at "
                "boot"
            )
        return self

    @model_validator(mode="after")
    def _budgets_name_distinct_agents(self) -> Self:
        seen = [budget.agent_id for budget in self.budgets]
        duplicates = sorted({name for name in seen if seen.count(name) > 1})
        if duplicates:
            raise ValueError(
                f"agents.budgets declares {duplicates} more than once; which record "
                f"wins would depend on iteration order"
            )
        return self


# ---------------------------------------------------------------------------
# telemetry
# ---------------------------------------------------------------------------


class TelemetrySettings(BaseModel):
    """Logging, metrics and tracing."""

    model_config = _FROZEN

    service_name: str = "fking"
    environment: Literal["local", "ci", "demo"] = "local"
    # Set by Compose or CI so a boot record identifies the code that produced it.
    # Read here rather than shelled out for at boot, because nothing outside this tree
    # reads the process environment.
    git_sha: str | None = None

    otlp_endpoint: str = "http://otel-collector:4317"
    otlp_timeout_seconds: float = Field(default=10.0, gt=0)

    log_level: Literal["debug", "info", "warning", "error"] = "info"
    # json in every environment, including local: no environment should run a
    # serialisation path whose parsing is untested.
    log_format: Literal["json"] = "json"
    # The log field registry. OBSERVABILITY.md section 7: redaction is allowlist-based,
    # so a field absent from this tuple is dropped by the pipeline and counted on
    # `fking_platform_log_fields_dropped_total` rather than emitted. A denylist would
    # expose every new field until somebody remembered to add it, and nobody remembers
    # during an incident -- which is exactly when new fields get added.
    #
    # Adding a field is therefore a deliberate, reviewed act, and because this value is
    # serialised into the config hash, which fields a historical record could have
    # carried is itself reconstructable.
    #
    # An ordered tuple rather than the frozenset CONFIGURATION.md section 10 shows,
    # because a set's iteration order varies with PYTHONHASHSEED -- which would make the
    # config hash differ between two processes holding identical configuration.
    log_field_allowlist: tuple[str, ...] = (
        # Mandatory on every record.
        "correlation_id",
        "environment",
        "level",
        "logger",
        "message",
        "service",
        "span_id",
        "timestamp",
        "trace_id",
        "version",
        # Mandatory when applicable.
        "audit_ref",
        "base_quantity",
        "binding_limit",
        "causation_id",
        "client_order_id",
        "exception",
        "notional_usd",
        "order_id",
        "outcome",
        "reason",
        "strategy_id",
        "strategy_version",
        "symbol",
        "venue",
        # Boot record. CONFIGURATION.md section 4 requires the full effective config, and
        # it is safe to emit only because every credential in it is already redacted by
        # type before the record is built.
        "allowed_hosts",
        "config",
        "config_hash",
        "git_sha",
        "key_path",
        "venue_endpoints",
        # Event bus.
        "consumer_group",
        "dlq_stream",
        "event_id",
        "event_type",
        "idempotency_key",
        "message_id",
        "reclaimed_count",
        "schema_version",
        "stream",
    )

    trace_sample_ratio: Decimal = Field(default=Decimal("0.10"), ge=0, le=1)
    # Never sampled: a sampled order path means some trades are unreconstructable, and
    # you do not learn which until you need one. OBSERVABILITY.md section 5.
    order_path_sample_ratio: Literal[1] = 1

    # Fixed, because renaming metrics breaks every dashboard and alert simultaneously
    # and silently.
    metric_prefix: Literal["fking_"] = "fking_"
    max_metric_series: int = Field(default=20_000, ge=1)


# ---------------------------------------------------------------------------
# platform
# ---------------------------------------------------------------------------


class DatabaseSettings(BaseModel):
    """Postgres/TimescaleDB connection and roles."""

    model_config = _FROZEN

    dsn: PostgresDsn = PostgresDsn("postgresql+asyncpg://fking_app@127.0.0.1:5432/fking")
    pool_min_size: int = Field(default=2, ge=0)
    # Raising this is not a fix for connection exhaustion: each connection carries
    # work_mem, so it converts a connection problem into a memory problem, and the
    # memory problem manifests as an OOM kill on Postgres.
    pool_max_size: int = Field(default=10, ge=1)
    statement_timeout_seconds: int = Field(default=30, gt=0)
    migration_role: str = "fking_migrator"
    # Holds no UPDATE or DELETE on audit tables. .claude/rules/append-only-audit.md.
    application_role: str = "fking_app"

    @model_validator(mode="after")
    def _pool_bounds_are_ordered(self) -> Self:
        if self.pool_min_size > self.pool_max_size:
            raise ValueError(
                f"pool_min_size={self.pool_min_size} exceeds pool_max_size={self.pool_max_size}"
            )
        return self


class BusSettings(BaseModel):
    """Redis Streams event bus."""

    model_config = _FROZEN

    redis_url: RedisDsn = RedisDsn("redis://127.0.0.1:6379/0")
    consumer_group_prefix: str = "fking"
    max_stream_length: int = Field(default=1_000_000, ge=1)
    claim_idle_ms: int = Field(default=30_000, gt=0)
    dlq_stream_suffix: str = ".dlq"
    # A missing correlation id sends the message to the dead-letter queue. It is never
    # invented: a minted id ties the log lines from one function call together and
    # nothing else, which is exactly the illusion of reconstructability.
    require_correlation_id: Literal[True] = True


class ApiSettings(BaseModel):
    """FastAPI surface. Localhost only."""

    model_config = _FROZEN

    # Never 0.0.0.0. This stack holds exchange credentials, and a service bound to all
    # interfaces is exposed to every device on the local network.
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8000, ge=1024, le=65_535)
    cors_origins: tuple[str, ...] = ("http://127.0.0.1:3000",)


class SchedulerSettings(BaseModel):
    """APScheduler configuration."""

    model_config = _FROZEN

    # Every datetime in this system is tz-aware UTC. A scheduler in a local timezone
    # reintroduces DST into a market with no session boundary to make the error obvious.
    timezone: Literal["UTC"] = "UTC"
    max_concurrent_jobs: int = Field(default=4, ge=1)
    misfire_grace_seconds: int = Field(default=60, ge=0)


# ---------------------------------------------------------------------------
# evolution
# ---------------------------------------------------------------------------


class EvolutionSettings(BaseModel):
    """Population lifecycle and promotion gates."""

    model_config = _FROZEN

    population_size: int = Field(default=20, ge=1)
    max_population_size: int = Field(default=50, ge=1)
    generation_interval: timedelta = timedelta(days=7)
    challenger_slots: int = Field(default=3, ge=0)

    min_forward_days_before_promotion: int = Field(default=30, ge=1)
    min_forward_trades_before_promotion: int = Field(default=100, ge=1)
    # Validation performance is the thing that was selected on; promoting from it is
    # circular.
    promotion_requires_forward_outperformance: Literal[True] = True

    # A trial count that only counts successes is a trial count designed to flatter,
    # and it feeds directly into the deflated Sharpe. Configurable and set to False,
    # this one line would silently invalidate every statistical defence in the system.
    trial_ledger_counts_failed_runs: Literal[True] = True
    retire_below_survival_score: Decimal = Field(default=Decimal("0.30"), ge=0, le=1)
    max_generations_without_promotion: int = Field(default=8, ge=1)

    @model_validator(mode="after")
    def _population_fits_its_ceiling(self) -> Self:
        if self.population_size > self.max_population_size:
            raise ValueError(
                f"population_size={self.population_size} exceeds "
                f"max_population_size={self.max_population_size}"
            )
        return self


# ---------------------------------------------------------------------------
# root
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """The whole configuration tree, constructed once at boot and never again.

    Frozen, so a component that was handed this object holds the values it was built
    with for the life of the process. `OBSERVABILITY.md` section 1 requires that a
    historical decision be reconstructable; if a component could pick up a new limit
    mid-run, the audit row saying "vetoed at max_notional" would no longer identify
    *which* value of `max_notional`, and the boot log that recorded it would be wrong
    for part of the run.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter=ENV_NESTED_DELIMITER,
        frozen=True,
        # An ignored key is a setting somebody believes is in effect. The FKING_ prefix
        # is what makes this safe to switch on: an unrelated variable inherited from
        # the shell cannot bind here, so forbidding extras rejects typos rather than
        # the environment.
        extra="forbid",
        # No env_file here on purpose. load_settings() passes the path explicitly, so
        # a caller can ask for "defaults and environment only" without the model
        # silently reaching for a .env that happens to be in the working directory.
        env_file=None,
    )

    data: DataSettings = Field(default_factory=DataSettings)
    exchange: ExchangeSettings = Field(default_factory=ExchangeSettings)
    backtest: BacktestSettings = Field(default_factory=BacktestSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    agents: AgentSettings = Field(default_factory=AgentSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    bus: BusSettings = Field(default_factory=BusSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    evolution: EvolutionSettings = Field(default_factory=EvolutionSettings)


def _nested_model(annotation: object) -> type[BaseModel] | None:
    """The sub-model behind a field annotation, or None for a leaf.

    `tuple[SymbolSpec, ...]` is deliberately a leaf: a collection of models binds from
    one JSON-encoded environment variable, so recursing into it would invent variable
    names that pydantic-settings never reads.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if get_origin(annotation) in (Union, UnionType):
        for member in get_args(annotation):
            if isinstance(member, type) and issubclass(member, BaseModel):
                return member
    return None


def environment_variable_names(
    model: type[BaseModel] = Settings, *, prefix: str = ENV_PREFIX
) -> tuple[str, ...]:
    """Every environment variable this tree reads, in the documented `FKING_A__B` shape.

    Derived from the model rather than maintained by hand, so `.env.example` can be
    checked against reality in both directions and cannot drift.
    """
    names: list[str] = []
    for field_name, field in model.model_fields.items():
        segment = f"{prefix}{field_name.upper()}"
        nested = _nested_model(field.annotation)
        if nested is None:
            names.append(segment)
        else:
            names.extend(
                environment_variable_names(nested, prefix=f"{segment}{ENV_NESTED_DELIMITER}")
            )
    return tuple(sorted(names))
