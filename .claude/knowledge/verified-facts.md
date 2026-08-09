# Verified Facts

Facts confirmed by direct research, with the date they were confirmed and the consequence they carry.

**The purpose of this file is to stop the same question being researched twice.** Research is the most expensive thing this project does per unit of output, and a fact that lives only in a closed session's context is a fact that will be re-purchased. If you are about to investigate something, read this file first. If you finish an investigation, add to it.

## Rules for this file

1. **Nothing enters without a verification method.** "I believe" and "the docs say" are not verification. "I made this request and observed this status code" is.
2. **Every entry is dated.** A fact about a third party is a fact about a *moment*. Undated, it is a rumour.
3. **Every entry states its re-verification trigger.** Some facts decay on a schedule; some decay when a dependency version changes; some do not decay at all.
4. **Entries are appended, never edited in place.** When a fact changes, add a superseding entry and mark the old one `SUPERSEDED by VF-NNN`, leaving both. The record of what we used to believe is what lets you debug code written under the old belief.
5. **Consequences, not trivia.** If a fact does not change what the code does, it does not belong here.
6. Things we tried to verify and could not are not facts. They go in [`./open-questions.md`](./open-questions.md).

Confidence values used below: **measured** (we made the observation ourselves), **documented** (vendor documentation, cross-checked against at least one other source), **derived** (follows necessarily from a measured fact).

---

## Index

| ID | Fact | Verified | Confidence | Decays |
|---|---|---|---|---|
| VF-001 | Spot testnet keys need only GitHub OAuth, no KYC | 2026-08-01 | measured | on Binance policy change |
| VF-002 | Spot `POST /api/v3/userDataStream` returns 410 Gone everywhere | 2026-08-01 | measured | on Binance API change |
| VF-003 | Spot user data requires Ed25519 `session.logon` + `userDataStream.subscribe` | 2026-08-01 | measured | with VF-002 |
| VF-004 | Futures `listenKey` still works | 2026-08-01 | measured | on Binance API change |
| VF-005 | Spot testnet wipes roughly every 30 days; keys survive, balances do not | 2026-08-01 | documented | never — design around it |
| VF-006 | Testnet is not a subset of production: 79 spot / 189 futures symbols missing | 2026-08-01 | measured | continuously; re-check at startup |
| VF-007 | Spot testnet order rate limit is 50/10s vs production 100/10s | 2026-08-01 | measured | on Binance limit change |
| VF-008 | Futures testnet: 7.5bp spread vs production 0.16bp, ~10x inflated volume | 2026-08-01 | measured | never — testnet is not a market |
| VF-009 | A deliberate Unicode symbol exists in testnet `exchangeInfo` | 2026-08-01 | measured | never — write Unicode-safe parsers |
| VF-010 | `ccxt` >= 4.5.70 is the only correct client for current Binance reality | 2026-08-01 | measured | on any client library release |
| VF-011 | `python-binance` is broken for spot user data | 2026-08-01 | derived | on `python-binance` release |
| VF-012 | Official `binance-sdk-*` shipped 11 and 16 major versions in ~12 months | 2026-08-01 | measured | quarterly |
| VF-013 | `data.binance.vision` serves BTCUSDT 1m from 2017-08-17, free, no auth | 2026-08-01 | measured | annually |
| VF-014 | Every `data.binance.vision` archive has a `.zip.CHECKSUM` sibling | 2026-08-01 | measured | annually |
| VF-015 | Spot timestamps became microseconds from 2025-01-01; futures stayed ms | 2026-08-01 | measured | never — it is a historical boundary |
| VF-016 | Futures kline CSVs have a header row; spot ones do not | 2026-08-01 | measured | on archive format change |
| VF-017 | Free full-depth L2 order book history does not exist | 2026-08-01 | measured | annually |
| VF-018 | The pinned `timescaledb-ha` digest carries PostgreSQL 16.14 and TimescaleDB 2.29.0 | 2026-08-03 | measured | on any image re-pin |
| VF-019 | Grafana's Loki-to-Tempo derived field only matches compactly separated JSON | 2026-08-03 | measured | on any change to the derived-field regex or the log renderer |
| VF-020 | Gemini's free tier trains on submitted content; Groq is contractually barred from training | 2026-08-03 | documented | on either provider's terms revision |
| VF-021 | Google and Mistral no longer publish free-tier rate limits at all | 2026-08-03 | measured | quarterly |
| VF-022 | Groq free-tier TPM ceilings are 6k–12k, below a single full-size agent prompt | 2026-08-03 | documented | quarterly |
| VF-023 | Groq schema-strict output exists on two models only, and never with tool use | 2026-08-03 | documented | on any Groq model release |
| VF-024 | GitHub Models was fully retired on 2026-07-30 | 2026-08-03 | documented | never — it is gone |
| VF-025 | `mlfinlab` has no installable release; `pandas-ta`'s upstream has vanished | 2026-08-03 | measured | annually |
| VF-026 | TA-Lib 0.7.1 ships prebuilt wheels everywhere this project runs — no C build | 2026-08-03 | measured | on any `ta-lib` release |
| VF-027 | Reddit's unauthenticated JSON endpoints return 403; GDELT takes ~15s per query | 2026-08-03 | measured | quarterly |
| VF-028 | Funding history starts 2020-01 and open interest 2020-09-01, neither at the perpetual's listing | 2026-08-05 | measured | annually, and per new symbol |
| VF-029 | `fundingRate` is monthly-only, `metrics` daily-only, and `metrics` stamps a naive datetime string | 2026-08-05 | measured | on archive format change |
| VF-030 | The Fear & Greed value stamped for a day is refreshed at the end of that day | 2026-08-05 | measured | quarterly |

