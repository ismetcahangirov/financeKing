# Tools

The tools available to runtime LLM agents, each with its contract, permissions, rate limits and failure behaviour.

These are the tools that agents inside `src/fking/agents/` may call at runtime. They are not development tooling and not the `Makefile` targets in `CLAUDE.md` §12.

---

## 1. The tool contract

Every tool is declared once, in code, with a typed signature. An agent sees the declaration; it never sees an escape hatch.

```python
class ToolSpec(BaseModel):
    name: str
    purpose: str
    input_model: str                  # Pydantic model name
    output_model: str                 # Pydantic model name
    permission: Literal["read", "propose", "escalate"]
    idempotent: Literal[True]                 # every tool. see §2
    reversible: Literal[True]                 # every tool. see §2
    max_calls_per_invocation: int
    max_calls_per_day: int
    timeout_seconds: float
    on_timeout: Literal["fail_call", "return_partial_flagged"]
    on_failure: str                           # the deterministic consequence
    audited: Literal[True]                    # every call, every time
```

Three fields are `Literal[True]` and cannot be otherwise. That is the design, not an accident of the current tool set — see §2.

Every tool call writes an episodic row with the correlation ID, the agent, the arguments, the result summary, the latency and the outcome. A tool call that is not audited did not happen, as far as reconstruction is concerned, and `OBSERVABILITY.md` §1 requires that agent contributions be reconstructable.

---

## 2. Permissions and the irreversibility principle

### The principle

> **A tool an agent can use to take an irreversible action must not exist.**

Not "must be guarded". Not "must require confirmation". **Must not exist.** The tool registry is the boundary, and the boundary is enforced by absence.

The reasoning is structural rather than about trust. An agent operating unattended, at 3am, on a quota-limited free tier, with a prompt that may have drifted, in a system edited mostly by other agents, will eventually call every tool it has. Every guard placed *inside* a tool is a guard that some future refactor, some prompt change, or some plausible-looking exception handler can defeat. A tool that does not exist cannot be called by any prompt, any injection, any bug, or any refactor.

This is the same reasoning that puts the host allowlist in a compiled-in `frozenset` rather than in configuration (`SECURITY.md` §3). Configuration is what changes; code that is absent is what stays absent.

### The three permission levels

| Level | Meaning | Examples |
|---|---|---|
| **`read`** | Observes state. Changes nothing | Query bars, read features, retrieve memory, read a backtest result |
| **`propose`** | Writes an artefact that a **deterministic gate** must accept before it has any effect | Submit a hypothesis, propose a strategy definition, request a backtest |
| **`escalate`** | Creates a record for a human. Takes no action itself | Open a `needs-human` issue, raise a blocking finding |

There is no fourth level. There is no `execute`, no `act`, no `admin`.

### What `propose` actually means

A `propose` tool writes a **row in a queue**, not a change to system state. Nothing downstream reads that row without a deterministic gate in between:

```
agent ──propose──► candidate row ──► deterministic gate ──► effect
                                          │
                                          └── rejection, recorded, no effect
```

`ARCHITECTURE.md` §9: an agent may propose a strategy, but the validation gate decides whether it lives; it may propose a thesis, but the risk engine decides the position; it may propose a parameter change, but the promotion gate decides whether it applies.

The consequence is that **every agent action is reversible by not acting on it.** A bad proposal that is rejected costs a queue row and some tokens. There is no proposal whose mere existence changes anything.

### Every tool is idempotent

Calling a tool twice with identical arguments produces the identical result and no additional effect. Not as a nicety — because the event bus is at-least-once (`CLAUDE.md` §2), because agents retry, and because a re-ask is banned but a re-invocation after a timeout is not.

`propose` tools achieve this with a deterministic content key: proposing the same hypothesis twice updates the same candidate row rather than creating two.

### What agents cannot reach at all

No tool exists for any of these. There is no guarded version, no admin variant, no test-only variant:

