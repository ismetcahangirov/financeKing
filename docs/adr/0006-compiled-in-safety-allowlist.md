---
number: 0006
title: Compiled-in host allowlist as the demo-only guarantee, not a configuration guardrail
date: 2026-08-03
status: accepted
deciders: [ismetcahangirov, architect, security, compliance]
supersedes: null
superseded_by: null
related_issues: ["#11", "#16", "#107"]
related_adrs: [ADR-0007, ADR-0008]
---

## Context

The prime directive is that this system never trades real money — not in development, not in testing, not "just once to verify", not behind a flag (`CLAUDE.md` §0). That is a property somebody has to implement, and where it is implemented decides what it defends against.

```
Forces:
- The obvious implementation is a setting: BINANCE_BASE_URL, or a
  TRADING_MODE=demo flag, or a production profile that ships disabled. It is
  one line, it is testable, and it is what every deployment guide describes.
- The threat model is not malice. It is a config edit made while debugging a
  data question and not reverted; a .env filled in from a personal account
  because that was the credential to hand; an LLM agent generating its own
  httpx client because that is what the training data does; a dependency
  changing a default base URL in a minor bump.
- ccxt accepts a per-call base URL override, so a client validated at
  construction can still issue a request to any host afterwards.
- The system runs unattended and will eventually write parts of itself.
- Read-only access to production is the request that will actually be made,
  and it sounds reasonable every time.

The constraint that forces a decision now:
#11 builds the kernel, and every network call in the project -- data, agents,
execution -- is written against whatever it exposes. Retrofitting the
guarantee after callers exist means auditing every call site instead of
providing the only one.
```

## Decision

**The set of permitted hosts is a `frozenset` literal compiled into `src/fking/platform/safety/_allowlist.py`, and every outbound HTTP or WebSocket request in the process goes through `guarded_client()`, which validates the final resolved host on every request rather than once at construction.** `SafetyViolation` inherits `BaseException` so that no `except Exception` anywhere in the process — including in third-party code we do not control — can absorb it. There is no override: no flag, no environment variable, no `--force`, no test fixture. Widening the set requires a source edit and a pull request labelled `safety:critical`, and that pull request fails a CI check by design. This decision covers which hosts may be reached; it does not decide which venue is primary (ADR-0007) or which client library is used (ADR-0008).

## Alternatives considered

### Alternative 1 — configuration-driven, with startup validation and a loud refusal (strongest rejected)

**What it would have given us.** The allowlist would be `config/safety.toml`, validated at startup against a schema, with the process aborting on anything that resolves outside it and the effective set logged at every boot. This is not a weak design — it has real advantages over the chosen one. It keeps environment-specific facts in the layer built for environment-specific facts, which is where every other deployment concern in this project lives (`CONFIGURATION.md`). It makes the guarantee *auditable at runtime*: an operator can read the running configuration and see exactly what the process may reach, without building the source. Adding Bybit's testnet hosts (ADR-0007) would not require a release. And the startup abort is a genuine control — a misconfigured host never reaches a socket, because the process does not finish booting.

**Why it lost.** Every failure in the threat model is a *change to configuration*. A guardrail that lives in configuration is a guardrail that the thing it defends against can edit. The startup validation is real, but it validates the configured value against the configured list, and both moved together in the scenario that matters: someone sets `BINANCE_BASE_URL=https://api.binance.com` and, finding the process refuses to boot, adds the host to the allowlist file — because that is what the error message asks for, and because at that moment they are debugging something else and this is in the way. Under the chosen design that same person hits a source file with a comment saying not to, a golden test that fails, a CI check that requires a label, and a reviewer. Four separate moments where someone has to state out loud that they intend this system to be able to reach a production exchange.

The runtime-auditability advantage is answered rather than lost: the allowlist literal is logged at every boot (`ARCHITECTURE.md` §8), so an operator reads it from the log without building anything. What they cannot do is *change* what they read.

**What survives the rejection, and is adopted.** The startup-validation half is correct and is kept in full: `verify_endpoints_or_abort()` resolves every configured endpoint against the compiled set before the API binds or any scheduler starts, and a single failure aborts the process rather than degrading. Configuration still chooses *which* allowlisted host to use; it simply cannot extend the set. That split — configuration selects, source constrains — is the part of Alternative 1 worth having.

### Alternative 2 — a read-only partition: production hosts reachable for GET, testnet for everything

**What it would have given us.** This is the request that will actually be made, and it has a real requirement behind it: `CLAUDE.md` §2 mandates that cost-model parameters be calibrated from production market data, because futures testnet showed a 7.5bp spread against production's 0.16bp with ~10x inflated volume (VF-008). Testnet is not a market. A read-only production path would satisfy that requirement directly, with no credentials attached and no order-placement code in sight — `exchangeInfo` is public and unauthenticated, and a spread sample is a `GET`.

