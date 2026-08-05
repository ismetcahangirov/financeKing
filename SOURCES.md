# SOURCES

Every external service this system may talk to, what it costs, what it limits, what its terms permit, and — for data sources — **how late its data is**.

**Why this file exists separately from the research report.** [`docs/research/free-tier-landscape.md`](docs/research/free-tier-landscape.md) is an investigation with a date on it; this is the operational register that code and configuration are written against. The report explains *why* a number is what it is. This file is where you look when you need the number.

**The column that earns this file its place is `availability_lag`.** A source's rate limit is an operational nuisance; its availability lag is a *correctness* property. [`.claude/rules/no-lookahead.md`](.claude/rules/no-lookahead.md) requires every record to carry `event_time` **and** `available_at`, and requires every registered feature to declare an `availability_lag`. That lag is a property of the source, not of the feature, so it belongs here — once, where it can be checked — rather than being re-guessed in each feature's registry entry. A feature that inherits a wrong lag does not fail; it produces a backtest that knew things early, which is the defect class `CLAUDE.md` §2 names as the most dangerous in the project.

**Rules for this file.**

1. Every row states the date it was checked. A limit is a fact about a moment.
2. A limit that is not published is written **`not published`**, never estimated. `unknown` and `unlimited` are different claims and only one of them is ever safe.
3. Terms-of-service position on automated use is recorded per source, because "the endpoint answered" and "we are permitted to call it on a schedule" are different questions and only the second one survives contact with a lawyer.
4. When a source is added to the code, it is added here in the same pull request.

---

## 1. Trading venues

These are the only hosts in the compiled-in allowlist (`src/fking/platform/safety/_allowlist.py`). Nothing else may be added without a `safety:critical` pull request — see [`.claude/rules/safety-kernel.md`](.claude/rules/safety-kernel.md). **Production exchange hosts are absent by design and there is no mechanism to add one at runtime.**

| Host | Purpose | Limit | Checked |
|---|---|---|---|
| `testnet.binance.vision` | spot testnet REST | 50 orders / 10s (production: 100 / 10s) — VF-007 | 2026-08-01 |
| `ws-api.testnet.binance.vision` | spot testnet WS API — `session.logon`, `userDataStream.subscribe` | — | 2026-08-01 |
| `stream.testnet.binance.vision` | spot testnet market-data WS | — | 2026-08-01 |
| `testnet.binancefuture.com` | USD-M futures testnet REST | — | 2026-08-01 |
| `stream.binancefuture.com` | USD-M futures testnet WS (`listenKey` keepalive) | — | 2026-08-01 |
| `api-testnet.bybit.com` | fallback venue REST | `not published` for our usage — OQ-009 | 2026-08-01 |
| `stream-testnet.bybit.com` | fallback venue WS | `not published` — OQ-009 | 2026-08-01 |

Access requires only GitHub OAuth, no KYC and no payment (VF-001). Spot testnet balances and open orders are wiped roughly every 30 days while keys survive (VF-005) — reconciliation is unconditional, not a recovery path.

---

## 2. Historical market data

| Source | Auth | Cost | Limit | Earliest data | Cadence | `availability_lag` | Terms | Checked |
|---|---|---|---|---|---|---|---|---|
| `data.binance.vision` — spot klines | none | free | `not published` | 2017-08-17 (BTCUSDT 1m; VF-013), per symbol otherwise (OQ-005) | daily and monthly archives per interval | > 6 h and ≤ 30 h after the UTC day closes — **measured** 2026-08-05T06:05Z: the 08-04 daily archive was not yet published while 08-03 was | public archive, automated download expected | 2026-08-05 |
| `data.binance.vision` — USDⓈ-M futures klines, trades, aggTrades | none | free | `not published` | 2019-09-08 corpus genesis; per symbol later | daily and monthly | as above | as above | 2026-08-05 |

Every archive has a `.zip.CHECKSUM` sibling and it is verified before the file is trusted (VF-014). BTCUSDT 1m runs from 2017-08-17 (VF-013); every other symbol starts later and is enumerated rather than assumed (OQ-005).

**The archive lag is an operational property, not the availability lag of the values inside it.** A daily archive publishes hours after the day it covers, but a live system reading the same market data over the WebSocket knew it at the bar close. `available_at` is *the earliest instant this system could have known the value*, so it is the live path that sets it and the archive is only how history is stored. Confusing the two would push every backtest feature a day into the past and make a real edge look like none — the conservative direction, but wrong, and wrong in a way that is invisible.

