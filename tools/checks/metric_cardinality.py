"""Multiply every metric's declared label domains before merging, not after.

Two separate failures are checked here, because closing one leaves the other open:

**The declared shape.** `fking.platform.telemetry` refuses a metric whose label domains
multiply past the per-metric budget at import time, so that half is already structural.
What it cannot see is the *sum*: forty individually-reasonable metrics can still exceed
what a single-node Prometheus should hold. This tool prints the product per metric and
fails when the total crosses `ACTIVE_SERIES_BUDGET`, which is the number that actually
decides whether the local stack survives an incident.

**The call site.** A bounded label name filled from an unbounded value is the same
outage with a clean-looking declaration. `symbol=payload["symbol"]` declares the
allowlisted `symbol` label and fills it from an exchange response, which is hostile
input; `venue=order.correlation_id` declares `venue` and builds one series per trade.
Neither is visible to the registry, so the increment call sites are walked as an AST.

`.claude/rules/naming.md` and `OBSERVABILITY.md` section 4 carry the reasoning.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from fking.platform.telemetry import (
    ACTIVE_SERIES_BUDGET,
    FORBIDDEN_VALUE_SOURCES,
    LABEL_DOMAINS,
    REGISTERED_METRICS,
)

# The acceptance grammar from issue #98, kept as a literal regex rather than derived from
# the registry's constants: a check that computes its own expectation from the thing it
# checks agrees with itself by construction.
NAME_GRAMMAR: Final[re.Pattern[str]] = re.compile(
    r"\Afking_(data|strategy|risk|execution|backtest|agents|evolution|platform|telemetry)"
    r"_.+_(seconds|bytes|usd|basis_points|ratio|count|messages|fields|total)\Z"
)

# Keys that name an identifier when read out of a raw mapping. Indexing a response for
# one of these and passing it as a label is the unbounded-value shape.
_UNVALIDATED_SUBSCRIPT_KEYS: Final[frozenset[str]] = FORBIDDEN_VALUE_SOURCES | {"symbol"}

_INCREMENT_METHODS: Final[frozenset[str]] = frozenset({"increment", "add", "record", "set"})


def _value_sources(node: ast.expr) -> list[str]:
    """Identifiers and subscript keys a label value is read from."""
    found: list[str] = []
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name):
            found.append(inner.id)
        elif isinstance(inner, ast.Attribute):
            found.append(inner.attr)
        elif isinstance(inner, ast.Subscript) and isinstance(inner.slice, ast.Constant):
            key = inner.slice.value
            if isinstance(key, str):
                found.append(key)
    return found


def check_source(source: str, label: str) -> list[str]:
    """Report label values read from an unbounded or unvalidated source."""
    failures: list[str] = []
    tree = ast.parse(source, filename=label)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _INCREMENT_METHODS:
            continue
        for keyword in node.keywords:
            if keyword.arg is None or keyword.arg not in LABEL_DOMAINS:
                continue
            sources = _value_sources(keyword.value)
            offending = sorted(set(sources) & FORBIDDEN_VALUE_SOURCES)
            if offending:
                failures.append(
                    f"{label}:{node.lineno} label {keyword.arg!r} is derived from "
                    f"{offending}, which builds one time series per value. Put the "
                    f"identifier on the log line and the span attribute instead"
                )
                continue
            subscripts = sorted(
                {
                    inner.slice.value
                    for inner in ast.walk(keyword.value)
                    if isinstance(inner, ast.Subscript)
                    and isinstance(inner.slice, ast.Constant)
                    and isinstance(inner.slice.value, str)
                    and inner.slice.value in _UNVALIDATED_SUBSCRIPT_KEYS
                }
            )
            if subscripts:
                failures.append(
                    f"{label}:{node.lineno} label {keyword.arg!r} is indexed out of a raw "
                    f"mapping by {subscripts}; a symbol taken from an exchange response "
                    f"is not validated against the resolved universe and is unbounded "
                    f"in practice"
                )
    return failures


def report_declared_cardinality() -> tuple[list[str], int]:
    """Print the per-metric series ceiling and return failures plus the total."""
    failures: list[str] = []
    total = 0
    for spec in sorted(REGISTERED_METRICS, key=lambda candidate: candidate.name):
        ceiling = spec.declared_series_ceiling
        total += ceiling
        breakdown = (
            " x ".join(f"{label}={LABEL_DOMAINS[label]}" for label in spec.labels) or "no labels"
        )
        print(f"{ceiling:>7}  {spec.name}  ({breakdown})")
        if NAME_GRAMMAR.fullmatch(spec.name) is None:
            failures.append(
                f"{spec.name} does not match the frozen name grammar "
                f"fking_<subsystem>_<measurement>_<unit>[_total]"
            )
    print(f"{total:>7}  TOTAL (budget {ACTIVE_SERIES_BUDGET})")
    if total > ACTIVE_SERIES_BUDGET:
        failures.append(
            f"the declared metric set can build {total} series, over the "
            f"{ACTIVE_SERIES_BUDGET} active-series budget. Individually reasonable "
            f"metrics still sum; drop a label or retire a metric"
        )
    return failures, total


def main(argv: Sequence[str]) -> int:
    # The declared-set report runs even with no trees to scan: the budget is a property
    # of the registry, not of whichever directory this was pointed at.
    failures, _total = report_declared_cardinality()
    for root in argv:
        for path in sorted(Path(root).rglob("*.py")):
            failures.extend(check_source(path.read_text(encoding="utf-8"), label=str(path)))
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
