"""Shared synthetic fixtures for the Monte Carlo tests.

`pseudo_trade_returns` and `momentum_market_returns` both use the same 32-bit LCG (the
`glibc` constants) as `tests/backtest/cpcv_support.py`'s `pseudo_returns`, for the reason
given there: the sequence must be identical on every machine and under every
`pytest-randomly` shuffle, and reusing the same generator rather than `random.Random`
keeps the market data these tests resample independent of the seeded streams the
production code under test derives from `run_seed` -- a test whose fixture and whose
subject drew from the same stream could not tell "the code derived the seed correctly"
apart from "the fixture happened to agree with it".

`momentum_edge` is a decision rule, not production code: classic time-series momentum,
position on day *t* is the sign of day *t-1*'s return, and the edge is the mean of the
resulting daily strategy return. It lives here rather than in `fking.backtest.montecarlo`
because that package's contract is that it never special-cases one strategy's rule
(`fking.backtest.montecarlo._block_bootstrap`'s own docstring) -- the evaluator is always
injected, and this is the injected value the block-bootstrap regression fixture uses.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final

# Same LCG as tests/backtest/cpcv_support.py's pseudo_returns, seeded differently so the
# two fixture families never collide.
_LCG_MULTIPLIER: Final = 1103515245
_LCG_INCREMENT: Final = 12345
_LCG_MODULUS: Final = 2**31


def pseudo_trade_returns(trade_total: int, *, seed: int = 20260810) -> tuple[Decimal, ...]:
    """A deterministic per-trade return series with no edge, scaled into [-1%, 1%)."""
    state = seed
    values: list[Decimal] = []
    for _ in range(trade_total):
        state = (_LCG_MULTIPLIER * state + _LCG_INCREMENT) % _LCG_MODULUS
        values.append((Decimal(state % 2001) - Decimal(1000)) / Decimal(100000))
    return tuple(values)


# The trend magnitude per day, and the jitter band around it. Jitter strictly smaller
# than magnitude is what keeps every day's sign equal to its block's direction, which is
# what makes the block-boundary mispredictions the only source of momentum error in the
# unshuffled series.
TREND_MAGNITUDE: Final = Decimal("0.004")
_JITTER_DIVISOR: Final = Decimal("100000")


def momentum_market_returns(
    *, block_total: int, block_length: int, seed: int = 20260810
) -> tuple[Decimal, ...]:
    """A daily return series built from trending blocks, for the block-bootstrap fixture.

    Each block holds one sign for its whole length -- `block_length` is deliberately the
    same number a caller would pass as `max_holding_horizon` to the block bootstrap, so
    the fixture and the acceptance criterion are stated in the same unit. Between blocks
    the sign is redrawn, so autocorrelation exists up to `block_length` and not beyond.
    """
    state = seed
    values: list[Decimal] = []
    for _ in range(block_total):
        state = (_LCG_MULTIPLIER * state + _LCG_INCREMENT) % _LCG_MODULUS
        direction = Decimal(1) if state % 2 == 0 else Decimal(-1)
        for _day in range(block_length):
            state = (_LCG_MULTIPLIER * state + _LCG_INCREMENT) % _LCG_MODULUS
            jitter = (Decimal(state % 21) - Decimal(10)) / _JITTER_DIVISOR
            values.append(direction * TREND_MAGNITUDE + jitter)
    return tuple(values)


#: The momentum rule needs a "yesterday" and a "today", so two observations is its floor.
_MIN_OBSERVATIONS_FOR_MOMENTUM: Final = 2


def momentum_edge(returns: Sequence[Decimal]) -> Decimal:
    """Mean daily return of "hold today's position in yesterday's sign" over `returns`."""
    if len(returns) < _MIN_OBSERVATIONS_FOR_MOMENTUM:
        raise ValueError("momentum_edge needs at least two observations")
    strategy_returns: list[Decimal] = []
    for index in range(1, len(returns)):
        prior = returns[index - 1]
        if prior > 0:
            position = Decimal(1)
        elif prior < 0:
            position = Decimal(-1)
        else:
            position = Decimal(0)
        strategy_returns.append(position * returns[index])
    return sum(strategy_returns, Decimal(0)) / Decimal(len(strategy_returns))


# A baseline the perturbation fixtures share, and the edge both synthetic evaluators
# agree the unperturbed baseline is worth.
PERTURBATION_BASELINE: Final[Mapping[str, Decimal]] = {
    "fast_period": Decimal("10"),
    "slow_period": Decimal("50"),
}
BASELINE_EDGE: Final = Decimal("1.00")

# Deviation is the sum of squared fractional jitters across axes; a single-axis ±10%
# jitter produces deviation = 0.10^2 = 0.01, which is what both curves below are shaped
# against.
_PLATEAU_CURVATURE: Final = Decimal("50")
_SPIKE_FLOOR_FRACTION: Final = Decimal("0.05")


def _squared_fractional_deviation(params: Mapping[str, Decimal]) -> Decimal:
    total = Decimal(0)
    for name, baseline_value in PERTURBATION_BASELINE.items():
        fractional = (params[name] - baseline_value) / baseline_value
        total += fractional * fractional
    return total


def plateau_evaluator(params: Mapping[str, Decimal]) -> Decimal:
    """A shallow quadratic bowl: a ±10% single-axis jitter retains ~99.98% of the edge."""
    deviation = _squared_fractional_deviation(params)
    return BASELINE_EDGE * (Decimal(1) - deviation / _PLATEAU_CURVATURE)


def spike_evaluator(params: Mapping[str, Decimal]) -> Decimal:
    """A needle: any deviation from the exact baseline collapses to 5% of the edge."""
    deviation = _squared_fractional_deviation(params)
    if deviation == Decimal(0):
        return BASELINE_EDGE
    return BASELINE_EDGE * _SPIKE_FLOOR_FRACTION
