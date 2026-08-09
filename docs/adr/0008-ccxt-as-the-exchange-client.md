---
number: 0008
title: ccxt >= 4.5.70 as the only exchange client library
date: 2026-08-03
status: accepted
deciders: [ismetcahangirov, architect, execution]
supersedes: null
superseded_by: null
related_issues: ["#16", "#112"]
related_adrs: [ADR-0006, ADR-0007]
---

## Context

Something has to speak Binance's REST and WebSocket protocols: signing, rate-limit headers, symbol and precision metadata, order lifecycle, and two different user-data mechanisms.

```
Forces:
- Binance retired spot user data's listenKey endpoint. POST
  /api/v3/userDataStream returns 410 Gone on testnet and production alike
  (VF-002), and spot now requires an Ed25519 session.logon handshake on the
  WebSocket API followed by userDataStream.subscribe (VF-003). Futures
  listenKey still works (VF-004). Most library code and essentially every
  tutorial predates this.
- The system runs unattended for years. A dependency that ships breaking
  changes frequently is a dependency that breaks the system while nobody is
  watching.
- Whatever is chosen must not construct its own transport, because every
  request has to pass the compiled-in host allowlist on every call (ADR-0006).
- A second venue is planned (ADR-0007, #112), so a Binance-only client would
  need a second client alongside it.
- Exchange responses are hostile input: they are parsed into typed models, not
  indexed into. Whatever the library returns is a starting point, not a
  contract.

The constraint that forces a decision now:
The venue adapter is the first thing P4 builds, and choosing wrong is
discovered when spot fills stop arriving -- silently, because a stream that
never delivers looks identical to a market with no activity.
```

## Decision

**We use `ccxt` (>= 4.5.70) as the only exchange client library in this repository, with its transport replaced by `guarded_aiohttp_session()` so every request it issues — including ones it makes internally, such as `load_markets` and keepalives — passes the host allowlist.** `set_sandbox_mode(True)` is applied and the resulting `urls` mapping is then re-validated against the allowlist rather than trusted, because those endpoints come from the dependency. No `python-binance`, no `binance-connector`, no official `binance-sdk-*`, and no hand-rolled REST. Responses are parsed into Pydantic models with `extra="ignore"` and decimals taken from the venue's raw **string** fields; `ccxt`'s parsed output is an input to that, never the domain object.

## Alternatives considered

### Alternative 1 — the official `binance-sdk-*` packages (strongest rejected)

**What it would have given us.** First-party libraries, published by the exchange, tracking its own API changes with no lag — which matters enormously here, because the single most disruptive fact in this domain is that Binance changed spot user data out from under every third-party client (VF-002, VF-003). An official SDK is the one client guaranteed to know about a change on the day it ships, and it is the only one with authoritative documentation of the Ed25519 signing scheme. For a system whose failure mode is "fills silently stop arriving", being first in line for protocol changes is a real advantage.

**Why it lost.** Measurement: the official `binance-sdk-*` packages shipped **11 and 16 major versions in roughly twelve months** (VF-012). Under semantic versioning a major version is a promise that something broke. Twenty-seven breaking releases in a year, in a dependency sitting in the order path of a system designed to run unattended, is not a maintenance burden — it is a scheduled outage of unknown date. Every one of them requires reading a changelog, re-recording the response fixtures, and re-verifying the two user-data paths, and the alternative is pinning and accumulating the divergence the SDK was chosen to avoid.

Being first in line for protocol changes is also worth less than it appears, because the change we care about has already happened and is already handled. `ccxt` >= 4.5.70 is correct on both the endpoint split and the post-`listenKey` model today (VF-010). The advantage is about the *next* change, which is a prediction, weighed against a release cadence that is an observation.

**What survives the rejection, and is adopted.** The concern that a third-party client will lag a Binance change is legitimate, and it is why VF-010 carries a re-verification trigger on any client release rather than being treated as settled, and why the recorded-fixture corpus is re-recorded nightly with a diff on response *shape* (`docs/rules/testing-rules.md`). A shape change in a nightly diff is how we find out, rather than by noticing that fills stopped.

