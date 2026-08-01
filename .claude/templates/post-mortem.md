# Template — Post-mortem

Copy this file to `docs/postmortems/<yyyy-mm-dd>-<kebab-slug>.md`, dated by the incident's start, not by the day you wrote the document. Example: `docs/postmortems/2026-07-14-reconciliation-drift-after-testnet-wipe.md`.

**This document is blameless, and detection lag is the headline number.**

Blameless means the analysis stops at mechanisms. Named individuals and named agents appear in this document as *actors* — who ran what, who was paged, who made the call — and never as *causes*. "The operator ran the wrong command" is not a finding; "the command that wipes open orders and the command that lists them differ by one character and neither prompts" is a finding. **"Human error" is not a root cause. It is the point at which the investigation has stopped too early.** Every time you write it, replace it with the answer to "what made that the easy thing to do?"

Detection lag is first in the metrics table because it predicts the cost of the next incident better than anything else here. Impact scales roughly with how long the system was wrong and nobody knew, and severity is largely a function of duration rather than of the triggering defect. Say this plainly to yourself while filling the table: **a two-hour outage detected in 2 minutes is a better outcome than a twenty-minute outage detected in 40.** The first is a system that can see itself; the second is a system that found out by accident and will find out by accident again, later, on something bigger.

Related: `../knowledge/failure-library.md`, `ERROR_RECOVERY.md`, `FAILSAFE.md`, `OBSERVABILITY.md`.

---

```yaml
---
incident_id: <INC-YYYY-NNN>
date: <yyyy-mm-dd, incident start, UTC>
severity: <S1 positions at risk or safety kernel involved | S2 trading degraded | S3 data or observability degraded | S4 contained, no external effect>
duration: <first bad event to resolution, e.g. 4h12m>
detection_lag: <first bad event to first human or alert awareness, e.g. 38m>
author: <human username or agent name>
correlation_ids: [<the ids that thread the incident through the audit trail>]
related_issues: [<#N, #N>]
related_adrs: [<ADR-NNNN, or empty>]
---
```

---

## Headline metrics

*Fill every row from the audit trail, not from recollection. Where a number is uncertain, give the range and say which side you are confident about.*

| Metric | Value | How it was measured |
|---|---|---|
| **Detection lag** | `<duration>` | `<first bad event timestamp from <source>, first awareness from <source>>` |
| Time to mitigate | `<duration>` | `<from awareness to the action that stopped the bleeding>` |
| Time to resolve | `<duration>` | `<from awareness to full restoration>` |
| Blast radius | `<what was affected: strategies, symbols, modules, data ranges>` | `<source>` |
| Positions affected | `<count and notional_usd>` | `<audit table query>` |
| Realised impact | `<demo P&L delta, corrupted rows, lost data window, wrong numbers published>` | `<source>` |

```
If detection lag exceeds time to mitigate, say so here in one line: <the mitigation was
ready and we simply did not know — that is a monitoring finding, not an engineering one>
```

---

## Summary

*Three to five sentences a reader can absorb without the rest of the document: what broke, what it did, how it was caught, what stopped it. Write this last. No mechanism detail, no timeline, no names.*

```
<summary>
```

---

## Timeline

*One row per event, UTC, ordered. **Every timestamp carries its source**, because the reliability of this document depends on it, and human memory is systematically wrong about durations in exactly the direction that flatters the detection lag. Mark any timestamp from recollection as `human memory` and treat it as approximate in the metrics table above.*

| Time (UTC) | Event | Source of timestamp |
|---|---|---|
| `<hh:mm:ss>` | `<first bad event — the actual first one, not the first one noticed>` | `<audit table / log / metric series>` |
| `<hh:mm:ss>` | `<event>` | `<audit table `<name>`, row id `<id>`>` |
| `<hh:mm:ss>` | `<first awareness — alert fired, or human noticed>` | `<alertmanager / Slack / human memory (approximate)>` |
| `<hh:mm:ss>` | `<mitigation applied>` | `<log>` |
| `<hh:mm:ss>` | `<resolution confirmed>` | `<verification command output>` |

```
Timestamps that could not be recovered from a system of record: <list them, or "none">
```

---

## What went wrong

*The mechanism. Follow the chain from the triggering condition to the impact, one link at a time, and stop only when the next "why" would be a design decision that is now on the change list. Names of individuals and agents may appear as actors; they may not appear as causes. If a sentence in this section could be rewritten as "X should have been more careful", it is not finished — rewrite it as a statement about what made the careless path the available one.*

```
1. <triggering condition>
   -> 2. <what that caused, mechanically>
      -> 3. <what that caused>
         -> 4. <the impact>

The design property that made step <n> possible: <state it>
```

