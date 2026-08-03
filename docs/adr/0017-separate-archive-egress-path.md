---
number: 0017
title: The archive host gets a second egress path, not a wider trading allowlist
date: 2026-08-03
status: accepted
deciders: [ismetcahangirov, architect, security, data-engineer]
supersedes: null
superseded_by: null
related_issues: ["#22", "#26", "#28", "#32"]
related_adrs: [ADR-0006, ADR-0013]
---

## Context

Every strategy this project will ever validate is validated against history from `data.binance.vision`, and every bulk loader in P1 (#22, #26, #28) needs to reach it. It is a plain HTTPS file host: unauthenticated, public, no order endpoint, no credential to attach (VF-013, VF-014).

ADR-0006 made the demo-only guarantee a compiled-in `frozenset` of permitted hosts in `fking.platform.safety`, validated per request by `guarded_client()`. That decision says nothing about a data host, because when it was made there was not one.

```
Forces:
- data.binance.vision must be reachable from this process. There is no
  alternative source of production-calibrated history, and a cost model
  calibrated on testnet is fiction (7.5bp spread against production's 0.16bp).
- PERMITTED_HOSTS is not a permission list. It is a proof about which hosts a
  process holding order-placement code can reach AT ALL. Every host in it is
  reachable by the OMS, the venue adapter, and anything that later shares
  their request helper.
- CLAUDE.md 11: read paths become write paths during refactors. A client built
  for fetch_balance acquires a `method` parameter, then a signing step, and
  nobody re-derives the safety property because the host was already listed.
- .claude/rules/exchange-integration.md already records the answer in one
  clause: "the archive is a data host, and the trading allowlist is not
  widened to reach it".
- The archive path needs no credential at all, which the trading path cannot
  say. That asymmetry is available to be made structural, and is otherwise
  wasted.

The constraint that forces a decision now:
#22 is the first task that has to reach a non-venue host, and whichever shape
it takes is the shape #26, #28 and #32 will copy. If the one-line change --
add the host to PERMITTED_HOSTS, reuse guarded_client() -- ships here, the set
that the safety kernel's golden test protects has become a list that grows
whenever somebody needs a URL, and its meaning is gone before anybody notices
it changed.
```

## Decision

**`data.binance.vision` goes in a second compiled-in literal, `ARCHIVE_HOSTS`, reached only by `guarded_archive_client()` in `fking.platform.safety.archive`, and never in `PERMITTED_HOSTS`.** The two sets are disjoint and the disjointness is asserted; each client refuses every host in the other's set. The archive client holds no credential and cannot acquire one — an `import-linter` contract forbids `fking.platform.safety.archive` and `fking.data.archive` from importing `fking.platform.config`, where every `SecretStr` in this system lives — and a second contract forbids `fking.execution` from importing either. The archive module is deliberately not re-exported from `fking.platform.safety.__init__`, so reaching it requires naming it, which is what gives the contract an import edge to forbid. Both literals sit inside the `safety-kernel-diff` CI job, so widening either still requires a pull request labelled `safety:critical`.

## Alternatives considered

### Alternative 1 — add the host to `PERMITTED_HOSTS` and reuse `guarded_client()` (strongest rejected)

**What it would have given us.** One allowlist, one client, one place to look, one golden test, one CI gate, and no Protocol standing between `fking.data` and the transport. The archive host is objectively the safest host in the entire system: it serves static files over HTTPS, it has no order endpoint, it has no authenticated endpoint at all, and no credential we hold would mean anything to it. If the allowlist is a list of hosts we are willing to talk to, this host belongs on it more clearly than any other. And the duplication that the chosen design accepts is real — two clients, two literals, two golden tests, a Protocol whose only production implementation is the one written alongside it, all to reach a public CDN.

**Why it lost.** The argument mistakes what `PERMITTED_HOSTS` is. It is not a list of hosts we are willing to talk to; it is the statement *these are the only hosts a process that can place orders can reach*, and its whole value is that the statement is short, entirely venue endpoints, and reviewable in one glance. The moment it also contains a data host, reviewing an addition requires knowing which category the new entry falls into, and "is this a data host or a venue?" is a judgement call made by whoever wants the entry added. That is the same shape as every guardrail that decays: the rule stays, and the criterion for applying it moves.

The concrete mechanism is the one CLAUDE.md §11 names. Nothing about a listed host announces that it was listed for reading. Six months from now a `_request()` helper is generalised across the fetcher and a venue adapter because they duplicate retry and backoff; the helper gains a signer; the signer is applied to whichever base URL it is handed. No step in that sequence is wrong, no step touches the allowlist, and the result is a signed request to a host that was added for bulk zip files. With two clients the same refactor does not compile past the import contract.

**What survives the rejection, and is adopted.** Its best point — that duplication in a safety kernel is itself a hazard, because two copies of a URL parser drift and the one nobody looks at is the one that gets it wrong — is correct and is adopted directly. The *allowlists* are duplicated; the *checking* is not. `fking.platform.safety._hostcheck` holds one implementation of scheme validation, userinfo stripping, trailing-dot normalisation and case folding, and takes the permitted set as an argument. Both clients call it. A hardening fix lands once and protects both paths, and the property being bought — two egress paths that cannot reach each other's hosts — is a property of the two literals, not of two parsers.

### Alternative 2 — fetch archives out of band and load only from disk

The safety kernel is untouched because nothing in `src/fking` reaches the network for history at all: a shell script with `curl` downloads and verifies, and the pipeline reads Parquet from a directory. This is the strictest possible reading of ARCHITECTURE.md §8, and it is what `.claude/rules/safety-kernel.md` describes when it refuses the "let me hit mainnet read-only" argument.

It lost on operability rather than on safety. #26 requires a resumable backfill with a gap and coverage registry, and #28 requires REST backfill of detected gaps reconciled on exchange trade id — both of which need the fetch to be a function the pipeline can call when it discovers a hole, not a step a human ran last Tuesday. Pushing it out of band does not remove the egress; it moves it somewhere with no host validation, no checksum enforcement in code, no audit and no test. The rule it appears to satisfy is about *production venue* hosts, and applying it to a public file server buys nothing while giving up every guarantee this ADR is for.

### Alternative 3 — do nothing

Costed as: P1 does not start. #22 through #28 all need this host, so "do nothing" is "no historical data", and with no historical data there is no backtest, no validation gate, no cost-model calibration and no evolution engine. There is no version of this project that does not reach `data.binance.vision`. The only open question was ever *how*.

## Consequences

**What becomes easier**
- Reviewing an allowlist change. Each literal has one category of host in it, so the question "does this belong here?" has an answer that does not depend on who is asking.
- Reasoning about credential blast radius. The archive path cannot sign a request, and that is enforced by an import contract rather than by the absence of a line somebody could add.
- Adding a future data source — a news feed, an alternative dataset (#32) — which is an entry in `ARCHIVE_HOSTS` with the same review, and touches nothing the order path can see.
- Testing `fking.data` with no transport at all. The `ArchiveEgress` Protocol is what keeps `httpx` out of `fking.data`, and it also means the fetcher's tests are in-process and deterministic.

**What becomes harder**
- Two literals, two golden tests, two sets of transport-configuration assertions. Roughly forty lines of test that a single allowlist would not need.
- A Protocol with one production implementation, which is the shape CLAUDE.md §3 warns about. It is accepted here because the alternative is a direct `httpx` import in `fking.data`, which a machine-checked contract forbids — the interface is paying for an architectural boundary, not for an anticipated second implementation.
- Anything needing both paths must hold two clients. Nothing does today, and a module that wanted both would be a module doing two jobs.

**What we now cannot do**
- Reach the archive from `fking.execution`, including from a shared helper it imports. Diagnostics that want both a venue and an archive read live above both, or in two places.
- Attach a credential to an archive request, or read the settings tree from the archive path. The cache root is a constructor argument, permanently.
- Merge the two allowlists later without deleting two contracts, two golden tests and this ADR — which is the intended cost, not an oversight.

## What would make us revisit this

```
Trigger:   A third egress category appears that is neither a trading venue nor
           a public data host -- an LLM provider is the live candidate (#71,
           #72) -- and it is proposed for ARCHIVE_HOSTS rather than for its
           own literal.
Observed:  Any pull request touching
           src/fking/platform/safety/_archive_allowlist.py, which the
           safety-kernel-diff CI job flags on every run.
Then:      Open a superseding ADR generalising to N named egress classes, each
           with its own literal, its own client and its own contract. Not one
           merged list with a category column: a category column is a
           judgement call, and the whole point of separate literals is that
           there is no call to make.
```

## Verification

```
Claim:         Two disjoint compiled-in allowlists with mutually unreachable
               clients prevent the order path from acquiring archive-host
               reachability, at a cost of one duplicated literal
Confirmed if:  by 2027-02-01, PERMITTED_HOSTS still contains exactly the seven
               venue endpoints it contained on 2026-08-03; ARCHIVE_HOSTS
               contains only public data hosts; and no module under
               src/fking/execution has an import path to either archive module
Refuted if:    a host appears in both literals, or ARCHIVE_HOSTS acquires a
               host with an authenticated endpoint, or either import contract
               is removed or given an ignore_imports exemption
Checked by:    security and compliance agents, via `make check` --
               tests/platform/safety/test_archive_allowlist.py asserts the
               disjointness, tests/adversarial/test_archive_egress_contract.py
               breaks both contracts deliberately on every run, and
               `lint-imports` evaluates them
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
- [x] Linked from #22 and from `.claude/knowledge/decisions-log.md`
