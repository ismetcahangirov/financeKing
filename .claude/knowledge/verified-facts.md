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
- Because it is unauthenticated bulk HTTP, it is also the one data path that does not need a credential — and it is still fetched through `guarded_client()`, because the allowlist is not a permission system, it is a proof about which hosts this process can reach at all.
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
