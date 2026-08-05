"""The boundary: everything that shells out to `git` or `gh`, and nothing that decides.

The split is the point. `preflight` and `changelog` are pure functions over values, so
their guarantees are testable without a repository, a network, or a red CI run to hand.
This module's only job is to turn a working copy into those values.

Two things it does differently from `RELEASE_PROCESS.md`'s worked commands.

**Merged pull requests come from `git log`, not from the search API.** See
`changelog.py` for the argument; the short version is that ancestry is exact and a
time-bounded index query is not.

**Check runs are read for the exact SHA, and a cancelled run counts as a failure.**
GitHub reports `cancelled` as a conclusion alongside `success`, and a run cancelled by
`ci.yml`'s concurrency group is a run whose verdict was thrown away — which is the same
epistemic state as no run at all, not the same as a pass.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from tools.release.changelog import (
    FIELD_SEPARATOR,
    RECORD_SEPARATOR,
    ChangelogError,
    MergedPullRequest,
    ResultsInvalidatingChange,
    parse_results_invalidating,
    pull_request_numbers,
)
from tools.release.migrations import Migration, scan
from tools.release.preflight import CiVerdict, RepositoryState
from tools.release.version import Version, VersionError

MIGRATIONS_DIR: Final[str] = "migrations/versions"
SAFETY_KERNEL_DIR: Final[str] = "src/fking/platform/safety"

# `git log --format` spells a literal byte as `%xNN`, so these produce exactly the
# separators `changelog.parse_results_invalidating` splits on. Derived from the parser's
# constants rather than typed twice: the two ends of this wire have to agree, and a
# format string that drifts from its parser fails as "every commit is malformed".
_TRAILER_FORMAT: Final[str] = (
    f"%H%x{ord(FIELD_SEPARATOR):02x}%s%x{ord(FIELD_SEPARATOR):02x}"
    f"%(trailers:key=Results-Invalidating,valueonly)%x{ord(RECORD_SEPARATOR):02x}"
)

# Anything not in this set means the verdict is not a pass. `neutral` and `skipped` are
# passes: a path-filtered job that legitimately did not apply still reported.
_PASSING_CONCLUSIONS: Final[frozenset[str]] = frozenset({"success", "neutral", "skipped"})


class RepositoryError(RuntimeError):
    """A `git` or `gh` invocation failed, or returned something unusable."""


def _run(command: Sequence[str], *, cwd: Path) -> str:
    """Run `command` and return stdout, raising on a non-zero exit.

    S603/S607: every argument here is a literal or a value this module derived from
    `git` itself; nothing a caller supplied reaches the argument vector, and the list
    form never involves a shell. Resolving `git` and `gh` by absolute path would make
    the tool depend on where a particular machine installed them.
    """
    completed = subprocess.run(  # noqa: S603
        list(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode != 0:
        raise RepositoryError(
            f"{' '.join(command)} exited {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout


def _git(arguments: Sequence[str], *, repo: Path) -> str:
    return _run(["git", *arguments], cwd=repo)


def _gh(arguments: Sequence[str], *, repo: Path) -> str:
    return _run(["gh", *arguments], cwd=repo)


def current_branch(repo: Path) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], repo=repo).strip()


def dirty_paths(repo: Path) -> tuple[str, ...]:
    listing = _git(["status", "--porcelain"], repo=repo)
    return tuple(line[3:].strip() for line in listing.splitlines() if line.strip())


def resolve(revision: str, *, repo: Path) -> str:
    return _git(["rev-parse", revision], repo=repo).strip()


def tags(repo: Path) -> frozenset[str]:
    listing = _git(["tag", "--list"], repo=repo)
    return frozenset(line.strip() for line in listing.splitlines() if line.strip())


def tag_message(tag: str, *, repo: Path) -> str:
    """The annotated tag's own message, without the tagged commit's message.

    `%(contents)` on an annotated tag is the tag body; on a lightweight tag it is the
    commit's. The workflow refuses a lightweight tag separately, so this returning the
    commit message for one is not a hazard here.
    """
    return _git(["for-each-ref", f"refs/tags/{tag}", "--format=%(contents)"], repo=repo)


def is_annotated(tag: str, *, repo: Path) -> bool:
    kind = _git(["cat-file", "-t", tag], repo=repo).strip()
    return kind == "tag"


def contains_commit(branch: str, commit: str, *, repo: Path) -> bool:
    """True when `commit` is an ancestor of, or equal to, `branch`."""
    completed = subprocess.run(  # noqa: S603
        ["git", "merge-base", "--is-ancestor", commit, branch],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return completed.returncode == 0


def _range(previous_tag: str | None, head: str) -> str:
    return f"{previous_tag}..{head}" if previous_tag else head


def migrations_in_range(
    previous_tag: str | None, head: str, *, repo: Path
) -> tuple[Migration, ...]:
    """Migrations added or modified between `previous_tag` and `head`.

    Modified, not just added: a migration whose `downgrade()` was edited after its
    revision shipped changes the rollback story for this release exactly as much as a
    new one does, and it is the change a reviewer is least likely to look at.

    With no previous tag the range is the whole history, which is correct rather than
    convenient: the first release does contain every migration, including the audit
    substrate, and it does have to be marked accordingly.
    """
    listing = _git(
        [
            "diff",
            "--name-only",
            "--diff-filter=AM",
            _range(previous_tag, head),
            "--",
            MIGRATIONS_DIR,
        ]
        if previous_tag
        else ["ls-tree", "-r", "--name-only", head, "--", MIGRATIONS_DIR],
        repo=repo,
    )
    paths = [
        repo / line.strip()
        for line in listing.splitlines()
        if line.strip().endswith(".py") and not line.strip().endswith("__init__.py")
    ]
    return scan(path for path in paths if path.is_file())


def commit_subjects(previous_tag: str | None, head: str, *, repo: Path) -> tuple[str, ...]:
    listing = _git(["log", "--format=%s", _range(previous_tag, head)], repo=repo)
    return tuple(line for line in listing.splitlines() if line.strip())


def results_invalidating(
    previous_tag: str | None, head: str, *, repo: Path
) -> tuple[ResultsInvalidatingChange, ...]:
    output = _git(["log", f"--format={_TRAILER_FORMAT}", _range(previous_tag, head)], repo=repo)
    return parse_results_invalidating(output)


def safety_kernel_diff(previous_tag: str | None, head: str, *, repo: Path) -> str:
    if previous_tag is None:
        return ""
    return _git(["diff", _range(previous_tag, head), "--", SAFETY_KERNEL_DIR], repo=repo)


def ci_verdict(commit: str, *, repo: Path) -> CiVerdict:
    """Aggregate the check runs reported for `commit`."""
    payload = json.loads(
        _gh(["api", f"repos/{{owner}}/{{repo}}/commits/{commit}/check-runs"], repo=repo)
    )
    runs = payload.get("check_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list) or not runs:
        return CiVerdict.ABSENT

    statuses: list[str] = []
    conclusions: list[str] = []
    for run in runs:
        if not isinstance(run, dict):
            raise RepositoryError(f"unexpected check-run entry for {commit}: {run!r}")
        statuses.append(str(run.get("status", "")))
        conclusions.append(str(run.get("conclusion") or ""))

    if any(status != "completed" for status in statuses):
        return CiVerdict.PENDING
    if all(conclusion in _PASSING_CONCLUSIONS for conclusion in conclusions):
        return CiVerdict.SUCCESS
    return CiVerdict.FAILURE


def merged_pull_requests(numbers: Sequence[int], *, repo: Path) -> tuple[MergedPullRequest, ...]:
    """Fetch title, url and labels for each pull request number, in the order given."""
    fetched: list[MergedPullRequest] = []
    for number in numbers:
        payload = json.loads(_gh(["api", f"repos/{{owner}}/{{repo}}/pulls/{number}"], repo=repo))
        if not isinstance(payload, dict):
            raise RepositoryError(f"pull request #{number} returned {payload!r}")
        raw_labels = payload.get("labels", [])
        if not isinstance(raw_labels, list):
            raise ChangelogError(f"pull request #{number} has unreadable labels {raw_labels!r}")
        fetched.append(
            MergedPullRequest(
                number=number,
                title=str(payload.get("title", "")),
                url=str(payload.get("html_url", "")),
                labels=frozenset(
                    str(label.get("name", "")) for label in raw_labels if isinstance(label, dict)
                ),
            )
        )
    return tuple(fetched)


def previous_release_tag(all_tags: frozenset[str], *, before: Version) -> str | None:
    """The highest release tag strictly below `before`, or None."""
    candidates = sorted(
        parsed
        for parsed in (_maybe_version(tag) for tag in all_tags)
        if parsed is not None and parsed < before
    )
    return candidates[-1].tag if candidates else None


def _maybe_version(tag: str) -> Version | None:
    try:
        return Version.parse(tag)
    except VersionError:
        return None


def create_annotated_tag(tag: str, message: str, *, repo: Path) -> None:
    """The one write this module performs. Never pushes: see `tools/release/__main__`."""
    _git(["tag", "-a", tag, "-m", message], repo=repo)


def collect(*, repo: Path, previous_tag: str | None) -> RepositoryState:
    """Assemble the value `preflight.refusals` decides on."""
    head = resolve("HEAD", repo=repo)
    _git(["fetch", "--quiet", "origin", "main", "--tags"], repo=repo)
    return RepositoryState(
        branch=current_branch(repo),
        dirty_paths=dirty_paths(repo),
        head_sha=head,
        origin_branch_sha=resolve("origin/main", repo=repo),
        tags=tags(repo),
        ci=ci_verdict(head, repo=repo),
        migrations_in_range=migrations_in_range(previous_tag, head, repo=repo),
    )


def merged_in_range(
    previous_tag: str | None, head: str, *, repo: Path
) -> tuple[MergedPullRequest, ...]:
    """Every pull request merged between `previous_tag` and `head`."""
    numbers = pull_request_numbers(commit_subjects(previous_tag, head, repo=repo))
    return merged_pull_requests(numbers, repo=repo)
