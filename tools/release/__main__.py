"""`python -m tools.release` — cut a release, or verify a tag someone already pushed.

    python -m tools.release cut --version 0.4.0 [--contains-irreversible-migration]
                                [--notes-out CHANGELOG-v0.4.0.md] [--confirm]
    python -m tools.release verify-tag --tag v0.4.0 [--notes-out notes.md]

`cut` without `--confirm` runs every refusal and writes the notes, and creates nothing.
That is the default because the tag is the one irreversible act in the process
(`GIT_WORKFLOW.md` section 9: tags are never moved), and the rollback procedure in the
notes is the thing a future operator will follow under pressure. Reading it before the
object it describes exists is cheap; discovering it says the wrong thing afterwards is
not.

`cut --confirm` creates the annotated tag and stops. It does not push. Pushing the tag
is what triggers `.github/workflows/release.yml`, so leaving it to a human keeps the
publish a deliberate act with a name attached to it in the reflog.

`verify-tag` is what that workflow runs. It re-derives, from the tagged commit alone,
everything the operator asserted — including whether the range really does contain an
irreversible migration — so a tag created by hand with `git tag -a` is caught before a
GitHub release is published from it.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from tools.release import repository
from tools.release.changelog import ReleaseNotes, render
from tools.release.migrations import contains_irreversible
from tools.release.preflight import (
    CiVerdict,
    ReleaseRequest,
    declared_irreversible,
    refusals,
    tag_message,
)
from tools.release.version import Version

REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def _repo_relative(path: Path, repo: Path) -> str:
    """`path` as `git status --porcelain` would spell it, or "" when it is outside."""
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return ""


def _report(problems: Sequence[str]) -> int:
    for problem in problems:
        print(f"::error title=Release refused::{problem}")
        print(f"release: refused: {problem}", file=sys.stderr)
    return 1 if problems else 0


def _build_notes(
    *, version: Version, previous_tag: str | None, head: str, repo: Path
) -> ReleaseNotes:
    return ReleaseNotes(
        version=version,
        previous_tag=previous_tag,
        commit_sha=head,
        generated_at_utc=datetime.now(UTC),
        pull_requests=repository.merged_in_range(previous_tag, head, repo=repo),
        results_invalidating=repository.results_invalidating(previous_tag, head, repo=repo),
        migrations=repository.migrations_in_range(previous_tag, head, repo=repo),
        safety_diff=repository.safety_kernel_diff(previous_tag, head, repo=repo),
    )


def cut(
    *, version_text: str, declares_irreversible: bool, notes_out: Path | None, confirm: bool
) -> int:
    repo = REPO_ROOT
    version = Version.parse(version_text)
    all_tags = repository.tags(repo)
    previous_tag = repository.previous_release_tag(all_tags, before=version)
    destination = notes_out or repo / f"CHANGELOG-{version.tag}.md"

    collected = repository.collect(repo=repo, previous_tag=previous_tag)
    # The notes this command writes are the one untracked path it must not refuse on:
    # `make release` writes CHANGELOG-v0.4.0.md, and the confirming `make release-tag`
    # would otherwise report the artifact of its own first half as a dirty tree. The
    # exclusion is exactly one path, named after the version being cut, so it cannot
    # hide anything else -- and dropping the whole check here would remove the refusal
    # that stops a tag naming a commit that does not contain what was tested.
    state = replace(
        collected,
        dirty_paths=tuple(
            path for path in collected.dirty_paths if path != _repo_relative(destination, repo)
        ),
    )
    request = ReleaseRequest(version=version, declares_irreversible_migration=declares_irreversible)

    problems = refusals(request, state)
    if problems:
        return _report(problems)

    notes = _build_notes(version=version, previous_tag=previous_tag, head=state.head_sha, repo=repo)
    destination.write_text(render(notes), encoding="utf-8")
    print(f"release: preflight clean; notes written to {destination}")

    if not confirm:
        print("release: no tag created. Read the rollback section, then re-run with --confirm.")
        return 0

    repository.create_annotated_tag(version.tag, tag_message(request, state), repo=repo)
    print(f"release: created annotated tag {version.tag}")
    print(f"release: publish it with: git push origin {version.tag}")
    return 0


def verify_tag(*, tag: str, notes_out: Path | None) -> int:
    repo = REPO_ROOT
    problems: list[str] = []

    if not repository.is_annotated(tag, repo=repo):
        # A lightweight tag carries no message, so the irreversibility assertion below
        # has nothing to read, and `git tag -a` is what the process specifies.
        problems.append(
            f"{tag} is a lightweight tag. Releases are annotated: the tag message is "
            f"where the irreversible-migration assertion lives, and a lightweight tag "
            f"cannot carry one"
        )
        return _report(problems)

    commit = repository.resolve(f"{tag}^{{commit}}", repo=repo)
    if not repository.contains_commit("origin/main", commit, repo=repo):
        problems.append(
            f"{tag} points at {commit[:12]}, which is not an ancestor of origin/main. "
            f"Releases are cut from main only"
        )

    verdict = repository.ci_verdict(commit, repo=repo)
    if verdict is not CiVerdict.SUCCESS:
        problems.append(
            f"CI on {commit[:12]} is {verdict.value}, not success; this tag must not be published"
        )

    version = Version.parse(tag)
    previous_tag = repository.previous_release_tag(repository.tags(repo), before=version)
    migrations = repository.migrations_in_range(previous_tag, commit, repo=repo)
    detected = contains_irreversible(migrations)
    declared = declared_irreversible(repository.tag_message(tag, repo=repo))

    if declared is None:
        problems.append(
            f"{tag}'s message carries no `Irreversible-Migration:` trailer, so it was "
            f"not created by `python -m tools.release cut --confirm`. Delete the tag and "
            f"cut it through the tool: the trailer is what makes the rollback procedure "
            f"in the notes checkable"
        )
    elif declared != detected:
        problems.append(
            f"{tag} declares Irreversible-Migration: {'yes' if declared else 'no'} but "
            f"the range {previous_tag or '(root)'}..{commit[:12]} "
            f"{'does' if detected else 'does not'} contain one. The declaration selects "
            f"which rollback procedure the notes publish, so a wrong one is a wrong "
            f"incident runbook"
        )

    if problems:
        return _report(problems)

    if notes_out is not None:
        notes = _build_notes(version=version, previous_tag=previous_tag, head=commit, repo=repo)
        notes_out.write_text(render(notes), encoding="utf-8")
        print(f"release: notes written to {notes_out}")
    print(f"release: {tag} verified: on main, green, marking matches the migrations")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.release", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    cut_parser = sub.add_parser("cut", help="preflight, write notes, optionally tag")
    cut_parser.add_argument("--version", required=True, help="x.y.z")
    cut_parser.add_argument(
        "--contains-irreversible-migration",
        action="store_true",
        help="assert that the range contains a migration whose downgrade() will not run",
    )
    cut_parser.add_argument("--notes-out", type=Path, default=None)
    cut_parser.add_argument(
        "--confirm", action="store_true", help="create the annotated tag (never pushes)"
    )

    verify_parser = sub.add_parser("verify-tag", help="check a pushed tag before publishing")
    verify_parser.add_argument("--tag", required=True)
    verify_parser.add_argument("--notes-out", type=Path, default=None)
    return parser


def main(argv: Sequence[str]) -> int:
    parsed = _parser().parse_args(argv)
    if parsed.command == "cut":
        return cut(
            version_text=str(parsed.version),
            declares_irreversible=bool(parsed.contains_irreversible_migration),
            notes_out=parsed.notes_out,
            confirm=bool(parsed.confirm),
        )
    return verify_tag(tag=str(parsed.tag), notes_out=parsed.notes_out)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