---

## VF-001 — Binance spot testnet API keys require only GitHub OAuth. No KYC.

- **Verified**: 2026-08-01 · **Confidence**: measured
- **Method**: completed the key-issuance flow at `testnet.binance.vision` end to end. The only identity step is a GitHub OAuth authorisation. No document upload, no residency question, no waiting period.
- **Consequence**: testnet onboarding is free, immediate, and requires no credential the user must personally supply beyond the generated key pair. This is what makes the zero-budget assumption in [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) §13 hold. It is also why Bybit testnet is only a *fallback* rather than a parallel primary — we do not need a second identity relationship.
- **Re-verify**: if Binance changes testnet access policy, or if key issuance starts failing.

## VF-002 — Spot `POST /api/v3/userDataStream` returns **410 Gone**. On testnet and on production alike.

- **Verified**: 2026-08-01 · **Confidence**: measured
- **Method**: direct signed `POST` to `/api/v3/userDataStream` against both the testnet and production spot REST hosts. Both returned HTTP 410. This is not a testnet limitation and not a transient outage — it is the endpoint's retirement.
- **Consequence**: **every tutorial, blog post and Stack Overflow answer describing spot user data is wrong.** So is most library code written before this change. Any spot implementation that starts by creating a `listenKey` is dead on arrival, and the failure mode is a clean 410 rather than a confusing one, so you will notice immediately — the danger is spending a day building around the pattern before you make the first call.
- **Re-verify**: only if Binance announces a reinstatement. Do not "re-check whether it works now" as a debugging step; it is not a flake.

## VF-003 — Spot user data now requires an Ed25519 `session.logon` handshake on the WebSocket API, followed by `userDataStream.subscribe`.

- **Verified**: 2026-08-01 · **Confidence**: measured
- **Method**: established a WebSocket API session with an Ed25519 key pair, issued `session.logon`, then `userDataStream.subscribe`, and received account update events.
- **Consequence**:
  - The key type is **not interchangeable**. An HMAC-SHA256 key cannot perform `session.logon`. Spot user data requires an Ed25519 key pair registered separately, so the system holds **two credentials of different types for the same account** and must not conflate them in config.
  - The signing scheme is different (Ed25519 over the canonical payload, not HMAC over a query string), so the "just sign it like the REST calls" instinct produces an authentication failure that reads like a bad secret.
  - Session state is now a thing that exists. A dropped WebSocket loses the logged-on session, not merely a subscription, so reconnection must re-run the handshake before re-subscribing.