- Placing, modifying or cancelling an order
- Setting or changing a position size, notional or leverage
- Promoting, retiring, enabling or disabling a strategy
- Changing any risk limit, threshold or ceiling
- Reading, writing or reaching around the host allowlist
- Making an arbitrary HTTP or WebSocket request
- Executing shell commands, or reading or writing arbitrary files
- Reading secrets, key material or credentials
- Mutating or deleting an audit or memory row
- Reading or writing the permanently held-out period
- Modifying its own prompt, budget, timeout or tool set
- Disabling an alert, a gate, a check or the kill switch

The last two are the ones an unconstrained agent optimising for its own success would reach for first, which is precisely why they are absent rather than restricted (`AI_MANIFEST.md` §6).

---

## 3. Budgets, rate limits, timeouts, failure

### Budgets are per-agent and per-invocation

Each tool declares `max_calls_per_invocation` and `max_calls_per_day`. Exceeding either **fails the call**, does not queue it, and does not wait. The agent receives a typed error it can reason about, and its output is still expected to be schema-valid.

Budgets exist for two different reasons that happen to have the same mechanism:

- **Cost.** Free-tier quotas are an architectural constraint (`ARCHITECTURE.md` §9).
- **Loop prevention.** An agent that can call a query tool 400 times has a way to spend an entire budget on a search it will not summarise. The per-invocation cap is the real defence against runaway tool loops, and it is a number rather than a heuristic.

### Timeouts

Every tool has one. `on_timeout` is either `fail_call` or `return_partial_flagged`; there is no `retry_forever` and no unbounded wait.

`return_partial_flagged` is used only where a partial result is genuinely interpretable — a market-data query that returns the first N rows with `truncated=True`. It is never used where partial means "possibly wrong", because a flag an agent may ignore is not a safeguard.

### Failure behaviour is deterministic, always

> **Every tool failure has a documented deterministic consequence, and no tool failure ever blocks the trading path.**

The system never waits for an agent, and therefore never waits for an agent's tool. If a tool fails, the agent fails; if the agent fails, the deterministic default applies and the fact that it applied is recorded (`PROMPT_LIBRARY.md` §7).

### Rate limits

Rate limits are enforced by the tool layer, not by the agent's cooperation. A limit an agent must respect is a limit an agent will exceed while being helpful.

---

## 4. Data and research tools

### `query_bars` — `read`

Read OHLCV bars for a symbol, market and interval over a window.

| | |
|---|---|
| **Input** | `BarQuery(market, symbol, interval, start, end, max_rows)` — all datetimes tz-aware UTC, range half-open `[start, end)` |
| **Output** | `BarSeries(rows, coverage, gaps, truncated, as_of)` — prices as **decimal strings**, never floats |
| **Permission** | `read` |
| **Limits** | 20/invocation, 200/day, `max_rows` capped at 5,000 |
| **Timeout** | 30s, `return_partial_flagged` |
| **Failure** | Returns `BarSeries` with `coverage="unavailable"` and an explicit reason. Never an empty series that looks like a quiet market |

**The non-obvious constraint:** the query is served from the **feature store's point-in-time view**, not from raw storage, and it enforces `end <= as_of`. An agent reasoning about a historical window cannot request bars past the analysis timestamp. Without this, the single most likely agent-authored bug — asking for "the last 30 days" while analysing a period two years ago — silently becomes look-ahead, and the resulting hypothesis will backtest beautifully.

Gaps are returned explicitly and are never filled (`DATA_PIPELINE.md` §4).

### `query_trades` — `read`

Tick trades over a window. Same shape as `query_bars`, with `max_rows` capped at 20,000 and an aggregation parameter, because a raw tick window will exhaust an agent's token budget before it produces anything.

Returns aggregated summaries by default (`volume`, `trade_count`, `buy_ratio`, `vwap` per bucket). Raw ticks require an explicit flag and a narrower window.

### `get_feature` — `read`

Read a named feature at or over a timestamp range.

| | |
|---|---|
| **Input** | `FeatureQuery(name, version, symbol, at \| range, as_of)` |
| **Output** | `FeatureSeries(values, feature_version, lookback, point_in_time_proof)` |
| **Permission** | `read` |
| **Limits** | 30/invocation, 300/day |
| **Timeout** | 20s, `fail_call` |
| **Failure** | Raises `FeatureUnavailable` **with the list of features that do exist** |