Two format traps are recorded in [`DATA_PIPELINE.md`](DATA_PIPELINE.md) and belong to the resolver in #21, not to this table: spot timestamps became microseconds from 2025-01-01 while futures stayed milliseconds (VF-015), and futures kline CSVs carry a header row while spot ones do not (VF-016).

**Free full-depth L2 order book history does not exist** (VF-017). `bookDepth` is aggregated depth bands sampled about once a minute. This is a permanent absence, not a gap to fill later, and it is why the cost model assumes taker execution (OQ-007).

**Egress note.** `data.binance.vision` is a data host, not a trading host, and it is deliberately **not** in the safety allowlist. #22 gives it a separate egress path. The same pattern applies to every source below.

---

## 3. LLM providers

Full analysis, including the data-training finding that bears on provider choice, is in [`docs/research/free-tier-landscape.md`](docs/research/free-tier-landscape.md) §1–§2. All rows checked **2026-08-03**.

### 3.1 Published limits

| Provider | Model | RPM | RPD | TPM | TPD | Scope |
|---|---|---:|---:|---:|---:|---|
| Groq | `llama-3.1-8b-instant` | 30 | 14,400 | 6,000 | 500,000 | organization |
| Groq | `llama-3.3-70b-versatile` | 30 | 1,000 | 12,000 | 100,000 | organization |
| Groq | `openai/gpt-oss-120b` | 30 | 1,000 | 8,000 | 200,000 | organization |
| Groq | `openai/gpt-oss-20b` | 30 | 1,000 | 8,000 | 200,000 | organization |
| Cerebras (Free **Trial**) | `gpt-oss-120b`, `zai-glm-4.7`, `gemma-4-31b` | 5 | `not published` | 30,000 | 1,000,000 | account |
| OpenRouter | `*:free` | 20 | 50 (< 10 lifetime credits) / 1,000 (≥ 10) | `not published` | `not published` | account |
| Google Gemini | all free-tier models | **`not published`** | **`not published`** | **`not published`** | **`not published`** | project |
| Mistral | free ("lowest limits") | **`not published`** | **`not published`** | **`not published`** | **`not published`** | account |

Google and Mistral both moved their free-tier numbers behind an account login during 2026. They are `not published`, which is a different and more useful statement than a number someone found in a forum post. Configuration uses conservative floors and the quota ledger measures the truth ([`.claude/rules/quota-management.md`](.claude/rules/quota-management.md)).

**GitHub Models was fully retired on 2026-07-30** — playground, catalogue, inference API and BYOK. It is not a candidate.

### 3.2 Terms: data use, retention, and what may be sent

| Provider | Trains on free-tier input? | Retention | Consequence for this project |
|---|---|---|---|
| **Groq** | **No.** Services Agreement §4.2: *"Groq is not permitted to use Inputs or Outputs for training or fine-tuning any AI Model Services or other models, unless explicitly granted permission or instructed by Customer."* | none by default for inference; abuse/troubleshooting logs ≤ 30 days; Zero Data Retention available to all customers | The only provider in this table that may currently receive a prompt containing strategy logic. |
| **Google Gemini** (unpaid tier) | **Yes.** *"Google uses the content you submit to the Services and any generated responses to provide, improve, and develop Google products"*; *"human reviewers may read, annotate, and process your API input and output"*; *"Do not submit sensitive, confidential, or personal information to the Unpaid Services."* | — | Public market context only. See the open recommendation in the research report §7.1. |
| **OpenRouter** | Separate settings exist for free and paid models; what the free setting entails is **not documented**. | not stated for free variants | Treated as "may train" until proven otherwise. |
| **Cerebras** | `unknown` | `unknown` | Not established by this investigation. |
| **Mistral** | `unknown` | `unknown` | Not established by this investigation. |

### 3.3 Structured output capability

The zero-re-ask rule in [`.claude/rules/llm-output-handling.md`](.claude/rules/llm-output-handling.md) depends on this row, so it is recorded here rather than left to the code to discover.

| Provider | Enforced JSON schema | With tool calling |
|---|---|---|
| Groq `openai/gpt-oss-20b` / `-120b` | yes (`strict: true`) | **no** — structured output and tool use are mutually exclusive |
| Groq, all other models | no — `json_object` guarantees syntax only | — |
| Gemini | yes (response schema; not all JSON Schema keywords supported) | documented together, Gemini 3 series |

