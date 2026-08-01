# Glossary

The vocabulary of this project, with units and with the place in the code where each term is authoritative.

Two kinds of entry live here. **Domain terms** mean what they mean in the wider trading world, and the entry exists to pin the definition this project uses when the wider world is loose. **Project terms** exist only here, and the entry is the definition — if the code and this file disagree about a project term, the code wins and this file is a bug.

Where a term needs a full mental model rather than a definition, the entry is short and points at the briefing in [`../contexts/`](../contexts/). This file is for looking a word up mid-task, not for learning the domain.

Terms are grouped, not alphabetised, because the groupings are how you actually need them.

---

## 1. The core objects

| Term | Definition | Units | Authoritative in |
|---|---|---|---|
| **Signal** | What a strategy emits. Carries `direction`, `conviction`, `horizon`, `invalidation`, `rationale`. **Says nothing about size.** | — | `fking.domain` |
| **Order** | An instruction to a venue. Constructed **only** by the risk engine — a strategy has no import path to it. | — | `fking.domain`, built in `fking.risk` |
| **Fill** | An execution report: quantity, price, fee, timestamp, order reference. Immutable and append-only. Partial fills are the normal case. | `Decimal` | `fking.domain` |
| **Position** | Net holding in one instrument. Frozen; transitions return a new `Position`. | `Decimal` | `fking.domain` |
| **ExecutionVenue** | The one thing that differs between backtest, paper and demo-live. `BacktestVenue`, `PaperVenue`, `DemoVenue`. | — | `fking.execution` |
| **direction** | `Literal["long", "short", "flat"]`. Not a number, so it cannot be accidentally multiplied by a quantity. | — | `fking.domain` |
| **conviction** | Strategy confidence, `Decimal` in `[0, 1]`. **An input to sizing, never a size.** The risk engine decides what conviction 0.8 is worth. | ratio 0–1 | `fking.domain` |
| **invalidation** | The price at which the strategy's thesis is wrong. A mandatory field. A strategy that cannot state it has a hope, not a thesis. | `Decimal`, quote currency | `fking.domain` |
| **horizon** | How long the thesis is expected to take to play out. A `timedelta`, and the basis for the purge and embargo in cross-validation. | duration | `fking.domain` |
| **rationale** | Free text from a strategy or agent. Stored, displayed, **never parsed and never acted on**. | — | `fking.domain` |

## 2. Safety

| Term | Definition |
|---|---|
| **Safety kernel** | `fking.platform.safety`. The module that makes demo-only structural rather than aspirational. 100% coverage floor. See [`../rules/safety-kernel.md`](../rules/safety-kernel.md). |
| **Allowlist** | A `frozenset` of permitted hosts **compiled into source**. Not config, not environment, not database, not file. Widening it requires a source edit and a PR labelled `safety:critical`. |
| **`guarded_client()`** | The only sanctioned way to construct an HTTP or WebSocket client. Validates the host on **every request**, not only at construction, because base URLs can be overridden per call. |
| **`SafetyViolation`** | The exception raised when the allowlist is breached. **Never caught anywhere.** An `except` clause naming it is a defect. |
| **`safety:critical`** | The PR label required for any change touching the allowlist or the kernel. Its purpose is friction. |
| **Demo-only** | The prime directive. No configuration value, environment variable, argument or feature flag enables production trading. Not "disabled by default" — absent. |

## 3. Time and data

| Term | Definition | Note |
|---|---|---|
| **`event_time`** | When the thing happened in the market. | Timezone-aware UTC, always. |
| **`available_at`** | When *we* could first have known it. | The field that makes point-in-time possible. Usually later than `event_time`, sometimes much later. |
| **`as_of`** | The mandatory query parameter on the feature store. Rows with `available_at > as_of` are physically unreachable. | Non-optional by design. See [`../rules/no-lookahead.md`](../rules/no-lookahead.md). |
| **Point-in-time** | A feature value at time *t* is reproducible using only data that existed at *t*. | The property, not a technique. |
| **Look-ahead** | Any leak of future information into a past decision. | Does not fail — makes bad strategies look excellent. The most dangerous defect class here. |
| **Feature store** | The declared inventory of what data exists, with earliest clean dates. **Refuses** requests for data we do not have rather than proxying. | `fking.data` |
| **Availability contract** | The declaration a data source must satisfy before a strategy may depend on it. Template: [`../templates/data-source.md`](../templates/data-source.md). |
| **Decision price** | The price at the moment the decision was made. Slippage is measured against this, not against arrival price. | The difference is where self-deception lives. |
| **Correlation ID** | An identifier minted at the top of the data flow and carried through every event, log line and audit row to the fill and beyond. | What makes a trade reconstructable months later. |

## 4. Risk

Full treatment in [`../contexts/risk-vocabulary.md`](../contexts/risk-vocabulary.md). What follows is the disambiguation layer.

