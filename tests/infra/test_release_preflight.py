"""The refusals that stand between a command and an immutable tag.

Every test here constructs a `RepositoryState` directly. That is the whole reason
`tools.release.preflight` takes a value rather than a repository: "the release refuses
to tag when CI is red" is only a guarantee if it can be asserted without arranging a red
CI run, and a check that reads the repository is a claim tested by nothing.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from tools.release.__main__ import _repo_relative
from tools.release.migrations import DowngradeKind, Migration
from tools.release.preflight import (
    CiVerdict,
    ReleaseRequest,
    RepositoryState,
    declared_irreversible,
    refusals,
    tag_message,
)
from tools.release.version import Version, VersionError

pytestmark = pytest.mark.unit

_HEAD: Final[str] = "a" * 40

REVERSIBLE: Final[Migration] = Migration("0003_market_data.py", DowngradeKind.REVERSIBLE)
IRREVERSIBLE: Final[Migration] = Migration("0002_audit_substrate.py", DowngradeKind.IRREVERSIBLE)
CONDITIONAL: Final[Migration] = Migration("0012_gap_resolution.py", DowngradeKind.CONDITIONAL)

# branch, dirty tree, red CI, version ordering, unmarked irreversible migration.
_EVERY_REFUSAL_AT_ONCE: Final[int] = 5


# A state on which nothing is refused. Every test below is `replace(CLEAN, ...)` with
# exactly the fields it is about, so a passing test names its own precondition and a
# new refusal cannot be smuggled in by widening the fixture.
CLEAN: Final[RepositoryState] = RepositoryState(
    branch="main",
    dirty_paths=(),
    head_sha=_HEAD,
    origin_branch_sha=_HEAD,
    tags=frozenset({"v0.3.0"}),
    ci=CiVerdict.SUCCESS,
    migrations_in_range=(REVERSIBLE,),
)


def request_for(version: str = "0.4.0", *, irreversible: bool = False) -> ReleaseRequest:
    return ReleaseRequest(
        version=Version.parse(version), declares_irreversible_migration=irreversible
    )


def test_a_clean_main_with_green_ci_is_not_refused() -> None:
    assert refusals(request_for(), CLEAN) == ()


def test_a_dirty_tree_is_refused() -> None:
    found = refusals(request_for(), replace(CLEAN, dirty_paths=("src/fking/api/__init__.py",)))
    assert any("working tree is dirty" in problem for problem in found)


def test_a_branch_other_than_main_is_refused() -> None:
    found = refusals(request_for(), replace(CLEAN, branch="chore/115-release-automation"))
    assert any("not 'main'" in problem for problem in found)


def test_a_local_main_that_diverges_from_origin_is_refused() -> None:
    found = refusals(request_for(), replace(CLEAN, origin_branch_sha="b" * 40))
    assert any("origin/main" in problem for problem in found)


@pytest.mark.parametrize("verdict", [CiVerdict.FAILURE, CiVerdict.PENDING])
def test_ci_that_is_not_success_is_refused(verdict: CiVerdict) -> None:
    found = refusals(request_for(), replace(CLEAN, ci=verdict))
    assert any(verdict.value in problem for problem in found)


def test_an_absent_ci_verdict_is_refused_rather_than_treated_as_neutral() -> None:
    """Absence is the normal state of a commit pushed a minute ago, not a pass."""
    found = refusals(request_for(), replace(CLEAN, ci=CiVerdict.ABSENT))
    assert any("Absence is not a pass" in problem for problem in found)


def test_an_existing_tag_is_refused_rather_than_moved() -> None:
    found = refusals(request_for("0.3.0"), CLEAN)
    assert any("already exists" in problem for problem in found)


def test_a_version_below_the_latest_release_is_refused() -> None:
    found = refusals(request_for("0.2.9"), replace(CLEAN, tags=frozenset({"v0.3.0", "v0.1.0"})))
    assert any("does not exceed" in problem for problem in found)


def test_a_release_shaped_tag_that_is_not_a_release_version_is_refused() -> None:
    """`v0.4.0-rc1` reads as a release, sorts unpredictably, and is invisible to max()."""
    found = refusals(request_for(), replace(CLEAN, tags=frozenset({"v0.3.0", "v0.4.0-rc1"})))
    assert any("v0.4.0-rc1" in problem for problem in found)


def test_the_first_release_has_no_ordering_constraint() -> None:
    assert refusals(request_for("0.1.0"), replace(CLEAN, tags=frozenset())) == ()


# ---------------------------------------------------------------------------
# The irreversible-migration gate. Issue #115: "a test proves the release refuses
# to tag when a migration in the range is irreversible unless the release is
# explicitly marked as such".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("migration", [IRREVERSIBLE, CONDITIONAL])
def test_an_undeclared_irreversible_migration_refuses_the_tag(migration: Migration) -> None:
    found = refusals(
        request_for(irreversible=False),
        replace(CLEAN, migrations_in_range=(REVERSIBLE, migration)),
    )
    assert any(migration.filename in problem for problem in found)
    assert any("--contains-irreversible-migration" in problem for problem in found)


@pytest.mark.parametrize("migration", [IRREVERSIBLE, CONDITIONAL])
def test_declaring_it_clears_the_refusal(migration: Migration) -> None:
    assert (
        refusals(
            request_for(irreversible=True),
            replace(CLEAN, migrations_in_range=(REVERSIBLE, migration)),
        )
        == ()
    )


def test_declaring_an_irreversible_migration_that_is_not_there_is_also_refused() -> None:
    """Over-declaring is not conservative: it publishes the wrong incident runbook."""
    found = refusals(request_for(irreversible=True), CLEAN)
    assert any("Over-declaring is not conservative" in problem for problem in found)


def test_every_refusal_is_reported_not_only_the_first() -> None:
    found = refusals(
        request_for("0.2.0", irreversible=False),
        replace(
            CLEAN,
            branch="feat/1-x",
            dirty_paths=("a.py",),
            ci=CiVerdict.FAILURE,
            migrations_in_range=(IRREVERSIBLE,),
        ),
    )
    # branch, dirty tree, red CI, version ordering, unmarked irreversible migration.
    assert len(found) == _EVERY_REFUSAL_AT_ONCE


# ---------------------------------------------------------------------------
# The tag message is a machine-readable assertion, not prose.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("declared", [True, False])
def test_the_tag_message_round_trips_the_irreversibility_assertion(declared: bool) -> None:
    message = tag_message(
        request_for(irreversible=declared),
        replace(CLEAN, migrations_in_range=(IRREVERSIBLE if declared else REVERSIBLE,)),
    )
    assert message.startswith("v0.4.0\n")
    assert declared_irreversible(message) is declared


def test_a_tag_message_without_the_trailer_reads_as_unknown_not_as_no() -> None:
    """None is distinct from False: no trailer means the tag was not cut by the tool."""
    assert declared_irreversible("v0.4.0\n\nhand-written\n") is None


def test_an_unparseable_trailer_value_reads_as_unknown() -> None:
    assert declared_irreversible("v0.4.0\n\nIrreversible-Migration: probably\n") is None


# ---------------------------------------------------------------------------
# The notes artifact must not refuse the tag step that follows it
# ---------------------------------------------------------------------------


def test_the_notes_file_the_cut_writes_is_the_one_untracked_path_it_ignores() -> None:
    """`make release` writes CHANGELOG-v0.4.0.md; `make release-tag` runs the same
    refusals afterwards and must not report the artifact of its own first half as a
    dirty tree. The exclusion is exactly one path, named after the version being cut."""
    repo = Path("/repo")
    assert _repo_relative(repo / "CHANGELOG-v0.4.0.md", repo) == "CHANGELOG-v0.4.0.md"


def test_a_notes_path_outside_the_repository_excludes_nothing() -> None:
    """`--notes-out /tmp/x.md` must not turn into an empty string that matches a path
    `git status` could actually produce."""
    assert _repo_relative(Path("/elsewhere/notes.md"), Path("/repo")) == ""


def test_the_dirty_tree_refusal_still_fires_for_anything_else() -> None:
    found = refusals(request_for(), replace(CLEAN, dirty_paths=("CHANGELOG.md",)))
    assert any("working tree is dirty" in problem for problem in found)


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["0.4.0", "v0.4.0", " v0.4.0 "])
def test_a_release_version_parses_with_or_without_the_tag_prefix(text: str) -> None:
    assert Version.parse(text) == Version(0, 4, 0)
    assert Version.parse(text).tag == "v0.4.0"


@pytest.mark.parametrize("text", ["0.4", "0.4.0-rc1", "0.4.0+build.7", "01.4.0", "latest", ""])
def test_anything_that_is_not_three_integers_is_refused(text: str) -> None:
    with pytest.raises(VersionError):
        Version.parse(text)
