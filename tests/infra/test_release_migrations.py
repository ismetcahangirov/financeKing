"""Classifying a migration by whether its `downgrade()` will actually run.

Half of these assert against synthetic sources, because the interesting shapes -- a
raise under a `with`, a raise in a nested helper that is never called -- do not all
exist in the repository yet and the classifier must be right about them before one
does.

The other half assert against the **real** migrations in `migrations/versions/`. That
matters more than it looks: the whole asymmetric rollback story rests on the claim that
`0002_audit_substrate.py` refuses to downgrade, and a classifier tested only on fixtures
would keep passing after somebody quietly relaxed the real file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from tools.release.migrations import (
    DowngradeKind,
    MigrationScanError,
    blocking,
    classify_downgrade,
    contains_irreversible,
    scan,
)

pytestmark = pytest.mark.unit

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
VERSIONS_DIR: Final[Path] = REPO_ROOT / "migrations" / "versions"


def sources() -> tuple[Path, ...]:
    return tuple(sorted(path for path in VERSIONS_DIR.glob("*.py") if path.name != "__init__.py"))


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------


def test_a_raise_in_the_function_body_is_irreversible() -> None:
    source = 'def downgrade() -> None:\n    raise RuntimeError("no")\n'
    assert classify_downgrade(source, filename="x.py") is DowngradeKind.IRREVERSIBLE


def test_no_raise_at_all_is_reversible() -> None:
    source = 'def downgrade() -> None:\n    op.execute("DROP TABLE t")\n'
    assert classify_downgrade(source, filename="x.py") is DowngradeKind.REVERSIBLE


@pytest.mark.parametrize(
    "body",
    [
        '    if rows:\n        raise RuntimeError("refuses")\n    op.execute("DROP TABLE t")\n',
        '    try:\n        op.execute("DROP TABLE t")\n'
        '    except KeyError:\n        raise RuntimeError("x") from None\n',
        '    with connection() as c:\n        raise RuntimeError("x")\n',
        '    for row in rows:\n        raise RuntimeError("x")\n',
    ],
)
def test_a_raise_under_control_flow_is_conditional(body: str) -> None:
    """Whether it fires is a property of the database at rollback time, so the release
    cannot know, so it must not assume the rollback works."""
    assert (
        classify_downgrade(f"def downgrade() -> None:\n{body}", filename="x.py")
        is DowngradeKind.CONDITIONAL
    )


def test_a_raise_inside_a_nested_helper_does_not_make_the_migration_irreversible() -> None:
    """A helper `downgrade()` may never call says nothing about `downgrade()`, and
    classifying on it would force a marking that does not apply."""
    source = (
        "def downgrade() -> None:\n"
        "    def _guard() -> None:\n"
        '        raise RuntimeError("only if called")\n'
        '    op.execute("DROP TABLE t")\n'
    )
    assert classify_downgrade(source, filename="x.py") is DowngradeKind.REVERSIBLE


def test_the_word_raise_in_a_docstring_does_not_count() -> None:
    """A grep would match this; the AST does not. Every migration that explains why it
    does *not* raise contains the word."""
    source = (
        "def downgrade() -> None:\n"
        '    """Reversible: nothing here holds a row, so this does not raise."""\n'
        '    op.execute("DROP TABLE t")\n'
    )
    assert classify_downgrade(source, filename="x.py") is DowngradeKind.REVERSIBLE


def test_a_missing_downgrade_is_refused_rather_than_assumed_safe() -> None:
    with pytest.raises(MigrationScanError, match="defines no module-level"):
        classify_downgrade("def upgrade() -> None:\n    pass\n", filename="x.py")


def test_a_file_that_does_not_parse_is_refused() -> None:
    with pytest.raises(MigrationScanError, match="does not parse"):
        classify_downgrade("def downgrade(\n", filename="x.py")


# ---------------------------------------------------------------------------
# Against the real migrations
# ---------------------------------------------------------------------------


def test_every_migration_in_the_repository_classifies() -> None:
    """No file under migrations/versions/ may be unclassifiable: an unreadable
    rollback verdict at release time is a release that cannot state its own procedure."""
    assert len(scan(sources())) == len(sources())


def test_the_audit_substrate_is_irreversible() -> None:
    """`docs/rules/append-only-audit.md`: `downgrade()` on an audit migration raises
    by design. If this ever passes as REVERSIBLE, either the classifier broke or the
    audit trail became droppable, and both are release-blocking."""
    audit = VERSIONS_DIR / "0002_audit_substrate.py"
    assert classify_downgrade(audit.read_text(encoding="utf-8"), filename=audit.name) is (
        DowngradeKind.IRREVERSIBLE
    )


def test_the_gap_resolution_migration_is_conditionally_irreversible() -> None:
    """0012 raises only when resolved gaps exist. Fail closed: at tag time nobody knows
    whether they do."""
    gaps = VERSIONS_DIR / "0012_gap_resolution.py"
    assert classify_downgrade(gaps.read_text(encoding="utf-8"), filename=gaps.name) is (
        DowngradeKind.CONDITIONAL
    )


def test_the_market_data_migration_reverses_cleanly() -> None:
    """A negative case, so the classifier is not trivially answering IRREVERSIBLE."""
    market = VERSIONS_DIR / "0003_market_data.py"
    assert classify_downgrade(market.read_text(encoding="utf-8"), filename=market.name) is (
        DowngradeKind.REVERSIBLE
    )


def test_a_release_spanning_the_whole_history_is_an_irreversible_release() -> None:
    """Which is what makes the marking a real gate rather than a field nobody sets:
    the first release of this repository contains 0002 and must be cut with
    --contains-irreversible-migration."""
    scanned = scan(sources())
    assert contains_irreversible(scanned)
    assert "0002_audit_substrate.py" in {migration.filename for migration in blocking(scanned)}


def test_scanned_migrations_are_ordered_by_filename() -> None:
    """The notes read in revision order; Path.glob does not promise one."""
    names = [migration.filename for migration in scan(sources())]
    assert names == sorted(names)
