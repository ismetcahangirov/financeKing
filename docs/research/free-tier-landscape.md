# Free-Tier Landscape

**Investigation**: #19 · **Fetched**: 2026-08-03 · **Supersedes**: nothing — this is the first completed pass. The 2026-08-01 attempt was cut short by a session limit and produced nothing on these four areas (OQ-001, OQ-002).

---

## 0. How to read this document

Every number below carries a source URL and the date it was fetched. Where a number could not be obtained from an official source it says **`unverified`** and names the probe that would obtain it. Nothing here is estimated, interpolated, or carried over from a blog post that quoted a vendor page.

Three confidence levels are used, matching [`.claude/knowledge/verified-facts.md`](../../.claude/knowledge/verified-facts.md):

| Level | Means here |
|---|---|
| **measured** | This project issued the request and observed the response. Reproduced in §4 and §3 with status codes and timings. |
| **documented** | Read from the vendor's own current documentation on 2026-08-03, not from a third party. |
| **unverified** | Neither. Stated as a gap, never as a number. |

**The one thing to know before reading further.** OQ-001 says measurement beats documentation for free-tier quotas, and it is right. **This investigation could not measure the LLM quotas**, because measuring requires a live API key per provider and that is an account signup this task may not perform. Everything in §1 is therefore `documented` at best. The consequence is deliberate and is the recommendation this document carries: OQ-001 stays **open**, downgraded from *nothing is known* to *documented, not measured*, and the quota ledger stays the authority over the configured number ([`.claude/rules/quota-management.md`](../../.claude/rules/quota-management.md)). Closing OQ-001 on a vendor page would be the false completion `CLAUDE.md` §7 forbids.

What *was* measured is everything in §3 and §4 — package metadata and data-source endpoints are reachable without credentials, so those are observations rather than readings.

---

## 1. Headline findings

Six findings change a decision. They are stated here in full because a reader who stops after this section should still have got the load-bearing parts.

### 1.1 The Gemini free tier trains on what you send it. Groq contractually may not.

This is the single most consequential finding in the investigation, and it was not on the list of things anyone expected to be decisive.

Google's pricing page marks every free-tier Gemini model **"data used to improve products: Yes"**, and the API terms say plainly:

> "Google uses the content you submit to the Services and any generated responses to provide, improve, and develop Google products and services and machine learning technologies"
>
> "human reviewers may read, annotate, and process your API input and output"
>
> "Do not submit sensitive, confidential, or personal information to the Unpaid Services."

Groq's Services Agreement §4.2 says the opposite, as a contractual commitment rather than a setting:

> "For the avoidance of any doubt and to the extent permitted by applicable law, Groq is not permitted to use Inputs or Outputs for training or fine-tuning any AI Model Services or other models, unless explicitly granted permission or instructed by Customer."

and its data page adds *"By default, Groq does not retain customer data for inference requests"*, with Zero Data Retention available to **all** customers, and troubleshooting/abuse logs kept *"up to 30 days"*.

ADR-0009 makes Gemini free tier **primary** and Groq the **fallback**. It was decided on quota and availability grounds, and the data question was never raised in it. It is raised now: this system's agents are sent market context, hypothesis statements, strategy specifications and critique of both. That is the entire research output of the project, and on the free Gemini tier it is training data with human reviewers attached. See §6.1 for the recommendation.

### 1.2 Google no longer publishes free-tier rate limits at all.

The Gemini rate-limits page has stopped carrying a per-model free-tier table. It now says only:

> "Rate limits depend on a variety of factors (such as your usage tier) and can be viewed in Google AI Studio."

with a link to a per-account page behind a login. Two things follow, and the second is the important one:

1. The commonly quoted figures for `gemini-2.5-flash` free tier (10 RPM / 250,000 TPM / 250 RPD) appear only in community forum posts, not on any Google page this investigation could fetch. They are recorded in §2.1 as `unverified` and must not be copied into configuration as though they were sourced.
2. **A vendor who does not publish a number cannot be held to it, and cannot be assumed to have kept it.** This is not an inconvenience to route around — it is a direct confirmation that `.claude/rules/quota-management.md` had the right shape before the research: the ledger measures reality and the configured limit is a conservative floor that the ledger corrects. That design is now the only correct design for Gemini, not merely a defensive one.

