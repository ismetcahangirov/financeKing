# Architecture

How financeKing is put together, and why. Decisions recorded here are expanded in `docs/adr/`.

---

## 1. The shape of the problem

The system must do six things on a loop, forever, without supervision:

1. Acquire and validate market data
2. Form hypotheses about market behaviour
3. Turn hypotheses into executable strategies
4. Prove or disprove those strategies rigorously
5. Execute the survivors under risk control on a demo account
6. Evaluate the outcome and feed it back into step 2

Steps 1–4 are where almost all the engineering lives. Step 5 is comparatively simple. Step 6 is where most autonomous trading projects quietly fail, because they measure the wrong thing and improve toward it.

The architecture is organized around one asymmetry: **generating strategies is cheap, and validating them is expensive and adversarial.** A system that makes generation easy without making validation hard produces confident nonsense at scale.

---

## 2. Topology: modular monolith

A single Python process (plus a separate Next.js dashboard), organized into strictly bounded modules.

**Why not microservices.** The usual arguments for microservices are independent scaling, independent deployment, and team autonomy. There is one developer, one machine, and zero budget. What microservices would actually add here is network partitions between components that must agree about position state, distributed tracing to answer questions a stack trace answers today, and eight deployment targets. That is cost with no corresponding benefit.

**Why boundaries still matter.** The modules are enforced statically by `import-linter`, so the architecture is executable rather than aspirational. If a component ever genuinely needs to scale independently, extracting it is mechanical — the seams already exist and are proven by CI. We get the option value of microservices without paying for it now.

```
src/fking/
  domain/     pure types, zero dependencies
  data/       ingestion, storage, features
  strategy/   strategy contract and implementations
  risk/       sizing, limits, kill switch
  execution/  venues, OMS, reconciliation
  backtest/   engine, cost model, validation
  agents/     LLM agents and runtime
  evolution/  lifecycle, scoring, mutation
  platform/   config, logging, telemetry, bus, persistence, safety
  api/        FastAPI application
```

Dependencies point inward toward `domain`. `platform` is importable by anyone. The load-bearing contract is that **`strategy` cannot import `execution`** — see §5.

---

## 3. The core data flow

```
                    ┌──────────────┐
   market data ────►│  data        │──── features (point-in-time)
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  strategy    │──── Signal (direction, conviction, invalidation)
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  risk        │──── Order  (or a logged rejection)
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  execution   │──── Fill
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  evolution   │──── survival score, lifecycle transition
                    └──────────────┘
```

Every arrow crosses a module boundary and emits an event onto the bus carrying a correlation ID that originated at the top. That is what makes any trade reconstructable end to end months later.

---

## 4. Backtest/live parity

The single most important architectural property.

Strategy code is **identical** in backtest, walk-forward, paper and demo-live. Only the venue implementation changes:

```
Strategy ──► Signal ──► RiskEngine ──► Order ──► ExecutionVenue
                                                   ├── BacktestVenue  simulated fills, historical clock
                                                   ├── PaperVenue     live data, simulated fills
                                                   └── DemoVenue      Binance testnet
```

If a strategy could behave differently in backtest than in demo, every backtest result would be unfalsifiable — you could never distinguish "the strategy is bad" from "the harness differs". Parity is guaranteed structurally, by there being exactly one code path, rather than by discipline.

This is why the backtest engine is custom rather than adopted. `NautilusTrader` is a genuinely strong alternative — event-driven, Rust core, well maintained — and was seriously considered. It was rejected because adopting it means adopting its domain model: the risk engine and evolution engine would become plugins to its lifecycle rather than first-class components with authority over it. That trade-off is recorded in ADR 0005 and is open to revisit, not closed.

Vectorized engines (VectorBT, `bt`) were rejected outright for the core: they cannot express path-dependent risk logic — trailing stops reacting to intrabar state, portfolio-level kill switches — without leaking look-ahead.

---

## 5. Risk is structural, not advisory

A strategy emits a `Signal`:

```python
direction: Literal["long", "short", "flat"]
conviction: Decimal          # 0..1
horizon: timedelta
invalidation: Decimal | None # price at which the thesis is wrong
rationale: str
```

It says *what it believes and what would prove it wrong*. It says nothing about size.

The risk engine alone constructs orders. It owns position sizing, exposure limits, correlation-aware netting, drawdown limits and veto authority. A strategy has no import path to order construction, enforced by `import-linter`.

