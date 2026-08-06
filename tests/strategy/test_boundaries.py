"""Two structural properties the strategy layer would otherwise depend on discipline for.

**No import path to the order path.** `import-linter` proves this on the whole graph and
`tests/adversarial/test_boundary_contracts.py` proves the contract fails when it is
violated. This file adds the statement in the form a reader of `fking.strategy` will look
for: a direct scan of every module in the package. It is redundant on purpose -- when the
layers contract breaks, the failure names a layer ordering; this one names the invariant.

**No magic constant in an `evaluate` body.** The evolution engine mutates declared
parameters within declared bounds. A `Decimal("0.004")` written inside `evaluate` is a
threshold chosen by a human, never charged to the global trial ledger, unsearchable, and
absent from the specification -- so the lineage recorded for its descendants is a claim
nobody can check. Nothing off-the-shelf catches it, and it is invisible in review because
it looks exactly like every other constant in the file.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from typing import Final

import pytest

import fking.strategy
from fking.strategy import SHIPPED_STRATEGIES, StrategyBuilder
from tests.strategy.harness import BTCUSDT
from tools.checks import clock_isolation

pytestmark = pytest.mark.unit

_PACKAGE_ROOT: Final[Path] = Path(fking.strategy.__file__).parent
_FORBIDDEN_PREFIXES: Final[tuple[str, ...]] = ("fking.execution", "fking.risk")

# `visible[-1]`, `not bars`, and an index of 0 or 1 are arithmetic on positions in a
# sequence, not thresholds a search could explore. Everything else must come from the
# declared parameter space.
_ALLOWED_INT_LITERALS: Final[frozenset[int]] = frozenset({0, 1})


def _strategy_id(build: StrategyBuilder) -> str:
    return str(getattr(build, "__name__", build))


def _imported_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.module is not None and node.level == 0:
        return [node.module]
    return []


@pytest.mark.parametrize("path", sorted(_PACKAGE_ROOT.rglob("*.py")), ids=lambda path: path.name)
def test_no_module_in_the_strategy_package_imports_the_order_path(path: Path) -> None:
    """A strategy that can construct an `Order` can bypass the risk engine entirely.

    The author this guards against is not a careless human. P5 and P6 generate strategies
    via LLM agents, and an agent asked to improve returns will size its own positions the
    moment the type system permits it -- plausibly, with a comment explaining why this
    case is different.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offending = [
        f"{path.name}:{node.lineno} imports {module}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for module in _imported_modules(node)
        if module.startswith(_FORBIDDEN_PREFIXES)
    ]

    assert offending == []


@pytest.mark.parametrize("build", SHIPPED_STRATEGIES, ids=_strategy_id)
def test_no_shipped_strategy_constructs_a_decimal_inside_evaluate(
    build: StrategyBuilder,
) -> None:
    """A threshold has to be a `Decimal`, and a `Decimal` cannot be written without
    constructing one -- so banning the construction bans the undeclared threshold."""
    tree = _evaluate_body(build)
    constructions = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "Decimal")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "Decimal")
        )
    ]

    assert constructions == [], (
        f"{_strategy_id(build)}.evaluate constructs a Decimal at lines {constructions}; "
        f"every number it compares against belongs in the declared parameter space"
    )


@pytest.mark.parametrize("build", SHIPPED_STRATEGIES, ids=_strategy_id)
def test_no_shipped_strategy_carries_a_numeric_literal_inside_evaluate(
    build: StrategyBuilder,
) -> None:
    """Beyond 0 and 1, which are sequence positions rather than thresholds."""
    tree = _evaluate_body(build)
    literals = [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
        and node.value not in _ALLOWED_INT_LITERALS
    ]

    assert literals == [], (
        f"{_strategy_id(build)}.evaluate carries the literals {literals}; a number chosen "
        f"in the body is one the trial ledger was never charged for"
    )


@pytest.mark.parametrize("build", SHIPPED_STRATEGIES, ids=_strategy_id)
def test_every_shipped_strategy_declares_a_falsifiable_invalidation_rule(
    build: StrategyBuilder,
) -> None:
    """A strategy that cannot state what would prove it wrong has a hope, not a thesis."""
    spec = build((BTCUSDT,)).spec

    assert spec.invalidation.adverse_move_fraction > 0
    assert spec.thesis.strip()


def test_the_clock_isolation_check_passes_over_the_strategy_package() -> None:
    """A strategy that reads the wall clock decides differently on Tuesday than it did on
    Monday against the same bar, which breaks parity at the only point parity is
    checkable."""
    assert clock_isolation.check_tree(_PACKAGE_ROOT.parent) == []


def test_the_clock_isolation_check_fails_when_a_strategy_reads_the_wall_clock(
    tmp_path: Path,
) -> None:
    """A gate that has never been observed to fail might be asserting `True == True`.

    Pointed at a tree containing one poisoned module rather than at a copy of the whole
    package: the check filters on the top-level package name, so `strategy/poisoned.py` is
    exactly what it would see in the real tree.
    """
    poisoned = tmp_path / "strategy" / "poisoned.py"
    poisoned.parent.mkdir(parents=True)
    poisoned.write_text(
        "from datetime import UTC, datetime\n\nas_of = datetime.now(UTC)\n", encoding="utf-8"
    )

    failures = clock_isolation.check_tree(tmp_path)

    assert failures, "the check accepted a wall-clock read inside strategy"
    assert "poisoned.py" in failures[0]


def _evaluate_body(build: StrategyBuilder) -> ast.AST:
    """The parsed source of the strategy's `evaluate`, dedented so it parses standalone."""
    evaluate = type(build((BTCUSDT,))).evaluate
    return ast.parse(textwrap.dedent(inspect.getsource(evaluate)))