**The refusal is the feature.** `DATA_PIPELINE.md` §8: the store refuses rather than returning something adjacent. An agent asking for `order_book_imbalance_top_10_levels` — which it will, because that feature exists in its training data — gets a refusal naming the actual ceiling: tick trades, futures top-of-book, ~1-minute aggregated depth bands. A usable refusal redirects the research; a `KeyError` does not.

`point_in_time_proof` is returned with the data so an agent reasoning about a feature can see how it is guaranteed non-anticipating.

### `list_available_data` — `read`

The availability contract: markets, symbols, datasets, earliest dates, known gaps, resolutions, declared features with versions and lookbacks.

Cheap, cacheable, and the tool agents are told to call **first**. Most bad hypotheses are bad because they assume data that does not exist, and this tool is how that gets discovered in one call instead of five.

### `query_news` — `read`

Free news and macro sources through the gateway.

| | |
|---|---|
| **Permission** | `read` |
| **Limits** | 5/invocation, 40/day |
| **Timeout** | 15s, `fail_call` |
| **Failure** | Returns `NewsResult(items=[], available=False, reason=...)`. Never fabricates; an absent source is stated |

**Every returned item is wrapped in untrusted-content delimiters before it reaches a model** (`PROMPT_LIBRARY.md` §4). A headline is attacker-influenceable text, and it is attacker-influenceable by accident long before anyone does it on purpose. This wrapping happens in the tool, not in the prompt, so an agent cannot receive an unwrapped headline by writing its prompt differently.

The tool goes through `guarded_client()` like everything else. A news host not on the allowlist is a rejection, not a fetch.

---

## 5. Memory tools

Contracts follow `MEMORY_SYSTEM.md`; this section is the agent-facing surface.

### `recall` — `read`

Retrieve semantic lessons and episodic rows relevant to a query.

| | |
|---|---|
| **Input** | `RecallQuery(query_text, scope, k=5, min_similarity=0.75, include_expired=True)` |
| **Output** | `RetrievalResult(items, query_embedding_model, total_candidates, filtered_by)` |
| **Permission** | `read` |
| **Limits** | 10/invocation, 200/day |
| **Timeout** | 10s, `fail_call` |
| **Failure** | Falls back to exact search and **reports degraded latency explicitly**. Never silently degrades to keyword matching — the agent's reasoning depends on knowing what it saw |

`k` is capped at 20 regardless of what the agent requests. Retrieval returning 40 lessons is retrieval that will be skimmed, and it costs a third of the agent's token budget to be skimmed.

Every item carries `evidence_count`, `age_days` and `status`. Expired lessons are returned **flagged**, never dropped. `total_candidates` tells the agent whether it saw a slice or the set.

### `record_observation` — `propose`

Append an episodic row.

| | |
|---|---|
| **Input** | `ObservationDraft(kind, payload, correlation_id, evidence_refs)` |
| **Output** | `EpisodicRow` as written, with its DB-assigned `row_id` and `created_at` |
| **Permission** | `propose` |
| **Limits** | 20/invocation, 200/day |
| **Timeout** | 10s, `fail_call` |
| **Failure** | Rejects with a reason. The reason is itself recorded |

`created_at` is assigned by the database. A client clock must not be able to reorder history.

**There is no `update_memory` tool and no `delete_memory` tool.** Corrections are new rows carrying `supersedes`. An agent asking to fix a previous observation writes a correction; an agent asking to remove one gets a refusal citing the rule.

### `propose_lesson` — `propose`

Nominate a semantic lesson for promotion. It does **not** write to semantic memory.

| | |
|---|---|
| **Input** | `LessonDraft(claim, falsifier, scope, evidence_row_ids)` |
| **Output** | `PromotionOutcome(accepted, reason, existing_similar_lesson_id \| None)` |
| **Permission** | `propose` |
| **Limits** | 3/invocation, 20/day |
| **Timeout** | 15s, `fail_call` |
| **Failure** | Rejects with the specific unmet criterion |