**Why this is worth the rigidity.** A strategy that sizes its own positions can bankrupt the portfolio regardless of how good its signals are. More pointedly: this system will eventually write its own strategies via LLM agents, and an LLM-authored strategy will absolutely attempt to size its own positions if the type system permits it. The constraint has to be structural because the author will not be a human who read this document.

Requiring an explicit `invalidation` level forces every strategy to state in advance what would falsify it. A strategy that cannot answer that has a hope, not a thesis.

---

## 6. Data platform

**Storage split.** PostgreSQL + TimescaleDB is the single operational datastore — relational state and time-series hypertables in one engine, rather than operating Postgres *and* ClickHouse for a single-node workload. Bulk historical bars additionally land as partitioned Parquet on disk, queried in-process by DuckDB for backtest scans. Neither choice requires a server we do not already run.

**Point-in-time semantics are mandatory.** A feature value computed at time *t* must be reproducible using only data that existed at *t*. Look-ahead bias is the most dangerous defect class in the project because it does not fail — it makes bad strategies look excellent. A dedicated adversarial test attempts to leak future data and must fail closed.

**Availability contract.** The feature store declares which data actually exists. Strategies cannot request data we do not have. This matters because of a hard constraint discovered in research: **free full-depth L2 order book history does not exist.** Binance `bookDepth` is not snapshots — it is aggregated depth bands sampled about once per minute. The zero-budget ceiling is tick trades, top-of-book on futures, and coarse depth bands. Rather than letting a strategy silently assume richer data, the feature store refuses.

**Three verified ingestion traps** (see `DATA_PIPELINE.md`): spot timestamps switched to microseconds from 2025-01-01 while futures stayed in milliseconds; futures kline CSVs have a header row and spot ones do not; spot trade files serialize booleans Python-style. Normalization is keyed on `(market, date)`, never on a global constant, and every archive is checksum-verified before it is trusted.

---

## 7. Execution

Binance testnet, via `ccxt` (>= 4.5.70). Bybit testnet is the fallback, reachable through the same abstraction.

`ccxt` was chosen because it is currently the only client correct on both the endpoint split and the post-`listenKey` user-data model. `python-binance` is broken for spot user data. `binance-connector` is frozen. The official `binance-sdk-*` packages shipped 11 and 16 major versions in roughly twelve months, which is disqualifying for unattended operation.

**Two user-data code paths behind one interface.** Spot `listenKey` is dead — `POST /api/v3/userDataStream` returns 410 Gone everywhere. Spot now requires a WebSocket `session.logon` handshake with **Ed25519 keys**. Futures `listenKey` still works. These are genuinely different mechanisms and are modelled as such.

**Reconciliation is a first-class feature, not a nicety.** Binance spot testnet wipes roughly every 30 days without notice: keys survive, balances and open orders vanish. The system must be able to rebuild its entire view of the world from the exchange at any moment. Exchange state is the source of truth; local state converges to it.

---

## 8. Safety kernel

The demo-only guarantee, implemented structurally.

- A `frozenset` of permitted hosts compiled into `fking.platform.safety` — not read from config, environment, database or file
- `guarded_client()` validates the host on **every request**, not only at construction, because base URLs can be overridden per call
- Startup resolves configured endpoints and aborts if any is not allowlisted; the allowlist is logged at every boot
- `import-linter` forbids `execution` from importing `httpx`, `aiohttp`, `websockets` or `requests` directly
- A non-venue host gets a **second** literal and a **second** client, never an entry in the trading set: `ARCHIVE_HOSTS` + `guarded_archive_client()` for `data.binance.vision`, credential-free, unimportable from `execution`. Two egress paths that cannot reach each other's hosts, rather than one list with an extra host on it (ADR 0017)
- No override exists. No flag, no environment variable, no `--force`

The threat model is not malice. It is a config edit, a copied environment variable, an agent generating its own HTTP client, or a library changing a default base URL in a minor bump. A guardrail living in configuration defends against none of those, because configuration is precisely what changes.

---

## 9. Agent layer

LLM agents sit **on top of** the deterministic core, never inside it.

**No agent output is trusted directly.** Agents propose; deterministic gates dispose. An agent may propose a strategy, but the validation gate decides whether it lives. It may propose a thesis, but the risk engine decides the position. Every output is parsed into a schema-validated typed structure; an unparseable response is a failure rather than something to interpret charitably.