| Term | Definition | Units |
|---|---|---|
| **`base_quantity`** | Amount of the base asset. 0.5 of BTC in BTCUSDT. | base asset |
| **`notional_usd`** | Position value in quote currency. | USD |
| **Margin** | Collateral posted against a position. Not the position value. | USD |
| **Equity** | Account value including unrealised P&L. Moves continuously; balance does not. | USD |
| **Gross exposure** | Sum of absolute notionals. What you owe the market in total. | USD |
| **Net exposure** | Signed sum of notionals. What you owe the market directionally. | USD |
| **Drawdown** | Peak-to-trough decline in equity. | ratio or USD — say which |
| **Drawdown duration** | Time spent below the previous peak. The number that decides whether a strategy survives contact with a human. | duration |
| **Kill switch** | The mechanism that flattens the book and stops trading. Trips on unexpected state, not only on loss. | — |
| **Risk limit breach** | A **hard negative** in the survival score, not a warning. A strategy that made money by breaching limits scores worse than one that made less within them. | — |
| **`bps`** | Basis point, 1/100th of a percent. The unit for edges, costs and slippage. Never express an edge as a percentage — the extra decimal place is where 0.3bp gets mistaken for 30bp. | 1e-4 |

## 5. Validation and statistics

Full treatment in [`../contexts/statistics-for-trading.md`](../contexts/statistics-for-trading.md).

| Term | Definition |
|---|---|
| **Global trial counter** | The project-wide count of every configuration ever specified. **Monotone, never reset, charged at specification time**, covering the full declared grid even if the search is abandoned early. See [`../rules/overfitting-defences.md`](../rules/overfitting-defences.md). |
| **Trial charge** | The increment a piece of work adds to the counter. Charged at registration, before data access. |
| **Deflated Sharpe** | A Sharpe ratio corrected for the number of trials the project has run, its sample length, and the skew and kurtosis of returns. A bare Sharpe leaving any agent is a defect. |
| **PSR** | Probabilistic Sharpe ratio: the probability the true Sharpe exceeds a benchmark, given the observed sample. |
| **CPCV** | Combinatorial purged cross-validation. Multiple train/test group combinations, with purging and embargo, instead of one split. |
| **Purge** | Removing training observations whose label horizon overlaps the test set. Sized to the label horizon. |
| **Embargo** | Additionally excluding observations immediately after the test set, to block serial-correlation leakage backwards. |
| **Walk-forward** | Repeated train-then-test moving forward through time. The minimum acceptable evidence; a single split is not evidence. |
| **Held-out period** | A reserved window that is **burned the moment it is read**, including for a plot. Requires human authorisation. One read per milestone at most. |
| **Independent episode** | A genuinely independent occurrence of the setup. The sample size that counts. 41,208 hourly observations may be 37 episodes, and the gap is usually the whole story. |
| **Fold sign consistency** | The fraction of CPCV folds with the same sign of return. A better forward predictor than a headline Sharpe at our sample sizes. |
| **`spec_hash`** | A hash of the pre-registered specification. Re-checked at test time; a mismatch voids the result. |
| **HARKing** | Hypothesising After the Results are Known. A modified hypothesis is a new hypothesis with a new charge. |
| **Effective sample size** | The independent-observation count after accounting for autocorrelation and overlap. Always reported next to the raw count. |

## 6. Evolution and lifecycle

| Term | Definition |
|---|---|
| **Survival score** | The objective function. Weighs risk-adjusted return, drawdown discipline, cross-regime consistency, per-trade edge after costs, capacity and out-of-sample decay. **Deliberately not profit.** `SCORING_ENGINE.md`. |
| **Champion / challenger** | The promotion pattern: a challenger must beat the incumbent on **forward** out-of-sample performance, never on validation performance alone. |
| **Promotion gate** | The deterministic check that decides whether a strategy advances a lifecycle stage. Reads the global trial counter. Not overridable by a flag. |
| **P2 validation gate** | The validation stage that decides whether a proposed strategy lives. An agent may propose; this decides. |
| **Lineage** | A strategy's ancestry — parent, mutation applied, and the `correlation_id` of the hypothesis it descends from. What lets you ask why a strategy exists. |
| **Forward decay** | Live performance divided by validated performance. The only metric that grades the validation process honestly. |
| **Regime** | A period with distinct market character (trend, volatility, funding sign). An edge concentrated in one regime is a story about that regime. |
| **Retirement** | Lifecycle exit for a strategy whose thesis has been invalidated or whose forward performance has decayed. Not a failure — the intended end state of most strategies. |

## 7. Exchange and execution

Full treatment in [`../contexts/binance-testnet.md`](../contexts/binance-testnet.md) and [`../contexts/crypto-perpetuals.md`](../contexts/crypto-perpetuals.md).

