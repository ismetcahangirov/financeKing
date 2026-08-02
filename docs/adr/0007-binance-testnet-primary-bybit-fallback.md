---
number: 0007
title: Binance testnet as the primary venue, Bybit testnet as the fallback behind one interface
date: 2026-08-03
status: accepted
deciders: [ismetcahangirov, architect, execution, market-research]
supersedes: null
superseded_by: null
related_issues: ["#16", "#112", "#22"]
related_adrs: [ADR-0006, ADR-0008]
---

## Context

The system trades on demo accounts only (ADR-0006). Which demo exchange it trades on decides the shape of the execution layer, the symbol universe, the rate-limit budget and where historical data comes from.

```
Forces:
- Onboarding must cost nothing and require no credential the user has to
  personally obtain beyond a generated key pair. Binance spot testnet needs
  only a GitHub OAuth authorisation -- no KYC, no document upload, no waiting
  period (VF-001).
- The venue must have free, complete, checksum-verified historical data for
  the production market, because cost models may only be calibrated from
  production (VF-008). data.binance.vision serves BTCUSDT 1-minute bars from
  2017-08-17 with a .zip.CHECKSUM sibling for every archive (VF-013, VF-014).
- Testnet is not production with fake money. Spot testnet is missing 79
  symbols present in production and futures testnet 189, in both directions
  (VF-006); order rate limits are tighter, 50/10s against 100/10s (VF-007);
  and balances are wiped roughly every 30 days while the API keys keep
  working (VF-005).
- A single venue is a single point of failure for a system that is supposed to
  run unattended for years, and "the venue abstraction works" is a claim that
  is false until a second implementation exists.
- Every additional venue costs a safety:critical pull request to widen the
  host allowlist, plus its own recorded-response fixture set.

The constraint that forces a decision now:
The execution layer's interface, the venue profile shape and the symbol
universe resolution are all written against a primary venue, and #22's
archive fetcher is written against a specific data host.
```

## Decision

**We use Binance testnet as the primary venue — spot and USD-M futures — and Bybit testnet as the fallback, both reached through one `ExecutionVenue` interface with per-venue `VenueProfile` data.** Every number that differs between venues or between testnet and production lives in the profile rather than in code: order-rate budget, `recvWindow`, keepalive interval, `clientOrderId` charset and length, timestamp unit, and `cost_model_calibratable`, which is `False` on every testnet profile so that `calibrate()` raises rather than warns. The tradable symbol universe is computed at startup as the intersection of the venue's listed set with the archive manifest, never assumed. Bybit is implemented in #112 to prove the abstraction is real; until then the interface has one caller and is therefore unproven.

## Alternatives considered

### Alternative 1 — Bybit testnet as primary, Binance as fallback (strongest rejected)

**What it would have given us.** Bybit's testnet is materially better as an *environment*. It is not wiped on a monthly cycle, so a balance and a set of open orders persist across weeks — which removes the single most disruptive operational fact in this project. Its testnet symbol coverage tracks production more closely, so the universe-intersection surprise (VF-006) is smaller. Its API is more uniform: one authentication scheme across spot and derivatives, rather than Binance's split between an Ed25519 `session.logon` handshake for spot user data and a `listenKey` with a keepalive for futures (VF-002, VF-003, VF-004) — a split that forces two genuinely different implementations behind one interface and is the single largest source of accidental complexity in the execution layer. Choosing Bybit would delete that complexity outright.

**Why it lost.** Data. Cost models may only be calibrated from production market data (`CLAUDE.md` §2), and Binance publishes complete, free, checksum-verified production archives at `data.binance.vision` going back to 2017 (VF-013, VF-014). Bybit has no equivalent public archive of that depth and completeness, which means calibrating a Bybit-primary system would require either paying for data — there is no budget — or calibrating against Binance production while trading Bybit, so the cost model would describe a different venue's microstructure than the one being traded. That is worse than the testnet-wipe problem, because a wrong cost model is silent and a wiped balance is loud.

The wipe objection is also the weaker one on inspection. Reconciliation from exchange state is a first-class feature regardless of venue (`ARCHITECTURE.md` §7): exchange state is the source of truth and local state converges to it. The monthly wipe does not create that requirement, it *exercises* it — a rebuild-from-venue path that runs monthly is a path that works, rather than one that first executes during an incident. The distinctive failure signature (authentication succeeds, requests succeed, account is simply empty) makes it diagnosable, and it is why reconciliation must distinguish a wipe (orders gone **and** balances reset) from an unrecorded rejection (order gone, balances intact).

**What survives the rejection, and is adopted.** Bybit's value as a second implementation is real and is kept: it is the fallback, and #112 exists specifically to prove the venue abstraction rather than to add capacity. An interface with one implementation is a guess about what varies, and the two-concrete-callers rule (`CLAUDE.md` §3) applies to venues as much as to anything else.

### Alternative 2 — a single venue, no abstraction until a second is needed

