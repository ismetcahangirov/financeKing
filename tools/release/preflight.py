"""The refusals that stand between a command and an immutable tag.

Everything here is a pure function of a `RepositoryState` value. That is deliberate:
"the release refuses to tag when CI is red" is only a guarantee if it can be asserted
without a red CI run to hand, and the moment the check reads the repository directly it
becomes a claim tested by nothing.

Two of the refusals are worth arguing for, because both look like over-caution.

**An absent CI verdict is a refusal, not a neutral.** The obvious reading of "no check
runs reported for this commit" is "nothing has failed". The correct reading is "nothing
has run", and those are opposite. Absence is the normal state of a commit pushed sixty
seconds ago, which is precisely the commit somebody is trying to tag in a hurry.
`GIT_WORKFLOW.md` section 7: never merge without green CI — a tag is a stronger claim
than a merge.

**Declaring an irreversible migration that is not there is refused too.** The
asymmetry is tempting to allow: over-declaring seems conservative. It is not, because
the declaration selects which of two rollback procedures is written into notes that are
immutable and read during an incident. A release wrongly marked irreversible tells a
future operator not to touch the schema when they safely could, and — worse — trains
them that the marker does not mean anything.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final

from tools.release import version as version_module
from tools.release.migrations import Migration, blocking, contains_irreversible
from tools.release.version import Version

RELEASE_BRANCH: Final[str] = "main"

IRREVERSIBLE_FLAG: Final[str] = "--contains-irreversible-migration"

# How many dirty paths to name before saying "and more". Enough to recognise what is
# uncommitted, short enough that the refusal stays readable in a CI annotation.
_DIRTY_PATHS_SHOWN: Final[int] = 10


class CiVerdict(enum.Enum):
    """The aggregate conclusion of the check runs reported for a commit."""

    SUCCESS = "success"
    FAILURE = "failure"
    PENDING = "pending"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class RepositoryState:
    """Everything about the repository that the decision depends on."""

    branch: str
    dirty_paths: tuple[str, ...]
    head_sha: str
    origin_branch_sha: str
    tags: frozenset[str]
    ci: CiVerdict
    migrations_in_range: tuple[Migration, ...]


@dataclass(frozen=True, slots=True)
class ReleaseRequest:
    """What the operator asked for."""

    version: Version
    declares_irreversible_migration: bool


def _branch_refusals(state: RepositoryState) -> list[str]:
    found: list[str] = []
    if state.branch != RELEASE_BRANCH:
        found.append(
            f"HEAD is on {state.branch!r}, not {RELEASE_BRANCH!r}. A release is cut from "
            f"{RELEASE_BRANCH} only: a tag on a branch names a commit that never passed "
            f"the required checks and may never reach {RELEASE_BRANCH} at all"
        )
    if state.dirty_paths:
        found.append(
            f"the working tree is dirty: {list(state.dirty_paths[:_DIRTY_PATHS_SHOWN])}"
            f"{' (+more)' if len(state.dirty_paths) > _DIRTY_PATHS_SHOWN else ''}. "
            f"The tag would name a "
            f"commit that does not contain what was tested"
        )
    if state.head_sha != state.origin_branch_sha:
        found.append(
            f"HEAD is {state.head_sha[:12]} but origin/{RELEASE_BRANCH} is "
            f"{state.origin_branch_sha[:12]}. Pull or push first: a tag pointing at a "
            f"commit no one else has is a rollback target that does not exist for anyone "
            f"but you"
        )
    return found


def _ci_refusals(state: RepositoryState) -> list[str]:
    if state.ci is CiVerdict.SUCCESS:
        return []
    if state.ci is CiVerdict.ABSENT:
        return [
            f"no check run is reported for {state.head_sha[:12]}. Absence is not a pass: "
            f"it is the normal state of a commit pushed a minute ago, which is exactly "
            f"the commit somebody tags in a hurry. Wait for CI"
        ]
    return [
        f"CI on {state.head_sha[:12]} is {state.ci.value}, not success. "
        f"GIT_WORKFLOW.md 7: nothing merges without green CI, and a tag claims more than "
        f"a merge does"
    ]


def _version_refusals(request: ReleaseRequest, state: RepositoryState) -> list[str]:
    found: list[str] = []
    if request.version.tag in state.tags:
        found.append(
            f"{request.version.tag} already exists. Tags are immutable and never moved "
            f"(GIT_WORKFLOW.md 9): a bad release gets the next patch version, because "
            f"the runtime snapshot in the existing notes describes a specific commit and "
            f"re-pointing the tag makes that description silently wrong"
        )

    malformed = version_module.release_shaped_but_unparseable(state.tags)
    if malformed:
        found.append(
            f"{list(malformed)} look like release tags but are not release versions. "
            f"They are invisible to the ordering check below, so 'is this version newer "
            f"than the last one' cannot be answered. Delete or rename them"
        )

    latest = version_module.latest(state.tags)
    if latest is not None and request.version <= latest:
        found.append(
            f"{request.version} does not exceed the latest release {latest}. Version "
            f"order is how a reader of the tag list works out what superseded what"
        )
    return found


def _migration_refusals(request: ReleaseRequest, state: RepositoryState) -> list[str]:
    detected = contains_irreversible(state.migrations_in_range)
    if detected and not request.declares_irreversible_migration:
        named = ", ".join(
            f"{migration.filename} ({migration.downgrade.value})"
            for migration in blocking(state.migrations_in_range)
        )
        return [
            f"the range contains a migration that will not undo itself: {named}. This "
            f"release has a different rollback procedure from one that does not - code "
            f"back, schema forward - and that procedure is only safe if every migration "
            f"in the range was additive. Confirm that, then re-run with "
            f"{IRREVERSIBLE_FLAG}"
        ]
    if request.declares_irreversible_migration and not detected:
        return [
            f"{IRREVERSIBLE_FLAG} was passed but every migration in the range reverses "
            f"cleanly. Over-declaring is not conservative: it writes the schema-forward "
            f"procedure into notes that cannot be edited, tells a future operator not to "
            f"touch a schema they safely could, and teaches them the marker means nothing"
        ]
    return []


def refusals(request: ReleaseRequest, state: RepositoryState) -> tuple[str, ...]:
    """Every reason this release must not be cut. Empty means go.

    All of them, never just the first: a release cut is a stop-the-world event with
    nothing merging while it runs, so three attempts to learn three problems costs
    three of those windows.
    """
    found: list[str] = []
    found.extend(_branch_refusals(state))
    found.extend(_ci_refusals(state))
    found.extend(_version_refusals(request, state))
    found.extend(_migration_refusals(request, state))
    return tuple(found)


def tag_message(request: ReleaseRequest, state: RepositoryState) -> str:
    """The annotated tag's message.

    The `Irreversible-Migration:` trailer is not decoration. `.github/workflows/
    release.yml` re-derives the classification from the tagged commit and refuses to
    publish when the trailer disagrees, which is what catches a tag created by hand with
    `git tag -a` instead of through this tool.
    """
    marker = "yes" if request.declares_irreversible_migration else "no"
    listing = (
        ", ".join(
            f"{migration.filename} ({migration.downgrade.value})"
            for migration in state.migrations_in_range
        )
        or "none"
    )
    return f"{request.version.tag}\n\nIrreversible-Migration: {marker}\nMigrations: {listing}\n"


def declared_irreversible(message: str) -> bool | None:
    """Read the `Irreversible-Migration:` trailer back. None when it is absent.

    None is distinct from False on purpose: a tag with no trailer was not created by
    this tool, and the workflow treats that as a refusal rather than as "no".
    """
    for line in message.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Irreversible-Migration:"):
            continue
        value = stripped.split(":", 1)[1].strip().lower()
        if value in {"yes", "true"}:
            return True
        if value in {"no", "false"}:
            return False
        return None
    return None