An LLM in the order path is an unbounded-risk design. An LLM in the hypothesis path is a research accelerator. This is the second.

**Provider abstraction.** Gemini free tier primary, Groq free tier fallback, behind a gateway owning routing, failover, quota accounting, caching, structured-output enforcement and full prompt/response audit logging. Free-tier quotas are a real architectural constraint: agent scheduling is quota-aware, and quota exhaustion degrades to deterministic-only operation rather than stalling.

**Memory in three tiers** — working (ephemeral), episodic (append-only, Postgres), semantic (distilled lessons via `pgvector`). Conflating them is the standard failure. Writes are append-only so an agent cannot rewrite history to flatter itself.

---

## 10. Evolution

Strategies compete; weak ones retire; strong ones reproduce.

The central difficulty is stated plainly in `EVOLUTION_ENGINE.md`: **an automated search over strategy space is a machine for producing overfit results.** Run enough configurations against fixed history and some will look excellent by chance alone. The defences — global trial counting feeding a deflated Sharpe ratio, combinatorial purged cross-validation, a permanently held-out period that is burned once touched, champion/challenger promotion requiring forward performance, and minimum sample sizes — are the actual feature. The mutation operators are the easy part.

The survival score deliberately is not profit. It weighs risk-adjusted return, drawdown discipline, cross-regime consistency, per-trade edge after costs, capacity, and out-of-sample decay, and treats **risk-limit violations as a hard negative**. A strategy that made money by breaching limits scores worse than one that made less within them. That difference is encoded in the objective function rather than in documentation, because the system optimizes what it measures.

---

## 11. Observability

OpenTelemetry → Collector → Prometheus (metrics), Loki (logs), Tempo (traces), Grafana (dashboards, provisioned as code). All OSS, self-hosted in the same Compose stack, zero cost.

The governing requirement: **any trade must be fully reconstructable from the audit log alone**, months later, with no access to application memory — what data existed, what features were computed, which strategy version and lineage fired, what risk decided and why, which agent reasoning contributed with exact prompt and response, what was sent, what came back, and the slippage against decision price.

That is a design constraint on every module, which is why correlation IDs and append-only audit tables are P0 work rather than a final polish phase.

---

## 12. Technology summary

| Concern | Choice | Rationale |
|---|---|---|
| Backend | Python 3.12 | The entire quant/ML/exchange ecosystem is Python |
| Dependencies | uv | One fast resolver, real lockfile, reproducible CI |
| API | FastAPI + Pydantic v2 | Models are the domain contract and the wire schema |
| Datastore | PostgreSQL 16 + TimescaleDB | One engine for relational and time-series |
| Analytics | Parquet + DuckDB | Columnar scans with no server |
| Event bus | Redis Streams | Kafka semantics at near-zero ops cost |
| Exchange client | ccxt | Only library correct on current Binance reality |
| Orchestration | Docker Compose | K8s unjustified for single-node, zero-budget |
| Scheduling | APScheduler + GH Actions cron | Temporal needs its own server and DB |
| Telemetry | OpenTelemetry stack | Vendor-neutral, self-hosted, free |
| LLM | Gemini free + Groq fallback | Most generous free tiers; abstracted for swap |
| Dashboard | Next.js 15, TS, Tailwind, shadcn/ui | Standard, fast to build, free to host locally |
| Tests | pytest, Hypothesis, testcontainers | Property tests for risk math are essential |

---

## 13. What this architecture assumes, and when to revisit

- **Single node is enough.** True while the strategy population is small and timeframes are minutes, not microseconds. If sub-second execution ever matters, the modular boundaries make extraction possible — but the honest answer is that this system is not built for latency arbitrage and should not pretend to be.
- **Free tiers hold.** Quota limits are an architectural constraint, and the agent scheduler is built around them. If a provider withdraws its free tier, the gateway abstraction absorbs the swap.
- **Testnet remains available and free.** Spot testnet requires only GitHub OAuth. Bybit is the fallback if that changes.
- **The evolution engine's defences are sufficient.** This is the assumption most likely to be wrong, and the one to watch hardest. If evolved strategies consistently outperform in validation and underperform forward, the scoring engine is lying and takes priority over everything else.