- **Consequence for design**: spot and futures user data are **genuinely different mechanisms**, not two configurations of one mechanism. They are modelled as two implementations behind one interface ([`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) §7). Attempting to unify them produces an abstraction that lies about both.
- **Re-verify**: with VF-002.

## VF-004 — Futures `listenKey` still works.

- **Verified**: 2026-08-01 · **Confidence**: measured
- **Method**: created a futures `listenKey`, opened the user data stream, received events; keepalive extended it.
- **Consequence**: the futures path keeps the classic model — create the key, open the stream, `PUT` a keepalive before expiry, recreate on reconnect. Do not "modernise" it to match the spot path. Do not assume the spot change will propagate to futures on any particular timetable; that is a prediction, not a fact.
- **Re-verify**: if futures user data events stop arriving without a corresponding connection error.

## VF-005 — Binance spot testnet is wiped roughly every 30 days. API keys survive. Balances and open orders do not.

- **Verified**: 2026-08-01 · **Confidence**: documented (cadence is approximate — see [`./open-questions.md`](./open-questions.md) OQ-004)
- **Consequence**: this is the fact that makes reconciliation a **first-class feature rather than a nicety**. The system must be able to rebuild its entire view of the world from the exchange at any moment, because at an unannounced point the exchange's view will change out from under it and the credentials will keep working, so nothing will error.
  - The exchange is the source of truth. Local state converges to it, never the reverse.
  - A test or a strategy that assumes a balance persists across a month is broken by design, not by accident.
  - The failure signature is distinctive: authentication succeeds, requests succeed, and the account is simply empty with no open orders. If you see that, check the wipe before you debug anything else.
- **Re-verify**: never — design for it unconditionally. Tighten the cadence estimate opportunistically by recording observed wipe dates.

## VF-006 — Testnet is not a subset of production. Spot testnet is missing **79** symbols present in production; futures testnet is missing **189**.

- **Verified**: 2026-08-01 · **Confidence**: measured
- **Method**: pulled `exchangeInfo` from testnet and production for both markets and diffed the symbol sets.
- **Consequence**: the intuitive model — "testnet is production with fake money and fewer coins" — is wrong in the direction that hurts. A symbol universe cannot be assumed; it must be **intersected at startup** and the result logged. A strategy configured against a production symbol list will get order rejections on symbols that simply do not exist, and the rejection reason will be about the symbol being invalid rather than about the environment, which sends you looking in the wrong place.
- **Note**: the counts are a snapshot. The *shape* of the fact — that the sets differ in both directions and must be intersected — is what is durable. Do not hardcode 79 or 189 anywhere.
- **Re-verify**: continuously, as a startup pre-flight, not as a research task.

## VF-007 — Spot testnet order rate limit is **50 orders / 10s**; production is **100 / 10s**.

- **Verified**: 2026-08-01 · **Confidence**: measured
- **Consequence**: testnet is the *tighter* environment for order throughput. That cuts both ways:
  - A throughput figure measured on testnet understates what production would permit — so testnet is a conservative place to test rate-limit handling, which is useful.
  - Sizing a rate limiter from production's published numbers will get you rejected on testnet, and rate-limit rejections during a position-flattening sequence are exactly the wrong time to discover this.
- The limiter therefore takes its budget from configuration keyed by environment, defaults to the tighter number, and never assumes the looser one.
- **Re-verify**: on any Binance rate-limit announcement, and whenever `exchangeInfo` rate-limit blocks change shape.

## VF-008 — Futures testnet showed a **7.5bp** spread against production's **0.16bp**, with roughly **10x inflated volume**.

- **Verified**: 2026-08-01 · **Confidence**: measured
- **Method**: sampled top-of-book and traded volume on the same instrument across both environments over the same window.
- **Consequence** — this is a **hard rule, stated in [`../../CLAUDE.md`](../../CLAUDE.md) §2**: testnet must never be used to calibrate a cost model, an impact model, a fill-probability model, or a capacity estimate. Roughly 47x the spread and an order of magnitude of fake volume means a strategy calibrated on testnet is being fitted to a market that does not exist. It would look uneconomic where it is fine, or — far worse — look tradeable at a size the real book cannot absorb, because inflated volume flatters every participation-rate assumption.
- Testnet is an **execution-plumbing environment**: it tells you whether your order was formed correctly, signed correctly, and acknowledged. It tells you nothing about price.
- Cost model parameters come from `data.binance.vision` production archives (VF-013).
- **Re-verify**: never. Even if testnet liquidity improved, calibrating on it would be a coincidence rather than a method.

## VF-009 — A deliberate Unicode symbol exists in testnet `exchangeInfo`.

- **Verified**: 2026-08-01 · **Confidence**: measured
- **Consequence**: it is there to break naive parsers, and it will. Symbol handling must:
  - never assume ASCII, never gate on `str.isalnum()` or a `[A-Z0-9]+` regex to decide validity;
  - never crash on encode when logging or writing to a file opened without an explicit `encoding="utf-8"` — this is a particular hazard on Windows, where the default encoding is not UTF-8;
  - never use the symbol string as a filesystem path component without sanitising;
  - carry the symbol through to the database as `TEXT`, which is Unicode-safe, rather than through any fixed-width byte path.
- The correct behaviour is to parse it successfully and then exclude it by the universe intersection (VF-006), not to crash on it and not to special-case it by name.
- **Re-verify**: never — write Unicode-safe parsers unconditionally.

## VF-010 — `ccxt` >= 4.5.70 is the exchange client, because it is currently the only one correct on both the endpoint split and the post-`listenKey` user-data model.

- **Verified**: 2026-08-01 · **Confidence**: measured
- **Consequence**: the version floor is not cosmetic — it is the version at which the corrections are present. Do not relax it. Do not substitute a "lighter" client to reduce a dependency; the weight of `ccxt` is the cost of it being right about VF-002 and VF-003.
- **Re-verify**: on any `ccxt` major release, and whenever a competing client claims to have fixed spot user data.

## VF-011 — `python-binance` is broken for spot user data.

- **Verified**: 2026-08-01 · **Confidence**: derived from VF-002 and VF-003
- **Consequence**: it still builds around `listenKey` for spot, which returns 410 (VF-002). It is the most-recommended library in search results and in model training data, which means **an agent or a human will propose it**. This entry exists so that proposal can be closed in one line rather than one afternoon.
- **Re-verify**: on a `python-binance` release that claims Ed25519 WebSocket-API session support.

## VF-012 — The official `binance-sdk-*` packages shipped **11** and **16** major versions in roughly twelve months.

- **Verified**: 2026-08-01 · **Confidence**: measured (release histories)
- **Consequence**: disqualifying for unattended operation. This system runs for weeks without supervision; a dependency averaging more than one breaking change per month is a scheduled outage. `binance-connector` is frozen, which is the opposite failure. `ccxt` is the remaining option, and VF-010 is why it is also the correct one rather than merely the surviving one.
- **Re-verify**: quarterly. If the release cadence stabilises for a year, this decision is worth revisiting via an ADR.

## VF-013 — `data.binance.vision` serves BTCUSDT 1-minute klines from **2017-08-17**, free, no authentication.

- **Verified**: 2026-08-01 · **Confidence**: measured
- **Consequence**: this is the project's historical data foundation and it costs nothing. 2017-08-17 is the earliest BTCUSDT 1m date; **other symbols start later and each symbol's earliest clean date must be recorded individually** in the availability contract. A hypothesis inherits the shortest history among its inputs, which is usually much shorter than the BTC history that made the idea seem testable.
- Because it is unauthenticated bulk HTTP, it is the one data path that needs no credential — and that asymmetry was made structural in #22 rather than wasted. It is fetched through `guarded_archive_client()`, a **second** compiled-in allowlist (`ARCHIVE_HOSTS`) with its own credential-free client, and **not** through `guarded_client()`. `PERMITTED_HOSTS` is not a permission system; it is a proof about which hosts a process holding order-placement code can reach at all, and `data.binance.vision` never enters it. ADR 0017.
- **Re-verify**: annually, and whenever a new symbol is onboarded (its own earliest date is a separate fact).

## VF-014 — Every `data.binance.vision` archive has a `.zip.CHECKSUM` sibling.

- **Verified**: 2026-08-01 · **Confidence**: measured
- **Consequence**: there is no excuse for trusting an unverified archive. Verify before extract, always — a truncated download produces a short file that parses cleanly into a shorter series, and a shorter series does not raise, it just quietly changes every statistic computed from it. That is the worst possible failure shape for this project.
- The checksum is fetched and compared as part of ingestion, not as an optional integrity mode.
- **Re-verify**: annually.

## VF-015 — Binance **spot** timestamps became **microseconds** from **2025-01-01**. **Futures** stayed **milliseconds**.

- **Verified**: 2026-08-01 · **Confidence**: measured · **ADR**: `docs/adr/0013`
- **Consequence**: the unit is a function of `(market, date)` and normalization must be keyed on exactly that. A global constant is wrong for at least one half of the corpus, and a per-market constant is wrong for spot before 2025.
- **The failure is silent in one direction and loud in the other**, which is why it is dangerous: parsing microseconds as milliseconds yields dates roughly a thousand times further from the epoch — obviously absurd, easy to catch. Parsing milliseconds as microseconds yields dates in 1970 — also obvious. But mixing the two *within one series* while resampling produces a series with correct-looking endpoints and corrupted spacing in the middle, and nothing raises.
- Always print the first and last raw integers and confirm they render as sane UTC before trusting a load.
- **Re-verify**: never — it is a fixed historical boundary. It applies to archives forever.

## VF-016 — Futures kline CSVs have a header row. Spot kline CSVs do not.

- **Verified**: 2026-08-01 · **Confidence**: measured
- **Consequence**: a shared loader with `header=None` silently ingests the futures header as a data row, producing one row of strings in numeric columns. Depending on the reader, that becomes `NaN` (a one-bar gap at the start of every futures file, which is easy to miss) or a dtype promotion to `object` (which makes every downstream numeric operation slow and subtly wrong).
- The loader detects the header per file, keyed on `(market, date)` like VF-015, rather than being configured globally.
- **Re-verify**: on any observed change to the archive format.

## VF-017 — Free full-depth L2 order book history does not exist.

- **Verified**: 2026-08-01 · **Confidence**: measured
- **Detail**: Binance `bookDepth` is **not** snapshots. It is aggregated depth bands sampled roughly once per minute. It cannot be replayed into a book, and it cannot answer any question about queue position or resting-liquidity dynamics.
- **Consequence**: the zero-budget data ceiling is **tick trades, top-of-book on futures, and coarse depth bands**. Everything downstream inherits this:
  - Queue-position and order-book-imbalance strategies are **untestable here**, not merely unimplemented. Record them as untestable and move on.
  - Passive fill probability is **unmeasurable**, which is why a maker-fill assumption in a backtest is an unfalsifiable input chosen because it produces the desired answer. Cost models assume taker execution unless a maker assumption can be evidenced.
  - The feature store **refuses** requests for data we do not have, rather than returning a proxy, so a strategy cannot silently assume richer data ([`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) §6).
