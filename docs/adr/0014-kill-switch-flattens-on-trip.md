---
number: 0014
title: Flatten the book on kill-switch trip, sourced from exchange state
date: 2026-08-02
status: accepted
deciders: [ismetcahangirov, risk-manager, architect]
supersedes: null
superseded_by: null
related_issues: ["#111", "#53", "#54", "#110", "#4"]
related_adrs: []
---

## Context

The repository contradicted itself on what the kill switch does to open positions.

```
Forces:
- Removing exposure is the one action that is correct when we no longer trust
  our own state. Nothing else bounds the loss.
- Flattening is a market order, executed under exactly the conditions that
  produce the worst fills — fast markets, wide spreads, degraded data.
- Several trip conditions specifically indicate that our position record may be
  wrong. Sending closing orders sized from a record we distrust can open a
  position rather than close one.
- This system is designed to run unattended. "Let a human decide" presumes a
  human inside the window over which an open position can move.
- The supervisor already flattens the book on any unhandled exception
  (`.claude/rules/error-handling.md`). Two paths through maximum uncertainty
  cannot behave oppositely.

The constraint that forces a decision now:
Epic #4 states the kill switch "flattens and blocks"; FAILSAFE.md §2.4 sets
`on_trip_flatten = false` and argues cancel-only. Issue #53 cannot ship a
default without one of them being wrong, and a safety-critical default that two
documents disagree about is resolved at 3am by whoever read the other one.
```

## Decision

**We flatten the book on kill-switch trip**, in `src/fking/risk/killswitch.py` as `KillSwitch.trip()`, and `on_trip_flatten` defaults to `true`. The flatten is sequenced after order-entry is blocked and after the book is snapshotted to the audit log, and **the quantities it closes are read from the venue, never from local position records**. If venue state cannot be read, the flatten does not proceed on a guess: the system stays halted with positions open, emits `killswitch.flatten_blocked` at `CRITICAL`, and pages.

This decision covers the kill switch only. It does not change `SafetyViolation`, which still kills the process without flattening, because a host outside the allowlist means the path we would send closing orders through is the path we have decided we do not understand.

## Alternatives considered

### Alternative 1 — cancel resting orders, leave positions open (strongest rejected)

**What it would have given us.** This is `FAILSAFE.md` §2.4's position and it is well argued. The conditions that trip a kill switch — a fast drawdown, a stale feed, a reconciliation mismatch, a rejection spike — are precisely the conditions under which market orders execute worst, so flattening converts an unrealised loss into a realised one at the moment the spread is widest, automatically, with nobody having looked at the book. A stale-data trip that responds by market-selling everything is acting decisively on the belief that our prices are unreliable, which is close to self-contradictory. And cancel-only is reversible in a way flatten is not: a blocked-but-open book can be flattened by a human in thirty seconds if that is the right call, whereas an automatically flattened book cannot be unflattened. Positions keep their invalidation-level protective orders, which are already resting at the venue, so the downside is bounded rather than unbounded.

**Why it lost.** Three reasons, in order of weight.

First, **it is incoherent with a decision already made.** `.claude/rules/error-handling.md` gives the supervisor exactly one sanctioned `except Exception`, and that handler trips the kill switch, calls `execution.flatten_all()`, writes the fatal audit row, and exits. An unhandled exception is the least-understood state the system can be in, and the repository already answers it by flattening. A kill switch that does not flatten while the supervisor does means the response to uncertainty depends on which code path noticed it, which is not a safety design.

Second, **the "let a human decide" premise does not hold for this system.** `FAILSAFE.md` §2.6 is right that resume requires a human, and that requirement is unaffected by this ADR. But the argument for cancel-only needs something stronger: a human within the window over which an open crypto position can move. This system schedules its own work, runs continuously, and has no on-call rotation. Between a 03:00 trip and the operator waking up, "stop making it worse" leaves a position exposed to a market that does not stop. The reversibility argument is real but asymmetric in the wrong direction: the cost of a flatten you did not need is slippage on one exit, and the cost of an open position you could not supervise is unbounded.

Third, **the slippage objection argues against the wrong thing.** Flattening does not require a price view; it requires a *position* view. Reducing exposure to zero is the action whose correctness is least sensitive to whether our prices are right — that is what makes it the appropriate response to not trusting them. Paying the spread is a known, bounded cost, and the trip conditions that make fills bad are the same ones that make holding bad.

