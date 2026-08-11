"""Compiled-in hard bounds, in both directions. Not config, not env, not database.

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

**Both directions, because half the limits run the other way.** Most limits are "larger
is riskier" and belong in `HARD_CEILINGS`. A few are "smaller is riskier" --
`min_free_margin_ratio`, `min_trades_for_kelly`, `conviction_floor` -- and a ceiling on
those bounds nothing. Stating only the ceilings leaves the floors to whichever field
constraint somebody happens to write, and the field constraint that gets written is the
permissive one: `conviction_floor` carried `ge=0` here for as long as the floors were
absent, which accepts acting on a zero-conviction signal and reads as a passing
configuration check (issue #171).

This module lives under `platform/config` rather than at the `fking/risk/ceilings.py`
path named in CONFIGURATION.md section 8, because the validator that enforces it hangs
off `RiskSettings` in the configuration tree, and `platform` imports no other `fking`
module (docs/rules/module-boundaries.md). `risk` may import `platform`; not the
reverse. So the *values* live here and `fking.risk.ceilings` imports them and adds the
direction as a type -- one copy of each number, because two compiled-in copies of a
safety constant are two numbers that can disagree, and the one that disagrees silently
is whichever the reader is not looking at.

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

# The other direction. Every value here is a *lower* bound: configuration may raise one
# freely and may never lower it. Values sourced individually, because a floor with no
# provenance is a number the next reader deletes as arbitrary.
HARD_FLOORS: Final[Mapping[str, Decimal]] = MappingProxyType(
    {
        # RISK_PHILOSOPHY.md section 4, portfolio limits table: min free margin defaults
        # to 40% of equity with a floor of 25%. Below that a maintenance-margin call can
        # precede the drawdown kill switch, which inverts the order in which the two
        # safety mechanisms are supposed to fire.
        "min_free_margin_ratio": Decimal("0.25"),
        # RISK_PHILOSOPHY.md section 3.3: the Kelly term is simply absent from the sizing
        # min() below 100 closed trades, because the fractional standard error on the
        # Kelly numerator is 1/(SR*sqrt(T)) and a short record makes a 2x overestimate a
        # one-sigma event -- which takes expected log growth to zero. Configuration may
        # demand a longer record; it may not accept a shorter one, so the floor is the
        # documented value itself.
        "min_trades_for_kelly": Decimal("100"),
        # RISK_PHILOSOPHY.md section 2: conviction is consumed through a calibration map
        # fitted on conviction *deciles*, so a floor finer than one decile discriminates
        # on a difference the map that reads it cannot resolve. The shipped default is
        # 0.15; 0.10 is one decile and the point below which the floor stops meaning
        # anything.
        "conviction_floor": Decimal("0.10"),
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