- **Re-verify**: annually, or if a budget ever exists. This is the single constraint most likely to be lifted by money.

---

## Adding an entry

Append at the end with the next `VF-NNN`, add a row to the index, and state: the claim in one sentence as a heading, the verification date, the confidence level, the method that produced it, the consequence for the code, and the re-verification trigger.

If the fact contradicts an existing entry, do not edit the old one. Add the new entry and mark the old one `SUPERSEDED by VF-NNN`.

If you could not verify it, it belongs in [`./open-questions.md`](./open-questions.md) instead — and putting it there is a useful contribution, not a failure.

## VF-018 — The `timescaledb-ha` digest pinned in `docker-compose.yml` is PostgreSQL 16.14 with TimescaleDB 2.29.0, and it accepts the modern hypertable syntax.

- **Verified**: 2026-08-03 · **Confidence**: measured
- **Method**: started the pinned digest (`sha256:15c65f41...add85`) and queried `SELECT version()` and `SELECT extversion FROM pg_extension WHERE extname='timescaledb'`, which returned `PostgreSQL 16.14 ... 64-bit` and `2.29.0`. Then applied `migrations/versions/0003_market_data.py` against it and read back `timescaledb_information.hypertables` and `.jobs`, which reported both `bar` and `funding_rate` as compression-enabled with a `compress_after` of `30 days`.
- **Consequence**: `create_hypertable(relation, by_range(column, interval))` is the correct call, not the deprecated two-argument `create_hypertable(relation, 'column')` form that most published examples still use. `by_range` has existed since TimescaleDB 2.13, so the modern form is safe against this digest and every plausible newer one, while the legacy form is on a removal path. `ALTER TABLE ... SET (timescaledb.compress, ...)` and `add_compression_policy` both still work on 2.29 despite the columnstore renaming in the 2.18 line, so the migration does not need version-conditional DDL.
- **Re-verify**: whenever the `timescaledb-ha` digest in `docker-compose.yml` is re-pinned. `tests/platform/persistence/test_market_data.py::test_market_data_tables_are_compressed_hypertables` fails loudly if a newer image stops accepting either statement, so this is a re-read of the version numbers rather than a re-derivation of the consequence.