> Example of the distinction: not "the agent requested an unlisted symbol", but "the feature store's availability contract was checked at strategy registration and not at signal time, so a symbol delisted between the two returned an empty frame that downstream code treated as a flat signal rather than as an error".

---

## Why detection took as long as it did

*Its own section, because it is the headline number. Answer four things concretely: what signal existed at the moment of the first bad event, whether anything was watching it, what threshold or alert would have fired and did not, and what eventually caused someone to look. If the answer to "what eventually caused someone to look" is a human noticing something odd, say so — that is the most important sentence in the document, because it means the system has no detector for this class at all.*

```
Signal present at first bad event: <the metric, log line, or audit row that already existed>
Was anything watching it:          <alert name, or "nothing">
Why the existing alerting missed it: <threshold too loose / wrong aggregation window /
                                      metric not emitted / alert routed nowhere>
What actually caused awareness:    <alert name, or the human observation, stated plainly>
Theoretical floor on detection for this class: <how fast this could have been caught with
                                                an alert we could realistically build>
```

---

## What went right

*Non-empty. Name the controls that worked, because the next set of changes will be tempted to trade one of them away for convenience. Include near-misses: things that would have made this much worse and did not, whether by design or otherwise, and say which.*

- <control that worked> — <what it prevented> — <by design | by luck>
- <control that worked> — <what it prevented> — <by design | by luck>

---

## Contributing factors

*Conditions that made this incident more likely or more expensive without causing it. Each one is a mechanism, not a person and not a mood. "Under time pressure" is not a contributing factor; "the deploy path has no dry-run mode, so the only way to check the change was to apply it" is.*

- <factor> — <how it contributed>
- <factor> — <how it contributed>

---

## What we are changing

*Three buckets, each with an issue number and a named owner. An action item with no issue number does not exist. Prefer one real change per bucket over five aspirational ones — the detect-faster bucket must not be empty, because detection lag is the number that predicts the next incident's cost.*

**Prevent** *(make the mechanism impossible, not unlikely)*

| Change | Issue | Owner | Landing by |
|---|---|---|---|
| `<change>` | `#N` | `<name>` | `<yyyy-mm-dd>` |

**Detect faster** *(reduce the detection lag for this class, with a target)*

| Change | Issue | Owner | New expected detection lag |
|---|---|---|---|
| `<alert, metric, or invariant check>` | `#N` | `<name>` | `<duration>` |

**Reduce blast radius** *(assume it happens again — make it cheaper)*

| Change | Issue | Owner | Effect on impact |
|---|---|---|---|
| `<change>` | `#N` | `<name>` | `<what the same incident would cost after this>` |

---

## What we deliberately are not changing, and why

*Non-empty, and this is the section that makes the document trustworthy. Every incident generates proposals that would trade away something more valuable than the incident cost. Name them and reject them on the record, so the same proposal does not arrive uncontested after the next one. Include here any control that would have prevented this incident but that we are keeping as-is, with the reason.*

- <proposal> — not doing it because <reason, with the cost that would be paid>
- <proposal> — not doing it because <reason>

> Example: adding a `--skip-preflight` flag to the deploy path would have saved roughly 20 minutes here. Not doing it: preflight is what catches an endpoint that is not on the allowlist, and a flag that skips it exists precisely for the moment someone is in a hurry, which is this moment (`CLAUDE.md` §11).

---

## Lessons for the failure library

*The distilled, reusable form. Write it as an entry that would be useful to someone who has never read this post-mortem and is working on unrelated code. Generic lessons are worthless here — "add better monitoring" teaches nothing. A good entry names a class of defect, the signature by which you would recognise it early, and the check that catches it. Append it to `../knowledge/failure-library.md` in the same pull request as this document.*

```
Class:      <the defect class, named>
Signature:  <how it looks in logs, metrics, or data before anyone knows it is a problem>
Check:      <the specific test, invariant, or alert that catches this class generally>
Cost if missed: <from this incident, as a number>
```

---

## Definition of done

- [ ] Detection lag is the first row of the metrics table and was measured from a system of record
- [ ] Every timeline timestamp names its source, and human recollection is marked as such
- [ ] No individual or agent appears as a cause anywhere in the document
- [ ] The phrase "human error" does not appear as a conclusion; every instance was replaced with what made that path the easy one
- [ ] "Why detection took as long as it did" answers all four questions, including what actually caused awareness
- [ ] "What went right" is non-empty and distinguishes by design from by luck
- [ ] The detect-faster bucket is non-empty and states a target detection lag
- [ ] Every action item has an issue number, an owner, and a date
- [ ] "What we deliberately are not changing" is non-empty
- [ ] A failure-library entry is written and appended to `../knowledge/failure-library.md` in the same pull request
- [ ] Every `correlation_id` in the frontmatter resolves in the audit trail
