# financeKing

An autonomous AI trading platform that researches markets, generates strategies, proves or rejects them, and trades them under risk control — **on demo accounts only.**

> **Demo only, structurally.** This system cannot trade real money. The set of permitted exchange hosts is a compiled-in constant, validated on every request, with no configuration key, environment variable or command-line flag that can widen it. Enabling real trading would require editing source code and merging a pull request. See [`SECURITY.md`](SECURITY.md).

---

## What this is

Not a trading bot. A bot executes a fixed rule.

This system decides *what rules should exist*: it forms hypotheses, turns them into strategies, subjects them to adversarial validation, sizes the survivors under a risk engine that can veto anything, executes on Binance testnet, and retires strategies when their edge decays.

The interesting engineering is not in generating strategies — that part is easy and mostly produces garbage. It is in **rejecting them correctly.** An automated search over strategy space is a machine for manufacturing results that look excellent and are noise. Most of this repository exists to say "no" convincingly:

- Every configuration ever evaluated is charged to a global, monotone trial counter that survives restarts and generations, feeding a **deflated Sharpe ratio**
- Validation is **combinatorial purged cross-validation** with purge and embargo, not a train/test split
- A **permanently held-out period** is burned the moment it is read
- Promotion requires **forward** performance, gathered after the specification hash was frozen
- The survival score treats **risk-limit violations as a hard negative**, so a strategy that made money by breaching limits scores worse than one that made less within them

## Status

Early. The operating system, architecture and roadmap are complete; implementation is underway.

Work is tracked in [GitHub Issues](https://github.com/ismetcahangirov/financeKing/issues), organised into eight phase milestones, P0 through P7. See [`ROADMAP.md`](ROADMAP.md) for the critical path.

## Architecture in one diagram

```
   data  ──►  strategy  ──►  risk  ──►  execution  ──►  evolution
    │           Signal        Order       Fill          survival score
    │        (conviction,   (sizing,   (Binance        (promote / retire)
    │         invalidation)  veto)      testnet)
    │
    └── point-in-time features, availability contract
```

Two structural guarantees hold this together:

1. **Strategies emit `Signal`, never `Order`.** Position sizing and veto authority belong entirely to the risk engine. A strategy has no import path to order construction, enforced by `import-linter` in CI — because this system will eventually write its own strategies, and the author will not be a human who read the documentation.

2. **Backtest and live share one code path.** Only the `ExecutionVenue` swaps between `BacktestVenue`, `PaperVenue` and `DemoVenue`. If a strategy could behave differently in backtest than in demo, every backtest result would be unfalsifiable.

Full reasoning in [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Stack

Python 3.12 · FastAPI · PostgreSQL 16 + TimescaleDB · Redis Streams · Parquet + DuckDB · ccxt · Docker Compose · OpenTelemetry → Prometheus / Loki / Tempo / Grafana · Next.js 15 dashboard

Everything runs locally at zero cost. Every technology choice is recorded with its rejected alternatives in `docs/adr/`.

## Getting started

Requires Docker, Docker Compose, and Python 3.12.

```bash
git clone https://github.com/ismetcahangirov/financeKing.git
cd financeKing
cp .env.example .env      # then fill in credentials
make up                   # bring up the local stack
make check                # lint, types, module boundaries, tests
```

Binance spot testnet keys are obtained at [testnet.binance.vision](https://testnet.binance.vision) via GitHub OAuth — no Binance account and no KYC required, which is what makes this viable at zero budget. Generate **Ed25519** keys: spot user-data streams now require a `session.logon` handshake, and the old `listenKey` endpoint returns 410 Gone.

Full setup in [`DEPLOYMENT.md`](DEPLOYMENT.md).

## Documentation

**Start here**

| | |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | The operating manual. Read before contributing. |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | System structure and why it is shaped this way |
| [`ROADMAP.md`](ROADMAP.md) | Phases, dependencies, critical path |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Setup, development loop, definition of done |

**The intellectual core**

| | |
|---|---|
| [`RISK_PHILOSOPHY.md`](RISK_PHILOSOPHY.md) | Why risk sits structurally between signal and order |
| [`SCORING_ENGINE.md`](SCORING_ENGINE.md) | The objective function, with formulas |
| [`SURVIVAL_PROTOCOL.md`](SURVIVAL_PROTOCOL.md) | What "survival" means, and why it is not profit |
| [`EVOLUTION_ENGINE.md`](EVOLUTION_ENGINE.md) | Strategy lifecycle and the overfitting defences |
| [`BACKTEST_ENGINE.md`](BACKTEST_ENGINE.md) | Engine design and validation methodology |
| [`AI_MANIFEST.md`](AI_MANIFEST.md) | What the AI is and is not permitted to be |

**Engineering**

[`DATA_PIPELINE.md`](DATA_PIPELINE.md) · [`SOURCES.md`](SOURCES.md) · [`SECURITY.md`](SECURITY.md) · [`OBSERVABILITY.md`](OBSERVABILITY.md) · [`TESTING.md`](TESTING.md) · [`CODING_STANDARDS.md`](CODING_STANDARDS.md) · [`GIT_WORKFLOW.md`](GIT_WORKFLOW.md) · [`CODE_REVIEW.md`](CODE_REVIEW.md) · [`FAILSAFE.md`](FAILSAFE.md) · [`ERROR_RECOVERY.md`](ERROR_RECOVERY.md) · [`CONFIGURATION.md`](CONFIGURATION.md) · [`DEPLOYMENT.md`](DEPLOYMENT.md) · [`PERFORMANCE_GUIDE.md`](PERFORMANCE_GUIDE.md) · [`MEMORY_SYSTEM.md`](MEMORY_SYSTEM.md) · [`PROMPT_LIBRARY.md`](PROMPT_LIBRARY.md) · [`TOOLS.md`](TOOLS.md) · [`DECISION_FRAMEWORK.md`](DECISION_FRAMEWORK.md) · [`DOCUMENTATION_GUIDE.md`](DOCUMENTATION_GUIDE.md) · [`RELEASE_PROCESS.md`](RELEASE_PROCESS.md)

**[`docs/rules/`](docs/rules)** — 16 enforceable rules, one per invariant: the rule, the wrong version and the runtime failure it produces, the correct version, the mechanism that enforces it, and the single exception where one exists. Indexed in [`CLAUDE.md` §14](CLAUDE.md).

**The `.claude/` operating system** — 45 agent definitions, 24 slash commands, 12 workflows, 10 templates, 6 domain briefings, and durable project knowledge including a dated register of [verified facts](.claude/knowledge/verified-facts.md) and a [failure library](.claude/knowledge/failure-library.md) indexed by observable symptom.

## A note on honesty

Some things this project does not have, stated plainly so nobody builds on a false assumption:

- **No free full-depth L2 order book history exists.** Binance `bookDepth` is not snapshots — it is aggregated depth bands sampled roughly once per minute. The realistic ceiling here is tick trades, futures top-of-book, and coarse depth bands. The feature store refuses to serve what does not exist rather than letting a strategy silently assume it.
- **Testnet is not a faithful mirror.** Binance futures testnet showed a 7.5bp spread against production's 0.16bp and roughly 10x inflated reported volume. Cost model parameters are therefore calibrated from production market data, never from testnet observations.
- **This is not a low-latency system** and does not pretend to be. It operates on minutes, not microseconds.
- **Nothing here is investment advice, and no result is evidence of profitability.** It is a research system that trades on a demo account.

## License

MIT.