## VF-019 — Grafana's provisioned Loki-to-Tempo derived field only matches log lines whose JSON has no space after the colon.

- **Verified**: 2026-08-03 · **Confidence**: measured
- **Method**: brought up the pinned Grafana, Loki, Tempo and OpenTelemetry Collector digests, emitted a two-span trace through the Collector on `127.0.0.1:4317`, and read the trace back both directly (`GET :3200/api/traces/<id>`) and through Grafana's datasource proxy (`GET :3001/api/datasources/proxy/uid/fking-tempo/api/traces/<id>`, HTTP 200). Then compared the rendered log line against the `matcherRegex` in `ops/grafana/provisioning/datasources/datasources.yaml`.
- **Detail**: the provisioned regex is `"trace_id":"([a-f0-9]{32})"`. `json.dumps` defaults to `separators=(", ", ": ")`, so a structlog `JSONRenderer` left at its defaults emits `"trace_id": "38b0..."` — with a space — and the regex does not match.
- **Consequence**: `structlog.processors.JSONRenderer` is configured with `separators=(",", ":")` in `fking.platform.logging.build_processor_chain`, and this is a correctness requirement rather than a size optimisation. The failure mode is the reason it is worth an entry: nothing raises, no panel errors, and no alert fires — the "open the trace from this log line" affordance simply does not appear, and an investigator concludes the trace was never recorded. `tests/platform/logging/test_pipeline.py` asserts a rendered line against the regex *and* asserts that the regex is still the one the provisioning file carries, because the two artifacts fail independently.
- **Re-verify**: whenever either the derived-field regex or the log renderer's separators change.


## VF-020 — The Gemini free tier trains on what you send it. Groq is contractually barred from training on what you send it.

- **Verified**: 2026-08-03 · **Confidence**: documented
- **Method**: fetched `ai.google.dev/gemini-api/docs/pricing`, which marks every free-tier model *"data used to improve products: Yes"*, and `ai.google.dev/gemini-api/terms`, which states *"Google uses the content you submit to the Services and any generated responses to provide, improve, and develop Google products and services and machine learning technologies"*, that *"human reviewers may read, annotate, and process your API input and output"*, and *"Do not submit sensitive, confidential, or personal information to the Unpaid Services."* Then fetched `console.groq.com/docs/legal/services-agreement` §4.2: *"Groq is not permitted to use Inputs or Outputs for training or fine-tuning any AI Model Services or other models, unless explicitly granted permission or instructed by Customer."* `console.groq.com/docs/your-data` adds *"By default, Groq does not retain customer data for inference requests"*, Zero Data Retention for all customers, and abuse/troubleshooting logs kept *"up to 30 days"*.
- **Consequence**: ADR-0009 made Gemini primary and Groq fallback on quota and availability grounds; the data question was never raised in it. This project's agent prompts carry hypothesis statements, strategy specifications and critique of both — which is the entire research output of the system. On the free Gemini tier that is training data with human review attached. The recommendation in [`../../docs/research/free-tier-landscape.md`](../../docs/research/free-tier-landscape.md) §7.1 is a **data-classification field on the agent declaration** rather than a straight provider swap, because VF-022 shows Groq cannot carry the workload alone.
- **Re-verify**: on any revision to either document. Both are versioned pages; a diff is the trigger.

