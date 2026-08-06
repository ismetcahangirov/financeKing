# Configuration

The `pydantic-settings` tree, how it layers, when it is validated, and the one rule that makes configuration safe in a system that trades.

---

## 1. Principles

| # | Principle | Consequence |
|---|---|---|
| 1 | **Configuration is data, and data is validated at the boundary.** | One `Settings` tree, constructed once, `mypy --strict` clean |
| 2 | **The process refuses to start on invalid config.** | No degraded start, no defaults papering over a typo |
| 3 | **Config is read once, at startup, and never at call time.** | A running process's behaviour cannot change under it |
| 4 | **The full effective config is logged at boot, with secrets redacted.** | "What was this process configured to do on the day of that incident" is answerable from the log |
| 5 | **Risk limits are configuration bounded by compiled-in hard ceilings.** | Config can only make the system *more* conservative |
| 6 | **The safety kernel is not configuration at all.** | `SECURITY.md` §3 — the allowlist is a compiled-in `frozenset` |

### On principle 3

> **No component reads configuration at call time. Settings are injected as constructor arguments, and a running component holds the values it was built with.**

Three reasons, in increasing order of importance:

- **Testability.** A component that takes its limits as constructor arguments is testable without an environment. A component that reads a global settings singleton requires the test to mutate process state, which makes tests order-dependent.
- **Purity.** `CLAUDE.md` §4 makes purity mandatory in `strategy` and `risk`. Reading configuration is I/O. A risk function that reads a limit from a global is not a pure function of its inputs and is not deterministically replayable.
- **Reconstruction.** `OBSERVABILITY.md` §1 requires that any historical decision be reconstructable. If a component can pick up a new limit mid-run, the audit row saying "vetoed at max_notional" no longer identifies *which* value of `max_notional` — and the boot log, which recorded the value, is now wrong for part of the run.

There is no hot reload. Changing configuration means restarting the process, which produces a new boot log with a new effective config, which is exactly the record that makes the change auditable. This is a deliberate trade of convenience for a property.

---

## 2. Layering and precedence

Three layers. Highest precedence wins.

```
   3. Environment variables            ← highest; what Compose and CI set
   2. .env file                        ← the developer's local configuration
   1. Code defaults on the model       ← lowest; the safe, documented baseline
```

There is no fourth layer. Specifically **there is no config file in a database, no remote config service, and no runtime override endpoint.** Each of those would reintroduce the property that principle 3 exists to eliminate.

### Environment variable naming

`pydantic-settings` with `env_nested_delimiter="__"` and a `FKING_` prefix:

```
FKING_RISK__MAX_PORTFOLIO_NOTIONAL_USD=25000
FKING_DATA__PARQUET_ROOT=/data/parquet
FKING_EXCHANGE__BINANCE__FUTURES_API_KEY=...
FKING_AGENTS__GEMINI__DAILY_TOKEN_BUDGET=800000
```

The `FKING_` prefix means an unprefixed environment variable from another project cannot bind to a setting here. That matters more than it sounds: `SECURITY.md` §2 vector 2 is a copied environment file, and a prefix makes most of that copying inert.

### Code defaults are the safe baseline

Every default is chosen so that a process started with **no `.env` and no environment variables** runs in the most conservative configuration that is still functional. Concretely: smallest position sizes, tightest limits, testnet venues, agents disabled, ingestion limited to the smallest symbol set.

A missing configuration file must never produce a *less* safe system than a present one. If it does, someone will eventually run the process in a context where the file did not mount.

### What has no default

Secrets have no default and no placeholder value. A missing exchange key is a startup failure, not an empty string that fails later with a confusing 401.

That requirement and "a fresh clone with no credentials runs" are both true because the subsystems that need credentials are **off by default and mandatory once on**: `exchange.enabled` and `agents.enabled` are `False` on the shipped defaults, and switching either to `True` makes its credentials required, with the failure naming the exact missing field. A credential is never an empty string, and the offline half of the system — backtest, validation, the whole evolution loop — runs with neither.

### Where the tree lives