The gate is deterministic and checks the criteria in `MEMORY_SYSTEM.md` §5: evidence count and independence, falsifier present and checkable, scope no broader than the evidence supports, no unaddressed contradiction, and a `review_after` date.

A draft whose evidence rows do not resolve is rejected. **An agent cannot cite evidence it did not receive** — the tool verifies the row ids exist and were returned to that agent in that invocation.

Rejections are the valuable half of this tool's output. They are the record of what the system tried to remember and was not allowed to, and a pattern of one agent repeatedly attempting to launder an opinion into semantic memory is only visible in that record.

---

## 6. Analysis tools

### `request_backtest` — `propose`

Queue a backtest. Does not run one synchronously and does not return a result.

| | |
|---|---|
| **Input** | `BacktestRequest(strategy_id, version, params, window, symbols, interval, cost_model_version)` |
| **Output** | `QueuedRun(run_id, queue_position, trials_consumed_estimate)` |
| **Permission** | `propose` |
| **Limits** | 2/invocation, 10/day |
| **Timeout** | 10s (queueing only), `fail_call` |
| **Failure** | Rejects with the reason: window overlaps the held-out period, trial budget exhausted, symbol unavailable, cost model provenance invalid |

Two properties an agent cannot influence:

**Every requested run consumes trials against the global ledger**, and the ledger counts failed and crashed runs (`CONFIGURATION.md` §12). An agent cannot search cheaply by requesting many runs, because every request deflates the Sharpe of whatever it eventually reports.

**The held-out period is unreachable.** A window overlapping it is rejected at the tool boundary, before anything reads a byte. Burning it is the user's decision, taken once, in source (`BACKTEST_ENGINE.md` §6.3).

The daily cap of 10 is deliberately low. An agent that wants 40 backtests wants a search, and searches go through the evolution engine with its trial accounting, not through an agent's tool budget.

### `get_backtest_result` — `read`

Read a completed result by `run_id`.

Returns the full `BacktestResult` including `credibility`, `audit_findings`, `trials_at_time_of_run` and `deflated_sharpe`. **A `not_credible` result is returned with its findings and without its equity curve** — the same suppression the tearsheet applies (`BACKTEST_ENGINE.md` §8), for the same reason. An agent shown a beautiful curve alongside "this result is not credible" will reason about the curve.

`sharpe` is never returned without `trials_at_time_of_run` and `deflated_sharpe` alongside it. The fields are on one model precisely so that no caller can read one without the others.

### `compute_statistic` — `read`

A closed set of statistical functions over data the agent already retrieved: descriptive statistics, correlation, autocorrelation, stationarity tests, rolling moments, distribution fits.

| | |
|---|---|
| **Permission** | `read` |
| **Limits** | 20/invocation, 200/day |
| **Timeout** | 30s, `fail_call` |

**Closed set, not arbitrary code.** There is no `eval`, no expression language, no user-supplied formula. An arbitrary-computation tool is an arbitrary-code-execution tool with a friendlier name, and the set of statistics a research agent actually needs is small and enumerable.

Its real value is preventing a different failure: an agent asked for a correlation will otherwise **state a number from its own reasoning**, and that number will be plausible and wrong. Making the computation a tool call makes the number checkable and puts it in the audit trail.

---

## 7. Escalation tools

### `escalate_to_human` — `escalate`

| | |
|---|---|
| **Input** | `Escalation(severity, category, summary, evidence_row_ids, recommended_action)` |
| **Output** | `EscalationRecord(issue_url, escalation_id)` |
| **Permission** | `escalate` |
| **Limits** | 3/invocation, 20/day |
| **Timeout** | 20s, `fail_call` |
| **Failure** | Writes the escalation to the audit table and emits a `page`-severity alert. **An escalation is never lost because a tool failed** |

Opens a GitHub issue labelled `needs-human`. It takes no action, changes no state, and stops nothing — it creates a record.

The daily cap is real, and it is there because **an agent that escalates everything has produced alert fatigue**, which `OBSERVABILITY.md` §8 treats as a safety failure in its own right. An agent hitting its escalation cap is itself an escalation, raised by the deterministic layer.

**`recommended_action` is required.** An escalation without one is a notification, and notifications train people to dismiss them.