## VF-021 — Google and Mistral no longer publish free-tier rate limits. The numbers are behind an account login.

- **Verified**: 2026-08-03 · **Confidence**: measured (the absence was observed, not inferred)
- **Method**: fetched `ai.google.dev/gemini-api/docs/rate-limits` and its `.md.txt` source. Neither contains a free-tier table; the page says *"Rate limits depend on a variety of factors (such as your usage tier) and can be viewed in Google AI Studio"* and links to a per-account page. Mistral's help centre likewise states only that *"Free mode (the default) has the lowest limits"* and directs to the Admin Console limits page.
- **Consequence**: the widely circulated `gemini-2.5-flash` free-tier figures (10 RPM / 250,000 TPM / 250 RPD) appear only in community forum posts and must be treated as **unverified** wherever they surface in configuration or comments. More usefully, this confirms the shape [`../../docs/rules/quota-management.md`](../../docs/rules/quota-management.md) already had: the ledger measures reality and the configured limit is a conservative floor the ledger corrects. That is now the *only* correct design for these two providers rather than merely a defensive one. Two facts that *are* published and do change code: Gemini limits are scoped **per project, not per API key**, and RPD resets at **midnight Pacific** — neither UTC nor local, so the day boundary needs a test rather than a comment.
- **Re-verify**: quarterly, or whenever a limit needs to be raised in configuration.

## VF-022 — No Groq free-tier model can accept a single 45k-token prompt. The binding limit is tokens per minute, not requests.

- **Verified**: 2026-08-03 · **Confidence**: documented
- **Method**: `console.groq.com/docs/rate-limits`, per model: `llama-3.3-70b-versatile` 30 RPM / 1,000 RPD / 12,000 TPM / 100,000 TPD; `openai/gpt-oss-120b` and `-20b` 30 / 1,000 / 8,000 / 200,000; `llama-3.1-8b-instant` 30 / 14,400 / 6,000 / 500,000. Limits are organization-level, not per key.
- **Consequence**: `.claude/agents/quant.md` declares a budget of ≤ 45k tokens per invocation. A request larger than the per-minute allowance cannot succeed by waiting, so **that agent has no Groq fallback at all** — the failover is a fiction on exactly the path where it matters, and it would be discovered at 03:00 with Gemini degraded. Either the agent's context budget comes down under 8k or the fallback claim is withdrawn. Separately, `x-ratelimit-*` headers are returned on **successful** responses, so the true limits are readable without ever provoking a 429, and `retry-after` is present only when a limit is actually exceeded — which makes the honour-`Retry-After`-never-retry-in-window rule implementable against Groq exactly as written.
- **Re-verify**: quarterly, and on any Groq tier announcement.

## VF-023 — Groq enforces a JSON schema on two models only, and never at the same time as tool use.

- **Verified**: 2026-08-03 · **Confidence**: documented
- **Method**: `console.groq.com/docs/structured-outputs`. `strict: true` schema enforcement is documented for `openai/gpt-oss-20b` and `openai/gpt-oss-120b`. Every other model, including `llama-3.3-70b-versatile`, gets `{"type": "json_object"}` — valid JSON syntax with no guarantee about shape. The page states *"streaming and tool use are not currently supported with Structured Outputs"*. Gemini by contrast documents an enforced response schema and documents combining it with function calling, the combined example being Gemini 3 series only.
- **Consequence**: [`../../docs/rules/llm-output-handling.md`](../../docs/rules/llm-output-handling.md) requires schema-validated output with **zero re-asks**, so the parse-failure rate is the instrument that tells you a prompt is wrong. Unenforced generation raises that rate for a reason that is not the prompt, which blinds the instrument. Any Groq-served agent must therefore run on `openai/gpt-oss-120b` or `-20b`, and inherits their 8,000 TPM ceiling from VF-022. The `llama-3.3-70b-versatile` id that appears in the example configuration in [`../../docs/rules/quota-management.md`](../../docs/rules/quota-management.md) should become `openai/gpt-oss-120b` when the gateway is built, because illustrations get copied.
- **Re-verify**: on any Groq model release.

## VF-024 — GitHub Models was fully retired on 2026-07-30.

- **Verified**: 2026-08-03 · **Confidence**: documented
- **Method**: `docs.github.com/en/github-models/prototyping-with-ai-models`: *"As of July 30, 2026, GitHub Models has been fully retired. The playground, model catalog, inference API, and bring your own key (BYOK) are no longer available to any customer."*
- **Consequence**: it was named in #19 as a provider to evaluate. It is not a candidate, and any design note or backlog item that lists it should be corrected rather than left to be rediscovered. Four days elapsed between the retirement and this check, which is the argument for the quarterly re-verification cadence in [`../../SOURCES.md`](../../SOURCES.md) — a provider can disappear inside one sprint.
- **Re-verify**: never.

