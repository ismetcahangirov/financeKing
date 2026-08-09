"""Release notes derived from what merged, never written from memory.

`RELEASE_PROCESS.md` section 4 is the specification. Four decisions in here are not
obvious from it.

**Merged pull requests are found from commit subjects, not from the search API.** The
documented recipe is `gh pr list --search "merged:>$(git log -1 --format=%aI <tag>)"`,
which asks GitHub's index a question about *time*. Two things go wrong with that: the
index is eventually consistent, so a pull request merged minutes before the query can
be absent from the answer; and the boundary is an instant, so a pull request merged in
the same second as the previous tag lands in both ranges or neither depending on
rounding. `git log <previous>..HEAD` is a question about *ancestry*, which is exact,
local, and identical on every machine. Squash merges leave `(#158)` on the subject and
merge commits leave `Merge pull request #158`; both are parsed here.

**A pull request labelled `safety:critical` appears twice** -- once in the leading
section, once in its type group. Listing it only at the top would make the feature list
a lie by omission, and listing it only in its type group is exactly the burial that the
label exists to prevent. Duplication costs a reader four seconds; either omission costs
them the review.

**More than one `type:` label is refused, not resolved.** The grouping is the whole
value of a derived changelog, and picking one of two labels means the section a change
appears in was decided by a sort order rather than by a human. The fix is one label
edit on the pull request, which is cheap; a silently mis-sectioned entry is not.

**A pull request with no `type:` label is listed under "Uncategorised", never dropped.**
A changelog that quietly omits what it could not classify is worse than one that admits
it could not, because the omission is invisible in exactly the place people go looking
for completeness.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from tools.release.migrations import Migration, blocking, contains_irreversible
from tools.release.version import Version

SAFETY_LABEL: Final[str] = "safety:critical"
TYPE_LABEL_PREFIX: Final[str] = "type:"

# Conventional Commit type -> the heading it lands under. The keys are the repository's
# `type:` label taxonomy (`gh label list`), so adding a label without adding it here is
# a refusal at release time rather than a silent "Uncategorised" entry.
SECTION_FOR_TYPE: Final[Mapping[str, str]] = {
    "feat": "Added",
    "fix": "Fixed",
    "refactor": "Changed",
    "perf": "Performance",
    "adr": "Architecture decisions",
    "research": "Research",
    "test": "Tests",
    "docs": "Documentation",
    "chore": "Housekeeping",
}

UNCATEGORISED: Final[str] = "Uncategorised"

# Behaviour first, housekeeping last. A reader scanning a release for "did anything I
# depend on change" reads top-down and stops when the answer stops mattering.
SECTION_ORDER: Final[tuple[str, ...]] = (
    "Added",
    "Fixed",
    "Changed",
    "Performance",
    "Architecture decisions",
    "Research",
    "Tests",
    "Documentation",
    "Housekeeping",
    UNCATEGORISED,
)

_SQUASH_SUBJECT: Final[re.Pattern[str]] = re.compile(r"\(#(?P<number>\d+)\)\s*$")
_MERGE_SUBJECT: Final[re.Pattern[str]] = re.compile(r"^Merge pull request #(?P<number>\d+)\b")

# sha, subject, trailer value: the three fields `repository.results_invalidating` asks
# `git log` for, unit-separated because a trailer's value is prose containing every
# delimiter a person would otherwise choose.
_LOG_RECORD_FIELDS: Final[int] = 3
RECORD_SEPARATOR: Final[str] = "\x1e"
FIELD_SEPARATOR: Final[str] = "\x1f"
_LINE_NOISE: Final[str] = "\r\n "


class ChangelogError(ValueError):
    """The notes cannot be generated from the inputs given."""


@dataclass(frozen=True, slots=True)
class MergedPullRequest:
    """One merged pull request, as the changelog needs it."""

    number: int
    title: str
    url: str
    labels: frozenset[str]

    @property
    def is_safety_critical(self) -> bool:
        return SAFETY_LABEL in self.labels

    @property
    def type_labels(self) -> tuple[str, ...]:
        return tuple(sorted(label for label in self.labels if label.startswith(TYPE_LABEL_PREFIX)))


@dataclass(frozen=True, slots=True)
class ResultsInvalidatingChange:
    """A commit carrying the `Results-Invalidating:` trailer (`GIT_WORKFLOW.md` 3)."""

    sha: str
    subject: str
    note: str


@dataclass(frozen=True, slots=True)
class ReleaseNotes:
    """Everything the rendered notes are a function of. No I/O reaches `render`."""

    version: Version
    previous_tag: str | None
    commit_sha: str
    generated_at_utc: datetime
    pull_requests: tuple[MergedPullRequest, ...]
    results_invalidating: tuple[ResultsInvalidatingChange, ...]
    migrations: tuple[Migration, ...]
    safety_diff: str


def pull_request_numbers(subjects: Sequence[str]) -> tuple[int, ...]:
    """Pull request numbers referenced by commit subjects, first occurrence order.

    Deduplicated, because a squash merge and a later revert both name the same number
    and a changelog listing an entry twice reads as two changes.
    """
    found: list[int] = []
    for subject in subjects:
        matched = _MERGE_SUBJECT.match(subject.strip()) or _SQUASH_SUBJECT.search(subject.strip())
        if matched is None:
            continue
        number = int(matched.group("number"))
        if number not in found:
            found.append(number)
    return tuple(found)


def parse_results_invalidating(log_output: str) -> tuple[ResultsInvalidatingChange, ...]:
    """Parse `git log --format='%H%x1f%s%x1f%(trailers:key=Results-Invalidating,valueonly)'`.

    Unit-separated rather than whitespace-separated because the trailer's value is prose
    that routinely contains every delimiter a person would otherwise reach for.
    """
    changes: list[ResultsInvalidatingChange] = []
    for raw_record in log_output.split(RECORD_SEPARATOR):
        # `.strip()` is deliberately NOT used, and this is not a style choice:
        # Python treats U+001C..U+001F as whitespace, so a bare `.strip()` eats the
        # trailing field separator of a record whose trailer is empty -- and every
        # commit without the trailer then reports as malformed. Strip line noise only.
        record = raw_record.strip(_LINE_NOISE)
        if not record:
            continue
        parts = record.split(FIELD_SEPARATOR)
        if len(parts) != _LOG_RECORD_FIELDS:
            raise ChangelogError(
                f"malformed git log record {record!r}: expected three unit-separated "
                f"fields (sha, subject, trailer value)"
            )
        sha, subject, note = (part.strip(_LINE_NOISE) for part in parts)
        if not note:
            continue
        changes.append(ResultsInvalidatingChange(sha=sha, subject=subject, note=note))
    return tuple(changes)


def section_of(pull_request: MergedPullRequest) -> str:
    """The heading `pull_request` belongs under. Refuses rather than guesses."""
    labels = pull_request.type_labels
    if len(labels) > 1:
        raise ChangelogError(
            f"#{pull_request.number} carries {list(labels)}; a pull request with two "
            f"type labels has no section, and picking one would let a sort order decide "
            f"where a change is announced. Remove one label and regenerate"
        )
    if not labels:
        return UNCATEGORISED
    kind = labels[0].removeprefix(TYPE_LABEL_PREFIX)
    section = SECTION_FOR_TYPE.get(kind)
    if section is None:
        raise ChangelogError(
            f"#{pull_request.number} carries {labels[0]!r}, which maps to no changelog "
            f"section. Add it to SECTION_FOR_TYPE and to SECTION_ORDER in the same "
            f"change that introduced the label"
        )
    return section


def group_by_section(
    pull_requests: Iterable[MergedPullRequest],
) -> Mapping[str, tuple[MergedPullRequest, ...]]:
    """Pull requests bucketed by heading, ascending by number within each bucket."""
    buckets: dict[str, list[MergedPullRequest]] = {}
    for pull_request in pull_requests:
        buckets.setdefault(section_of(pull_request), []).append(pull_request)
    return {
        section: tuple(sorted(entries, key=lambda entry: entry.number))
        for section, entries in buckets.items()
    }


def _entry(pull_request: MergedPullRequest) -> str:
    marker = " **[safety:critical]**" if pull_request.is_safety_critical else ""
    return f"- #{pull_request.number} {pull_request.title}{marker} — {pull_request.url}"


def _safety_section(notes: ReleaseNotes) -> list[str]:
    """Every `safety:critical` pull request, individually, with the kernel diff inlined.

    `RELEASE_PROCESS.md` section 4.3: not summarised. These notes are what somebody
    reads months later to establish whether the allowlist ever changed and when, and a
    summary requires trusting whoever wrote it.
    """
    lines = ["## Safety-relevant changes", ""]
    critical = tuple(
        sorted(
            (entry for entry in notes.pull_requests if entry.is_safety_critical),
            key=lambda entry: entry.number,
        )
    )
    if not critical:
        # Stated explicitly. An absent section is ambiguous between "nothing changed"
        # and "nobody checked".
        lines.extend(["None.", ""])
        return lines
    lines.extend(_entry(entry) for entry in critical)
    lines.append("")
    lines.extend(["Diff against `src/fking/platform/safety/`:", "", "```diff"])
    lines.append(notes.safety_diff.rstrip("\n") if notes.safety_diff.strip() else "(empty)")
    lines.extend(["```", ""])
    return lines


def _results_invalidating_section(notes: ReleaseNotes) -> list[str]:
    lines = ["## Results-invalidating changes", ""]
    if not notes.results_invalidating:
        lines.extend(["None.", ""])
        return lines
    lines.extend(
        [
            "Every backtest, survival score and walk-forward fold produced **before** "
            "this release is on a different scale from those produced after it. "
            "Re-score before the next lifecycle decision.",
            "",
        ]
    )
    for change in notes.results_invalidating:
        lines.append(f"- `{change.sha[:12]}` {change.subject}")
        lines.append(f"  - {change.note}")
    lines.append("")
    return lines


def _migrations_section(notes: ReleaseNotes) -> list[str]:
    lines = ["## Migrations", ""]
    if not notes.migrations:
        lines.extend(["None.", ""])
        return lines
    lines.extend(["| Migration | `downgrade()` |", "|---|---|"])
    lines.extend(
        f"| `{migration.filename}` | {migration.downgrade.value} |"
        for migration in notes.migrations
    )
    lines.append("")
    return lines


def _rollback_forward_schema(notes: ReleaseNotes) -> list[str]:
    """The procedure when the range contains a migration that will not undo itself."""
    previous = notes.previous_tag or "<no previous release — see below>"
    named = ", ".join(f"`{migration.filename}`" for migration in blocking(notes.migrations))
    return [
        "**This release contains an irreversible migration. Code rolls back; schema "
        "rolls forward.**",
        "",
        f"Blocking: {named}.",
        "",
        "`alembic downgrade` is **not** part of this procedure and must not be run. "
        "Those migrations refuse by design — rolling back a schema that holds the audit "
        "trail is a data-destruction operation dressed as a schema operation "
        "(`docs/rules/append-only-audit.md`).",
        "",
        "The refusal is **not a clean abort**, which is the part that catches people. "
        "`migrations/env.py` commits each revision on its own, so a `downgrade` "
        "succeeds through every revision above the blocking one — dropping hypertables, "
        "functions, triggers and grants as it goes — and only then raises. You are left "
        "with a half-torn-down schema, not the one you started from. Measured, not "
        "reasoned about: `make rollback-drill`, `RELEASE_PROCESS.md` 7.3.",
        "",
        "```bash",
        "make down",
        f"git checkout {previous}",
        "make up            # NOT `make migrate`: the schema stays where it is",
        "python -m fking.execution.reconcile --full",
        "```",
        "",
        "This is safe **only because every migration in the range was additive** — new "
        "columns nullable, new tables unread by the old code. That is a review "
        "requirement (`CODE_REVIEW.md` 1), and it is asserted at tag time rather than "
        "discovered at rollback time, which is the reason this section exists at all.",
        "",
        "If the old code cannot run against the forward schema, **the rollback is a "
        "forward fix, not a checkout.** Cut a patch release. Say so out loud rather "
        "than reaching for `downgrade`.",
        "",
    ]


def _rollback_symmetric(notes: ReleaseNotes) -> list[str]:
    previous = notes.previous_tag or "<no previous release — see below>"
    lines = [
        "**Every migration in this range reverses cleanly.** Code and schema can move together.",
        "",
        "```bash",
        "make down",
        f"git checkout {previous}",
        "make up && make migrate",
        "python -m fking.execution.reconcile --full",
        "```",
        "",
        "`make migrate` moves forward only; against the previous tag it is a no-op "
        "because the schema is already at that revision or beyond. Prefer leaving the "
        "schema forward even here: `alembic downgrade` is available in this range but "
        "buys nothing, and a habit of reaching for it survives into the next release, "
        "where it will not be available.",
        "",
    ]
    return lines


def _rollback_section(notes: ReleaseNotes) -> list[str]:
    lines = ["## Rollback", ""]
    if contains_irreversible(notes.migrations):
        lines.extend(_rollback_forward_schema(notes))
    else:
        lines.extend(_rollback_symmetric(notes))

    if notes.previous_tag is None:
        lines.extend(
            [
                "**There is no previous release to roll back to.** This is the first "
                "tag, so the only recovery is forward: fix and cut the next version.",
                "",
            ]
        )

    lines.extend(
        [
            "In every case:",
            "",
            "- **Reconciliation is mandatory, not optional.** Rolling back code does "
            "not roll back orders already placed. Without a full reconciliation the "
            "system resumes trading against a book it has hallucinated "
            "(`RELEASE_PROCESS.md` 7.4).",
            "- **Do not roll back the global trial counter or the held-out-period "
            "flag.** Both are monotone and survive rollback on purpose; restoring "
            "either would disable the primary overfitting defence "
            "(`RELEASE_PROCESS.md` 5.1).",
            "- **Never move a tag and never force-push `main`.** A bad release gets the "
            "next patch version. This section is quoted from during an incident, and a "
            "moved tag makes it silently describe a commit that is no longer there.",
            "- **If a testnet wipe falls between this tag and the rollback**, the full "
            "reconciliation will report total divergence and trip the kill switch. "
            "That is correct behaviour and needs a human to confirm the cause before "
            "trading resumes.",
            "",
        ]
    )
    return lines


def render(notes: ReleaseNotes) -> str:
    """The complete release notes. Pure: same inputs, byte-identical output."""
    grouped = group_by_section(notes.pull_requests)

    lines: list[str] = [
        f"# {notes.version.tag}",
        "",
        f"Cut from `main` at `{notes.commit_sha}` on {notes.generated_at_utc.isoformat()}.",
        "",
        f"Previous release: "
        f"{f'`{notes.previous_tag}`' if notes.previous_tag else '_none — first release_'}.",
        "",
    ]
    lines.extend(_safety_section(notes))
    lines.extend(_results_invalidating_section(notes))

    for section in SECTION_ORDER:
        entries = grouped.get(section, ())
        if not entries:
            continue
        lines.extend([f"## {section}", ""])
        lines.extend(_entry(entry) for entry in entries)
        lines.append("")

    lines.extend(_migrations_section(notes))
    lines.extend(_rollback_section(notes))
    return "\n".join(lines).rstrip("\n") + "\n"
