# Decisions Log

The index of every load-bearing decision this project has made, with the alternative it rejected.

**This is not a replacement for `docs/adr/`.** ADRs are the full argument, immutable once accepted, and they are where a decision is made. This log is the **index** — one screen you can read in two minutes to know what has already been settled, so that a session with no memory of the last one does not reopen a closed question, and so that a proposal can be checked against the record before it is written up.

The single most valuable column is **"Rejected alternative"**. A decision recorded without the path not taken is a preference. A decision recorded with it is an argument you can attack, which is the only kind worth keeping.

## Rules for this file

1. **Append only.** A superseded decision is marked `SUPERSEDED by D-NNN` and left in place, exactly as ADRs are. The record of what we used to think is what lets you understand code written under the old belief.
2. **Every entry names the alternative and why it lost.** "We chose X" with no rejected alternative is not an entry.
3. **Every entry names its revisit trigger** — the observable condition under which reopening it is legitimate. Without one, every decision is permanently reopenable by anyone who finds it inconvenient, which is the same as having decided nothing.
4. Decisions expanded in [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) or [`../../CLAUDE.md`](../../CLAUDE.md) are **canonical there**. This log points at them and must not contradict them.
5. Where no ADR number appears, the decision is recorded in the canonical document named in the entry. Write the ADR when the decision is next argued about — an ADR written for a decision nobody is questioning is filing, not thinking.

Status: **active** · **SUPERSEDED by D-NNN** · **open to revisit** (a decision that was made *and* explicitly flagged as provisional)

---

## Index

| ID | Decision | Rejected alternative | Status | Canonical source |
|---|---|---|---|---|
| D-001 | Demo-only, enforced by a compiled-in allowlist | A configuration flag or environment guard | active | `CLAUDE.md` §0 |
| D-002 | `Decimal` from `str` for all money | `float`, or `Decimal` from numeric literals | active | `CLAUDE.md` §2 |
| D-003 | Timezone-aware UTC everywhere | Naive datetimes normalised by convention | active | `CLAUDE.md` §2 |
| D-004 | Immutable domain objects | Mutable objects with careful discipline | active | `CLAUDE.md` §2 |
| D-005 | Strategies emit `Signal`; risk alone builds `Order` | Strategies size their own positions | active | `ARCHITECTURE.md` §5 |
| D-006 | Modular monolith | Microservices | active | `ARCHITECTURE.md` §2 |
| D-007 | Custom backtest engine | `NautilusTrader` | **open to revisit** | ADR 0005 |
| D-008 | No vectorized engine in the core | VectorBT, `bt` | active | `ARCHITECTURE.md` §4 |
| D-009 | PostgreSQL 16 + TimescaleDB as the single operational store | Postgres *and* ClickHouse | active | `ARCHITECTURE.md` §6 |
| D-010 | Parquet + DuckDB for bulk historical scans | Everything in Timescale | active | `ARCHITECTURE.md` §6 |
| D-011 | Redis Streams as the event bus | Kafka | active | `ARCHITECTURE.md` §12 |
| D-012 | `ccxt` >= 4.5.70 as the exchange client | `python-binance`, `binance-connector`, official `binance-sdk-*` | active | `ARCHITECTURE.md` §7 |
| D-013 | Two user-data code paths behind one interface | One unified user-data abstraction | active | `ARCHITECTURE.md` §7 |
| D-014 | Reconciliation as a first-class feature | Trusting local state between restarts | active | `ARCHITECTURE.md` §7 |
| D-015 | Cost models calibrated from production data only | Calibrating on testnet, which is free and convenient | active | `CLAUDE.md` §2 |
| D-016 | Append-only audit enforced by the database | Application-level append-only discipline | active | `CLAUDE.md` §2 |
| D-017 | Idempotent consumers by design | Exactly-once delivery semantics | active | `CLAUDE.md` §2 |
| D-018 | LLM agents on top of the core, never inside it | An LLM in the order path | active | `ARCHITECTURE.md` §9 |
| D-019 | Gemini free tier primary, Groq fallback, behind a gateway | A single provider, called directly | active | `ARCHITECTURE.md` §9 |
| D-020 | Survival score, not profit, as the objective | Return-based ranking | active | `ARCHITECTURE.md` §10 |
| D-021 | Global, monotone trial counting charged at `max(declared, executed)` per specification | Per-study counts; execution-only counting; declaration-only counting | active | `.claude/rules/overfitting-defences.md` |
| D-022 | Property-based tests mandatory for risk and position math | Example-based tests only | active | `CLAUDE.md` §5 |
| D-023 | Real Postgres in tests; exchange mocked from recorded responses | Mocked database; hand-written fixtures | active | `CLAUDE.md` §5 |
| D-024 | Timestamp normalization keyed on `(market, date)` | A global or per-market unit constant | active | ADR 0013 |
| D-025 | Feature store refuses unavailable data | Returning a proxy or a best-effort substitute | active | `ARCHITECTURE.md` §6 |
| D-026 | Docker Compose, single node | Kubernetes | active | `ARCHITECTURE.md` §12 |
| D-027 | APScheduler + GitHub Actions cron | Temporal | active | `ARCHITECTURE.md` §12 |
| D-028 | Two concrete callers before an abstraction exists | Designing the interface first | active | `CLAUDE.md` §3 |
| D-029 | `import-linter` contracts as executable architecture | Documented conventions and code review | active | `ARCHITECTURE.md` §2 |
| D-030 | Self-hosted OpenTelemetry stack, instrumented from P0 | A hosted APM, or instrumenting later | active | `ARCHITECTURE.md` §11 |
| D-031 | Kill switch flattens on trip, sized from venue state | Cancel-only with positions left open; flatten-or-cancel per trigger class | active | ADR 0014 |

