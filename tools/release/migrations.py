"""Classify each Alembic migration in a release range by whether it can be undone.

This is the module that makes the rollback path in the release notes true rather than
aspirational, so it is worth being explicit about what it is measuring.

A rollback of a stateless service is "deploy the previous tag". Here it is not, because
`downgrade()` on the audit substrate **raises by design** -- dropping a table that holds
the audit trail is a data-destruction operation dressed as a schema operation
(`docs/rules/append-only-audit.md`). So a release whose range contains such a
migration has a *different* rollback procedure from one that does not: code goes back,
schema stays forward, and that only works if every migration in the range was additive.
Which of the two procedures applies is a fact about the code, knowable at tag time, and
the entire reason it is computed here rather than discovered at 03:00.

**The classification fails closed, and the interesting case is `CONDITIONAL`.**
`migrations/versions/0012_gap_resolution.py` raises only when resolved gaps exist:

    if resolved:
        raise RuntimeError("0012 refuses to downgrade with N resolved gap(s) ...")

Whether that raises is a property of the *database at rollback time*, not of the file.
At tag time it is unknowable, and the only safe reading of an unknowable rollback is
that it will not work -- so `CONDITIONAL` is treated exactly as `IRREVERSIBLE` by
`contains_irreversible()`. Classifying it as reversible would put the wrong procedure
into notes that are immutable and read during an incident.

The parse is AST-based rather than a grep for `raise`. A grep matches the word inside
the docstring of every migration that explains why it does *not* raise, and misses
nothing that an AST walk would catch -- which makes it wrong in the direction that
reports safety.
"""

from __future__ import annotations

import ast
import enum
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

DOWNGRADE_FUNCTION: str = "downgrade"


class DowngradeKind(enum.Enum):
    """How a migration's `downgrade()` behaves when it is actually run."""

    REVERSIBLE = "reversible"
    CONDITIONAL = "conditionally irreversible"
    IRREVERSIBLE = "irreversible"


class MigrationScanError(ValueError):
    """A file under migrations/versions/ could not be classified."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One migration file and the verdict on undoing it."""

    filename: str
    downgrade: DowngradeKind

    @property
    def blocks_rollback(self) -> bool:
        return self.downgrade is not DowngradeKind.REVERSIBLE


# A `raise` inside one of these is not a statement about `downgrade()`; it is a helper
# `downgrade()` may never call. Descending into one would classify a perfectly
# reversible migration as irreversible and force a marking that does not apply.
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _raises_below(node: ast.AST) -> bool:
    """True when a `raise` is reachable below `node` without entering a nested scope.

    `ast.iter_child_nodes` rather than a hand-rolled walk over `ast.stmt` lists: an
    `except` handler is an `ast.excepthandler`, not an `ast.stmt`, and a walk that
    filters on `ast.stmt` therefore never enters one. That is not an academic gap --
    `raise ... from err` inside an `except` block is the single most common way a
    migration refuses.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _NESTED_SCOPES):
            continue
        if isinstance(child, ast.Raise) or _raises_below(child):
            return True
    return False


def classify_downgrade(source: str, *, filename: str) -> DowngradeKind:
    """Verdict on the `downgrade()` defined in `source`.

    IRREVERSIBLE when a `raise` sits directly in the function body, because nothing can
    reach past it. CONDITIONAL when a `raise` sits under an `if`, `try`, loop or `with`.
    REVERSIBLE when there is none.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as invalid:
        raise MigrationScanError(f"{filename} does not parse: {invalid}") from invalid

    definitions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == DOWNGRADE_FUNCTION
    ]
    if not definitions:
        # Alembic generates `downgrade()` into every revision; a file without one has
        # been hand-edited, and a release must not guess which way.
        raise MigrationScanError(
            f"{filename} defines no module-level {DOWNGRADE_FUNCTION}(); a migration "
            f"whose rollback behaviour cannot be read is not classifiable, and a "
            f"release will not assume it is safe"
        )
    if len(definitions) > 1:
        raise MigrationScanError(
            f"{filename} defines {DOWNGRADE_FUNCTION}() {len(definitions)} times; the "
            f"last definition wins at runtime and the reader sees the first"
        )

    definition = definitions[0]
    if any(isinstance(statement, ast.Raise) for statement in definition.body):
        return DowngradeKind.IRREVERSIBLE
    # `definition` itself, not each body statement: the skip test in `_raises_below`
    # applies to the *children* it iterates, so passing a nested `def` in directly
    # would descend into the very scope it exists to exclude.
    if _raises_below(definition):
        return DowngradeKind.CONDITIONAL
    return DowngradeKind.REVERSIBLE


def scan(paths: Iterable[Path]) -> tuple[Migration, ...]:
    """Classify every path, ordered by filename so the notes read in revision order."""
    scanned = [
        Migration(
            filename=path.name,
            downgrade=classify_downgrade(path.read_text(encoding="utf-8"), filename=str(path)),
        )
        for path in paths
    ]
    return tuple(sorted(scanned, key=lambda migration: migration.filename))


def contains_irreversible(migrations: Iterable[Migration]) -> bool:
    """True when any migration in the range cannot be relied on to undo itself."""
    return any(migration.blocks_rollback for migration in migrations)


def blocking(migrations: Iterable[Migration]) -> tuple[Migration, ...]:
    """The migrations that force the schema-forward rollback procedure, in order."""
    return tuple(migration for migration in migrations if migration.blocks_rollback)
