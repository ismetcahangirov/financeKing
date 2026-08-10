"""No configuration reachable from `strategy` or `risk` can say which venue is running.

`import-linter` stops a strategy importing `execution`, so it cannot name a venue class.
That closes the obvious door and leaves the quiet one open: a settings object carrying
`is_backtest`, `simulated`, or `dry_run` is importable from `platform.config`, reads as
ordinary plumbing, and gives a strategy or a risk limit exactly the discriminant the venue
Protocol was built to withhold.

The failure it produces is the worst kind. `if settings.is_backtest: skip_the_cooldown()`
passes review as a test convenience, and afterwards the backtest and the live path are two
strategies with one track record -- and the track record is the backtest's.

The scan is source-level rather than import-and-introspect. A field can be declared on a
pydantic model, a frozen dataclass, a `TypedDict` or a bare annotation, and introspection
sees only the ones whose class it managed to instantiate. It also covers keyword
parameters, because `def decide(*, is_backtest: bool)` is the same discriminant arriving
by a different route.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.unit

_SRC: Final[Path] = Path(__file__).resolve().parents[2] / "src" / "fking"

#: The packages a strategy or a risk decision can reach. `domain` and `platform.config`
#: are in scope because both are importable from `risk` today -- `risk.ceilings` reads
#: `platform.config.HARD_CEILINGS` -- so a flag added there is a flag a risk limit can
#: branch on, whatever the layering diagram says about intent.
REACHABLE: Final[tuple[str, ...]] = ("strategy", "risk", "domain", "platform/config")

#: `is_backtest`-shaped: a name whose whole job is to say which harness is running. The
#: pattern is anchored so it flags `is_backtest` and `paper_mode` and leaves
#: `backtest_start_utc` or `simulation_seed` alone -- those name a value, not a branch.
DISCRIMINANT: Final[re.Pattern[str]] = re.compile(
    r"^(is_|use_|in_|running_)?"
    r"(back_?test|paper|replay|demo|live|simulated|simulation|dry_run|sandbox|testnet)"
    r"(_mode|_run|ing)?$"
)


def _package_names() -> frozenset[str]:
    """The top-level packages under `src/fking`.

    `Settings.backtest: BacktestSettings` is a configuration *section* mirroring the
    package it configures, not a discriminant: reading it yields a group of values, and
    there is no boolean in it that says "you are in a backtest". The same exemption
    `tools/checks/naming.py` makes for `data`, and for the same reason -- and derived from
    the tree, so it can only be widened by creating a package, which the layering contract
    already reviews.
    """
    return frozenset(
        entry.name
        for entry in _SRC.iterdir()
        if entry.is_dir() and (entry / "__init__.py").exists()
    )


def _is_discriminant(name: str) -> bool:
    """Whether `name` tells its reader which harness is running."""
    return DISCRIMINANT.match(name) is not None and name not in _package_names()


def _declared_names(source: str, *, label: str) -> list[tuple[str, int]]:
    """Every field, keyword parameter and module constant a module declares."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(source, filename=label)):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.append((node.target.id, node.lineno))
        elif isinstance(node, ast.arg):
            found.append((node.arg, node.lineno))
        elif isinstance(node, ast.Assign):
            found.extend(
                (target.id, node.lineno) for target in node.targets if isinstance(target, ast.Name)
            )
    return found


def _offences(root: Path) -> list[str]:
    return [
        f"{path}:{lineno} '{name}' tells the caller which harness is running"
        for path in sorted(root.rglob("*.py"))
        for name, lineno in _declared_names(path.read_text(encoding="utf-8"), label=str(path))
        if _is_discriminant(name)
    ]


@pytest.mark.parametrize("package", REACHABLE)
def test_no_harness_discriminant_is_reachable_from_strategy_or_risk(package: str) -> None:
    """The acceptance criterion, one package at a time so a failure names the culprit."""
    root = _SRC.joinpath(*package.split("/"))
    assert root.exists(), f"{package} is not where this test thinks it is"

    assert _offences(root) == []


@pytest.mark.parametrize(
    "declared",
    ["is_backtest", "back_test", "paper_mode", "dry_run", "simulated", "in_replay", "use_testnet"],
)
def test_the_pattern_catches_the_names_somebody_would_actually_write(declared: str) -> None:
    """A probe that flags nothing is indistinguishable from one that is not wired up."""
    assert _is_discriminant(declared)


@pytest.mark.parametrize(
    "declared",
    [
        "backtest_start_utc",
        "simulation_seed",
        "live_bars_consumed",
        "paper_trail",
        "demo_account",
        # The settings section that mirrors the package, not a flag -- see _package_names.
        "backtest",
    ],
)
def test_the_pattern_leaves_names_that_carry_a_value_alone(declared: str) -> None:
    """A check that flags everything gets disabled within a week."""
    assert not _is_discriminant(declared)


def test_the_scan_reads_pydantic_fields_and_keyword_parameters() -> None:
    """Both declaration sites, because banning only one teaches the other."""
    source = "\n".join(
        (
            "class Settings(BaseModel):",
            "    is_backtest: bool = False",
            "",
            "def decide(*, dry_run: bool) -> None: ...",
        )
    )
    flagged = [name for name, _ in _declared_names(source, label="x.py") if _is_discriminant(name)]

    assert flagged == ["is_backtest", "dry_run"]
