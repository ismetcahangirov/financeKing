"""`Order`'s constructor is reachable only from `fking.risk`.

Two mechanisms, because neither is sufficient alone.

`import-linter` forbids `fking.strategy` from importing `fking.risk` at all, which is what
stops a strategy calling the engine. It cannot express the rule under test here: `Order`
lives in `fking.domain`, and every layer above `domain` is *supposed* to import it --
`execution` submits orders, `backtest` fills them, `api` serialises them. A contract on the
import edge would either forbid nothing or forbid the whole order path from naming the type
it works with.

So the call itself is checked in the syntax tree by `tools/checks/order_construction.py`,
which `make checks` runs. This test drives that checker against a module that deliberately
constructs an `Order` from `fking/strategy/`, and asserts the failure names the contract --
because a checker whose message does not name the invariant sends the reader to the wrong
file.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from tools.checks.order_construction import CONTRACT_NAME, check_tree, main

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# argparse-style: 2 is a misinvocation, 1 is a violation. Distinct so a broken CI wiring
# is never read as a clean tree.
_USAGE_EXIT_CODE = 2

_STRATEGY_THAT_SIZES_ITSELF = '''\
"""A strategy that decided to size its own position."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from fking.domain import Order, OrderType, Side, TimeInForce


def build(instrument: object) -> Order:
    return Order(
        order_id=uuid4(),
        client_order_id="hand-rolled",
        correlation_id=uuid4(),
        instrument=instrument,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
        base_quantity=Decimal("1"),
        limit_quote_price=None,
        created_at_utc=datetime.now(UTC),
    )
'''

_STRATEGY_THAT_EDITS_AN_ORDER = '''\
"""A strategy that reached the same place through `replace`."""

from dataclasses import replace
from decimal import Decimal

from fking.domain import Order


def embiggen(order: Order) -> Order:
    return replace(order, base_quantity=Decimal("100"))
'''


def _write_module(root: Path, package: str, source: str) -> None:
    package_root = root / package
    package_root.mkdir(parents=True, exist_ok=True)
    (package_root / "sizer.py").write_text(source, encoding="utf-8")


def test_the_repository_as_it_stands_constructs_orders_only_in_risk() -> None:
    """The live invariant, not a simulation of it."""
    assert check_tree(REPOSITORY_ROOT / "src" / "fking") == []


def test_the_risk_engine_is_the_one_place_that_may(tmp_path: Path) -> None:
    """The identical source under `risk/` is permitted, so the rule is about place."""
    _write_module(tmp_path, "risk", _STRATEGY_THAT_SIZES_ITSELF)
    assert check_tree(tmp_path) == []


def test_a_strategy_constructing_an_order_fails_the_check_with_the_contract_named(
    tmp_path: Path,
) -> None:
    _write_module(tmp_path, "strategy", _STRATEGY_THAT_SIZES_ITSELF)
    failures = check_tree(tmp_path)
    assert len(failures) == 1
    assert "constructs Order(...)" in failures[0]
    assert CONTRACT_NAME in failures[0]


def test_reaching_the_same_place_through_replace_fails_too(tmp_path: Path) -> None:
    """`replace` is a constructor with a different spelling: it runs `__post_init__` and
    yields an `Order` whose quantity somebody outside the risk engine chose."""
    _write_module(tmp_path, "strategy", _STRATEGY_THAT_EDITS_AN_ORDER)
    failures = check_tree(tmp_path)
    assert len(failures) == 1
    assert "replace is a constructor with another spelling" in failures[0]
    assert CONTRACT_NAME in failures[0]


def test_the_checker_exits_non_zero_so_make_check_fails(tmp_path: Path) -> None:
    """A checker that reports and exits zero is a checker CI ignores."""
    _write_module(tmp_path, "strategy", _STRATEGY_THAT_SIZES_ITSELF)
    assert main([str(tmp_path)]) == 1
    assert main([]) == _USAGE_EXIT_CODE


def test_a_module_that_never_names_order_is_not_swept_for_replace(tmp_path: Path) -> None:
    """Otherwise every frozen-dataclass transition in the repository is a violation, and a
    check with false positives is a check that earns a blanket ignore."""
    _write_module(
        tmp_path,
        "backtest",
        "from dataclasses import replace\n\n\n"
        "def progress(resting: object) -> object:\n"
        "    return replace(resting, queue_ahead_base=0)\n",
    )
    assert check_tree(tmp_path) == []


def test_import_linter_still_forbids_a_strategy_reaching_the_order_path() -> None:
    """The other half. `lint-imports` runs it; this asserts the contract is configured, so
    deleting it fails a test rather than silently widening the boundary."""
    configuration = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = configuration["tool"]["importlinter"]["contracts"]
    forbidding = [
        contract
        for contract in contracts
        if contract["type"] == "forbidden" and contract["source_modules"] == ["fking.strategy"]
    ]
    assert len(forbidding) == 1
    assert set(forbidding[0]["forbidden_modules"]) >= {"fking.risk", "fking.execution"}