**What it would have given us.** The two-callers rule argues *for* this: write Binance concretely, extract an interface when Bybit actually arrives, and avoid the speculative abstraction that is the main way codebases become unnavigable. It would also be less code today, with no `VenueProfile` indirection to read through.

**Why it lost.** The second implementation already exists inside the first. Binance spot and Binance futures are not one venue with a flag — spot user data requires an Ed25519 `session.logon` bound to the socket, and futures requires a `listenKey` that outlives the socket and dies on a missed keepalive. Those are opposite session lifetimes (VF-003, VF-004), and modelling them as one mechanism with a branch leaks a futures `listenKey` on every spot reconnect or silently stops delivering futures fills. So there are two concrete callers on day one, and the rule is satisfied rather than violated.

Separately, the profile is not an abstraction over behaviour — it is **data**. Its job is to keep a testnet-measured constant from becoming a compiled-in fact, which is exactly how a 50/10s rate limit or a 7.5bp spread ends up governing a production-calibrated model. `cost_model_calibratable=False` living in the profile is what makes "never calibrate on testnet" a raised exception instead of a sentence in a document.

### Alternative 3 — do nothing (paper trading only, no venue)

```
Cost of the status quo: no order is ever formed, signed or acknowledged by a
real exchange, so the execution layer is verified only against our own
simulator. Every venue-specific defect -- filter rejections, recvWindow
drift, clientOrderId charset limits, the Unicode symbol in exchangeInfo
(VF-009) -- would be discovered by reading documentation rather than by
being rejected. P4 does not exist, and the end-to-end acceptance run (#113)
has nothing to run against.
Why that is no longer payable: the project's sentence is that a strategy is
validated and then executed on a demo account. Without a venue the second
half is unimplemented.
```

## Consequences

**What becomes easier**
- Onboarding is free and immediate: GitHub OAuth, a generated key pair, no KYC (VF-001). That is what makes the zero-budget assumption in `ARCHITECTURE.md` §13 hold rather than aspire.
- Production-calibrated cost models are possible at all, from checksum-verified public archives on a data-host egress path that is not the trading path (#22).
- The monthly wipe is a regularly exercised rebuild-from-venue drill rather than an untested recovery path.
- Venue-varying numbers are data, so adding a venue is writing a profile and a recorded-fixture set, not editing constants scattered through the execution layer.

**What becomes harder**
- Two user-data mechanisms must be maintained, tested and reconnected correctly, with opposite session lifetimes. This is the largest single source of complexity in `execution` and it is inherent to the venue, not to our design.
- The symbol universe must be intersected at startup and a missing requested symbol must be fatal, because a strategy configured against a production symbol list gets rejections whose message is about the symbol rather than about the environment (VF-006).
- Rate-limit budgets must default to the tighter testnet number. A limiter sized from production's published figures gets rejected on testnet, and rate-limit rejections during a flatten are the worst possible time to find out (VF-007).
- Adding Bybit costs a `safety:critical` pull request to widen the allowlist (ADR-0006) plus its own recorded-response corpus.

**What we now cannot do**
- Assume any testnet observation describes the market. Spread, depth, volume and fill probability from testnet are inadmissible as model inputs, enforced by `cost_model_calibratable=False` raising `UncalibratableVenue`. Reopening that would mean a testnet-calibrated cost model, which looks conservative and is fiction (VF-008).

## What would make us revisit this

```
Trigger:   Binance testnet key issuance requires KYC or a fee, OR spot testnet
           availability falls below 95% over any 30-day window, OR
           data.binance.vision stops publishing archives or checksums.
Observed:  The startup pre-flight's venue-reachability result, the
           `execution.venue.availability_ratio` panel, and #22's fetcher
           failure rate.
Then:      Promote Bybit to primary in a superseding ADR, and state explicitly
           where production cost-model data will come from -- that is the
           decision, not the venue swap.
```

## Verification

```
Confirmed if:  the startup universe intersection succeeds and the monthly
               reconciliation-after-wipe path completes without manual
               intervention on every occurrence through 2027-02-01
Refuted if:    any cost-model parameter is traced to testnet data, or a
               testnet-measured constant is found compiled into
               src/fking/execution/ outside a VenueProfile
Checked by:    execution agent, via `make test -k reconciliation` and the
               venue-profile assertions in tests/
Review date:   2027-02-01
```

## Definition of done

- [x] `number` is the next unused value in `docs/adr/` and the filename matches `NNNN-<kebab-slug>.md`
- [x] Context names one constraint that forces a decision
- [x] Decision is one paragraph, active voice, and names the owning module
- [x] The strongest rejected alternative is argued at its strongest, and the part of it that was correct is adopted rather than discarded
- [x] "Do nothing" is costed
- [x] All three Consequences lists are non-empty, including what we now cannot do
- [x] The revisit trigger is observable without judgement and names where it is observed
- [x] Verification states both a confirming and a refuting value, with a date and an owner
- [x] Linked from #16 and from `.claude/knowledge/decisions-log.md` (D-013, D-014, D-015)
