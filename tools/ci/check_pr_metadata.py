"""Enforce the two pull-request metadata rules from GIT_WORKFLOW.md.

Both catch something a diff view cannot show you.

**A `docs` pull request may not touch `src/`.** GIT_WORKFLOW.md section 4: "a docs
branch that quietly changed code is the one nobody reads carefully". A reviewer opening
something labelled `docs` reads prose, and skims anything that is not prose.

**A diff touching the safety kernel must carry `safety:critical`.** GIT_WORKFLOW.md
section 7 lists what counts, and it is broader than the allowlist itself: the kernel's
tests, the import-linter contracts, even a reformat. "It especially includes changes
that look like cleanups. A frozenset reformatted into a multi-line literal is exactly
the diff in which one entry changes and nobody notices."

Both rules fail closed. An unparseable title is a violation rather than a skipped
check, because a rule that switches itself off when its input is unrecognised is a rule
that can be disabled by retitling a pull request.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# Conventional Commits: type(optional scope)(optional !): description
_CONVENTIONAL_TITLE: Final = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?!?:\s+\S")

DOCUMENTATION_TYPES: Final[frozenset[str]] = frozenset({"docs"})
SOURCE_PREFIX: Final[str] = "src/"

# Every path GIT_WORKFLOW.md section 7 counts as the safety kernel. The kernel's own
# tests are included deliberately: deleting an assertion there weakens the guarantee
# exactly as much as editing the allowlist, and it looks far more innocent.
SAFETY_PATH_PREFIXES: Final[tuple[str, ...]] = (
    "src/fking/platform/safety/",
    "tests/platform/safety/",
)
SAFETY_LABEL: Final[str] = "safety:critical"


@dataclass(frozen=True, slots=True)
class PullRequestFacts:
    title: str
    labels: frozenset[str]
    changed_files: tuple[str, ...]


def commit_type(title: str) -> str | None:
    """The Conventional Commits type, or None when the title is not conventional."""
    matched = _CONVENTIONAL_TITLE.match(title.strip())
    return matched.group("type") if matched else None


def _normalise(path: str) -> str:
    """Windows checkouts produce backslashes; the rules are written with forward ones."""
    return path.strip().replace("\\", "/")


def violations(facts: PullRequestFacts) -> list[str]:
    """Every rule broken by this pull request, one message each."""
    found: list[str] = []
    paths = tuple(_normalise(path) for path in facts.changed_files)

    pull_request_type = commit_type(facts.title)
    if pull_request_type is None:
        found.append(
            f"title {facts.title!r} is not a conventional commit "
            f"(`type(scope): description`), so the docs-touching-src rule cannot be "
            f"evaluated; see GIT_WORKFLOW.md section 3"
        )
    elif pull_request_type in DOCUMENTATION_TYPES:
        source_changes = [path for path in paths if path.startswith(SOURCE_PREFIX)]
        if source_changes:
            found.append(
                f"a {pull_request_type} pull request changed source files "
                f"{sorted(source_changes)}; split the code change out, because a docs "
                f"branch that quietly changed code is the one nobody reads carefully"
            )

    safety_changes = [path for path in paths if path.startswith(SAFETY_PATH_PREFIXES)]
    if safety_changes and SAFETY_LABEL not in facts.labels:
        found.append(
            f"{sorted(safety_changes)} touches the safety kernel but the pull request "
            f"is not labelled {SAFETY_LABEL}; this path changes the demo-only "
            f"guarantee and needs a human decision, not a review rubber-stamp"
        )
    return found


def _parse(argv: Sequence[str]) -> PullRequestFacts:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-title", required=True)
    parser.add_argument("--labels", default="", help="comma-separated")
    parser.add_argument(
        "--changed-files",
        required=True,
        type=Path,
        help="file holding one changed path per line, as produced by git diff --name-only",
    )
    parsed = parser.parse_args(argv)
    listing = parsed.changed_files.read_text(encoding="utf-8")
    return PullRequestFacts(
        title=parsed.pr_title,
        labels=frozenset(label.strip() for label in parsed.labels.split(",") if label.strip()),
        changed_files=tuple(line for line in listing.splitlines() if line.strip()),
    )


def main(argv: Sequence[str]) -> int:
    found = violations(_parse(argv))
    for violation in found:
        # ::error is what makes this land as an annotation on the pull request rather
        # than as a line somebody has to open the log to find.
        print(f"::error title=PR metadata::{violation}")
        print(f"pr-metadata: {violation}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
