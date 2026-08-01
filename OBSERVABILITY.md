# Observability

Logging, metrics, tracing, alerting, and the reconstruction guarantee they exist to serve.

This expands `ARCHITECTURE.md` §11. Observability here is not a reporting feature bolted on at the end. It is a design constraint on every module, which is why correlation IDs and append-only audit tables are P0 work. `CLAUDE.md` §11 names the anti-pattern precisely: deferring instrumentation until the end means it never gets added properly, and it is missing from exactly the history an investigation needs.

---

## 1. The reconstruction requirement

> **Any trade must be fully reconstructable from the audit log alone, months later, with no access to application memory, no running process, and no code that has not changed since.**

This is the governing requirement. Everything else in this document is subordinate to it.

Concretely, for any historical fill, these **eight questions** must be answerable from stored data alone:

| # | Question | Where the answer lives |
|---|---|---|
| 1 | **What data existed** at decision time — which bars, which trades, what was the last tick, what was stale, what was gapped | `audit.data_snapshot` + `NormalizationResult` provenance |
| 2 | **What features were computed**, with which feature versions and which exact input values | `audit.feature_snapshot` |
| 3 | **Which strategy version and lineage fired**, with its exact parameter set as `Decimal` strings | `audit.signal` |
| 4 | **What the signal said** — direction, conviction, horizon, invalidation level, rationale | `audit.signal` |
| 5 | **What risk decided and why** — the size, every limit evaluated, which limit bound, or the rejection with its reason | `audit.risk_decision` |
| 6 | **Which agent reasoning contributed**, with the exact prompt (by content hash, resolvable to text), the exact response, the model and provider | `audit.agent_call` |
| 7 | **What was sent and what came back** — the order payload, the venue's raw response, every partial fill, every rejection with its raw body | `audit.order`, `audit.venue_response`, `audit.fill` |
| 8 | **The slippage against decision price**, decomposed | `audit.shortfall` |

### The reconstruction test

Not a design intention — a test that runs.

`ReconstructionTest` picks a random `demo_live` fill at 30, 90 and 180 days of age and rebuilds all eight facts **from storage only**. It runs weekly in CI against the real database and reports per-fact success.

```python
class ReconstructionTest(BaseModel):
    fill_id: UUID
    correlation_id: UUID
    age_days: int
    reconstructed: dict[str, bool]        # one key per required fact
    missing: list[str]
    verdict: Literal["reconstructable", "partial", "failed"]
```

A failure on any `demo_live` fill escalates to the user immediately. The system's central guarantee is broken and nothing else is more important.

### Why "months later" is in the requirement

The obvious reading is regulatory. It is not — this is a demo-only research system (`SECURITY.md` §1) with no regulator. The real reason is that **the system's most important failures are slow.** A strategy that quietly degrades, an agent whose prompt drifted, a cost model that stopped matching reality: none of these produce an incident on the day they start. They are discovered as a trend, weeks later, and the investigation must reach backwards into a period nobody was watching.

An observability stack that answers "what is happening now" brilliantly and "what happened in March" not at all is optimised for the failure mode this system does not have.

### Audit rows are not telemetry

The distinction is load-bearing:

- **Audit rows** go to Postgres, transactionally, in the **same transaction as the state change they describe.** They are append-only, enforced by the database. They are kept **forever**. They do not pass through the OpenTelemetry Collector.
- **Telemetry** (metrics, logs, traces) goes through the Collector to Prometheus, Loki and Tempo. It is sampled, retained for days, and best-effort.

An audit row committed separately from the fact it records can be lost independently of that fact. A fire-and-forget audit write is worse than no audit table, because it looks like one. And routing audit data through the Collector would mean that an observability outage — a full disk, an OOM-killed Loki — silently destroys the record that matters.

---

## 2. Pipeline