`src/fking/platform/config/`. The root `Settings` is the only `BaseSettings`; every section below it is a frozen `BaseModel`, which is what makes `FKING_EXCHANGE__BINANCE__FUTURES_API_KEY` resolve through one nested-delimiter pass rather than through each sub-model reading the environment on its own.

`.env.example` is generated from that tree and checked against it in both directions by `tests/platform/config/test_env_example.py` — a new setting with no entry fails, and an entry for a setting that no longer exists fails. Every value in it is blank rather than a plausible-looking placeholder: a placeholder shaped like a credential is a credential somebody pastes over with a real one and then commits.

---

## 3. Validation at startup

The `Settings` tree is constructed exactly once, in `fking.platform.config.load_settings()`, called from the process entrypoint before any component is constructed.

### The ordered startup sequence

```
1.  Construct Settings  (pydantic field + model validators)      → exit 78 on failure
2.  Resolve every configured venue endpoint                      → exit 78 on failure
3.  Validate every host against ALLOWED_HOSTS                    → SafetyViolation; see below
4.  Assert every risk limit is within its compiled-in ceiling    → folded into step 1
5.  Verify secret file permissions (Ed25519 key mode <= 0600)    → exit 78 on failure
6.  Log the allowlist and every resolved endpoint
7.  Log the full effective config, redacted
8.  Construct components, injecting settings
9.  Start
```

Steps 1 and 5 are `load_settings()` and `bootstrap()` in `fking.platform.config`; steps 2, 3, 6 and 7 are `bootstrap()`. Exit code **78** (`EX_CONFIG`), so a supervisor can distinguish a configuration error from a crash and decline to restart-loop on it.

Two of those lines are not what a first reading suggests, and both differences make the guarantee stronger rather than weaker:

**Step 4 happens during step 1.** The ceiling check is a `model_validator` on `RiskSettings`, so a `Settings` object holding an out-of-bounds limit cannot be constructed at all. No code path can obtain one — including a test fixture, which is the path a separate post-construction check would leave open.

**Step 3 does not exit 78.** It delegates to `fking.platform.safety.verify_endpoints_or_abort`, which raises `SafetyViolation` — a `BaseException` that `tools/checks/no_catch_safety.py` forbids catching anywhere in `src/` or `tests/`. A non-allowlisted endpoint therefore terminates the process on an uncaught exception rather than exiting tidily. That is deliberate: a process whose model of what it is talking to is wrong should die loudly, and the kernel outranks this document's exit-code convention.

The **archive host and the LLM provider base URLs are deliberately not in step 2.** They are not venues and are not in the trading allowlist; adding them so one download or one completion works would widen the allowlist permanently, which `.claude/rules/safety-kernel.md` refuses. Those paths get their own egress checks.

### The process refuses to start

Not "logs an error and continues with defaults". Not "warns and disables the affected subsystem". **Exits.**

A trading system that continues after an unexpected state is more dangerous than one that stops (`CLAUDE.md` §4). Configuration is the highest-leverage unexpected state available: a typo in `max_portfolio_notional_usd` that falls back to a default is a system operating under a limit nobody chose, and it will not announce itself.

### Validation classes

| Class | Example |
|---|---|
| **Type** | `max_notional_usd` must parse as `Decimal` |
| **Range** | `conviction_floor` ∈ `[0, 1]`; every timeout > 0 |
| **Enum** | `venue` ∈ `{binance_spot_testnet, binance_futures_testnet, bybit_testnet}` |
| **Cross-field** | `warmup_bars * bar_interval` must not exceed the configured data window |
| **Ceiling** | Every risk limit ≤ its compiled-in hard ceiling (§8) |
| **Host** | Every endpoint host ∈ `ALLOWED_HOSTS` |
| **Filesystem** | `parquet_root` exists and is writable; Ed25519 key mode ≤ `0600` |
| **Referential** | Every symbol in `universe` has an availability declaration in the feature store |

The referential check is the one that saves the most time. A symbol configured but never ingested produces a backtest that silently covers fewer symbols than requested, and the result looks fine.

### Decimal from string, always

