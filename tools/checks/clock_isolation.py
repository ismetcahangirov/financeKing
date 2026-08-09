"""No module under strategy/, risk/ or backtest/ may read the wall clock.

A strategy that calls `datetime.now()` produces a different decision on Tuesday than
the same code produced on Monday against the same bar. That breaks backtest/live
parity at the only point where parity is checkable, and it makes an evolution result
irreproducible -- which means the survival score is scoring noise.

`backtest` is here for a sharper version of the same reason. Its clock is *simulated*:
the loop advances it to each event's instant, and every result is reproducible only
while that is the sole source of time in the run. One `datetime.now(UTC)` inside the
engine and two runs of the same `config_hash` disagree -- which, per BACKTEST_ENGINE.md
section 5, voids not just that run but every result the engine has ever produced, since
nothing distinguishes the ones that happened to agree.

`monotonic` and `perf_counter` are banned here too. Pure functions have no business
measuring their own runtime, and a strategy that branches on how long it took to
compute is no longer deterministically replayable.

What is NOT banned is `clock.now()` on an injected `Clock` -- that is the pattern this
rule exists to force. So the check keys on the *receiver*, not on the attribute name:
`datetime.now()` reads the wall clock, `clock.now()` reads a parameter the caller
supplied and a test can freeze. Keying on the attribute alone would flag the correct
code and get the check disabled.

See docs/rules/time-and-timezones.md.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

PURE_PACKAGES: Final[frozenset[str]] = frozenset({"backtest", "risk", "strategy"})

# The stdlib names that are wall-clock sources. `datetime.now`, `datetime.datetime.now`
# and `time.monotonic` all have one of these at the root of the attribute chain.
CLOCK_ROOTS: Final[frozenset[str]] = frozenset({"date", "datetime", "time"})

CLOCK_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "fromtimestamp",
        "monotonic",
        "monotonic_ns",
        "now",
        "perf_counter",
        "perf_counter_ns",
        "time",
        "time_ns",
        "today",
        "utcfromtimestamp",
        "utcnow",
    }
)

# `from time import monotonic` then `monotonic()` has no receiver to inspect, so these
# are banned as bare calls. `now` is deliberately absent: a bare `now()` is far more
# likely to be a local the caller injected.
BARE_CLOCK_CALLS: Final[frozenset[str]] = frozenset(
    {
        "monotonic",
        "monotonic_ns",
        "perf_counter",
        "perf_counter_ns",
        "time_ns",
        "utcnow",
        "utcfromtimestamp",
    }
)


def attribute_root(node: ast.expr) -> str | None:
    """Return the leftmost identifier of an attribute chain, or None if it is not one.

    `datetime.datetime.now` -> "datetime"; `self._clock.now` -> None, because the chain
    is rooted in a `self` attribute rather than an imported module.
    """
    current = node
    while isinstance(current, ast.Attribute):
        current = current.value
    return current.id if isinstance(current, ast.Name) else None


def offending_nodes(tree: ast.AST) -> list[ast.Call]:
    """Return every call in `tree` that reads a clock rather than an injected one."""
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in CLOCK_ATTRIBUTES and attribute_root(func) in CLOCK_ROOTS:
                found.append(node)
        elif isinstance(func, ast.Name) and func.id in BARE_CLOCK_CALLS:
            found.append(node)
    return found


def check_source(source: str, *, label: str) -> list[str]:
    tree = ast.parse(source, filename=label)
    return [
        f"{label}:{node.lineno} reads the clock directly; accept a Clock parameter instead"
        for node in offending_nodes(tree)
    ]


def check_tree(root: Path) -> list[str]:
    failures: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.relative_to(root).parts[0] not in PURE_PACKAGES:
            continue
        failures.extend(check_source(path.read_text(encoding="utf-8"), label=str(path)))
    return failures


def main(argv: Sequence[str]) -> int:
    failures: list[str] = []
    for root in argv:
        failures.extend(check_tree(Path(root)))
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