```
   application (OpenTelemetry SDK)
        │  OTLP/gRPC :4317
        ▼
   OpenTelemetry Collector
        ├── metrics ──► Prometheus   (15d retention)
        ├── logs    ──► Loki         (30d retention)
        └── traces  ──► Tempo        ( 7d retention)
                              │
                              ▼
                          Grafana  (dashboards provisioned as code)

   application ──(same DB transaction)──► PostgreSQL audit tables  (forever)
```

All OSS, self-hosted in the same Compose stack, zero cost. Service definitions, memory limits and volumes are in `DEPLOYMENT.md` §3.

### Retention, and why the asymmetry

| Store | Retention | Why |
|---|---|---|
| Tempo (traces) | 7 days | Traces answer "what happened in this request". That question has a short half-life |
| Prometheus (metrics) | 15 days | Enough for week-over-week comparison; longer belongs in derived series |
| Loki (logs) | 30 days | Long enough for a post-mortem on a slow-developing problem |
| Postgres audit tables | **Forever** | The guarantee |

The first three are convenience. The last is the guarantee. If disk becomes the constraint, the answer for audit data is **archiving to cold Parquet with a verified checksum, never deletion** (`DEPLOYMENT.md` §6).

TimescaleDB compression and retention policies are **forbidden** on any hypertable backing an audit trail. Compression rewrites chunks, which is mutation of append-only data by a different name, and a compressed chunk that fails to decompress is silent data loss on exactly the rows that must never be lost.

### Collector failure

The SDK buffers and then drops. Emit `fking_telemetry_spans_dropped_total` and alert on it — silent trace loss during an incident is the worst possible moment to lose traces.

---

## 3. Correlation IDs

### Origination

> **The correlation ID originates at the top — the market data event — and propagates unchanged through feature computation, signal, risk decision, order, fill, and evolution scoring.**

One ID per causal chain. A UUIDv7 (time-ordered, so it sorts usefully and indexes without page splits) generated at exactly one place: the ingestion boundary where a market data event enters the system.

### The rule that gets broken

> **You may not generate a new correlation ID mid-flow.**

Regenerating it because "the context wasn't available here" severs the chain at exactly the boundary an investigation needs to cross. The result is not a missing link — it is two chains that each look complete, which is worse, because nothing about either one looks broken.

### Propagation, and where it actually breaks

| Boundary | Mechanism |
|---|---|
| Function call | Explicit parameter or `contextvars` context |
| Async task spawn | `contextvars` copy — verified by test, because a task created with `loop.create_task` from the wrong context silently starts fresh |
| **Redis Streams publish/consume** | **In the event payload.** There is no ambient context on the other side of Redis |
| Database write | Column on the audit row |
| HTTP request out | `traceparent` header via OTel propagator |
| LLM agent call | Field on the request, recorded on the audit row |
| Scheduled job | New root ID, tagged `origin=scheduler`, and the job records which IDs it acted upon |

Bus hops are where it breaks in practice, and every new Redis Streams consumer breaks it the same way unless the consumer base class extracts it from the payload. That is why extraction lives in the base class rather than in each consumer.

**A message arriving without a correlation ID is rejected to the dead-letter stream with a reason. Never invent one.** An invented ID creates a false chain that looks complete, which is the one outcome worse than a broken chain.

### Relationship to trace IDs

The OpenTelemetry trace ID and the correlation ID are different things and both are recorded. A trace ID covers one span tree, which ends at a bus boundary or a process restart. A correlation ID covers the whole causal chain from market tick to evolution scoring, which may span hours, several processes and a restart. Every span carries `fking.correlation_id` as an attribute; every audit row carries both.

---

## 4. Metric naming

### The convention

```
fking_<subsystem>_<measurement>_<unit>[_total]
```