## VF-025 — `mlfinlab` has no installable release, and `pandas-ta`'s upstream has vanished while it remains installable.

- **Verified**: 2026-08-03 · **Confidence**: measured
- **Method**: `GET pypi.org/pypi/mlfinlab/json` returns **404** — no releases. Its GitHub repository's last push is **2023-10-02** and GitHub resolves its license to `NOASSERTION`. For `pandas-ta`: latest release `0.4.71b0` dated **2025-09-14**, still classified Beta, and both declared project URLs are dead — the repository `twopirllc/pandas-ta` returns **404** and the homepage `pandas-ta.dev` does not resolve in DNS (`www.` fails to resolve, the apex times out).
- **Consequence**: `mlfinlab` was OQ-002's primary candidate for CPCV, purging, embargo and the deflated Sharpe ratio. There is no library to adopt, so **every statistic that gates a promotion decision must be implemented in-project** and validated against a hand-computed worked example from its source paper — which OQ-002 already identified as the real risk, since a bug in a penalty term almost always understates the penalty and therefore fails in the flattering direction. The `pandas-ta` case is the more instructive failure: a package whose upstream has disappeared but whose wheel still installs is *worse* than one that was removed, because `uv sync` keeps succeeding and nothing signals the abandonment. Indicators come from TA-Lib (VF-026) or are written here.
- **Re-verify**: annually. A resurrection would be surprising and would still not restore three years of missing review.

## VF-026 — TA-Lib 0.7.1 ships prebuilt wheels for every platform this project runs on. The C-build objection is dead.

- **Verified**: 2026-08-03 · **Confidence**: measured
- **Method**: `GET pypi.org/pypi/ta-lib/json` — version 0.7.1 (2026-07-16) publishes 54 wheels and one sdist, covering `manylinux_2_17`/`_2_28` on x86_64 and aarch64, `musllinux_1_2`, macOS 13/14, and `win_amd64`.
- **Consequence**: the standing reason to prefer a pure-Python indicator library over TA-Lib was that TA-Lib wraps a C library needing a source build inside Docker. There is no compile step on any target platform, which removes the only real argument for `pandas-ta` at exactly the moment VF-025 removed `pandas-ta`. It matters twice on the development machine: #117 records that Windows Smart App Control blocks some compiled artefacts, and a signed prebuilt `win_amd64` wheel avoids the source-build path that triggers it.
- **Re-verify**: on any `ta-lib` release — wheel coverage is a policy of that project and can narrow.

## VF-027 — Reddit's unauthenticated JSON endpoints return 403, and a GDELT query takes about fifteen seconds.

- **Verified**: 2026-08-03 · **Confidence**: measured
- **Method**: unauthenticated requests, status and wall-clock recorded. `reddit.com/r/CryptoCurrency/new.json` → **403** with `Retry-After: 0` and an HTML body. `api.gdeltproject.org/api/v2/doc/doc` with `maxrecords=3` → **200 in 14.9 s**. `api.stlouisfed.org/fred/...` → **400**, *"Variable api_key is not set."* `cryptopanic.com/api/developer/v2/posts/` → **404**, and `/api/v1/posts/` with a placeholder token → **403** from Cloudflare. Open and fast, for contrast: `api.alternative.me/fng/` → 200 in 0.6 s, `mempool.space/api/v1/fees/recommended` → 200 in 0.25 s, `api.blockchain.info/stats` → 200 in 0.12 s.
- **Consequence**: the two sources most wanted for a sentiment feature — CryptoPanic and Reddit — both require credentials this project does not hold, and the two that are open are respectively *one number per day* and *a fifteen-second query*. **There is no zero-credential, low-latency news or social feed**, and #32 should be planned on that basis rather than discovering it mid-implementation. The GDELT latency is the service rather than an outlier, which rules it out of any request path and into a scheduled job with a generous timeout. Reddit's 403 has the same shape as VF-002: every tutorial and code sample that appends `.json` to a Reddit URL is now wrong, and the failure is a clean status code rather than a confusing one. The Fear & Greed response carries `time_until_update` in seconds, so that source reports its own availability lag per call and the value should be read rather than assumed.
- **Re-verify**: quarterly. These probes need no credentials, which makes them the cheapest row in [`../../SOURCES.md`](../../SOURCES.md) §5 and the most likely to catch a real change.

## VF-028 — Funding-rate and open-interest history do not reach a perpetual's listing date, and each starts on its own date.