Every monetary or quantity setting is `Decimal`, parsed from the string form of the environment variable. `pydantic` does this correctly when the annotation is `Decimal`; the failure mode to avoid is annotating a monetary field as `float` "because it comes from an env var anyway". `Decimal(0.1) != Decimal("0.1")` and the difference compounds across thousands of fills.

---

## 4. The effective config at boot

Immediately after validation, the full tree is serialised and logged at `info` as a single structured record:

```json
{
  "timestamp": "2026-08-02T09:14:22.481Z",
  "level": "info",
  "message": "effective_config",
  "config_hash": "sha256:4f2a91c8...",
  "git_sha": "a3f1e2c",
  "allowed_hosts": ["testnet.binance.vision", "testnet.binancefuture.com", "..."],
  "config": {
    "risk": {
      "max_portfolio_notional_usd": "25000",
      "max_position_notional_usd": "5000",
      "max_daily_drawdown_ratio": "0.03",
      "...": "..."
    },
    "exchange": {
      "binance": {
        "futures_api_key": "***",
        "futures_api_secret": "***",
        "spot_ed25519_key_path": "/run/secrets/ed25519_spot.pem"
      }
    },
    "...": "..."
  }
}
```

Three properties worth naming:

**Redaction is by type, not by field name.** Every secret is `SecretStr`, whose `model_dump()` yields `"***"` (`SECURITY.md` §4.2). There is no list of field names to keep in sync, and a new secret field is redacted the day it is added rather than the day someone remembers it.

**The `config_hash` is over the redacted dump.** It changes when configuration changes and is stable across restarts with identical config. It appears on every backtest result and every audit row written by that process, which is how a historical decision is tied to the exact configuration that produced it.

**Paths are logged; contents are not.** `spot_ed25519_key_path` is logged because knowing which key file was in use is essential to an investigation. The key is not.

---

## 5. `data` — ingestion, storage, feature store

```python
class DataSettings(BaseSettings):
    # Sources
    archive_base_url: HttpUrl = "https://data.binance.vision"
    verify_checksums: Literal[True] = True          # not configurable; see below
    archive_download_concurrency: int = 4
    archive_retry_attempts: int = 2

    # Universe
    universe: tuple[SymbolSpec, ...] = (SymbolSpec("BTCUSDT", "futures_um"),)
    bar_intervals: tuple[str, ...] = ("1m",)
    history_start: date = date(2023, 1, 1)

    # Storage
    parquet_root: Path = Path("data/parquet")
    archive_cache_root: Path = Path("data/archive")   # verified .zip cache; deletable
    parquet_compression: Literal["zstd", "snappy"] = "zstd"
    parquet_compression_level: int = 3
    parquet_target_file_mb: int = 256
    bar_partition_granularity: Literal["month"] = "month"
    trade_partition_granularity: Literal["day"] = "day"

    # TimescaleDB
    hypertable_chunk_interval: timedelta = timedelta(days=1)
    operational_bar_window: timedelta = timedelta(days=90)

    # Live ingestion
    ws_reconnect_base_seconds: float = 1.0
    ws_reconnect_cap_seconds: float = 60.0
    kline_cadence_grace_seconds: int = 90
    staleness_threshold_seconds: int = 120
    backfill_from_rest: bool = True

    # Quality gates
    max_row_rejection_ratio: Decimal = Decimal("0.001")
    max_ohlc_incoherence_ratio: Decimal = Decimal("0.0001")

    # Feature store
    feature_cache_size_mb: int = 512
    refuse_undeclared_features: Literal[True] = True   # not configurable; see below
```

Two fields are typed `Literal[True]`. That is deliberate and is the pattern used throughout this document for **properties that must not become configurable**:

- `verify_checksums` — an unverified archive is untrusted data, and a flag to skip verification is a flag that gets set when a download is slow.
- `refuse_undeclared_features` — a permissive mode is exactly where a strategy silently receives an approximation of data that does not exist (`DATA_PIPELINE.md` §8).