**What survives the rejection, and is adopted.** The reconciliation objection is not a slippage argument and it is correct: flattening from a position record we have just established we distrust can open a position rather than close one. That is a real bug, and it is why the decision above sources quantities from the venue and refuses to flatten when the venue cannot be read. Cancel-only was rejected; the specific failure it identified was not.

### Alternative 2 — flatten on some trigger classes, cancel on others

**What it would have given us.** The trigger taxonomy in #54 already distinguishes conditions, so it could distinguish responses: flatten on "we are losing money" (drawdown, daily loss), cancel-only on "we do not know what our position is" (reconciliation divergence, feed outage). That routes around the strongest objection by construction rather than by mitigation, and it is the answer a careful reader arrives at.

**Why it lost.** It makes the kill switch's behaviour a function of the trigger's *classification*, and misclassification then becomes a safety failure with no backstop. Trip conditions arrive correlated and ambiguous — a testnet wipe presents simultaneously as a reconciliation divergence and a balance collapse, and `.claude/rules/exchange-integration.md` exists partly because telling those apart is hard. A design whose worst case is "we chose the wrong response because we labelled the incident wrong" is worse than one that always takes the bounded action. Sourcing the flatten from venue state achieves the same protection unconditionally, without requiring the taxonomy to be right under pressure.

It is also two behaviours to test, exercise and reason about instead of one, on the code path that must work when nothing else does.

### Alternative 3 — do nothing

```
Cost of the status quo: issue #53 blocked; two documents in the merged
operating system stating opposite defaults; every future reader of either
one reaching a different conclusion about what the system does.
Why that is no longer payable: #53 is on P3's critical path, and an
operating system that contradicts itself on a safety-critical default
teaches readers that its rules are approximate.
```

## Consequences

**What becomes easier**
- One answer to maximum uncertainty across the whole system: supervisor and kill switch behave identically, and a reader learns the rule once.
- Exposure after a trip is bounded by the flatten's execution quality rather than by how quickly a human is reached.
- The flatten path is exercised by every kill-switch test, so it is not code that first runs during an incident.

**What becomes harder**
- The kill switch now depends on the execution venue being reachable, which widens the failure surface of the component that must work when others do not. `killswitch.flatten_blocked` is a new paging condition that did not previously exist.
- Every trip now has a measurable cost, and that cost must be tracked — slippage on kill-switch flattens becomes a metric with an alert, because a switch that is expensive to trip creates pressure to raise its thresholds.
- Partial flattens need explicit semantics: an exit that fills three of five positions and then fails leaves a state that neither "flat" nor "open" describes.

**What we now cannot do**
- Trip the switch as a "freeze and inspect" — the book will be closed before an investigator sees it. This is a genuine loss and is why the trip sequence snapshots positions, open orders and the last reconciliation result to the audit log **before** flattening. The snapshot is the post-mortem artefact; reopening the freeze-and-inspect capability would require a separate, explicitly non-flattening halt mode and a superseding ADR.

## What would make us revisit this

```
Trigger:   Across any 10 consecutive kill-switch trips, median realised
           slippage on the flatten exceeds 50 bps, OR the flatten's realised
           loss exceeds the drawdown that triggered it in 3 or more of them.
Observed:  Grafana panel `killswitch.flatten_slippage_bps` (p50) and the
           per-incident comparison recorded in `killswitch_events`.
Then:      Open a superseding ADR reconsidering Alternative 2, with the
           incident data as its evidence rather than as a prediction.
```

A second, independent trigger: if `killswitch.flatten_blocked` fires more often than the flatten succeeds, the venue-sourced precondition is the wrong shape and the decision needs revisiting on availability grounds rather than cost grounds.

## Verification

```
Confirmed if:  zero incidents in docs/postmortems/ attribute a loss to a
               position left open by a trip, measured by 2027-02-01
Refuted if:    the revisit trigger above fires, or any flatten is shown to
               have opened a position rather than closed one
Checked by:    risk-manager agent, via `make test -k killswitch` and the
               kill-switch incident review
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
- [x] Linked from the issue it resolves (#111) and from `.claude/knowledge/decisions-log.md`