### `report_finding` — `escalate`

Register a structured finding — a security concern, a data-quality issue, a suspected leak — against a specific artefact.

Findings state the **exploit path or failure sequence, not the category.** "Cost model looks wrong" is not actionable. "`run_id=4f2a` has `cost_model_calibration_source='binance_futures_testnet_2026-05'`, which voids it; nine other runs share that cost model version" is.

A `critical` finding blocks the artefact it names from progressing, deterministically, without waiting for a human.

---

## 8. Tools that will never exist

Listed explicitly so that the absence is a documented decision rather than an oversight someone helpfully corrects.

| Proposed tool | Why not |
|---|---|
| `place_order` | Irreversible. The entire architecture exists to keep an LLM out of the order path (`AI_MANIFEST.md` §4) |
| `cancel_order` | Sounds safe; is not. Cancelling a protective stop is irreversible in effect |
| `set_position_size` | Sizing is the risk engine's sole authority (`RISK_PHILOSOPHY.md`) |
| `adjust_risk_limit` | A limit an agent can raise is not a limit |
| `promote_strategy` / `retire_strategy` | Deterministic gates dispose. An agent nominates; it does not decide |
| `http_request` | Would bypass `guarded_client()` by construction, defeating the safety kernel |
| `run_shell` / `read_file` / `write_file` | Arbitrary code and arbitrary filesystem access. Nothing an agent legitimately needs requires either |
| `update_memory` / `delete_memory` | Append-only is the property that makes memory trustworthy (`MEMORY_SYSTEM.md` §4) |
| `read_held_out_data` | It is burned on read. No tool may burn it |
| `modify_own_prompt` | Severs replay and lets an agent optimise away its own constraints (`PROMPT_LIBRARY.md` §2) |
| `disable_alert` / `silence_check` | A monitored system where the monitored party controls the monitoring is unmonitored |
| `request_more_budget` | Budget is a ceiling. An agent negotiating its own ceiling does not have one |
| `execute_sql` | An `UPDATE` away from breaking audit immutability, and no read case justifies it that a typed query tool cannot serve |

The last row is the one that comes up most, usually as "read-only SQL for research". The answer is the same as for read-only mainnet access in `CLAUDE.md` §11: **read paths become write paths during refactors.** A `execute_sql(readonly=True)` tool is one parameter default away from not being read-only, and the parameter will be defaulted differently by someone in a hurry.

---

## 9. Adding a tool

1. Write the `ToolSpec` first, including `on_failure`, and get it reviewed before any implementation.
2. Answer: **can any sequence of calls to this tool cause an irreversible effect?** If yes, the tool does not get built. Not guarded — not built.
3. Answer: **what does the deterministic layer do when this tool fails?** If the answer is "the agent retries" or "we wait", the design is wrong.
4. Write the golden cases, including at least one where the correct behaviour is **not** calling the tool. Agents over-call tools that exist.
5. Add the tool's cap to the agent's budget, and check the total against its token budget.
6. Add an injection probe if the tool returns any external text.
7. Add the audit row shape and confirm the correlation ID propagates through the call.

`CLAUDE.md` §3 applies here too: an abstraction requires two concrete callers before it exists. A tool built for one agent's anticipated future need is a tool that widens the agent surface for nothing.

---

## 10. Cross-references

| For | See |
|---|---|
| What the AI system is permitted to be | `AI_MANIFEST.md` |
| Prompt-side forbidden decisions and how to write them | `PROMPT_LIBRARY.md` §6 |
| Memory tier semantics behind `recall` and `propose_lesson` | `MEMORY_SYSTEM.md` |
| The availability contract behind `get_feature` | `DATA_PIPELINE.md` §8 |
| Trial accounting behind `request_backtest` | `BACKTEST_ENGINE.md` §6 |
| The safety kernel that makes `http_request` impossible | `SECURITY.md` §3 |
| Agent budgets, timeouts, degradation | `CONFIGURATION.md` §9, `PROMPT_LIBRARY.md` §7 |
| Why risk has sole authority over sizing | `RISK_PHILOSOPHY.md` |
