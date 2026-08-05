"""Release automation: preflight refusals, a derived changelog, a stated rollback path.

`RELEASE_PROCESS.md` is the specification. This package is the part of it a machine can
run, and it exists because the two things that most need to be true at a release --
that the tagged commit was green, and that the notes describe a rollback that actually
works -- are exactly the two a person cuts corners on at the end of a long day.

Three properties shape the design.

**Everything that decides is a pure function over a value.** `preflight.refusals` takes
a `RepositoryState`, not a repository; `changelog.render` takes a `ReleaseNotes`, not a
`gh` client. The subprocess calls live in `repository.py` and decide nothing. That is
what makes "the release refuses to tag when X" a test rather than a claim.

**Every refusal is reported, never just the first.** A release cut is a stop-the-world
event -- nothing merges while it runs (`RELEASE_PROCESS.md` section 2) -- so learning
about the dirty tree, then the red CI, then the unmarked migration across three
attempts costs three of those windows.

**The tag is not created without `--confirm`.** Tags here are immutable and never moved
(`GIT_WORKFLOW.md` section 9), which makes tag creation the one irreversible step in the
process. The notes are written first precisely so a human reads the rollback procedure
*before* the object that procedure will be quoted from exists.
"""

__all__: tuple[str, ...] = ()