**Why it lost.** Read paths become write paths during refactors, and the mechanism is mundane rather than dramatic. A `_request()` helper written for klines acquires a `method` parameter six weeks later because a second caller needs `POST` somewhere unrelated; then a signing step, because a third caller needs authentication; and nobody re-derives the safety property, because the host was already in the allowlist and the allowlist is where that property was recorded. The allowlist cannot distinguish intent, because intent is not a property of a socket — only the destination is.

The requirement is met without the exception. Production market data comes from the `data.binance.vision` public archives: checksum-verified, downloaded out of band, loaded from Parquet (VF-013, VF-014). That is a data-host egress path (#22), not a trading path, and it does not put a production exchange host into the set that the order-placement code can reach.

### Alternative 3 — do nothing (no kernel; rely on testnet credentials being the only ones present)

```
Cost of the status quo: the demo-only guarantee would rest on the fact that
no production API key exists on the machine. That holds until someone tests
against their own account once, and it does not hold at all for
unauthenticated endpoints. #11 is blocked, and CLAUDE.md 0 -- the one
property the whole document says matters most -- would be enforced by
nothing.
Why that is no longer payable: it was never payable. It is listed because
"the credentials aren't there" is the argument that gets made, and it is an
argument about the machine's current state rather than about the system.
```

## Consequences

**What becomes easier**
- There is exactly one way to make a network call, so a reviewer's question is "does this go through `guarded_client()`" rather than "is this URL right".
- `import-linter` can state the property mechanically: `execution`, `data`, `agents`, `api` and the rest may not import `httpx`, `aiohttp`, `websockets` or `requests` directly, with the kernel's own transports as the two named exceptions.
- A violation is unmissable. `SafetyViolation` inherits `BaseException`, so it propagates through every defensive handler in the process and in its dependencies; the process dies rather than retrying against a host we do not recognise.
- The guarantee is testable to 100% coverage in one small module, which is why `platform/safety` carries a 100% floor rather than 95%.

**What becomes harder**
- Adding a venue is a source change plus a `safety:critical` pull request plus a deliberately failing CI check that requires an admin override with a written reason. That is the intended friction and it will be felt every time (ADR-0007's Bybit path pays it).
- Every module that needs to fetch anything must route through the kernel, including ones where it is obviously harmless — the archive fetcher, the LLM gateway, the telemetry exporter.
- The `frozenset` is a compile-time fact, so per-environment host differences must be expressed as *selection among* allowlisted hosts rather than as different lists.

**What we now cannot do**
- Reach a production exchange host at all, for any purpose, including unauthenticated reads, spread sampling, and comparing testnet against reality. Reopening that requires editing source under a `safety:critical` label — which is the point, not an oversight. The cost-model requirement is met from the public archives instead.

## What would make us revisit this

```
Trigger:   None. This decision has no revisit trigger, and the absence is
           deliberate.
Observed:  n/a
Then:      If you are constructing an argument for revisiting it, the argument
           is the symptom. Stop and ask the user (CLAUDE.md 0).
```

The one thing that *does* change without superseding this ADR is the membership of the set — adding Bybit testnet hosts, or removing a Binance host that Binance retires. That is a `safety:critical` pull request against `_allowlist.py`, not a new decision about where the guarantee lives.

## Verification

```
Confirmed if:  zero requests to a non-allowlisted host are observed in the
               audit log or the process logs, and the golden allowlist test
               has never been edited except under a safety:critical PR,
               measured by 2027-02-01
Refuted if:    any SafetyViolation is caught anywhere in src/fking or tests,
               or platform/safety coverage falls below 100%, or an override
               mechanism of any kind appears
Checked by:    security and compliance agents, via `make check`, the dedicated
               safety coverage gate, and the safety-kernel-diff CI job
Review date:   2027-02-01
```

## Definition of done

- [x] `number` is the next unused value in `docs/adr/` and the filename matches `NNNN-<kebab-slug>.md`
- [x] Context names one constraint that forces a decision
- [x] Decision is one paragraph, active voice, and names the owning module
- [x] The strongest rejected alternative is argued at its strongest, and the part of it that was correct is adopted rather than discarded
- [x] "Do nothing" is costed
- [x] All three Consequences lists are non-empty, including what we now cannot do
- [x] The revisit trigger is stated, including the deliberate absence of one, and the mechanism that changes set membership without superseding this ADR is named
- [x] Verification states both a confirming and a refuting value, with a date and an owner
- [x] Linked from #16 and from `.claude/knowledge/decisions-log.md` (D-001)