A `Literal[True]` field appears in the config tree and in the boot log, so its value is visible and auditable, but `mypy --strict` rejects any attempt to set it to `False`. Making it visible-but-fixed is better than omitting it, because omission invites someone to add the flag.

**Epoch units, header presence and boolean encodings are not configuration.** They are resolved per `(market, dataset, date)` in code (`DATA_PIPELINE.md` §3). Exposing them as settings would make trap 1 a one-line environment variable away.

---

## 6. `exchange` — venues, credentials, execution

```python
class BinanceSettings(BaseSettings):
    spot_rest_url: HttpUrl = "https://testnet.binance.vision"
    spot_ws_url: WebsocketUrl = "wss://testnet.binance.vision/ws-api/v3"
    futures_rest_url: HttpUrl = "https://testnet.binancefuture.com"
    futures_ws_url: WebsocketUrl = "wss://stream.binancefuture.com/ws"

    spot_api_key: SecretStr
    spot_ed25519_key_path: Path
    futures_api_key: SecretStr
    futures_api_secret: SecretStr

    recv_window_ms: int = 5000
    request_timeout_seconds: float = 10.0
    rate_limit_headroom_ratio: Decimal = Decimal("0.5")


class FeeSettings(BaseSettings):
    # Binance VIP-0 production rates. Assuming a better tier manufactures edge.
    spot_maker_bp: Decimal = Decimal("10.0")
    spot_taker_bp: Decimal = Decimal("10.0")
    futures_maker_bp: Decimal = Decimal("2.0")
    futures_taker_bp: Decimal = Decimal("5.0")
    bnb_discount_applied: bool = False


class ExecutionSettings(BaseSettings):
    default_urgency: Literal["passive", "normal"] = "normal"
    max_order_duration_seconds: int = 300
    reconcile_on_startup: Literal[True] = True
    reconcile_interval_seconds: int = 60
    max_reconciliation_age_seconds: int = 120
    retry_after_ambiguous_response: Literal[False] = False   # never; reconcile instead
    client_order_id_prefix: str = "fk"
```

The URL fields **are** configurable, and that is not a hole in the safety kernel — it is why the kernel validates hosts rather than URLs. A configured URL pointing at a non-allowlisted host aborts startup (§3, step 3), and even a URL that passes startup is re-validated on every request (`SECURITY.md` §3.2). Configuration can select *among* allowlisted venues; it cannot add one.

`rate_limit_headroom_ratio` at 0.5 means the system plans to use at most half the venue's stated budget in steady state. A rate limit hit in normal operation is an architectural finding, not something to back off from.

`retry_after_ambiguous_response` is `Literal[False]` because a timeout is not a rejection. Retrying a possibly-filled order is how a system doubles a position, and reconciliation is the only correct response.

---

## 7. `backtest` — engine and cost model

```python
class CostModelSettings(BaseSettings):
    version: str = "2026-05-production-v3"
    calibration_source: str = "binance_um_production_2026-03..2026-05"
    calibration_method: str = "bookTicker p50/p99 by hour-of-day; sqrt impact prior"
    calibrated_at: date = date(2026, 5, 31)

    spread_profile: Literal["p50", "p99"] = "p50"
    impact_coefficient: Decimal = Decimal("0.35")
    impact_exponent: Decimal = Decimal("0.5")          # square-root law prior
    passive_markout_bp: Decimal = Decimal("3.2")       # measured BTCUSDT 5-min markout
    latency_ack_ms: int = 180
    latency_fill_ms: int = 95
    funding_enabled: Literal[True] = True
    reject_orders_exceeding_band_depth: Literal[True] = True

    @field_validator("calibration_source")
    @classmethod
    def _no_testnet(cls, v: str) -> str:
        if "testnet" in v.lower():
            raise ValueError("cost model calibration source must not be testnet")
        return v


class BacktestSettings(BaseSettings):
    initial_equity_usd: Decimal = Decimal("10000")
    warmup_bars: int = 500
    min_credible_trade_count: int = 200
    min_trades_per_cpcv_fold: int = 30
    min_edge_to_cost_ratio: Decimal = Decimal("2.0")
    max_pbo: Decimal = Decimal("0.30")

    cpcv_groups: int = 8
    cpcv_test_group_size: int = 2
    monte_carlo_paths: int = 1000
    parameter_perturbation_ratio: Decimal = Decimal("0.10")

    run_seed: int = 20260101
    duckdb_memory_limit_gb: Decimal = Decimal("3")
    duckdb_threads: int = 4

    held_out_start: date = date(2026, 6, 1)
    held_out_end: date = date(2026, 8, 1)
    held_out_burned: Literal[False] = False            # only the user may change this
```

