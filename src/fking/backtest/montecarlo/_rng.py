"""One `random.Random` stream per path, derived rather than drawn.

`path_rng` calls `fking.backtest._config.derive_seed`, the same function every other
seeded source in the engine uses, so a Monte Carlo run composes with the rest of a
`RunConfig` under one `run_seed` instead of owning a second seeding scheme. Two processes
given the same `run_seed` derive the same per-path integer seed and construct the same
`random.Random`, which produces the same draws -- CPython's Mersenne Twister is portable
across processes and platforms for a given integer seed, which is what makes
`tests/backtest/test_monte_carlo_determinism.py` a property of this function rather than
an accident of one machine.

The label carries both the method and anything that changes what "the same path" means
for that method -- `block_bootstrap:{block_length}` so that path 3 at block length 1 and
path 3 at block length 6 are different draws rather than the same draw replayed against a
different reassembly rule.
"""

from __future__ import annotations

import random

from fking.backtest._config import derive_seed


def path_rng(run_seed: int, *, label: str, path_index: int) -> random.Random:
    """A `random.Random` for exactly one path, independent of every other path and label."""
    return random.Random(derive_seed(run_seed, f"{label}:{path_index}"))
