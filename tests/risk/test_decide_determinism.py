"""The same decision replays to the same order, in this process and in another one.

This is what makes a backtest and a demo run comparable at all: if the same signals against
the same book could produce two different orders, every backtest result is unfalsifiable.
It is also the idempotency property the venue sees -- a resubmission after a timeout carries
the same `client_order_id`, so the venue recognises it as the same order rather than a
second one (`docs/rules/idempotency.md`).

The cross-process half is not ceremony. Python's `hash()` is salted per process, so an
identity derived from it reproduces perfectly inside one run and silently diverges between
two. `uuid5` and `sha256` do not.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from fking.risk import ORDER_ID_NAMESPACE
from tests.support.risk_engine import (
    CORRELATION_ID,
    SEED,
    frozen_clock,
    make_engine,
    make_market_state,
    make_portfolio_state,
    make_signal,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# Binance's `newClientOrderId` filter, from the spot API reference.
_BINANCE_CLIENT_ORDER_ID_LIMIT = 36

_IN_ANOTHER_PROCESS = """
import sys
sys.path.insert(0, {root!r})
from tests.support.risk_engine import (
    CORRELATION_ID, SEED, frozen_clock, make_engine, make_market_state,
    make_portfolio_state, make_signal,
)

batch = make_engine().decide(
    signals=[make_signal("alpha")],
    portfolio_state=make_portfolio_state(),
    market_state=make_market_state(),
    clock=frozen_clock,
    correlation_id=CORRELATION_ID,
    seed=SEED,
)
order = batch.orders[0]
print(order.order_id, order.client_order_id, order.base_quantity, sep="|")
"""


def _decide_once(*, seed: int = SEED) -> object:
    return make_engine().decide(
        signals=[make_signal("alpha")],
        portfolio_state=make_portfolio_state(),
        market_state=make_market_state(),
        clock=frozen_clock,
        correlation_id=CORRELATION_ID,
        seed=seed,
    )


def test_two_runs_of_the_same_inputs_produce_the_identical_order() -> None:
    first = _decide_once().orders[0]  # type: ignore[attr-defined]
    second = _decide_once().orders[0]  # type: ignore[attr-defined]
    assert first == second
    assert first.order_id == second.order_id
    assert first.client_order_id == second.client_order_id


def test_the_order_reproduces_in_another_process() -> None:
    """`hash()` is salted per process; `uuid5` and `sha256` are not."""
    expected = _decide_once().orders[0]  # type: ignore[attr-defined]
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
        [sys.executable, "-c", _IN_ANOTHER_PROCESS.format(root=str(REPOSITORY_ROOT))],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPOSITORY_ROOT,
    )
    order_id, client_order_id, base_quantity = completed.stdout.strip().split("|")
    assert order_id == str(expected.order_id)
    assert client_order_id == expected.client_order_id
    assert Decimal(base_quantity) == expected.base_quantity


def test_a_different_seed_produces_a_different_client_order_id() -> None:
    """Two replay runs of the same batch must not collide on an id at the venue, while each
    run stays byte-identical to itself."""
    first = _decide_once().orders[0]  # type: ignore[attr-defined]
    second = _decide_once(seed=SEED + 1).orders[0]  # type: ignore[attr-defined]
    assert first.base_quantity == second.base_quantity
    assert first.client_order_id != second.client_order_id
    assert first.order_id != second.order_id


def test_the_client_order_id_fits_every_shipped_venue_filter() -> None:
    """Binance rejects a clientOrderId over 36 characters or outside its charset, and it
    does so at submission -- after the risk decision is already recorded."""
    permitted = set("abcdefghijklmnopqrstuvwxyz0123456789-")
    client_order_id = _decide_once().orders[0].client_order_id  # type: ignore[attr-defined]
    assert client_order_id.startswith("fk-")
    assert len(client_order_id) <= _BINANCE_CLIENT_ORDER_ID_LIMIT
    assert set(client_order_id) <= permitted


def test_the_namespace_is_pinned() -> None:
    """Changing it makes every historical order id unreproducible, which is a change to the
    audit trail rather than a refactor."""
    assert str(ORDER_ID_NAMESPACE) == "6f2b7d64-7f43-5f9c-9f0e-2c9c8f9a4d11"
