"""Every strategy this package ships, as builders rather than as instances.

Builders and not instances because a specification names *instruments*, and instruments
are venue facts resolved at startup from the intersection of the venue's tradable set and
the archive's history (`.claude/rules/exchange-integration.md`). A module-level instance
would have to hardcode a tick size and a lot step, which is a declaration that the run's
venue may contradict.

The tuple exists so that the test suite quantifies over it rather than over a list
somebody maintains: the determinism replay, the warm-up property, the look-ahead probe and
the no-magic-constant check are all parametrised over `SHIPPED_STRATEGIES`, so a strategy
added here inherits every one of them with no test-file edit. That is the same property
`fking.data.features.registry` buys for features, and it exists for the same reason -- the
route by which something reaches production unprobed is not being in the list at all.

Which of these actually runs is a deployment decision made by the caller that builds a
`StrategyRegistry`, not a fact about the package.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from fking.domain import Instrument
from fking.strategy._contract import Strategy
from fking.strategy.trailing_return import TrailingReturnContinuation

__all__ = ["SHIPPED_STRATEGIES", "StrategyBuilder"]

# Not an abstraction over behaviour -- there is nothing to dispatch on. It names the shape
# the catalogue holds, so a caller can see what it is being handed without reading the
# constructor of every entry.
StrategyBuilder = Callable[[tuple[Instrument, ...]], Strategy]

SHIPPED_STRATEGIES: Final[tuple[StrategyBuilder, ...]] = (TrailingReturnContinuation,)