- **`fking_` prefix on everything.** Non-negotiable. It makes every metric this system emits selectable with one regex in a Grafana variable, and unambiguously separates application metrics from the exporters sharing the same Prometheus.
- **Unit suffix mandatory.** `_seconds`, `_bytes`, `_usd`, `_basis_points`, `_ratio`, `_count`. Base units only — never `_milliseconds`, never `_bp`.
- **`_total` suffix on counters**, per Prometheus convention.
- **Subsystem matches the module**: `data`, `strategy`, `risk`, `execution`, `backtest`, `agents`, `evolution`, `platform`.

`CLAUDE.md` §4 on naming applies to telemetry with full force. `size` is dangerous in a trading system and it is equally dangerous on a graph — `fking_execution_order_size` could be a quantity, a notional, or a byte count, and someone will alert on it having guessed wrong.

### Why naming is fixed early, and effectively frozen

> **Renaming a metric breaks every dashboard, every alert, and every historical query that referenced it — simultaneously, silently, and in the direction of "no data" rather than "error".**

A renamed metric does not raise. The panel goes blank and the alert stops firing. An alert that stops firing looks exactly like an alert that has nothing to report, which means a rename can disable a safety alert and the disabling is invisible until the thing it watched for happens.

Prometheus also cannot rename historical series. Old data stays under the old name, so a rename does not migrate history — it forks it, and every query must union both names forever, which nobody does.

Consequently: metric names are chosen at design time, reviewed like an API, and changed only through a deliberate migration that dual-emits both names for at least one full retention window (15 days) before the old one is dropped. `SpanContract` and metric names are published **before** implementation so every module instruments to the same shape.

### Cardinality

> **Unbounded values are forbidden as Prometheus labels.**

Forbidden: `order_id`, `correlation_id`, `trade_id`, `client_order_id`, raw symbol strings taken from exchange responses, any user-supplied string, any agent-generated string.

Permitted: `strategy_id` (population is capped), `symbol` from the configured universe (bounded and validated against it), `venue`, `market`, `side`, `order_type`, `outcome`, `reason` from a closed enum.

Multiply the label domains before merging. If the product is unbounded or over a few thousand, redesign now. One unbounded label will take down the local Prometheus, and it will do it during an incident, because incidents are when the unbounded thing gets interesting.

High-cardinality identifiers belong in **logs and trace attributes**, which are indexed for exactly that and do not build a time series per value.

### Money in metrics

> **A `Decimal` price or quantity recorded as a float metric is not the record.**

Metrics may carry a float approximation for graphing. The authoritative value goes into the audit row and the log line as the **exact decimal string**. Reconstructing a fill price from a Prometheus sample — 15-day retention, float precision, subject to sampling — is not reconstruction.

### The core metric set

**Data**

| Metric | Type | Labels |
|---|---|---|
| `fking_data_bars_ingested_total` | counter | market, symbol, interval, source |
| `fking_data_rows_rejected_total` | counter | market, dataset, reason |
| `fking_data_gap_seconds_total` | counter | market, symbol, detector |
| `fking_data_stream_staleness_seconds` | gauge | market, symbol |
| `fking_data_checksum_failures_total` | counter | market, dataset |
| `fking_data_feature_compute_seconds` | histogram | feature_name, feature_version |

**Strategy and risk**

| Metric | Type | Labels |
|---|---|---|
| `fking_strategy_signals_emitted_total` | counter | strategy_id, direction |
| `fking_risk_decisions_total` | counter | strategy_id, outcome (`sized`/`vetoed`) |
| `fking_risk_veto_total` | counter | strategy_id, binding_limit |
| `fking_risk_position_notional_usd` | gauge | strategy_id, symbol |
| `fking_risk_portfolio_drawdown_ratio` | gauge | — |
| `fking_risk_limit_utilisation_ratio` | gauge | limit_name |
| `fking_risk_kill_switch_active` | gauge | — (1/0) |

**Execution**