### 1.3 Groq's free tier cannot serve this project's declared agent token budget. At all.

Groq publishes its free-tier limits per model, and the binding constraint is **tokens per minute**, not requests:

| Model | TPM (free) |
|---|---|
| `llama-3.3-70b-versatile` | 12,000 |
| `openai/gpt-oss-120b` | 8,000 |
| `openai/gpt-oss-20b` | 8,000 |
| `llama-3.1-8b-instant` | 6,000 |

`.claude/agents/quant.md` declares a token budget of **≤ 45k tokens per invocation**. No Groq free-tier model can accept a single call of that size — the request is larger than the per-minute allowance, so it cannot succeed by waiting. Either the agent's context budget comes down to fit under 8k, or Groq is not a fallback for that agent and the failover is a fiction on exactly the path where it matters.

This is a *design* finding rather than a tuning one: a fallback provider that cannot run the primary's workload is not a fallback, and discovering that at 03:00 when Gemini is degraded is the worst available time.

### 1.4 Structured output on Groq is schema-enforced on two models only, and cannot be combined with tool use.

`.claude/rules/llm-output-handling.md` requires every response to be parsed against a Pydantic model with `extra="forbid"`, **zero re-asks**, and a schema failure that fails the call. Schema-enforced generation is what keeps that failure rate low enough to be an instrument rather than a nuisance.

On Groq, `strict: true` JSON-schema mode is documented for `openai/gpt-oss-20b` and `openai/gpt-oss-120b` only. Every other Groq model — including `llama-3.3-70b-versatile`, which is the model named in the example configuration in `.claude/rules/quota-management.md` — gets `{"type": "json_object"}`, which guarantees syntactically valid JSON and nothing about its shape. The docs also state that *"streaming and tool use are not currently supported with Structured Outputs"*.

Gemini supports an enforced response schema and documents combining it with function calling, with the combined example restricted to the Gemini 3 series.

Consequence: if Groq is used for any agent whose output is schema-validated — which is all of them — the model must be `openai/gpt-oss-120b` or `openai/gpt-oss-20b`, and the 8,000 TPM ceiling from §1.3 applies to it.

### 1.5 GitHub Models is retired. It is not a candidate.

> "As of July 30, 2026, GitHub Models has been fully retired. The playground, model catalog, inference API, and bring your own key (BYOK) are no longer available to any customer."

Four days before this investigation. It is listed in the issue body as an area to evaluate; the evaluation is that it no longer exists.

### 1.6 The LLM gateway cannot route through `guarded_client()`, and needs its own egress path.

`src/fking/platform/safety/_allowlist.py` contains seven hosts, all of them exchange endpoints. `guarded_client()` refuses anything else by design. An LLM call therefore **fails the safety kernel**, correctly — `generativelanguage.googleapis.com` is not a trading venue and must never be in the trading allowlist.

So the P5 gateway needs the same shape #22 already specifies for the archive fetcher: a **separate, separately-allowlisted egress path**, with its own host set and its own named `ignore_imports` entry in the `import-linter` contract. The precedent exists and should be reused rather than reinvented; what must not happen is the LLM hosts being added to `PERMITTED_HOSTS`, which would widen the trading allowlist for a non-trading reason and is exactly the move `CLAUDE.md` §0 exists to stop.

---

## 2. Free LLM tiers

All rows fetched **2026-08-03**.

### 2.1 Google — Gemini (AI Studio / Gemini Developer API)