---

## 4. Alternative series

The five sources `fking.data.alt.registry` declares. Every one of them is registered in code with the same `availability_lag`, `cadence` and terms position stated here, and `tests/data/test_alt_sources.py` fails if a registered source is missing from this file.

**Two of the five are reachable and three are not**, and the split is a decision rather than a sequencing accident — see §4.2.

### 4.1 The register

| Source | Endpoint | Auth | Observed limit | Earliest data | Cadence | `availability_lag` | Terms on automated use | Checked |
|---|---|---|---|---|---|---|---|---|
| `binance.fundingRate` | `data.binance.vision/data/futures/um/**monthly**/fundingRate/<SYM>/` | none | `not published` | **2020-01** for BTCUSDT and ETHUSDT — four months after the corpus genesis (VF-028) | 8 h, and checked per row against `funding_interval_hours` | **1 min** — the realised rate is broadcast at settlement over `<symbol>@markPrice@1s`; one minute is the stream-delivery and reconnect envelope | public archive, automated download expected | 2026-08-05 |
| `binance.openInterest` | `data.binance.vision/data/futures/um/**daily**/metrics/<SYM>/` | none | `not published` | **2020-09-01** for BTCUSDT — almost a year after the corpus genesis (VF-028) | 5 min | **5 min** — the archived series is the five-minute aggregate, so the value stamped at T is in the series once T's period closes | public archive, automated download expected | 2026-08-05 |
| `alternative.me.fearGreed` | `api.alternative.me/fng/` | none | `not published`, no headers | `not established` | 24 h | **24 h** — the value stamped for day D carries `time_until_update` ≈ 18 h at 06:05Z on D, so it is refreshed at the *end* of D and is not knowable at its own timestamp (VF-030) | public API, no stated restriction on automated use | 2026-08-05 |
| `cryptopanic.posts` | `cryptopanic.com/api/` | **API key** | key-tier dependent | `not established` | polling; the source is continuous | **5 min** polling envelope — and this is *not* an information lag: the timestamp is the republication instant, not the first print | key required and the free-tier terms **have not been read**. Unusable until they are | 2026-08-03 |
| `stlouisfed.fredReleases` | `api.stlouisfed.org/fred/release/dates` | **API key** | per-key, **not obtained** (OQ-001) | series-dependent | series-dependent | **published, not inferred** — the release calendar is a first-class resource and gives the exact release instant per print. The declared 1-day lag is only a floor that refuses a release predating its own observation period | free key by registration, attribution required, automated use permitted | 2026-08-03 |

Three notes that change how the code is written:

**Granularity is a property of the dataset, not of the calendar.** `fundingRate` is published monthly only and `metrics` daily only (VF-029), so the daily-versus-monthly rule that picks by distance from today is wrong for both, in opposite directions, on every date. Every alternative fetch states its granularity explicitly.

**`metrics` carries no epoch.** Its first column is `create_time,2024-01-02 00:00:00` — a naive datetime string with no offset. That is a declared encoding, `TimestampEncoding.NAIVE_UTC_DATETIME`, resolved per `(market, dataset, date)` like every other format decision, and `parse_naive_utc_datetime` attaches UTC because VF-029 checked that the publisher means UTC — not because a naive string implies it. **Only `sum_open_interest` is ingested** of the file's eight columns: the four long/short ratios are separate series that would need their own declaration and their own measured lag, and `sum_open_interest_value` is the same position restated at a mark price the file does not carry. A column ingested with no reader is a maintenance surface nobody can later justify.

**Neither start date reaches the contract's listing, and they differ from each other.** That is why the start is probed per `(source, symbol)` and a window opening before it is refused rather than answered short.

### 4.2 Why three of them are not reachable from this system

None of these three is blocked on effort. Each is blocked on a decision that does not belong inside a data pull request.

- **`api.alternative.me` and `cryptopanic.com` are in no allowlist.** Reaching either means adding it to `ARCHIVE_HOSTS`, which [`docs/adr/0017`](docs/adr/0017-separate-archive-egress-path.md) sanctions for a public data host and which is a pull request labelled `safety:critical`. That is one decision, made on its own, not bundled with an ingestion change.
- **FRED cannot go in `ARCHIVE_HOSTS` at all.** ADR-0017's verification block states the decision is *refuted if* "`ARCHIVE_HOSTS` acquires a host with an authenticated endpoint", and FRED requires an API key. Making it reachable needs a superseding ADR introducing a third egress class for credentialed data sources — which is exactly the revisit trigger that ADR already names.
- **CryptoPanic additionally has unread terms.** The row above says so, and a source whose terms have not been read is not a source this project calls on a schedule.

