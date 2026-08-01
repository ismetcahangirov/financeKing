# Template — Operational Runbook

Copy this file to `docs/runbooks/<kebab-slug>.md`, named for the procedure rather than the incident. Example: `docs/runbooks/rebuild-state-after-testnet-wipe.md`.

A runbook is written for someone at 03:00 who did not build this, is missing context, and will do exactly what the page says. Write for that reader: exact commands, exact expected output, and an explicit branch for what to do when the output differs. Prose explaining the design belongs in `ARCHITECTURE.md` and gets skipped here.

Related: `FAILSAFE.md`, `ERROR_RECOVERY.md`, `../rules/safety-kernel.md`, `../contexts/binance-testnet.md`, `post-mortem.md`.

---

```yaml
---
procedure: <what this does, imperative, e.g. "Rebuild local state from the exchange after a testnet wipe">
service: <the component this operates on, e.g. execution / reconciliation>
owner: <human username or agent name accountable for this procedure being correct>
last_rehearsed: <yyyy-mm-dd — the last time someone executed this end to end against a non-incident environment>
rehearsal_cadence: <e.g. quarterly>
severity: <the severity levels at which this applies, e.g. S1-S2>
estimated_duration: <how long the whole procedure takes when it goes well>
---
```

---

## When to use this

*The exact symptom or alert that brings someone here, stated so specifically that a reader can tell in under a minute whether they are in the right document. Include the symptoms that look like this one and are not, with a pointer to the right runbook — a wrong runbook executed confidently is worse than no runbook.*

```
Use this when:      <the exact alert name, log signature, or observable symptom>
Confirming check:   <the one command that distinguishes this from things that look like it>
Expected output confirming you are in the right place: <output>

Do not use this when:
- <similar symptom> -> <docs/runbooks/<other>.md>
- <similar symptom> -> <docs/runbooks/<other>.md>
```

> Example: use this when `GET /fapi/v2/balance` succeeds with valid keys but returns zero balances and zero open orders while the local OMS shows open positions. That is the testnet wipe signature (keys survive, state does not). If keys are also rejected, this is a credential problem and not a wipe.

---

## Preconditions and what you must have in hand

*Everything needed before step 1, listed so that a missing item is discovered now rather than halfway through. Credentials require asking the user; an agent does not go and find them.*

```
Access:          <which hosts, which dashboards, which container shells>
Credential scope: <which key, which permissions — read-only where read-only will do>
Correlation id:  <the current incident's correlation_id, needed to thread every action
                  taken here into the audit trail>
Environment:     <which compose stack, which branch, which config file>
In another window: <the log tail or dashboard panel you should be watching throughout>
```

```
If a credential is missing: stop and ask the user. Do not source one from elsewhere.
```

---

## Safety check first

*Before anything else, and before any command that could send. This is not ceremony. The prime directive is that this system never trades real money, and the moment at which someone is most likely to point a client at the wrong host is the moment they are under pressure and following a script. Run these two checks and read the output.*

```console
$ <command that prints the compiled-in safety allowlist>
Expected: <the exact frozenset of permitted hosts>
```

```console
$ <command that prints the resolved endpoint the running process will actually use>
Expected: <a demo endpoint from the allowlist above>
```

```
If the resolved endpoint is not in the allowlist: stop. Do not proceed, do not adjust the
allowlist, do not add an override. Escalate immediately per the escalation section below.
Widening the allowlist is a source change behind a `safety:critical` pull request and is
never part of an operational procedure.
```

---

## Steps

*Numbered. Each step carries the exact command, the expected output, and what to do when the output differs. A step with no "if the output differs" branch is a step that will be improvised at 03:00. Keep each step independently re-runnable where possible, and say explicitly where a step is not.*

**1. `<what this step accomplishes>`**

```console
$ <exact command>
```

```
Expected: <exact output, or the shape of it>
If it differs:
  <observed variation> -> <action>
  <observed variation> -> <action>
Re-runnable: <yes | no, because <reason>>
```

