"""Engine, cost model and validation. Knows about simulated venues and historical
clocks.

Backtest and live share one code path; only the `ExecutionVenue` swaps. If a strategy
could behave differently here than on the demo account, every result this module
produces would be unfalsifiable.

Cost model parameters are calibrated from production market archives, never from
testnet -- testnet's measured spread is roughly fifty times production's.
"""

__all__: tuple[str, ...] = ()