| Metric | Type | Labels |
|---|---|---|
| `fking_execution_orders_submitted_total` | counter | venue, symbol, side, order_type |
| `fking_execution_fills_total` | counter | venue, symbol, side, liquidity (`maker`/`taker`) |
| `fking_execution_rejections_total` | counter | venue, reason |
| `fking_execution_shortfall_basis_points` | histogram | symbol, component |
| `fking_execution_stage_latency_seconds` | histogram | stage |
| `fking_execution_reconciliation_age_seconds` | gauge | venue |
| `fking_execution_reconciliation_divergences_total` | counter | venue, kind |

**Agents**

| Metric | Type | Labels |
|---|---|---|
| `fking_agents_calls_total` | counter | agent, provider, outcome |
| `fking_agents_tokens_consumed_total` | counter | agent, provider, direction |
| `fking_agents_quota_remaining_ratio` | gauge | provider |
| `fking_agents_parse_failures_total` | counter | agent, provider |
| `fking_agents_abstentions_total` | counter | agent |
| `fking_agents_latency_seconds` | histogram | agent, provider |

**Platform**

| Metric | Type | Labels |
|---|---|---|
| `fking_platform_allowlist_rejections_total` | counter | host |
| `fking_platform_bus_lag_messages` | gauge | stream, consumer_group |
| `fking_platform_bus_dlq_depth` | gauge | stream |
| `fking_platform_audit_write_failures_total` | counter | table |
| `fking_telemetry_spans_dropped_total` | counter | — |

`fking_platform_allowlist_rejections_total` is labelled by host, which looks like it violates the cardinality rule. It does not: the label is the *rejected* host, and in correct operation the metric is always zero. A non-zero value is a critical incident, and knowing which host was attempted is the entire value of the metric. If it ever becomes high-cardinality, the system is being probed and the cardinality is the least of the problems.

---

## 5. Spans

Span names are `module.operation`, lowercase, dot-separated, **stable across versions** — renaming a span breaks every historical query the same way renaming a metric does.

```python
class SpanContract(BaseModel):
    span_name: str                    # "risk.size_position"
    module: str
    required_attributes: list[str]    # always includes correlation_id
    optional_attributes: list[str]
    sampling: Literal["always", "parent", "ratio"]
    parent_span: str | None
    emits_events: list[str]
```

### The order path is never sampled

> **Every span between `Signal` and `Fill` is retained at 100%.**

Metrics can be sampled, dashboards can be approximate, feature-computation traces can be sampled at 1%. The order path cannot. A sampled order path means some trades are unreconstructable and **you will not know which until you need one of them.** There is also no cost argument available here — the stack is self-hosted and free. Anyone proposing to sample the order path is solving a problem that does not exist.

### Required attributes on order-path spans

`fking.correlation_id`, `fking.strategy_id`, `fking.strategy_version`, `fking.symbol`, `fking.venue`, `fking.audit_row_id`.

### What never goes in a span

Prompts, responses, API keys, Ed25519 material, exchange credentials, raw exchange response bodies. Spans carry the prompt **hash** and the **audit row id**; the text lives in the access-controlled, append-only audit tables. Tempo has 7-day retention and no access control worth the name; it is the wrong place for anything that matters or anything that is secret.

**A span whose attributes are empty is treated as no span.** An untyped span is a timing measurement, not a record.

### The decision-point rule

Every place the system *chooses* something emits **one structured record containing its inputs and its outcome**. Not five lines narrating the process — one line with the inputs.

And specifically: **instrument the rejections.** Systems instrument the happy path and lose the vetoes. A risk rejection with no record makes "why did we *not* trade that?" unanswerable, and in this system that question is asked as often as its inverse — the whole architecture is organised around saying no correctly, so the record of every "no" is the primary evidence that it is working.

---

## 6. Structured logging

JSON only. No human-readable format in any environment, including local development — a format that differs between environments is a format whose parsing is only tested in one of them.

### Mandatory fields on every line