- **Verified**: 2026-08-05 · **Confidence**: measured
- **Method**: `HEAD`/`GET` against `data.binance.vision`, recording status codes at the boundary. `monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-YYYY-MM.zip` → **404** for 2019-09, 2019-10, 2019-11 and 2019-12; **200** from 2020-01. ETHUSDT is the same boundary: 2019-11 → 404, 2020-01 → 200. `daily/metrics/BTCUSDT/BTCUSDT-metrics-YYYY-MM-DD.zip` → **404** through 2020-08-31 and **200** from 2020-09-01, confirmed by bisection at 2020-06-01, 2020-07-31, 2020-08-15, 2020-08-20, 2020-08-25 and 2020-08-27 through 2020-08-31.
- **Consequence**: this answers OQ-006 in the direction that costs work. The USDⓈ-M corpus begins 2019-09-08; funding begins **four months** later and open interest **almost a year** later, and the two boundaries are different, so neither can be derived from the other or from the contract's listing. A backtest that assumed either reached the listing runs a window whose first months are empty, and an empty window reads downstream as *no signal in this period* rather than *no data in this period* — a strategy scored on history it never saw. So the start date is probed per `(source, symbol)` at startup by `fking.data.alt.probe`, recorded in an `AltAvailability`, and a window opening before it is refused rather than answered short. The probe is a binary search over the archive's period index against the `.CHECKSUM` sibling, so it costs roughly seven requests for a monthly series and twelve for a daily one.
- **Re-verify**: annually, and whenever a symbol joins the universe — the boundary is per symbol and only BTCUSDT and ETHUSDT have been measured.

## VF-029 — `fundingRate` is published monthly only, `metrics` daily only, and `metrics` stamps a naive datetime string rather than an epoch.

- **Verified**: 2026-08-05 · **Confidence**: measured
- **Method**: `daily/fundingRate/BTCUSDT/...` → **404** for 2024-01-02, 2026-07-15 and 2026-08-03, while `monthly/fundingRate/...` → **200**. `monthly/metrics/BTCUSDT/BTCUSDT-metrics-2024-01.zip` → **404**, while `daily/metrics/...-2024-01-02.zip` → **200**. Reading the two members: funding is `calc_time,funding_interval_hours,last_funding_rate` with a header row and a millisecond epoch; metrics is `create_time,symbol,sum_open_interest,...` with a header row and `create_time` serialised as `2024-01-02 00:00:00` — a **naive datetime string carrying no offset**, sampled every five minutes.
- **Consequence**: three separate things. First, `resolve_granularity` decides daily-versus-monthly by distance from today and is therefore wrong for both of these datasets, in opposite directions, on every date — so granularity is declared per dataset in `fking.data.alt.registry.ARCHIVE_GRANULARITY` and every fetch passes it explicitly. Second, this is a fourth instance of the trap class `docs/adr/0013` is about — a timestamp encoding that no field, header or filename announces, where the wrong assumption produces no exception, and the quietest of the four: `datetime.fromisoformat` accepts the string and returns a *naive* datetime that is correct to the second and joins against nothing. Third, `ArchiveFormat` therefore carries a `timestamp_encoding` with three members rather than an `epoch_unit` with two (#155), `(futures_um, metrics)` is declared in `DECLARED_FORMATS` like every other format, and `require_epoch_unit()` refuses on the non-epoch declaration rather than returning a `None` somebody picks a unit for.
  - Between #32 and #155 this fact had a fourth consequence that no longer holds and is recorded here because the shape recurs: `metrics` was *fetchable and probeable but not parseable*, and `PARSED_SOURCES` existed to state that asymmetry rather than hide it. The split between "is this source declared" and "can its payload be read" survives the parser; the instance does not.
- **Re-verify**: on any archive format change. The `metrics` parser landed in #155 against the recording at `tests/fixtures/alt/futures_um/metrics/BTCUSDT/`.

## VF-030 — The Fear & Greed value stamped for a day is refreshed at the *end* of that day, so it is not knowable at its own timestamp.

- **Verified**: 2026-08-05 · **Confidence**: measured
- **Method**: `GET api.alternative.me/fng/?limit=3` at **2026-08-05T06:05Z** → 200. The most recent entry carries `timestamp` = 1785888000 (2026-08-05T00:00:00Z) and `time_until_update` = **64,448 s** ≈ 17.9 h, which places the next refresh at roughly 2026-08-05T23:59Z. Older entries carry no `time_until_update` at all.
- **Consequence**: the index is stamped with the *start* of the day it is refreshed at the *end* of, so a value labelled day D may incorporate observations from within D. Joining it on its own timestamp is look-ahead of up to a full day — small in wall-clock terms and total in information terms for a daily-cadence sentiment feature, which is the only cadence this source has. `alternative.me.fearGreed` therefore declares `availability_lag = 24 h`: the value stamped for D is knowable from D+1 00:00Z. The source is not reachable from this system today — `api.alternative.me` is in no allowlist — so the declaration is registered and the fetch is refused rather than stubbed.
- **Re-verify**: quarterly, alongside VF-027's probes. `time_until_update` is self-reported and should be read per call rather than assumed once the source is reachable.