`held_out_burned` typed `Literal[False]` means burning the held-out period requires a **source edit**, not an environment variable — the same friction pattern as the allowlist, for the same reason. It is burned on read and burning it is a decision taken once by the user (`BACKTEST_ENGINE.md` §6.3).

`duckdb_memory_limit_gb` is mandatory rather than optional. DuckDB takes most of available memory by default and is the single most likely thing to trigger an OOM kill on Postgres, which the kernel selects by score rather than by importance (`DEPLOYMENT.md` §4).

---

## 8. `risk` — configuration bounded by compiled-in hard ceilings

This is the section with the rule that matters.

> **Risk limits are configuration, bounded by compiled-in hard ceilings. Configuration can only make the system more conservative. It can never make it more permissive than the ceiling.**

```python
# fking/platform/config/_ceilings.py — compiled in. Not config, not env, not database.
#
# Under platform/config rather than under risk/ because the validator that enforces it
# hangs off RiskSettings in the configuration tree, and platform imports no other fking
# module (.claude/rules/module-boundaries.md). risk may import platform; not the reverse.
#
# Widening any of these requires a source edit and a PR labelled safety:critical.
HARD_CEILINGS: Final[Mapping[str, Decimal]] = MappingProxyType({
    "max_portfolio_notional_usd":     Decimal("100000"),
    "max_position_notional_usd":      Decimal("25000"),
    "max_leverage":                   Decimal("3"),
    "max_daily_drawdown_ratio":       Decimal("0.05"),
    "max_total_drawdown_ratio":       Decimal("0.20"),
    "max_open_positions":             Decimal("10"),
    "max_orders_per_minute":          Decimal("30"),
    "max_single_order_notional_usd":  Decimal("10000"),
    "max_correlated_exposure_ratio":  Decimal("0.40"),
})
```

```python
class RiskSettings(BaseSettings):
    max_portfolio_notional_usd: Decimal = Decimal("25000")
    max_position_notional_usd: Decimal = Decimal("5000")
    max_leverage: Decimal = Decimal("2")
    max_daily_drawdown_ratio: Decimal = Decimal("0.03")
    max_total_drawdown_ratio: Decimal = Decimal("0.10")
    max_open_positions: int = 5
    max_orders_per_minute: int = 10
    max_single_order_notional_usd: Decimal = Decimal("2000")
    max_correlated_exposure_ratio: Decimal = Decimal("0.25")

    kill_switch_enabled: Literal[True] = True
    kill_switch_daily_loss_ratio: Decimal = Decimal("0.03")
    conviction_floor: Decimal = Decimal("0.15")
    require_invalidation_level: Literal[True] = True

    @model_validator(mode="after")
    def _within_ceilings(self) -> "RiskSettings":
        for name, ceiling in HARD_CEILINGS.items():
            value = Decimal(str(getattr(self, name)))
            if value > ceiling:
                raise ValueError(
                    f"{name}={value} exceeds compiled-in hard ceiling {ceiling}. "
                    f"Raising a ceiling requires a source edit and a "
                    f"safety:critical pull request."
                )
        return self
```

### Why this asymmetry

A limit stored purely in configuration is not a limit. It is a suggestion that lives in the file most likely to be edited by someone in a hurry, and `SECURITY.md` §2 vector 1 is exactly that person. The realistic sequence is not sabotage — it is "the backtest wants more notional, let me bump the env var", made at 1am, never reverted.

A limit stored purely in code is inflexible in the wrong direction: different strategies and different experiments genuinely need different, *tighter* limits, and requiring a code change to tighten a limit means limits get tightened less often than they should.