| Field | Note |
|---|---|
| `timestamp` | RFC 3339, UTC, microsecond precision |
| `level` | `debug` / `info` / `warning` / `error` / `critical` |
| `logger` | Dotted module path |
| `message` | Short, static, **no interpolated values** — values are fields |
| `correlation_id` | Where a causal chain exists; explicit `null` where genuinely none |
| `trace_id`, `span_id` | For Loki↔Tempo linking |
| `service`, `version`, `environment` | Emitter identity and git SHA |

Conditional but mandatory when applicable: `strategy_id`, `strategy_version`, `symbol`, `venue`, `order_id`, `client_order_id`, `audit_row_id`.

### Money and time in logs

Monetary values are **strings** carrying the exact decimal: `{"notional_usd": "1043.27000000"}`. Never a JSON number — JSON numbers are IEEE 754 doubles in every parser that will ever read the line, and the log is a reconstruction source.

All timestamps are tz-aware UTC.

### Static messages, structured values

```python
# wrong: unqueryable, high-cardinality, and the value is trapped in prose
logger.info(f"Sized position {qty} for {symbol} at conviction {c}")

# right
logger.info(
    "position_sized",
    symbol=symbol,
    base_quantity=str(qty),
    conviction=str(conviction),
    binding_limit=limit_name,
    correlation_id=cid,
)
```

The static `message` acts as an event type. `{message="position_sized"} | json | binding_limit="max_notional"` is a query. A formatted sentence is not.

---

## 7. Redaction

> **Redaction happens in the logging pipeline, and it is allowlist-based.**

Two properties, both essential:

**In the pipeline, not at call sites.** A call site that remembers to redact is a call site whose next editor will forget. The processor sits in the structlog chain, before any renderer, and applies to every record.

**Allowlist, not denylist.** The serialiser emits only explicitly permitted field names. A denylist means every new field is exposed until someone remembers to add it, and nobody remembers during an incident — which is precisely when new fields get added and debug logging gets enabled.

Rules:

| Rule | Behaviour |
|---|---|
| Unknown field name | Dropped, and counted in `fking_platform_log_fields_dropped_total` |
| `SecretStr` value | Rendered `"***"` by the type itself; the processor asserts no raw secret survived |
| Exchange response object | Only allowlisted keys serialised; **headers never serialised at all** |
| Ed25519 key material | Never enters a log record. A processor assertion raises if a PEM header appears in any string value |
| Prompt / response text | Never logged. The prompt hash and audit row id are logged instead |
| Full `model_dump()` of a config object | Blocked at type level: secret fields are `SecretStr` and the config's dump excludes them |

The dropped-field counter is what makes the allowlist maintainable. A field being silently dropped is a bug; a field being dropped *and counted* is a signal that shows up on a dashboard and gets the field added deliberately.

`SECURITY.md` §4 covers the secret lifecycle end to end; this section covers only the logging boundary.

---

## 8. Alerting

### The principle

> **Alert fatigue is itself a safety failure.**

This is not a productivity concern. An operator who has learned to dismiss notifications from this system will dismiss the one that says the kill switch fired. Every alert that fires and turns out not to need action makes the next alert less effective, and the degradation is permanent — trust is not rebuilt by the alert being right once.

Therefore:

1. **Every alert must have an action.** If the response is "look at it and then do nothing", it is a dashboard panel, not an alert.
2. **Every alert names its runbook** in an annotation. An alert without a documented response is an alert whose response will be improvised at 3am.
3. **Two severities only.** `page` (a human must act now) and `ticket` (a human must act this week). A third severity becomes the default and is then ignored, which is a denylist problem in a different costume.
4. **An alert that fires more than twice a week without action is deleted or fixed.** Reviewed monthly. Deletion is a legitimate outcome.
5. **Resource alerts are `ticket`, never `page`.** They are capacity signals, not correctness signals.

### What pages

