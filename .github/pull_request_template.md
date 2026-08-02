<!--
GIT_WORKFLOW.md section 5 makes labels, milestone, assignee and `Closes #<n>` mandatory.
Pull them from the issue rather than guessing:  gh issue view <n> --json labels,milestone
-->

## What

<!-- One paragraph. What the diff does, not how. -->

## Why

<!-- The problem, not the solution restated. If the reason is only obvious to you
     today, it is the part worth writing down. -->

## Verification

<!-- Actual command output, pasted. CLAUDE.md section 7: a claim is not evidence.
     If something was not run, say so here rather than omitting it. -->

```
$ make check

```

Coverage on touched modules: <module> <pct> (floor <pct>)

## Risk

- Safety kernel touched: yes / no <!-- yes => `safety:critical` label, and say so in the first line above -->
- Backtest/live parity affected: yes / no
- Migration included: yes / no <!-- audit tables must stay append-only; downgrade() raises -->

## Out of scope

<!-- What was deliberately left out, and why. Scaling work down is the user's
     decision, so it has to be visible rather than silent. -->

Closes #
