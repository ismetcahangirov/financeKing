# Open Questions

Things this project needs to know and does not. Each one is a question that has come up, been recognised as load-bearing, and not yet been answered.

**Why this file exists.** An unrecorded open question gets rediscovered as a bug. A recorded one gets answered on purpose. The second-order benefit matters more: this file tells you which of the project's current behaviours are running on an *assumption* rather than on a [verified fact](./verified-facts.md), which is exactly what you need to know when something behaves strangely.

## Rules for this file

1. Every entry states **what would answer it** — a specific observation, experiment or document. A question with no answering method is not a question, it is a worry, and it belongs in a design note.
2. Every entry states **what the code currently assumes** in the absence of an answer, and whether that assumption is conservative. An open question with an aggressive default is a live risk.
3. Every entry has an **owner or a blocker**. "Nobody" is a valid blocker and should be written down as such.
4. When a question is answered, move the answer to [`./verified-facts.md`](./verified-facts.md), then mark the entry here `ANSWERED -> VF-NNN` and leave it. The trail from question to fact is worth keeping.
5. Questions that turn out to be unanswerable get marked `CLOSED — unanswerable`, with the reason. That is a real finding and it stops the next person spending a week on it.

Status values: **open** · **blocked** · **in-progress** · **ANSWERED -> VF-NNN** · **CLOSED — unanswerable**

---

## Index

