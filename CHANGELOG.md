# Changelog

**This file is an index, not a record.** The record for each release is the generated
`CHANGELOG-v<version>.md` attached to its GitHub release, derived from what actually
merged in the range — pull request titles, `type:` labels, `safety:critical` labels,
`Results-Invalidating:` git trailers, and the classification of every migration in the
range. `RELEASE_PROCESS.md` section 4 is the specification; `tools/release/` generates it.

Nothing is hand-written into a release's notes, and that is a design decision rather
than laziness. A hand-maintained changelog drifts from what shipped within two
releases, and the divergence is silent: nobody diffs a changelog against a tag range.
Deriving it makes the labels load-bearing — a pull request with two `type:` labels, or
one carrying a label with no section, **fails the release** rather than landing in the
wrong section, which is the only way a taxonomy stays accurate.

The one thing you may add here by hand is a correction, as a new entry, saying what was
missed and why. Do not edit a past entry: the release notes it describes are attached to
an immutable tag, and an index that disagrees with the object it indexes is worse than
one that admits a mistake.

---

## Releases

_None yet._ The first release will be cut with
`make release VERSION=0.1.0 IRREVERSIBLE=1` — the `IRREVERSIBLE` marker is not optional
for it, because the first release's range is the whole history and therefore contains
`0002_audit_substrate`, whose `downgrade()` raises by design. Its rollback section will
be the schema-forward procedure, and there will be no previous tag to check out, so its
only recovery is forward.

| Version | Date | Irreversible migration | Notes |
|---|---|---|---|

---

## Reading a release's notes

Four sections carry more than the feature list, in the order they matter:

1. **Safety-relevant changes** — every `safety:critical` pull request, individually,
   with the diff to `src/fking/platform/safety/` inlined verbatim. This is what you read
   months later to establish whether the host allowlist ever changed and when. "None."
   is stated explicitly, because an absent section is ambiguous between "nothing
   changed" and "nobody checked".
2. **Results-invalidating changes** — built from `Results-Invalidating:` commit
   trailers. If it is non-empty, every backtest and survival score produced before the
   release is on a different scale from those after it, and comparing across the
   boundary is meaningless.
3. **Migrations** — each one in the range, with its `downgrade()` classified
   `reversible`, `conditionally irreversible`, or `irreversible`.
4. **Rollback** — one of two procedures, selected by section 3. They differ in whether
   the schema moves.