**2. `<what this step accomplishes>`**

```console
$ <exact command>
```

```
Expected: <exact output>
If it differs:
  <observed variation> -> <action>
Re-runnable: <yes | no>
```

**3. `<what this step accomplishes>`**

```console
$ <exact command>
```

```
Expected: <exact output>
If it differs:
  <observed variation> -> <action>
Re-runnable: <yes | no>
```

---

## Verification

*How you know it worked, as commands with expected output. Verifying that the command you ran exited zero is not verification; verify the state you were trying to reach. Include at least one check that would fail if the procedure half-worked, because half-working is the common outcome and it is the one that looks fine.*

```console
$ <verification command>
Expected: <output>
```

```console
$ <the check that catches a half-completed run>
Expected: <output>
```

```
Record in the audit trail: <what to write, with the correlation_id, and where>
```

---

## Rollback

*What to do if the procedure made things worse. Say plainly where there is no rollback — a procedure with an irreversible step must announce which step that is, before the reader reaches it.*

```
Point of no return: step <n>, because <what becomes irreversible there>
Before that point:  1. <command> 2. <command> 3. <verification>
After that point:   <the forward-only recovery path, or "escalate; there is no rollback">
```

---

## If this does not work, escalate to

*Named, in order, with what to hand over. Escalating with the correlation id and the outputs already collected is worth more than escalating quickly.*

```
1. <role or person> — <contact route> — <when to go here>
2. <role or person> — <contact route> — <when to go here>
Open a `needs-human` issue when: <condition>

Hand over: the correlation_id, the output of every command run, the step number you stopped
at, and the safety check output from the top of this document.
```

---

## Known failure modes

*The ways this procedure has actually failed, with the signature and the response. Add a row every time the procedure is run and something unexpected happens — this table is the difference between a runbook and a wish.*

| Symptom during the procedure | Cause | Response | First seen |
|---|---|---|---|
| `<what you observe>` | `<mechanism>` | `<action>` | `<yyyy-mm-dd>` |
| `<what you observe>` | `<mechanism>` | `<action>` | `<yyyy-mm-dd>` |

---

## Last rehearsal

*An unrehearsed runbook is a document, not a procedure. It has never been executed against a real system, its commands may reference flags that no longer exist, and its expected outputs may describe a version that shipped a year ago — none of which is discoverable until the one moment when discovering it is most expensive. Rehearse on the cadence in the frontmatter, in a non-incident window, end to end, and update this record and the `last_rehearsed` field in the same pull request.*

| Date | Rehearsed by | Duration | What was wrong with the runbook | Fixed in |
|---|---|---|---|---|
| `<yyyy-mm-dd>` | `<name>` | `<duration>` | `<what did not match reality, or "nothing">` | `<PR #N>` |

```
Cadence:        <e.g. quarterly, and after any change to the module it operates on>
Next rehearsal: <yyyy-mm-dd>
Overdue action: if `last_rehearsed` is older than the cadence, this runbook is treated as
                unverified — follow it, but verify each expected output against reality
                before acting on a mismatch, and open an issue to rehearse it.
```

---

## Definition of done

- [ ] "When to use this" names an exact alert or symptom and lists at least one look-alike with a pointer elsewhere
- [ ] Preconditions list credentials by scope, and state that missing credentials mean asking the user
- [ ] The safety check is the first executable section and reads both the allowlist and the resolved endpoint
- [ ] Every step has an exact command, an expected output, and an "if it differs" branch
- [ ] Every step states whether it is re-runnable
- [ ] Verification checks the reached state, and includes a check that catches a half-completed run
- [ ] The point of no return is named explicitly
- [ ] Escalation names roles in order and states what to hand over
- [ ] The known failure modes table has at least one row, or says the procedure has never surprised anyone and gives the rehearsal count backing that
- [ ] `last_rehearsed` is within the stated cadence, and the rehearsal record names what the rehearsal found