| ID | Question | Status | Blocker | Issue |
|---|---|---|---|---|
| OQ-001 | Actual free-tier quota limits for Gemini and Groq | **open** | needs an API key per provider | [#19](https://github.com/ismatjahangirov/financeKing/issues/19) |
| OQ-002 | Which quant libraries to adopt, and which to refuse | **open** | none — the remainder is a code task | [#19](https://github.com/ismatjahangirov/financeKing/issues/19) |
| OQ-003 | Lifetime and renewal semantics of the spot Ed25519 `session.logon` session | open | none | — |
| OQ-004 | Exact cadence and trigger of the spot testnet wipe | open | requires ~90 days of observation | — |
| OQ-005 | Earliest clean date per symbol on `data.binance.vision`, beyond BTCUSDT | open | none — mechanical work | — |
| OQ-006 | Whether funding-rate history is available back to each perpetual's listing | open | none | — |
| OQ-007 | Whether passive fill probability can be estimated at all without L2 | **blocked** | VF-017 — data does not exist | — |
| OQ-008 | Whether the evolution engine's overfitting defences are sufficient | open | needs forward outcomes; months of data | — |
| OQ-009 | Bybit testnet's equivalent limits, wipe policy and user-data model | open | not urgent until Binance fails | — |
| OQ-010 | Whether `NautilusTrader` should be revisited for the backtest core | open | needs a concrete pain point first | ADR 0005 |
| OQ-011 | Realistic capacity ceiling for the strategies this system produces | open | needs production depth data | — |
| OQ-012 | Whether Timescale compression can be enabled without breaking point-in-time reads | open | none | — |
| OQ-013 | Whether Cerebras or Mistral train on free-tier input | open | none — read their terms | [#19](https://github.com/ismatjahangirov/financeKing/issues/19) |

---

## OQ-001 — What are the actual free-tier quota limits for Gemini and Groq?

- **Status**: open · **Opened**: 2026-08-01 · **Last worked**: 2026-08-03 · **Tracked as**: GitHub issue **#19**
- **Blocker**: **an API key per provider.** The 2026-08-03 pass ([`../../docs/research/free-tier-landscape.md`](../../docs/research/free-tier-landscape.md)) closed the documentary half and could not close the measured half, because measuring a quota requires a live key and that is an account signup.
- **What changed on 2026-08-03**: Groq's limits are now known and cited (VF-022) — and are *lower than this project's own declared agent budget*, which is a finding rather than a footnote. Google and Mistral **stopped publishing free-tier limits entirely** (VF-021), so for those two there is no document to read and measurement is the only route that will ever exist. The commonly quoted `gemini-2.5-flash` figures (10 RPM / 250,000 TPM / 250 RPD) come from forum posts and are **unverified**; do not let them into config as though they were sourced. Separately, the question grew a dimension nobody costed: Gemini's free tier trains on the input and Groq's contractually does not (VF-020), so "which provider" is no longer only a quota question.
- **Why it matters**: free-tier quota is not an operational detail here, it is an architectural constraint ([`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) §9). Agent scheduling is quota-aware, and quota exhaustion is supposed to degrade the system to deterministic-only operation rather than stall it. Sizing that behaviour against numbers nobody checked means the degradation path is untested against reality — and the one moment it matters is the one moment you cannot afford it to be wrong.
- **What would answer it**: for each provider and model actually used —
  - requests per minute, requests per day, tokens per minute, tokens per day, and whether the day boundary is UTC or account-local;
  - whether the limits are per key, per project, or per account;
  - the exact `429` response shape and whether `Retry-After` is populated;
  - whether input and output tokens are counted against the same budget;
  - whether the free tier is rate-limited or hard-capped (throttle vs refusal), which changes the degradation design entirely.
  These are all directly measurable with a probe script against a throwaway key, which is the fastest route — vendor documentation for free tiers changes without notice and is a weaker source than measurement.
- **What the code assumes meanwhile**: conservative configured defaults, with the **quota ledger measuring reality** rather than trusting the configuration ([`../rules/quota-management.md`](../rules/quota-management.md)). The ledger is the authority; the configured number is a guess that the ledger corrects. This is the right shape regardless of how OQ-001 resolves, so the work is not wasted — but it is *tuned* wrong until this closes.
- **Assumption is conservative?** Yes, by construction. The risk is wasted headroom, not a breach.

## OQ-002 — Which quantitative libraries should this project adopt, and which should it refuse?

- **Status**: open · **Opened**: 2026-08-01 · **Last worked**: 2026-08-03 · **Tracked as**: GitHub issue **#19**
- **Blocker**: none. What remains is a code task in #40, not a research task.
- **What changed on 2026-08-03**: the library survey is done ([`../../docs/research/free-tier-landscape.md`](../../docs/research/free-tier-landscape.md) §4) and it resolved in the least convenient direction. **`mlfinlab` has no installable release and its repository has been untouched since 2023 (VF-025)** — the López de Prado toolchain named here as the primary candidate is simply not available. `pandas-ta` is refused for the same class of reason and TA-Lib's Docker objection turned out to be obsolete (VF-026). `statsmodels`, `arch`, `scikit-learn`, `lightgbm` and `polars` are adopted; `quantstats` is admitted for human-facing tearsheets only and barred from computing anything that reaches the survival score. What is left open is exactly the risk this entry identified first: with no reference implementation to check against, every promotion-gating statistic must agree numerically with a hand-computed worked example from its source paper.
- **Why it matters**: the backtest engine is deliberately custom ([`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) §4), but the *statistics* around it — deflated Sharpe, combinatorial purged CV, block bootstrap, stationarity tests, effective sample size — are exactly the kind of thing that is wrong when hand-rolled and hard to notice. A subtly wrong deflated Sharpe implementation would corrupt every promotion decision the system ever makes, and it would do so in the flattering direction, because a bug in a penalty term almost always understates the penalty.
- **Candidates that need evaluation, not adoption**: the López de Prado toolchain (CPCV, purging, embargo, deflated Sharpe), `statsmodels` for time-series tests, `arch` for volatility models and bootstrap, `scipy.stats`, and whatever currently exists for probabilistic Sharpe. Each needs: maintenance status, dependency weight, whether it forces a `float`/`ndarray` boundary that conflicts with [`../rules/decimal-and-money.md`](../rules/decimal-and-money.md), and whether its implementation of a formula matches the paper it cites — the last one requires reading source, not README.
- **What would answer it**: an evaluation note per library against those four criteria, plus a numerical agreement test against a hand-computed worked example for every statistic we intend to rely on. Agreement with a known-good example is the acceptance criterion; popularity is not.
- **What the code assumes meanwhile**: statistics used in a promotion decision are implemented in-project, tested against worked examples with published values, and every such implementation is flagged for re-verification when this closes.
- **Assumption is conservative?** No. This is the entry on this page most likely to be hiding a real defect.

## OQ-003 — What is the lifetime of a spot Ed25519 `session.logon` session, and what renews it?

- **Status**: open · **Opened**: 2026-08-01
- **Why it matters**: the futures `listenKey` path has an explicit keepalive with a documented expiry (VF-004). The spot WebSocket-API session (VF-003) has session state, but we have not established whether it expires on a timer, on idleness, on server-side rotation, or not at all — and whether re-authenticating requires tearing down the socket or can be done in place.
- **Failure shape if we get it wrong**: user data events stop arriving with no error and no disconnect. Position state silently goes stale while orders continue to work. That is the worst combination available, because the system keeps trading on a view of the account that stopped updating.
- **What would answer it**: hold a logged-on spot session idle for 1h / 6h / 24h with a heartbeat-only workload and record when events stop; separately, test whether `session.logon` can be re-issued on a live connection. Roughly a day of elapsed time and almost no attention.
- **What the code assumes meanwhile**: a defensive full re-handshake on a fixed interval, plus a **liveness watchdog** — if no user-data event and no heartbeat has arrived within a bounded window, tear down and reconnect rather than waiting. Reconciliation against the exchange on every reconnect makes an unnecessary reconnect cheap and a missed one survivable.
- **Assumption is conservative?** Yes.

## OQ-004 — Exactly how often does the spot testnet wipe, and what triggers it?

- **Status**: open · **Opened**: 2026-08-01
- **Why it matters**: VF-005 establishes "roughly every 30 days" and that is enough to design for — reconciliation is unconditional. A precise cadence would only buy the ability to *anticipate* a wipe, which is a convenience, not a correctness property.
- **What would answer it**: record the observed timestamp of every wipe in the audit log with an explicit event type. After three or four observations the cadence is either evident or evidently irregular. This is passive: the detection already has to exist.
- **What the code assumes meanwhile**: a wipe can happen at any moment, is detected by reconciliation rather than by a calendar, and is a normal event rather than an incident.
- **Assumption is conservative?** Yes — deliberately more conservative than the fact requires.

## OQ-005 — What is the earliest clean date for each symbol on `data.binance.vision`?

- **Status**: open · **Opened**: 2026-08-01
- **Why it matters**: VF-013 establishes 2017-08-17 for BTCUSDT 1m. Every other symbol starts later, and a hypothesis inherits the **shortest** history among its inputs. Assuming the BTC start date for a multi-symbol study silently shortens the usable window or, worse, produces a survivorship-shaped universe of exactly the symbols that happen to have long histories.
- **What would answer it**: mechanical enumeration — walk the archive index per symbol per market, record first and last available date, and separately record the first date that passes integrity checks (which may be later than the first date that exists). Both numbers go in the availability contract.
- **What the code assumes meanwhile**: the feature store refuses a request for a window it cannot serve rather than truncating it silently, so the failure is loud. Nothing depends on an assumed start date.
- **Assumption is conservative?** Yes.

## OQ-006 — Is funding-rate history available back to each perpetual's listing date?

- **Status**: open · **Opened**: 2026-08-01
- **Why it matters**: funding is a P&L line item, not a decoration ([`../contexts/crypto-perpetuals.md`](../contexts/crypto-perpetuals.md)). A backtest over a window where funding history is absent is not a backtest of a perpetuals strategy; it is a backtest of a strategy that pays no carry. And funding-based hypotheses are among the most plausible mechanisms available to this project, so the answer bounds a whole category of research.
- **What would answer it**: for the intended symbol universe, fetch funding history and compare its first timestamp to the symbol's first kline timestamp. Note whether early history uses a different settlement interval — the 8-hour convention is not universal across a contract's life.
- **What the code assumes meanwhile**: funding is a declared feature with its own earliest clean date, and a strategy that uses it inherits that date. No window is extended on the assumption that funding was zero before the data starts.
- **Assumption is conservative?** Yes.

## OQ-007 — Can passive fill probability be estimated at all without L2 data?

- **Status**: **blocked** · **Opened**: 2026-08-01
- **Blocker**: VF-017. The data required does not exist at zero budget, and `bookDepth` bands sampled once a minute cannot answer a question about queue position.
- **Why it matters**: it is the difference between a maker cost assumption and a taker one, which for a high-turnover strategy is the difference between an edge and nothing. It is also the single most tempting unfalsifiable input in the project: assuming maker fills makes marginal strategies pass, and nothing in the data can contradict the assumption.
- **What would answer it**: only richer data — a paid L2 feed, or a long period of self-collected top-of-book with our own resting orders as probes. The second is possible in principle on testnet and worthless in practice, because testnet is not a market (VF-008).
- **What the code assumes meanwhile**: **taker execution**, always, unless a maker assumption is separately evidenced. Cost models are calibrated from production trade data.
- **Assumption is conservative?** Yes, and deliberately so — it is the assumption that rejects strategies rather than the one that accepts them.

## OQ-008 — Are the evolution engine's overfitting defences actually sufficient?

- **Status**: open · **Opened**: 2026-08-01
- **Why it matters**: [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) §13 names this as **the assumption most likely to be wrong in the entire system**. If evolved strategies consistently validate well and underperform forward, the scoring engine is lying, and every decision built on it is void.
- **What would answer it**: the ratio of live Sharpe to validated Sharpe across a population of promoted strategies, aggregated over enough promotions to mean something. Nothing else answers it. In particular, more validation does not answer it — the defences cannot audit themselves.
- **What the code assumes meanwhile**: the defences are treated as necessary but not sufficient. Forward decay is tracked as a first-class metric from the first promotion, not added later. A sustained decay ratio below the target is an escalation, not a data point.
- **Assumption is conservative?** Unknown — which is the point of the entry. This resolves on months of forward data and cannot be shortcut.

## OQ-009 — What are Bybit testnet's limits, wipe policy and user-data model?

- **Status**: open · **Opened**: 2026-08-01
- **Why it matters**: Bybit testnet is the declared fallback venue ([`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) §7, §13). A fallback nobody has characterised is a hope with a name. If Binance testnet access changes, we would be discovering Bybit's equivalents of VF-002 through VF-009 under time pressure.
- **What would answer it**: the same probe suite that produced VF-001 to VF-009, pointed at Bybit. The work is already specified by the Binance entries.
- **What the code assumes meanwhile**: the venue interface is shaped by Binance's two-user-data-path reality, which is the more complex case, so a simpler venue fits. That is an argument, not evidence.
- **Assumption is conservative?** Partially. The abstraction is probably adequate; the operational knowledge is definitely absent.

## OQ-010 — Should `NautilusTrader` be revisited for the backtest core?

- **Status**: open · **Opened**: 2026-08-01 · **Reference**: ADR 0005
- **Why it matters**: ADR 0005 rejected it — not on quality, but because adopting it means adopting its domain model, which would demote the risk engine and evolution engine to plugins of its lifecycle rather than components with authority over it. The ADR explicitly records the decision as open to revisit rather than closed.
- **What would answer it**: a concrete, recurring pain point in the custom engine that Nautilus would remove, weighed against whether the risk engine could retain order-construction authority inside its lifecycle. Absent a real pain point, revisiting is speculation, and speculation is how a working engine gets replaced by an unfamiliar one.
- **What the code assumes meanwhile**: the custom engine, with backtest/live parity guaranteed structurally by there being exactly one code path.
- **Assumption is conservative?** Yes — the cost of being wrong is engineering time, not correctness.

## OQ-011 — What is a realistic capacity ceiling for the strategies this system produces?

- **Status**: open · **Opened**: 2026-08-01
- **Why it matters**: an edge that dies at small notional is not an edge for anyone, and capacity is a term in the survival score. We cannot currently estimate it well, because impact estimation without depth data is guesswork (VF-017, OQ-007).
- **What would answer it**: production depth-band data plus a participation-rate model, calibrated on real traded volume — and an explicit statement of the confidence interval, which will be wide. A wide honest interval is usable; a narrow fabricated one is not.
- **What the code assumes meanwhile**: capacity is scored on a coarse participation-rate proxy and reported with its assumption stated, never as a single number without provenance.
- **Assumption is conservative?** Unclear, which is worth flagging: a participation proxy calibrated on reported volume inherits any wash-trading inflation in that volume.

## OQ-012 — Can TimescaleDB compression be enabled without breaking point-in-time reads?

- **Status**: open · **Opened**: 2026-08-01
- **Why it matters**: compression is the obvious answer to hypertable growth, and it changes the mutability characteristics of compressed chunks. Two things must be checked before enabling it: that late-arriving or revised rows can still be written correctly, and that nothing about compression interferes with the append-only guarantees on audit tables ([`../rules/append-only-audit.md`](../rules/append-only-audit.md)) or with `available_at` filtering ([`../rules/no-lookahead.md`](../rules/no-lookahead.md)).
- **What would answer it**: enable compression on a copy of a hypertable, then run the existing look-ahead probe and the audit-immutability tests against it. If both pass unchanged, the answer is yes.
- **What the code assumes meanwhile**: compression is off. Storage is not currently a constraint, so this is cheap to defer.
- **Assumption is conservative?** Yes.

## OQ-013 — Do Cerebras and Mistral train on free-tier input?

- **Status**: open · **Opened**: 2026-08-03 · **Tracked as**: GitHub issue **#19**
- **Why it matters**: VF-020 and VF-022 together produce a genuine bind. The provider that will not train on our prompts (Groq) has a per-minute token ceiling too low for a full-size agent call; the provider with the headroom (Gemini) trains on the input and puts human reviewers on it. Every way out of that bind runs through a *third* provider that is both non-training and large enough — and Cerebras and Mistral are the two candidates whose data terms this project has not read. If either is non-training with a workable ceiling, the data-classification design in the research report §7.1 becomes much cheaper, because fewer prompts have to be squeezed to fit.
- **What would answer it**: read Cerebras's and Mistral's terms of service and data-processing pages the way Groq's Services Agreement §4.2 was read — looking for an explicit prohibition on training, not for the absence of a permission. Silence is not a prohibition, and a page that does not mention training is an unanswered question rather than a negative answer. An hour of reading.
- **What the code assumes meanwhile**: both may train. No prompt containing strategy logic goes to either.
- **Assumption is conservative?** Yes — it forbids rather than permits, and the cost of being wrong is unused headroom.

---

## Adding an entry

Append with the next `OQ-NNN`, add a row to the index, and state: the question as a heading, the date it was opened, the blocker if any, why it matters (the decision it affects), what would answer it (specific and executable), what the code assumes in the meantime, and whether that assumption is conservative.

The last field is the one people skip and the one that matters. An open question with a conservative default is a backlog item. An open question with an aggressive default is a live risk, and writing that down is how it gets prioritised.