---

## The entries that carry the most weight

Most rows above are adequately argued in their canonical document. These five are the ones where the reasoning is most often forgotten and most often re-litigated, so they are expanded here.

### D-001 — Demo-only, enforced by a compiled-in allowlist

**Rejected**: a configuration flag, an environment variable guard, or a "production mode" that ships disabled.

**Why it lost**: the threat model is not malice. It is a config edit, a copied environment variable, an agent generating its own HTTP client, or a library changing a default base URL in a minor version bump. A guardrail living in configuration defends against none of those, **because configuration is precisely the thing that changes**. Compiling the allowlist into source means widening it requires a source edit and a reviewed PR labelled `safety:critical` — friction that is deliberate and is the single most important property of the system.

**Revisit trigger**: none. This one does not have a revisit trigger, and the absence is intentional. If you are constructing an argument for revisiting it, the argument is the symptom. See [`../rules/safety-kernel.md`](../rules/safety-kernel.md).

### D-005 — Strategies emit `Signal`; the risk engine alone constructs `Order`

**Rejected**: letting strategies size their own positions, which is how nearly every trading framework works and is what any experienced practitioner will expect.

**Why it lost**: a strategy that sizes its own positions can bankrupt the portfolio regardless of how good its signals are. The decisive argument is forward-looking rather than defensive — **this system will eventually write its own strategies via LLM agents, and an LLM-authored strategy will absolutely attempt to size its own positions if the type system permits it.** The constraint must be structural because the author will not be a human who read the documentation. Enforced by `import-linter`: `strategy` has no import path to `execution` or to order construction.

**Revisit trigger**: none foreseen. A strategy that "needs" to size itself has misunderstood the design; the correct move is to enrich `Signal` (conviction, horizon, invalidation) so the risk engine has what it needs.

### D-007 — Custom backtest engine over `NautilusTrader` — **open to revisit**

**Rejected**: `NautilusTrader`, which was seriously considered and is genuinely strong — event-driven, Rust core, well maintained.

**Why it lost**: adopting it means adopting its domain model. The risk engine and the evolution engine would become plugins to its lifecycle rather than first-class components with authority over it. That inverts D-005 and D-020, which are the two decisions the whole architecture is organised around.

**Why this entry is flagged provisional**: ADR 0005 explicitly records the decision as open to revisit rather than closed. Reopening requires a concrete recurring pain point in the custom engine that Nautilus would remove, plus a demonstration that the risk engine retains order-construction authority inside its lifecycle. Absent that, reopening is speculation. Tracked as [`./open-questions.md`](./open-questions.md) OQ-010.