| Term | Definition |
|---|---|
| **`listenKey`** | The classic Binance user-data mechanism. **Still works on futures. Dead on spot** — `POST /api/v3/userDataStream` returns 410 Gone (VF-002). |
| **`session.logon`** | The WebSocket API handshake that replaced `listenKey` for spot. Requires **Ed25519** keys, which are not interchangeable with HMAC keys. |
| **`userDataStream.subscribe`** | The WebSocket API call that follows a successful spot `session.logon`. |
| **Reconciliation** | Rebuilding the entire local view of the world from the exchange. A first-class feature, because spot testnet wipes about every 30 days (VF-005). Exchange state is the source of truth. |
| **Testnet wipe** | The periodic reset of spot testnet. Keys survive; balances and open orders do not. Detected by reconciliation, not by a calendar. |
| **`clientOrderId`** | A deterministically derived client-side order identifier. The exchange-side idempotency key — it is what stops a retried placement double-filling. |
| **`recvWindow`** | The tolerance Binance allows between your request timestamp and its clock. Clock drift presents as authentication failure. |
| **Mark price** | The price used for liquidation and unrealised P&L on perpetuals. **Not** the last traded price. The source of most "liquidated at a price that never traded" confusion. |
| **Funding rate** | The periodic transfer between longs and shorts that keeps a perpetual near spot. A transfer, not an exchange fee. A backtest ignoring it does not have that P&L line at all. |
| **Tick size / step size** | The minimum price increment and quantity increment for a symbol. Quantities round `ROUND_DOWN`; an order violating either is rejected, and rejection is a first-class case. |
| **`minNotional`** | The minimum order value a symbol accepts. Small-conviction signals hit this constantly. |

## 8. Platform and agents

| Term | Definition |
|---|---|
| **Event bus** | Redis Streams. **At-least-once delivery**, so every consumer is idempotent by design. See [`../rules/idempotency.md`](../rules/idempotency.md). |
| **Idempotency key** | A stable key derived from an event's semantic content — never from `uuid4()` at consumption time, never from the stream message id alone. |
| **Append-only audit** | Audit tables the application cannot rewrite, enforced by database grants and triggers plus a per-row hash chain. An audit log the application can rewrite is not an audit log. |
| **Gateway** | The single module through which any LLM provider is reached. Owns routing, failover, quota accounting, caching, structured-output enforcement and prompt/response audit logging. |
| **Quota ledger** | The persistent record of provider consumption, keyed by `(provider, model, window)`. Survives restarts, because an in-memory counter resets exactly when you are rate limited and restarting. |
| **Degraded mode** | Deterministic-only operation when LLM quota is exhausted. The **designed** behaviour of exhaustion, not an error path. |
| **Working memory** | Ephemeral, within one agent invocation. |
| **Episodic memory** | Append-only history of what an agent did, in Postgres. An agent cannot rewrite its own history to look better. |
| **Semantic memory** | Distilled lessons in `pgvector`. A valid entry states a measured relationship; "be careful of overfitting" is not a semantic memory, it is a slogan. |
| **Forbidden decisions** | The explicit list of things an agent may never decide. **Matters more than the allowed list**, and is expected to be longer. |
| **Escalation path** | Where an agent goes when it is out of its authority — normally a `needs-human` GitHub issue. |

## 9. Words this project bans

Each of these is ambiguous in a way that has a plausible reading which is wrong by orders of magnitude. Enforced by [`../rules/naming.md`](../rules/naming.md).

| Banned | Because | Say instead |
|---|---|---|
| `size` | Base quantity? Notional? Margin? Byte count? | `base_quantity`, `notional_usd` |
| `price` | Bid, ask, mid, mark, index, last, decision? | `quote_price`, `mark_price`, `decision_price` |
| `amount` | `ccxt` uses it for base quantity; most humans read it as money | `base_quantity` or `quote_notional_usd` |
| `qty` | Abbreviated and unitless | `base_quantity` |
| `pnl` | Realised or unrealised? Which currency? Gross or net of fees? | `realised_pnl_usd`, `unrealised_pnl_usd` |
| `timeout` | Seconds or milliseconds | `timeout_seconds` |
| `time` | Event time or availability time | `event_time`, `available_at` |
| `fee` | Base or quote currency | `fee_quote_usd` |
| `slippage` | Absolute or relative, against which reference | `slippage_bps` |
| `return` | Simple, log, gross, net, per period? | `log_return`, `net_return_bps` |

---

## Adding a term

Add it to the group it belongs to, not to the end. Give the unit if it has one, and give the place in the code that is authoritative if it is a project term.

Do not add a term you had to look up in order to define. Add the term, then write the paragraph in [`../contexts/`](../contexts/) that would have saved you the lookup, and point the glossary entry at it.
