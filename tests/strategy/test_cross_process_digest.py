"""The same bars and the same seed digest identically in a *second process*.

`test_determinism` already replays twice inside one interpreter, and that is the weaker
statement. A within-process replay cannot see the nondeterminism that actually bites here:
`PYTHONHASHSEED` is randomised per process, so anything that iterates a `set` or a `dict`
keyed on objects whose `__hash__` is address-derived produces one order for the whole run
and a different one tomorrow. Two replays in one interpreter agree on that order and both
are wrong the next day -- which surfaces as a backtest that cannot be reproduced from its
own recorded seed, months later, with real capital allocated on the strength of it.

The child is run with an explicitly different hash seed rather than an inherited one, so
the check is adversarial rather than incidental.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from fking.strategy import SHIPPED_STRATEGIES, StrategyBuilder, replay
from tests.strategy.harness import (
    BTCUSDT,
    bars_from_closes,
    exercising_closes,
    feature_values_for,
    signal_digest,
)

pytestmark = pytest.mark.unit

_SEED = 20260801
_BAR_COUNT = 128
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# A fixed, deliberately unusual hash seed for the child. Zero would disable randomisation
# and make the test agree with the parent for the wrong reason; the point is that a
# *different* randomisation still produces the same bytes.
_CHILD_HASH_SEED = "1"

_CHILD_PROGRAM = """
import sys

from fking.strategy import SHIPPED_STRATEGIES, replay
from tests.strategy.harness import (
    BTCUSDT,
    bars_from_closes,
    exercising_closes,
    feature_values_for,
    signal_digest,
)

build = {builder}
strategy = build((BTCUSDT,))
series = bars_from_closes(exercising_closes({bar_count}))
signals = replay(
    strategy,
    series,
    seed={seed},
    feature_values_at=feature_values_for(strategy.spec, series),
)
sys.stdout.write(f"{{len(signals)}} {{signal_digest(signals)}}")
"""


def _strategy_id(build: StrategyBuilder) -> str:
    return str(getattr(build, "__name__", build))


@pytest.mark.parametrize("build", SHIPPED_STRATEGIES, ids=_strategy_id)
def test_a_replay_in_a_second_process_produces_the_same_digest(build: StrategyBuilder) -> None:
    strategy = build((BTCUSDT,))
    series = bars_from_closes(exercising_closes(_BAR_COUNT))
    here = replay(
        strategy,
        series,
        seed=_SEED,
        feature_values_at=feature_values_for(strategy.spec, series),
    )
    assert here, "an empty signal stream digests identically to another empty one"

    index = SHIPPED_STRATEGIES.index(build)
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = _CHILD_HASH_SEED
    completed = subprocess.run(  # noqa: S603 - a fixed argv, no shell, nothing a caller supplied
        [
            sys.executable,
            "-c",
            _CHILD_PROGRAM.format(
                builder=f"SHIPPED_STRATEGIES[{index}]",
                bar_count=_BAR_COUNT,
                seed=_SEED,
            ),
        ],
        capture_output=True,
        check=True,
        cwd=_REPOSITORY_ROOT,
        env=environment,
        text=True,
    )

    assert completed.stdout == f"{len(here)} {signal_digest(here)}", (
        f"{strategy.spec.describe()} replayed to a different stream in a second process; "
        f"a result that cannot be reproduced from its own seed is not evidence of anything"
    )