### Alternative 2 — `python-binance`, or `binance-connector`

**What it would have given us.** Both are Binance-focused rather than exchange-generic, so their surface is smaller and more direct: no unified-symbol translation layer, no abstraction over concepts that differ between exchanges, and error codes that arrive as Binance's own rather than mapped into a common taxonomy. `python-binance` in particular has the largest community and the most searchable answers, which is not nothing for one developer.

**Why it lost.** `python-binance` is broken for spot user data (VF-011): it is built around the retired `listenKey` flow, so a spot implementation using it fails at the first call with a 410 and cannot be made to work without replacing the part that was the reason to use a library. `binance-connector` is frozen, which for a protocol that changed materially within the last year means it encodes a model of Binance that is already wrong. Neither would serve the second venue (ADR-0007) either, so choosing one means committing to a second client library for Bybit and two adapters with different shapes.

### Alternative 3 — do nothing (hand-rolled REST and WebSocket clients)

```
Cost of the status quo: implementing HMAC-SHA256 request signing, Ed25519
session.logon signing, recvWindow and clock-drift handling, rate-limit header
accounting, exchangeInfo filter parsing, and two user-data lifecycles --
roughly three weeks, of which the signing and the WebSocket session handling
are the parts where a subtle error produces authentication failures that read
like a bad secret (VF-003).
Why that is no longer payable: none of that work is differentiating. The
system's job is to reject bad strategies convincingly (CLAUDE.md 1), and a
hand-written signer contributes nothing to that while owning a permanent
correctness risk in the order path.
```

## Consequences

**What becomes easier**
- Both user-data mechanisms are supported by one library, so the two implementations behind `UserDataStream` differ in session lifetime rather than in dependency.
- The second venue (#112) reuses the same client, so proving the venue abstraction does not also mean proving a second client library.
- Symbol metadata, precision, filters and rate-limit headers arrive already parsed, which removes a large surface of parsing bugs from our code — though not the validation, which we still do ourselves.

**What becomes harder**
- `ccxt` constructs its own `aiohttp` session by default, so it is a complete allowlist bypass unless the guarded session is injected. That injection is load-bearing and easy to lose in a refactor, which is why `import-linter` forbids `execution` from importing `aiohttp` directly and why the `urls` mapping is re-validated after `set_sandbox_mode`.
- `ccxt`'s unified model is an abstraction over many exchanges, so it occasionally normalises away a venue detail we need. Every response is re-parsed into our own typed models from the raw string fields rather than consumed as `ccxt` presents it.
- A `ccxt` release can change behaviour without changing our code. The version floor is asserted in tests and the nightly fixture diff is the detector.

**What we now cannot do**
- Use a Binance-specific client for a Binance-specific capability, even where `ccxt` lacks it. Reopening that means a second client library in the order path, with a second transport to guard and a second failure surface — so the answer to a missing capability is a raw-request call through `ccxt`'s own guarded transport, not a second dependency.

## What would make us revisit this

```
Trigger:   `ccxt` is found incorrect on a Binance behaviour we depend on --
           evidenced by the nightly recorded-fixture diff reporting a response
           shape change that ccxt mis-parses -- and no ccxt release corrects
           it within 30 days.
Observed:  The nightly `scripts/record_exchange.py --diff` job and the issue
           it opens.
Then:      Open a superseding ADR. A raw-request path through ccxt's guarded
           transport is the first remedy considered, ahead of a second client.
```

## Verification

```
Confirmed if:  both user-data paths deliver fills continuously and the nightly
               fixture diff reports no unhandled shape change, through
               2027-02-01
Refuted if:    any module constructs a transport ccxt did not receive from
               guarded_aiohttp_session(), or a second exchange client library
               enters the dependency set
Checked by:    execution agent, via `make imports`, the guarded-client tests,
               and the nightly fixture diff
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
- [x] Linked from #16 and from `.claude/knowledge/decisions-log.md` (D-012)
