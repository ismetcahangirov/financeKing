"""Compiled-in hard ceilings. Not config, not env, not database.

> Risk limits are configuration, bounded by compiled-in hard ceilings. Configuration
> can only make the system more conservative. It can never make it more permissive
> than the ceiling.

A limit stored purely in configuration is not a limit -- it is a suggestion living in
the file most likely to be edited by someone in a hurry. The realistic sequence is not
sabotage; it is "the backtest wants more notional, let me bump the env var", made at
1am and never reverted.

A limit stored purely in code is inflexible in the wrong direction: experiments
genuinely need *tighter* limits, and requiring a source change to tighten one means
limits get tightened less often than they should.

The bounded pattern gets both. Tightening is free; loosening past a ceiling requires a
source edit and a pull request labelled `safety:critical`. The direction of friction
matches the direction of risk.

This module lives under `platform/config` rather than at the `fking/risk/ceilings.py`
path named in CONFIGURATION.md section 8, because the validator that enforces it hangs
off `RiskSettings` in the configuration tree, and `platform` imports no other `fking`
module (.claude/rules/module-boundaries.md). `risk` may import `platform`; not the
reverse.

CONFIGURATION.md sections 8 and 9.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Final

# MappingProxyType rather than a dict: a dict here can be widened in-process by a test
# fixture or by generated code, and a ceiling that can be raised at runtime is not a
# ceiling. Mutating this raises TypeError at the point of the attempt.
HARD_CEILINGS: Final[Mapping[str, Decimal]] = MappingProxyType(
    {
        "max_portfolio_notional_usd": Decimal("100000"),
        "max_position_notional_usd": Decimal("25000"),
        "max_leverage": Decimal("3"),
        "max_daily_drawdown_ratio": Decimal("0.05"),
        "max_total_drawdown_ratio": Decimal("0.20"),
        "max_open_positions": Decimal("10"),
        "max_orders_per_minute": Decimal("30"),
        "max_single_order_notional_usd": Decimal("10000"),
        "max_correlated_exposure_ratio": Decimal("0.40"),
    }
)

# The same pattern applied to agent spend. An agent's token budget is a cost limit, and
# cost limits are the ones that get raised at 1am when a research run is stalling.
# Sized against the free-tier figures the project has *measured*, not against a vendor
# page -- the published quotas are unverified and tracked as OQ-001 (issue #19).
AGENT_HARD_CEILINGS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "daily_token_budget": 2_000_000,
        "requests_per_minute": 60,
        "daily_invocations": 50,
    }
)