| Field | Value | Confidence | Source |
|---|---|---|---|
| Free-tier models | `gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-3-flash-preview`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-embedding-001`, Gemma 4 | documented | [pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| Requests per minute | **`unverified`** — not published | — | [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) |
| Tokens per minute | **`unverified`** — not published | — | same |
| Requests per day | **`unverified`** — not published | — | same |
| Limit scope | per **project**, not per API key | documented | same |
| Daily reset | midnight **Pacific** time | documented | same |
| Input vs output token accounting | TPM is documented as *tokens per minute (input)*; output accounting is not stated | documented / partial | same |
| Credit card required | No | documented | pricing |
| **Trains on free-tier data** | **Yes** — explicitly, with human review | documented | [terms](https://ai.google.dev/gemini-api/terms) |
| Enforced JSON schema | Yes (`response_format` with `mime_type: application/json` and a `schema`); "not all JSON Schema features are supported", deeply nested schemas may be rejected | documented | [structured output](https://ai.google.dev/gemini-api/docs/structured-output) |
| Schema + tool calling together | Documented, with the combined example restricted to Gemini 3 series | documented | same |

Community-reported free-tier figures for `gemini-2.5-flash` (10 RPM / 250,000 TPM / 250 RPD) are recorded here **only** so that a future reader recognises them when they appear in a config file and knows they were never sourced. They are `unverified`.

`gemini-2.0-flash` and `gemini-2.0-flash-lite` were **shut down on 2026-06-01** and no longer exist. ADR-0009 pins `gemini-2.5-flash-002`; that generation is still on the free tier, but it is now two generations behind the current free-tier default, which is a re-pin decision rather than a fact (§6.2).

### 2.2 Groq

Groq publishes per-model free-tier limits, which makes it the only provider in this survey whose numbers can be cited rather than guessed. Source: [rate limits](https://console.groq.com/docs/rate-limits), fetched 2026-08-03.

| Model | RPM | RPD | TPM | TPD |
|---|---:|---:|---:|---:|
| `llama-3.1-8b-instant` | 30 | 14,400 | 6,000 | 500,000 |
| `llama-3.3-70b-versatile` | 30 | 1,000 | 12,000 | 100,000 |
| `openai/gpt-oss-120b` | 30 | 1,000 | 8,000 | 200,000 |
| `openai/gpt-oss-20b` | 30 | 1,000 | 8,000 | 200,000 |
| `groq/compound` | 30 | 250 | 70,000 | — |
| `whisper-large-v3` | 20 | 2,000 | — (audio seconds) | — |

| Field | Value | Confidence |
|---|---|---|
| Limit scope | organization-level, not per key | documented |
| Rate-limit headers | `x-ratelimit-limit-requests`, `-limit-tokens`, `-remaining-*`, `-reset-*` on every successful response | documented |
| 429 behaviour | `retry-after` present **only** when the limit is actually exceeded | documented |
| Credit card required | No | documented |
| **Trains on data** | **No** — Services Agreement §4.2 prohibits it absent explicit customer permission | documented |
| Retention | none by default for inference; abuse/troubleshooting logs ≤ 30 days; Zero Data Retention available to all customers | documented |
| Enforced JSON schema | `strict: true` on `openai/gpt-oss-20b` / `-120b` only; all others `json_object` (syntax only) | documented |
| Structured output + tools | **Not supported together** | documented |

The 429-header detail matters more than it looks: `.claude/rules/quota-management.md` requires honouring `Retry-After` and never retrying inside the same exhausted window. Groq populates it, so that rule is implementable against Groq exactly as written. Gemini's 429 shape (`429 RESOURCE_EXHAUSTED` is documented for spend-based limits) was not confirmed to carry `Retry-After`, and that is `unverified`.

### 2.3 Cerebras

Source: [rate limits](https://inference-docs.cerebras.ai/support/rate-limits), fetched 2026-08-03. The tier is named **Free Trial**, which is itself a finding — a *trial* is not a standing free tier and should not be planned against as one.

| Field | Value |
|---|---|
| Models | `gpt-oss-120b`, `zai-glm-4.7`, `gemma-4-31b` |
| RPM | 5 |
| TPM | 30,000 |
| Tokens per hour | 1,000,000 |
| Tokens per day | 1,000,000 |
| RPD | not published |
| Trains on data | **`unverified`** — not established by this investigation |

TPH equal to TPD means the daily allowance can be consumed in a single hour, so there is no throttle protecting the rest of the day. Any scheduler using Cerebras must ration by day itself; the provider will not.

### 2.4 OpenRouter (free model variants)

Source: [limits](https://openrouter.ai/docs/api-reference/limits), fetched 2026-08-03.

| Field | Value |
|---|---|
| RPM (free models) | 20 |
| RPD, lifetime credits < 10 | 50 |
| RPD, lifetime credits ≥ 10 | 1,000 |
| Token limits | not published |
| Logging/training | separate settings exist for paid and free models; what the free-model setting *entails* is not documented. **`unverified`**, and treated as "may train" until it is not |

The credit-gated daily limit is the finding: the free tier's usable size is a function of having once paid, which makes "free" conditional in a way that a zero-budget project cannot satisfy. At 50 RPD, OpenRouter is a spillover, not a provider.

### 2.5 Mistral (La Plateforme)

A free tier exists. **Mistral no longer publishes its numeric limits publicly** — the help article states only that *"Free mode (the default) has the lowest limits, intended for evaluation and prototyping"* and directs users to the Admin Console limits page behind a login. Same shape as Gemini (§1.2), same conclusion: `unverified`, and the ledger is the authority.

### 2.6 GitHub Models

**Retired 2026-07-30.** Not a candidate. See §1.5.

### 2.7 The ranking the issue asked for

The issue asks these providers be ranked on *structured JSON reliability, tool-calling support, throughput, and hardest-reasoning quality*.

Three of the four can be answered from documentation and are answered in the table below. **The fourth cannot, and this document declines to fake it.** "Hardest-reasoning quality" is a claim about model behaviour on *this project's* prompts, and the only honest instrument for it is the golden-set harness that `.claude/rules/llm-output-handling.md` already specifies — the same harness that owns prompt repair. A ranking assembled from public leaderboards would be a number with the appearance of evidence and none of the substance, and it would then be cited in a promotion decision. The method to obtain it is in §7.

| | Structured JSON | Tool calling | Throughput ceiling (free) | Trains on data |
|---|---|---|---|---|
| **Groq** (`gpt-oss-120b`) | **Strongest**: `strict: true` schema enforcement | Yes, but **not simultaneously** with structured output | 8,000 TPM / 200,000 TPD | **No** |
| **Gemini** (2.5/3.x flash) | Strong: enforced response schema, some keywords unsupported | Yes, documented alongside structured output (Gemini 3 series) | `unverified` | **Yes** |
| **Cerebras** | `unverified` | `unverified` | 30,000 TPM / 1M TPD, 5 RPM | `unverified` |
| **OpenRouter** | Inherits the upstream model's | Inherits | 20 RPM / 50 RPD | assume yes |
| **Mistral** | `unverified` | `unverified` | `unverified` | `unverified` |

---

## 3. Provider abstraction: LiteLLM vs Pydantic AI vs direct SDKs

Package metadata **measured** on 2026-08-03 from the PyPI JSON API; repository status from the GitHub API.

| Package | Version | Released | License | Core deps (excl. extras) |
|---|---|---|---|---|
| `litellm` | 1.95.0 | 2026-08-02 | MIT | **12** |
| `pydantic-ai` | 2.22.0 | 2026-08-01 | MIT | 1 |
| `google-genai` | 2.16.0 | 2026-07-30 | Apache-2.0 | 10 |
| `groq` | 1.6.0 | 2026-07-24 | Apache-2.0 | 6 |

All four are actively maintained; `litellm` and `pydantic-ai` both had commits on 2026-08-03. Maintenance is not the discriminator.

### Verdict: direct SDKs behind this project's own gateway. Adopt neither.

Three reasons, in descending weight.

**1. Neither library provides the thing the gateway exists for.** ADR-0009's gateway is not a provider abstraction — it is an **admission-control point** with persistent reserve-then-reconcile accounting in Postgres, priority classes, and a `Degraded` return value instead of an exception. LiteLLM has routing, retries and fallbacks; it does not have a durable quota ledger that survives the restart the rate limiting caused, and it cannot, because that ledger has to share a transaction with this project's own database. Adopting LiteLLM would mean writing the gateway anyway *and* carrying LiteLLM underneath it.

**2. `litellm` constructs its own transports.** Its core dependency list is `fastuuid, httpx, openai, python-dotenv, tiktoken, importlib-metadata, tokenizers, click, jinja2, aiohttp, pydantic, jsonschema`. Two entries there are load-bearing:

- `aiohttp` and `httpx` are both on the `forbidden_modules` list of the `import-linter` contract "only the safety kernel constructs network clients", and LiteLLM opens its own clients rather than accepting an injected session. The contract permits the *indirect* path, so this would not fail CI — it would pass CI while removing the property the contract is a proxy for. That is worse than a failing contract.
- `openai` is on the `forbidden_modules` list of the "Only the LLM gateway may import a provider SDK" contract in `.claude/rules/llm-output-handling.md`. Adopting LiteLLM pulls the OpenAI SDK into the graph as a transitive dependency of the very module the contract is written to constrain.

**3. Dependency weight, and what it is a proxy for.** 12 core dependencies including two tokenizer libraries, a template engine and a CLI framework, against 6 for the Groq SDK and 10 for `google-genai`, to serve two providers. `CLAUDE.md` §3's rule is that an abstraction needs two concrete callers before it exists — LiteLLM is an abstraction over a hundred providers adopted to serve two.

**What survives the rejection.** Pydantic AI's discipline — a typed agent whose output model *is* the contract — is the design this project already independently arrived at in `.claude/rules/llm-output-handling.md`, and it should stay. `parse_or_fail` with a Pydantic v2 model, `extra="forbid"`, zero re-asks, is Pydantic AI's good idea implemented without its dependency and without its retry loop, which this project forbids. Re-evaluate `pydantic-ai` if and only if a third provider is added *and* its 1-core-dependency footprint holds; at that point the two-callers rule is genuinely satisfied.

---

## 4. Quant stack

Package metadata **measured** 2026-08-03 from the PyPI JSON API; repository status from the GitHub API.

### 4.1 Adopt

| Package | Version | Released | License | Note |
|---|---|---|---|---|
| `statsmodels` | 0.14.6 | 2025-12-05 | BSD-3 | Time-series tests, stationarity. The default. |
| `arch` | 8.0.0 | 2025-10-21 | NCSA | Volatility models and, more importantly, the **block bootstrap** the Monte Carlo work in #43 needs. NCSA is BSD-equivalent and permissive. |
| `scikit-learn` | 1.9.0 | 2026-06-02 | BSD-3 | Splitter protocol is what CPCV (#41) should implement against, not replace. |
| `lightgbm` | 4.7.0 | 2026-07-18 | MIT | Gradient boosting choice — see §4.4. |
| `polars` | 1.43.2 | 2026-08-01 | MIT | See §4.5 on interop. |
| `ta-lib` | 0.7.1 | 2026-07-16 | — | See §4.3. The C-dependency objection is dead. |

### 4.2 Refuse, with reasons

**`mlfinlab` — refuse.** The PyPI JSON API returns **404**: there is no installable release. The GitHub repository's last push was **2023-10-02**, nearly three years before this fetch, and its license resolves to `NOASSERTION` — GitHub could not identify a recognised open-source license. This is the López de Prado toolchain that OQ-002 named as the primary candidate for CPCV, purging, embargo and the deflated Sharpe ratio, and it is not available on any terms this project can rely on.

This settles OQ-002's hardest question in the least convenient direction: **the statistics that gate every promotion decision must be implemented in-project.** OQ-002 already warned that a hand-rolled deflated Sharpe would be wrong in the flattering direction, because a bug in a penalty term almost always understates the penalty. There is now no library to check against, so the acceptance criterion becomes numerical agreement with a **hand-computed worked example from the source paper**, per statistic, as OQ-002 specified. `.claude/rules/overfitting-defences.md` already carries the `expected_max_sharpe` and `deflated_sharpe` implementations with the Euler–Mascheroni constant cited to Bailey & López de Prado (2014) eq. 5; that citation is now load-bearing rather than courteous.

**`pandas-ta` — refuse.** Latest release `0.4.71b0` on **2025-09-14**, still classified Beta. Both of its declared project URLs are dead as of 2026-08-03: the GitHub repository `twopirllc/pandas-ta` returns **404**, and the homepage `pandas-ta.dev` **does not resolve in DNS** (`www.` fails to resolve; the apex times out). A package whose upstream has vanished while remaining installable is worse than one that was removed, because `uv sync` keeps succeeding and nothing signals the abandonment. Indicators come from `ta-lib` or are written in-project against a worked example.

### 4.3 TA-Lib: the C-dependency objection no longer applies

The historical argument against TA-Lib is that it wraps a C library requiring a source build inside Docker. Version 0.7.1 publishes **54 wheels** covering `manylinux_2_17`/`_2_28` (x86_64 and aarch64), `musllinux_1_2`, macOS 13/14, and `win_amd64`. There is no compile step on any platform this project targets, on Linux containers or on the Windows development machine — which matters here for a second reason: issue #117 records that Windows Smart App Control blocks some compiled artefacts, and a prebuilt signed wheel avoids the source-build path entirely.

This removes the only real reason `pandas-ta` was ever preferred, which is convenient given §4.2.

### 4.4 Portfolio and reporting libraries: adopt narrowly or not at all

| Package | Version | Released | License | Verdict |
|---|---|---|---|---|
| `skfolio` | 0.20.1 | 2026-04-21 | BSD-3 | **Evaluate for #51** (correlation-aware exposure). scikit-learn-native API, actively developed (pushed 2026-07-31). Its cross-validation and model-selection pieces are the closest surviving substitute for the purged-CV parts of `mlfinlab`. |
| `riskfolio-lib` | 7.3.0 | 2026-05-31 | BSD-3 | Evaluate alongside `skfolio`; broader risk-measure coverage, heavier. Only one of the two should be adopted. |
| `PyPortfolioOpt` | 1.6.0 | 2026-02-26 | MIT | Refuse for now — overlaps `skfolio` with less activity, and mean-variance optimisation is not what #51 needs. |
| `quantstats` | 0.0.81 | 2026-01-13 | Apache-2.0 | **Refuse for anything scoring-related.** Fine for a human-facing tearsheet (#45); it must never compute a number that reaches the survival score. Its version number is still `0.0.x` after years, and #44's credibility gate needs metrics whose definitions this project controls. |

Gradient boosting: `lightgbm` 4.7.0 (MIT), over `xgboost` 3.3.0 (Apache-2.0) and `catboost` 1.2.10 (Apache-2.0). All three are healthy; LightGBM wins on install weight and on native categorical handling, and the choice is reversible — nothing in the design depends on which one it is. Recorded so the next session does not re-litigate it.

### 4.5 Polars-first: realistic, with one boundary

`polars` 1.43.2 released 2026-08-01, MIT, actively developed. A polars-first pipeline is realistic **on the ingestion and feature side**, where this project owns both ends. It is not realistic through `statsmodels`, `arch`, `scikit-learn` or `lightgbm`, all of which take NumPy arrays or pandas frames.

That is not a defect, it is the boundary [`.claude/rules/decimal-and-money.md`](../../.claude/rules/decimal-and-money.md) already draws: the `Decimal` → `float64` conversion happens at a *named boundary function*, one direction at a time. Polars-to-NumPy sits at exactly the same boundary and should use the same named function, so there is one place where the money contract is handed over rather than several. A polars frame that reaches `statsmodels` directly is the same class of leak as a float that reaches an order quantity.

---

## 5. Remaining data sources

Every row here was **measured** on 2026-08-03 — an actual unauthenticated request, with the observed status code. That is a stronger source than any documentation page, and it is why this section carries `measured` where §2 cannot.

| Source | Endpoint probed | Status | Key required | Note |
|---|---|---|---|---|
| **Fear & Greed** (alternative.me) | `GET api.alternative.me/fng/?limit=2` | **200** | No | Returns `time_until_update` in seconds (44,906 observed), so the update cadence is self-describing — the feed tells you its own availability lag rather than requiring you to assume one. No rate-limit headers. |
| **GDELT DOC 2.0** | `GET api.gdeltproject.org/api/v2/doc/doc?...&timespan=1d` | **200** | No | **14.86 s** for a 3-record response. That is not an outlier to retry around; it is the service. Any scheduled job calling GDELT needs a timeout well above the default and must not sit in a request path. |
| **FRED** | `GET api.stlouisfed.org/fred/series/observations?series_id=DFF` | **400** | **Yes** | `"Variable api_key is not set."` Free registration, no payment. Blocks on a credential the project does not yet hold. |
| **CryptoPanic** | `GET cryptopanic.com/api/developer/v2/posts/?currencies=BTC` | **404** | **Yes** | And `/api/v1/posts/` with a placeholder token returns **403** from Cloudflare. Both public paths are closed; a key is mandatory. |
| **Reddit** | `GET reddit.com/r/CryptoCurrency/new.json` | **403** | **Yes (OAuth)** | Returned `Retry-After: 0` with an HTML body — the unauthenticated JSON endpoints that most tutorials use are closed. OAuth registration required. Treat every code sample predating this as wrong, the same way VF-002 requires for Binance spot `listenKey`. |
| **mempool.space** | `GET mempool.space/api/v1/fees/recommended` | **200** | No | 0.25 s. Bitcoin on-chain fee/mempool state, free, no key. |
| **blockchain.info** | `GET api.blockchain.info/stats` | **200** | No | 0.12 s. Network-level Bitcoin statistics, free, no key. |

**The finding that changes plans:** the two sources this project most wants for a sentiment feature — CryptoPanic and Reddit — both require credentials, and the two that are open (Fear & Greed, GDELT) are respectively one number per day and a 15-second query. There is no zero-credential, low-latency news or social feed in this survey. #32 should be planned on that basis rather than discovering it during implementation.

None of these endpoints published a rate-limit header. Where a source states a limit in its terms rather than its headers, it is recorded in [`../../SOURCES.md`](../../SOURCES.md); where it states nothing, that is recorded as unknown rather than as unlimited.

---

## 6. Combined free-tier quota budget, in agent cycles per day

The issue asks for a concrete number. Here is the arithmetic, with its assumption stated first so the number can be recomputed when the assumption changes.

**Assumption**: one agent cycle = one LLM call of **11,000 tokens** (10,000 input + 1,000 output). This is a deliberate working figure, not a measurement, chosen because it is roughly a fenced market-data prompt plus a bounded structured response. It is a quarter of `.claude/agents/quant.md`'s 45k ceiling; see §1.3 for why that ceiling is unreachable on Groq at all.

| Provider / model | Binding limit | Cycles/day | Note |
|---|---|---:|---|
| Groq `llama-3.1-8b-instant` | 500,000 TPD | **45** | 6k TPM — a single 11k call is too large. Usable only for short prompts. |
| Groq `openai/gpt-oss-120b` | 200,000 TPD | **18** | 8k TPM — same problem, and this is the schema-strict model. |
| Groq `openai/gpt-oss-20b` | 200,000 TPD | **18** | 8k TPM. |
| Groq `llama-3.3-70b-versatile` | 100,000 TPD | **9** | 12k TPM; the only Groq model that fits an 11k call, and it has no schema enforcement. |
| **Groq total** | per-model limits are independent | **≈ 90** | Only if work is spread across all four models. |
| Cerebras `gpt-oss-120b` (Free **Trial**) | 1,000,000 TPD | **90** | Per model; ×3 models if all are used. 5 RPM and TPH = TPD, so it can be exhausted in one hour. |
| OpenRouter free variants | 50 RPD (< 10 lifetime credits) | **50** | Request-capped, not token-capped. Assume "may train". |
| Gemini free tier | **`unverified`** | **`unverified`** | Not published. §1.2. |
| Mistral free tier | **`unverified`** | **`unverified`** | Not published. §2.5. |

**Headline: a defensible floor of ≈ 90 agent cycles per day from Groq alone, and ≈ 180/day if Cerebras is included** — with the caveat that Cerebras calls its tier a *trial*, so planning against it is planning against something that has announced it is temporary.

Two things this number is not. It is **not** 90 *useful* cycles: §1.3 means most of that budget sits on models whose per-minute token ceiling is below a realistic prompt, so the effective figure for a full-size agent call on Groq is the `llama-3.3-70b-versatile` row — **9 cycles per day** — and that model cannot enforce a schema. And it is **not** a limit the scheduler should trust: it is a floor to configure against, which the ledger then corrects upward or downward from observation.

Against `ARCHITECTURE.md`'s "dozens of agent invocations per day", the honest conclusion is that **Gemini is not optional to the P5 design as currently drawn** — Groq alone cannot carry it — and that is precisely the provider whose free tier trains on the input. §6 of this document exists because those two facts are in tension and the tension has to be resolved deliberately.

---

## 7. Contradictions with current assumptions, and what to do about them

### 7.1 ADR-0009's provider priority was decided without the data-training question

**Contradiction.** ADR-0009 makes the Gemini free tier primary. §1.1 establishes that the Gemini free tier trains on submitted content, subjects it to human review, and is accompanied by an explicit instruction not to submit confidential information — while the declared fallback, Groq, is contractually prohibited from training and retains nothing by default.

**Recommendation — a data-classification rule, not a straight reversal.** Promoting Groq to primary looks like the obvious response and is the wrong one, because §6 shows Groq cannot carry the workload: 9 full-size cycles per day on the only model that fits an 11k prompt. The recommendation is instead to make *what is sent* the decision variable rather than *who is primary*:

- Prompts carrying **strategy specifications, parameter values, hypothesis statements or lineage** go to a non-training provider only — Groq today — and are sized to fit its per-minute ceiling. If they do not fit, they do not go.
- Prompts carrying **only public market context** may go to Gemini, because the training exposure of publicly available data is nil.
- The classification is a field on `AgentDeclaration`, checked in the gateway before routing, so it cannot be forgotten at a call site — the same argument ADR-0009 already makes for why admission control cannot be distributed.

This is an architectural change to an accepted ADR and therefore not made inside a research pull request: ADRs are immutable once accepted (`CLAUDE.md` §13), so it needs a superseding ADR and a human decision. Tracked as **[#128](https://github.com/ismetcahangirov/financeKing/issues/128)**, which carries the two sub-questions the ADR has to settle — whether a *hypothesis statement* counts as public context or as strategy logic, and what the default is when a prompt's classification is missing.

### 7.2 The pinned model id is two generations old

ADR-0009 pins `gemini-2.5-flash-002`. `gemini-3.6-flash`, `gemini-3.5-flash` and `gemini-3.5-flash-lite` are now on the free tier, and `gemini-2.0-flash` was shut down on 2026-06-01 — which is the argument *for* pinning, demonstrated: an unpinned alias would have moved under the project without a diff. The pin is working. Re-pinning is a deliberate act with a golden-set re-run behind it, and it belongs to the same follow-up as §7.1, not to this document.

### 7.3 `.claude/agents/quant.md`'s 45k token budget is unservable by the fallback

See §1.3. Either the budget comes down or the failover claim does. This one is cheap to fix and should be fixed when the agent is implemented (P5), not now.

### 7.4 The example Groq model in `.claude/rules/quota-management.md` cannot enforce a schema

`llama-3.3-70b-versatile` appears in that file's test examples. Per §1.4 it supports `json_object` only. The rule file's *design* is unaffected — it is the model id in an illustration — but the illustration will be copied, so it should become `openai/gpt-oss-120b` when the gateway is implemented.

---

## 8. What remains unverified, and the probe that would close it

OQ-001 stays **open**. Its blocker changes from *"the research session hit its limit"* to *"the vendors that matter no longer publish the numbers, and measurement needs a key"*, which is a smaller and more specific blocker.

The probe that closes it, once a key exists per provider:

1. Issue a minimal completion to each provider/model and record every response header. Groq returns `x-ratelimit-*` on **success**, so the true limits are readable without ever hitting a 429.
2. Drive one model to its documented RPD, capture the 429 body and headers verbatim, and record whether `Retry-After` is present and whether it is seconds or a timestamp.
3. Repeat at a day boundary to establish whether the window is UTC, account-local or rolling. Gemini documents midnight **Pacific**, which is neither UTC nor local for this project — a fact worth a test rather than a comment.
4. Send one call with a large input and a small output and one with the reverse, at identical total token counts, to establish whether input and output share a budget.
5. Write every observation to `llm_quota_ledger` through the ordinary path, so the `observed_limits` view brackets the truth from the first day rather than after the first incident.

Steps 1–4 are roughly an hour of elapsed time per provider and almost no attention. Step 5 is free — it is the gateway doing its job.

OQ-002 is **partly answered**: `mlfinlab` and `pandas-ta` are refused with reasons (§4.2), the adopt list is in §4.1, and TA-Lib's Docker objection is retired (§4.3). What remains open is the part OQ-002 correctly identified as the real risk — numerical agreement between this project's own statistics and a worked example from the source paper. That is a code task in P2 (#40), not a research one, and it is now unavoidable rather than optional.

---

## 9. Re-verification

| Fact | Decays | Trigger |
|---|---|---|
| Groq free-tier numbers (§2.2) | quarterly | any Groq pricing or tier announcement |
| Groq no-training clause (§1.1) | on any Services Agreement revision | a diff to `console.groq.com/docs/legal/services-agreement` |
| Gemini free-tier data use (§1.1) | on any terms revision | a diff to `ai.google.dev/gemini-api/terms` |
| Gemini model availability (§2.1) | on each model generation | a shutdown notice like `gemini-2.0-flash`'s |
| Library versions and licenses (§3, §4) | on any dependency bump | `uv lock --upgrade` |
| Data-source reachability (§5) | quarterly | re-run the probes in §5; they need no credentials |

The §5 probes are the cheapest row in this table and the most likely to catch a real change, because they require nothing but a network connection. They should be a scheduled job, not a memory.