The bounded pattern gets both: **tightening is free, loosening past the ceiling is impossible without a reviewed source change.** The direction of friction matches the direction of risk.

The same pattern applies wherever a numeric limit protects something: agent token budgets, order rate limits, position counts. Anything that can be raised to make the system take more risk gets a ceiling.

### The floors, which are where the bug lives

The loop above is correct for every limit in `HARD_CEILINGS` and **backwards for every limit where smaller is riskier** — `min_free_margin_ratio`, `min_trades_for_kelly`, `conviction_floor`. `0 > 0.25` is `False`, so `if value > bound` accepts `min_free_margin_ratio = 0`, authorises trading with no margin buffer at all, and reports a passing configuration check.

So the bounds are two mappings, checked by two validators, holding values of two **distinct types** — `Ceiling` and `Floor`, in `fking.risk.ceilings`, each offering exactly one comparison in its own direction. `mypy --strict` then rejects `HARD_FLOORS[name] > requested` and rejects handing `HARD_FLOORS` to the ceiling validator; the type system does the reviewing, rather than a human scanning a `>` inside a loop over a dictionary. Both are frozen single-field classes rather than `typing.NewType` over `Decimal`: a `NewType` is assignable to `Decimal`, inherits its comparison operators, and the backwards expression compiles.

`fking.risk.ceilings` imports the ceiling *values* from `fking.platform.config` rather than restating them — two compiled-in copies of a safety constant are two numbers that can disagree, and the one that disagrees silently is whichever the reader is not looking at. `platform` still owns them because it may import no other `fking` module.

Floors obey the same direction-of-friction rule read the other way: **raising a floor is free, lowering one past its compiled-in value requires a source edit and a `safety:critical` pull request.** The bound values and their provenance are in `RISK_PHILOSOPHY.md` §4 and §9; `tests/risk/test_limits_property.py` is the guarantee, asserting over arbitrary generated configurations that a configuration is accepted **if and only if** it is within every ceiling and above every floor.

`kill_switch_enabled` and `require_invalidation_level` are `Literal[True]`. `CLAUDE.md` §11 names the anti-pattern: adding a config flag to bypass a gate. Gates exist because someone will be in a hurry later, and that someone is you.

---

## 9. `agents` — providers, quotas, budgets

```python
class ProviderSettings(BaseSettings):
    api_key: SecretStr
    base_url: HttpUrl
    model: str
    daily_token_budget: int
    requests_per_minute: int
    timeout_seconds: float = 60.0


class AgentSettings(BaseSettings):
    enabled: bool = False                        # off by default; see below
    primary: ProviderSettings                    # Gemini free tier
    fallback: ProviderSettings | None = None     # Groq free tier
    prompt_root: Path = Path("prompts")
    require_prompt_hash_match: Literal[True] = True
    max_reask_attempts: Literal[0] = 0           # no free-text fallback, ever
    degrade_to_deterministic_on_quota_exhaustion: Literal[True] = True

    budgets: tuple[AgentBudget, ...] = ()      # agent_id, token_budget, timeout, invocations
```

The per-agent budgets are **one record per agent** rather than three mappings keyed by agent id. Three parallel mappings can disagree about which agents exist, and the disagreement is silent — an agent present in the token map and absent from the timeout map gets whatever the lookup's fallback happens to be. A duplicate `agent_id` is refused at construction for the same reason: which record wins would depend on iteration order, and the loser would be a budget somebody believes is in force.

`enabled` defaults to `False`. The system must be fully functional with every LLM agent disabled — that is the degraded mode, and a degraded mode that has never been the default is a degraded mode that does not work. It also means a fresh clone with no API keys runs.

`max_reask_attempts: Literal[0]` encodes `PROMPT_LIBRARY.md` §3: an unparseable response is a failure, not something to retry with "please output valid JSON". A re-ask hides a systematic prompt problem behind a per-call patch, and it spends quota that the golden set needs.

`degrade_to_deterministic_on_quota_exhaustion` is fixed `True` because the alternative — stalling — is worse in an unattended system (`ARCHITECTURE.md` §9).