Their measurements are kept regardless. The measurement is the expensive part, and `Delivery.EGRESS_NOT_PROVISIONED` states what is missing without pretending the source is fetchable.

---

## 5. News, macro, sentiment and on-chain

**The reachability survey.** All rows **measured** on 2026-08-03 by issuing the request without credentials and recording the status code. Measurement is used here in preference to documentation because these services change access policy more often than they update their docs.

Three of these — Fear & Greed, CryptoPanic and FRED — are now registered sources and **§4.1 is authoritative for them**. This table is kept because it is the wider survey, including the candidates that were probed and not adopted, and because a source that was refused is worth recording once rather than re-probing. Where the two disagree, §4.1 is the newer measurement.

| Source | Auth | Cost | Observed | Limit | `availability_lag` | Terms | Checked |
|---|---|---|---|---|---|---|---|
| **Fear & Greed** `api.alternative.me/fng/` | none | free | HTTP 200, 0.6 s | `not published`, no headers | ~24 h — the response carries `time_until_update` in seconds, so the lag is self-reported per call and should be read rather than assumed | public API, no stated restriction on automated use | 2026-08-03 |
| **GDELT DOC 2.0** `api.gdeltproject.org/api/v2/doc/doc` | none | free | HTTP 200, **14.9 s** for 3 records | `not published` | ~15 min (GDELT's own update cadence) plus the source's own publication delay | open data, automated querying is the intended use | 2026-08-03 |
| **FRED** `api.stlouisfed.org` | **API key** | free (registration) | HTTP 400 without a key | published per-key limits; not yet obtained | series-dependent, and **often revised** — treat a revision as a new row with a later `available_at`, never an update | requires attribution; automated use permitted | 2026-08-03 |
| **CryptoPanic** `cryptopanic.com/api/` | **API key** | free tier exists | HTTP 404 (`developer/v2` without key); HTTP 403 from Cloudflare (`v1` with placeholder token) | key-tier dependent | minutes — but it is an aggregator, so its timestamp is the *republication* time, not the first print. Clustering republications to one event with a first-print timestamp is required before this can be a feature at all ([`.claude/agents/news.md`](.claude/agents/news.md)) | key required; free tier terms not yet read | 2026-08-03 |
| **Reddit** `oauth.reddit.com` | **OAuth** | free tier exists | HTTP **403** on the unauthenticated `.json` endpoint, with `Retry-After: 0` | OAuth-tier dependent | seconds | registered OAuth app required; the free-tier terms restrict commercial and bulk use and must be read before #32 proceeds | 2026-08-03 |
| **mempool.space** `mempool.space/api/` | none | free | HTTP 200, 0.25 s | `not published` | seconds (mempool state) | public API; a self-hosted instance is the polite option at volume | 2026-08-03 |
| **blockchain.info** `api.blockchain.info/stats` | none | free | HTTP 200, 0.12 s | `not published` | ~minutes (block cadence) | public API | 2026-08-03 |

**The finding to plan #32 around:** the two sources most wanted for a sentiment feature — CryptoPanic and Reddit — both require credentials the project does not hold, and the two that are open are respectively *one number per day* and *a fifteen-second query*. There is no zero-credential, low-latency news or social feed here. Any design that assumes one is designing against a source that does not exist.

**On the 14.9-second GDELT response**: that is the service, not an outlier. It rules GDELT out of any request path and into a scheduled job with a generous timeout — and it is the kind of number that is obvious in a probe and invisible in documentation, which is why this table records observations rather than promises.

---

## 6. Adding a source

Add the row in the same pull request as the code that calls it, and state all seven columns. If you cannot state `availability_lag`, you cannot register a feature that uses the source — that is not a formality, it is what sizes the purge and embargo in cross-validation ([`.claude/rules/no-lookahead.md`](.claude/rules/no-lookahead.md)).

New hosts get their own egress path. They do **not** go in `PERMITTED_HOSTS`; that allowlist is for trading venues and widening it for a data source would trade the project's single most important structural guarantee for a convenience.
