"""The two pull-request metadata rules GIT_WORKFLOW.md already documents.

Both exist because the thing they catch is invisible in a diff view. A `docs` branch
that quietly changed code is the one nobody reads carefully, and a safety-path change
without its label is a change to the demo-only guarantee that reached review looking
like an ordinary chore.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.ci.check_pr_metadata import PullRequestFacts, commit_type, main, violations

pytestmark = pytest.mark.unit


def facts(
    *, title: str = "feat(risk): net correlated exposure", labels: str = "", files: str = ""
) -> PullRequestFacts:
    return PullRequestFacts(
        title=title,
        labels=frozenset(label for label in labels.split(",") if label),
        changed_files=tuple(path for path in files.split(",") if path),
    )


class TestCommitType:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("docs(standards): record the gate", "docs"),
            ("feat(risk): net exposure", "feat"),
            ("chore(platform)!: drop the shim", "chore"),
            ("fix: correct the epoch unit", "fix"),
        ],
    )
    def test_conventional_titles_parse(self, title: str, expected: str) -> None:
        assert commit_type(title) == expected

    @pytest.mark.parametrize("title", ["updated some docs", "WIP", "docs - standards"])
    def test_non_conventional_titles_do_not_parse(self, title: str) -> None:
        assert commit_type(title) is None


class TestDocsTouchingSource:
    def test_a_docs_pr_touching_src_is_rejected(self) -> None:
        found = violations(
            facts(title="docs(standards): tidy", files="CODING_STANDARDS.md,src/fking/risk/x.py")
        )
        assert len(found) == 1
        assert "src/fking/risk/x.py" in found[0]

    def test_a_docs_pr_touching_only_documentation_is_accepted(self) -> None:
        assert violations(facts(title="docs(standards): tidy", files="CODING_STANDARDS.md")) == []

    def test_a_feat_pr_touching_src_is_accepted(self) -> None:
        assert violations(facts(title="feat(risk): size", files="src/fking/risk/x.py")) == []

    def test_an_unparseable_title_is_rejected_rather_than_skipped(self) -> None:
        """Fail closed: otherwise retitling a PR silently disables the rule."""
        found = violations(facts(title="some changes", files="src/fking/risk/x.py"))
        assert len(found) == 1
        assert "conventional" in found[0].lower()


class TestSafetyKernelLabel:
    SAFETY_FILE = "src/fking/platform/safety/_allowlist.py"

    def test_touching_the_safety_kernel_without_the_label_is_rejected(self) -> None:
        found = violations(facts(files=self.SAFETY_FILE))
        assert len(found) == 1
        assert "safety:critical" in found[0]

    def test_touching_the_safety_kernel_with_the_label_is_accepted(self) -> None:
        assert violations(facts(labels="safety:critical", files=self.SAFETY_FILE)) == []

    def test_the_safety_kernel_tests_count_as_the_safety_path(self) -> None:
        """Editing the kernel's own tests is how a guarantee is weakened quietly."""
        found = violations(facts(files="tests/platform/safety/test_guarded_client.py"))
        assert len(found) == 1

    def test_an_unrelated_change_needs_no_label(self) -> None:
        assert violations(facts(files="src/fking/risk/x.py")) == []

    def test_both_rules_can_fire_at_once(self) -> None:
        """One report per run: fixing the label only to be told about the docs rule
        next time is how a two-minute check becomes a twenty-minute round trip."""
        reported = " | ".join(violations(facts(title="docs(x): tidy", files=self.SAFETY_FILE)))
        assert "changed source files" in reported
        assert "safety:critical" in reported


class TestCommandLineInterface:
    """The argv shape `ci.yml` invokes.

    Worth testing separately from the rules: a checker whose CLI drifts from the
    workflow calling it fails as a broken run rather than as a rejected change, and a
    broken run is the kind of red people learn to re-run rather than read.
    """

    SAFETY_FILE = "src/fking/platform/safety/_allowlist.py"

    def _argv(self, listing: Path, *, labels: str) -> list[str]:
        return [
            "--pr-title",
            "feat(platform): add a host",
            "--labels",
            labels,
            "--changed-files",
            str(listing),
        ]

    def test_a_violation_exits_one_and_annotates_the_pull_request(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        listing = tmp_path / "changed-files.txt"
        listing.write_text(f"{self.SAFETY_FILE}\nREADME.md\n", encoding="utf-8")

        assert main(self._argv(listing, labels="type:feat,area:security")) == 1
        # ::error is what turns this into an annotation on the pull request rather
        # than a line somebody has to open the log to find.
        assert "::error title=PR metadata::" in capsys.readouterr().out

    def test_a_compliant_pull_request_exits_zero(self, tmp_path: Path) -> None:
        listing = tmp_path / "changed-files.txt"
        listing.write_text(f"{self.SAFETY_FILE}\n", encoding="utf-8")

        assert main(self._argv(listing, labels="safety:critical")) == 0

    def test_a_blank_line_in_the_listing_is_not_a_changed_file(self, tmp_path: Path) -> None:
        """`git diff --name-only` output ends with a newline; an empty final entry
        would otherwise be a zero-length path that matches no prefix but still counts."""
        listing = tmp_path / "changed-files.txt"
        listing.write_text("README.md\n\n", encoding="utf-8")

        assert main(self._argv(listing, labels="")) == 0