Per-agent budgets are bounded by compiled-in ceilings using the same pattern as §8. An agent's token budget is a cost limit, and cost limits are the ones that get raised at 1am.

---

## 10. `telemetry` — logging, metrics, tracing

```python
class TelemetrySettings(BaseSettings):
    service_name: str = "fking"
    environment: Literal["local", "ci", "demo"] = "local"

    otlp_endpoint: str = "http://otel-collector:4317"
    otlp_timeout_seconds: float = 10.0

    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_format: Literal["json"] = "json"             # json in every environment
    log_field_allowlist: tuple[str, ...] = ...       # allowlist, never denylist

    trace_sample_ratio: Decimal = Decimal("0.10")    # feature/background paths
    order_path_sample_ratio: Literal[1] = 1          # never sampled

    metric_prefix: Literal["fking_"] = "fking_"
    max_metric_series: int = 20000
```

`log_format` is `Literal["json"]` so that no environment ever runs a different serialisation path from the one whose parsing is tested. `order_path_sample_ratio` is fixed at 1 for the reason in `OBSERVABILITY.md` §5: a sampled order path means some trades are unreconstructable and you will not know which until you need one.

`metric_prefix` is fixed because renaming metrics breaks every dashboard and alert simultaneously and silently (`OBSERVABILITY.md` §4).

`log_field_allowlist` is an ordered `tuple`, not the `frozenset` its semantics suggest, because it is serialised into `config_hash` and a set's iteration order varies with `PYTHONHASHSEED` — which would make two processes holding identical configuration report different hashes, and the hash's whole job is to say when configuration is the same.

It is also the **log field registry**, not a filter applied on top of one: a field absent from this tuple is dropped by the pipeline and counted on `fking_platform_log_fields_dropped_total` rather than emitted (`OBSERVABILITY.md` §7). That has a consequence worth stating, because it is the thing that surprises people: **adding a field to a log call is not enough to make it appear in Loki.** The field has to be added here too, in a reviewed diff — and because the tuple is part of `config_hash`, which fields a historical record *could* have carried is itself reconstructable. The dropped-field counter is what makes this maintainable rather than a silent hole: a field being dropped and counted shows up on a dashboard, whereas a field being silently dropped is a bug nobody finds until an investigation needs it.

---

## 11. `platform` — database, bus, API, scheduler

```python
class DatabaseSettings(BaseSettings):
    # Three DSNs, one per privilege class, each connecting as its own LOGIN role.
    # Cross-field validation refuses two that share a role: a single shared
    # connection string collapses least privilege on the first deploy and
    # nothing fails, because the over-privileged connection serves every query.
    dsn: PostgresDsn                         # fking_app_login
    ingest_dsn: PostgresDsn                  # fking_ingest_login
    migrator_dsn: PostgresDsn                # fking_migrator_login; alembic only
    pool_min_size: int = 2
    pool_max_size: int = 10
    statement_timeout_seconds: int = 30
    # The NOLOGIN group roles that hold the grants. Not what anything connects as.
    migration_role: str = "fking_migrator"   # owns every object; holds no grants
    application_role: str = "fking_app"      # no UPDATE/DELETE on audit tables
    ingest_role: str = "fking_ingest"        # writes market data; app has SELECT only


class BusSettings(BaseSettings):
    redis_url: RedisDsn
    consumer_group_prefix: str = "fking"
    max_stream_length: int = 1_000_000
    claim_idle_ms: int = 30_000
    dlq_stream_suffix: str = ".dlq"
    require_correlation_id: Literal[True] = True   # missing → DLQ, never invented


class ApiSettings(BaseSettings):
    host: Literal["127.0.0.1"] = "127.0.0.1"      # never 0.0.0.0
    port: int = 8000
    cors_origins: tuple[str, ...] = ("http://127.0.0.1:3000",)


class SchedulerSettings(BaseSettings):
    timezone: Literal["UTC"] = "UTC"
    max_concurrent_jobs: int = 4
    tick_interval_seconds: int = 15
```

