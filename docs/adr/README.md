# Architecture Decision Records

An accepted ADR is immutable. It is not edited to reflect a change of mind and it is not deleted when it turns out to be wrong — a new ADR supersedes it, the old one's `status` line is updated to point at the successor, and both stay in the tree. That status line is the only permitted post-acceptance edit.

The record of paths this project rejected is worth more than the record of paths it took, because the rejected ones are the ones someone will propose again next quarter.

Write new records from [`.claude/templates/adr.md`](../../.claude/templates/adr.md). Numbers are four digits, zero-padded, never reused.

## Index

| # | Title | Date | Status |
|---|---|---|---|
| [0014](0014-kill-switch-flattens-on-trip.md) | Flatten the book on kill-switch trip, sourced from exchange state | 2026-08-02 | accepted |

Records 0001–0012 are delivered by [#16](https://github.com/ismetcahangirov/financeKing/issues/16) and 0013 by [#21](https://github.com/ismetcahangirov/financeKing/issues/21); the numbers are reserved for them. 0014 was written ahead of that sequence because it resolves a contradiction between two documents already in the tree ([#111](https://github.com/ismetcahangirov/financeKing/issues/111)), and leaving a safety-critical default ambiguous until the ADR backlog cleared was not an option.

An index that is maintained by hand drifts. When #16 lands, this table is generated from the front-matter of the files in this directory, and a CI check fails if it is stale.