**Revisit trigger**: a named, recurring engine defect that Nautilus's design would have prevented.

### D-015 — Cost models calibrated from production data only

**Rejected**: calibrating on testnet, which is free, convenient, already connected, and requires no extra code.

**Why it lost**: measurement. Binance futures testnet showed a **7.5bp** spread against production's **0.16bp**, with roughly **10x inflated volume** ([`./verified-facts.md`](./verified-facts.md) VF-008). Roughly 47x the spread and an order of magnitude of fake volume means a testnet-calibrated model is fitted to a market that does not exist — and the error is not conservative in both directions. Inflated volume flatters every participation-rate and capacity assumption, so a strategy can look tradeable at a size the real book cannot absorb.

Testnet is an **execution-plumbing** environment: it proves your order was formed, signed and acknowledged correctly. It proves nothing about price.

**Revisit trigger**: none. Even if testnet liquidity improved, calibrating on it would be a coincidence rather than a method.

### D-021 — Global, monotone trial counting, charged at specification time

**Rejected**: per-study trial counts, charged when a configuration is actually executed. This is what everyone does and it feels obviously correct.

**Why it lost**, in three parts:

- **Charging at specification, not execution.** If you charge on execution, a declared 200-point grid abandoned after 12 points charges 12 — and the deflation understates by the 188 you *would* have run had the first 12 looked worse. The selection pressure was applied at specification. Charge there.
- **Global, not per-study.** The deflation correction depends on how many things the **project** has tried, because that is the population the best-looking result was selected from. This means every test anyone runs makes every future result harder to prove, which is the correct incentive and is why the counter appears in every report.
- **Monotone forever.** No expiry, no reset on refactor, no "those trials were on different data". If the counter could be reset, it would be, and the deflation would become decorative.

The behavioural corollary is the point: **a hypothesis with a large parameter grid is expensive to everyone, forever.** So the right design is one or two parameters fixed a priori from a mechanism, tested once. If you cannot fix a parameter from theory, that is evidence you do not have a mechanism — and a hypothesis without a mechanism is a search.

**Revisit trigger**: none. Exempting "exploratory" runs is the exact hole that makes the whole defence ornamental. See [`../rules/overfitting-defences.md`](../rules/overfitting-defences.md).

### D-031 — The kill switch flattens on trip, sized from venue state

**Rejected**: cancel resting orders and leave positions open — which `FAILSAFE.md` §2.4 previously argued for, at length and well.

**Why it lost**: two documents in this repository stated opposite defaults, so one had to go. The argument that settled it was not about slippage but about **consistency**: `.claude/rules/error-handling.md` already has the supervisor flatten the book on any unhandled exception, which is the least-understood state the system can reach. A kill switch that left positions open would make the response to maximum uncertainty depend on which code path happened to notice it. Second, cancel-only's premise — *stop making it worse and let a human decide* — requires a human inside the window a crypto position can move in, and this system runs unattended.

**What survived the rejection**: cancel-only's sharpest objection was not about execution quality. It was that several triggers indicate our *position record itself* is untrustworthy, so closing orders sized from it can open a position rather than close one. That is correct, and it is why the flatten reads quantities from the venue and refuses to proceed at all when the venue cannot be read — halting with positions open and paging, rather than guessing.

**Revisit trigger**: median flatten slippage above 50bp across ten consecutive trips, or the flatten's realised loss exceeding the drawdown that triggered it in three of them. Observed on `killswitch.flatten_slippage_bps`. Then reconsider the per-trigger-class variant with incident data rather than prediction. Full argument in ADR 0014.

---

## Adding an entry

Append a row with the next `D-NNN`. State the decision in the active voice, name the alternative that was actually considered and why it lost, and give the revisit trigger. Point at the canonical document; do not restate its argument here unless the entry is one of the load-bearing few, in which case expand it below with the same structure as the five above.

If the decision changes an existing entry, do not edit the old row. Add a new one and mark the old `SUPERSEDED by D-NNN`.

If the decision is architecturally significant, write the ADR too — [`../templates/adr.md`](../templates/adr.md) — and put its number in the row. The ADR is where the argument lives; this is where you find out the argument happened.