`ApiSettings.host` is `Literal["127.0.0.1"]`. This stack holds exchange credentials and a Grafana on `0.0.0.0` is exposed to every device on the local network. `SchedulerSettings.timezone` is `Literal["UTC"]` — every datetime in this system is tz-aware UTC (`CLAUDE.md` §2), and a scheduler in a local timezone reintroduces DST into a market that has no session boundary to make the error obvious.

**There is no `misfire_grace_seconds` here, and its absence is the point.** A grace window is a single global answer to "what happens to a run that was missed", and this system has three answers: gap detection runs once, hourly ingestion replays every missed window in order, and reconciliation runs once stamped now. So the decision lives on the job, as a required `MisfirePolicy` with no default (`fking.platform.scheduler`, ADR-0019), and there is no configuration value that could override it — which is deliberate, because the value somebody would reach for during an incident is the one that turns six distinct windows into one.

`tick_interval_seconds` is the beat's *resolution*, not any job's cadence: a job fires within one tick of its scheduled fire time. Lowering it costs one `max(scheduled_fire_utc)` query per registered job per tick and buys precision no job currently needs.

`pool_max_size` deserves a note: raising it is not a fix for connection exhaustion. Each connection carries `work_mem`, so raising `max_connections` converts a connection problem into a memory problem, and the memory problem manifests as an OOM kill on Postgres.

---

## 12. `evolution`

```python
class EvolutionSettings(BaseSettings):
    population_size: int = 20
    max_population_size: int = 50               # ceiling-bounded
    generation_interval: timedelta = timedelta(days=7)
    challenger_slots: int = 3

    min_forward_days_before_promotion: int = 30
    min_forward_trades_before_promotion: int = 100
    promotion_requires_forward_outperformance: Literal[True] = True

    trial_ledger_counts_failed_runs: Literal[True] = True
    retire_below_survival_score: Decimal = Decimal("0.30")
    max_generations_without_promotion: int = 8
```

`trial_ledger_counts_failed_runs` is fixed `True`. A trial count that only counts successes is a trial count designed to flatter, and it feeds directly into the deflated Sharpe (`BACKTEST_ENGINE.md` §6.5). This is a one-line setting that, if it were configurable and set to `False`, would silently invalidate every statistical defence in the system.

---

## 13. Anti-patterns

| Anti-pattern | Why it is wrong |
|---|---|
| Reading `os.environ` outside `load_settings()` | Bypasses validation, the boot log, and the config hash |
| A global settings singleton read at call time | Breaks purity, testability and reconstruction (§1) |
| A config flag that bypasses a gate | `CLAUDE.md` §11. Gates exist because someone will be in a hurry |
| A risk limit with no compiled-in ceiling | A suggestion in the most-edited file in the repository |
| Adding a mainnet URL, even commented out | One uncomment from live (`SECURITY.md` §4.3) |
| A `float` monetary setting | `Decimal(0.1) != Decimal("0.1")`, compounding across fills |
| Hot reload | Makes the boot log wrong for part of the run |
| A default that is more permissive than the documented safe baseline | The process that runs without a config file is the one that matters |
| Different `log_format` per environment | Only one environment's parsing is ever tested |
| Secrets in `docker-compose.yml` | Compose files are committed; `.env` is not |

---

## 14. Cross-references

| For | See |
|---|---|
| Why the allowlist is not configuration | `SECURITY.md` §3, `ARCHITECTURE.md` §8 |
| Secret typing, `.env` hygiene, key file permissions | `SECURITY.md` §4 |
| Boot-time redaction and the logging pipeline | `OBSERVABILITY.md` §7 |
| Compose env wiring, volumes, resource limits | `DEPLOYMENT.md` §3 |
| Feature availability contract | `DATA_PIPELINE.md` §8 |
| Cost model provenance enforcement | `BACKTEST_ENGINE.md` §4 |
| Why risk has sole authority over order construction | `RISK_PHILOSOPHY.md` |
| Agent budgets, timeouts, escalation paths | `TOOLS.md` §3, `AI_MANIFEST.md` §5 |