| Alert | Condition | Why |
|---|---|---|
| `AllowlistRejection` | `fking_platform_allowlist_rejections_total` increases at all | The demo-only guarantee was tested. The single loudest signal the system has |
| `KillSwitchActive` | `fking_risk_kill_switch_active == 1` | The system has stopped itself |
| `AuditWriteFailure` | `fking_platform_audit_write_failures_total` increases | The reconstruction guarantee is degrading right now |
| `ReconciliationDivergence` | `fking_execution_reconciliation_divergences_total` increases | Local and exchange state disagree about a position |
| `UserDataStreamDown` | `fking_execution_reconciliation_age_seconds > 60` with open positions | Blind with exposure |
| `OrderPathStalled` | Orders submitted but no ack or fill for 120s | Orders in an unknown state |
| `DrawdownLimitApproached` | `fking_risk_portfolio_drawdown_ratio > 0.8 * limit` | Enough warning to intervene before the kill switch |

Seven paging alerts. That is close to the practical ceiling; each addition must displace one or justify why the set was wrong.

### What tickets

Stream staleness beyond 120s, checksum failures, data rejection rate above threshold, agent parse failures, an agent abstention rate collapsing to zero (`PROMPT_LIBRARY.md` §5 — this reads as an improvement in every other metric and is almost always a regression), quota exhaustion, bus consumer lag, DLQ depth non-zero, spans dropped, container OOM events, disk above 70%, backtest determinism failure.

### What is deliberately not an alert

- **Individual losing trades.** Losses are the expected cost of trading. Alerting on them trains an operator to associate this system's notifications with normal operation.
- **A single risk veto.** Vetoes are the risk engine working. A *sustained rate* of vetoes is a ticket, because it means strategies and limits have diverged.
- **Latency percentiles in isolation.** They belong on a dashboard next to their cost consequence; alone they generate noise proportional to market activity.

---

## 9. Dashboards as code

Every dashboard is JSON in `ops/grafana/dashboards/`, provisioned at container start via `ops/grafana/provisioning/`. Datasources likewise. Nothing is created through the UI.

The reason is the same one that governs metric names: a dashboard built by clicking exists on one machine, is not reviewed, is not versioned, and disappears when the volume is recreated. It is also the dashboard someone will be looking at during an incident, which makes it the worst possible thing to have no history of.

Grafana's UI runs read-only in the provisioned folder. Edits are made to the JSON, committed, and reloaded — which is slower, and which is the point.

### The dashboard set

| Dashboard | Answers |
|---|---|
| **System health** | Is everything up, are consumers keeping up, is the DLQ empty, are spans dropping, is disk fine |
| **Data pipeline** | Ingestion rate, staleness per symbol, gaps, rejection reasons, feature compute latency |
| **Trading** | Open positions, exposure, P&L, orders and fills, rejections by reason, drawdown against limit |
| **Execution quality** | Shortfall decomposed into the three components, per-stage latency, fill rates, reconciliation age |
| **Risk** | Limit utilisation, veto rate by binding limit, kill-switch state and history |
| **Agents** | Calls, quota remaining, parse failures, abstention rate, latency, cost per decision |
| **Evolution** | Population state, trial ledger, promotions and retirements, deflated Sharpe distribution |

Every panel that displays a monetary value shows it derived from the audit tables via the Postgres datasource, not from Prometheus. Prometheus panels are for rates, latencies and counts. The distinction reappears here for the last time: **Prometheus is for shape, Postgres is for truth.**

---

## 10. Cross-references

| For | See |
|---|---|
| Why reconstruction is a design constraint on every module | `ARCHITECTURE.md` §11 |
| Redaction, secrets, and the audit trail's security properties | `SECURITY.md` §4, §6 |
| Retention enforcement, volumes, resource limits | `DEPLOYMENT.md` §3, §6 |
| Telemetry configuration surface | `CONFIGURATION.md` §10 |
| Prompt hashes and why agent outputs must stay replayable | `PROMPT_LIBRARY.md` §2 |
| Memory tiers and provenance on retrieved items | `MEMORY_SYSTEM.md` §3 |
| Kill switch, degraded modes, incident response | `FAILSAFE.md`, `ERROR_RECOVERY.md` |
