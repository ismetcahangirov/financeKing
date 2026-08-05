"""The configurable risk limits, every one of them bounded in a compiled-in direction.

One field per bound and one bound per field. `HARD_CEILINGS` names the limits where
larger is riskier, `HARD_FLOORS` the limits where smaller is riskier, and this model
carries exactly the union of the two plus the gates that are not numbers at all.

**What this model deliberately does not validate.** There are no cross-field rules here
and no sign checks, and both omissions are load-bearing rather than oversights:

- A limit at zero, or below it, halts trading. That is the most conservative
  configuration available, and `RISK_PHILOSOPHY.md` section 9 property 1 says
  configuration may always make this system more conservative. Refusing it would mean
  refusing safety.
- Every rejection this model can produce is therefore a bound violation or a disabled
  gate, and nothing else. That is what lets `tests/risk/test_limits_property.py` assert
  the biconditional -- accepted if and only if within every ceiling and above every
  floor -- rather than a one-directional implication with a list of exceptions. A model
  that also refused, say, a daily drawdown above its total would break that property,
  and the property is the actual guarantee (`RISK_PHILOSOPHY.md` section 9). Those
  cross-field relations are checked where the configuration tree is parsed, on
  `fking.platform.config.RiskSettings`, which is where a boot-time reader looks for
  them.

The type is frozen: a risk limit that another module can mutate after construction is a
limit whose value depends on evaluation order, and the risk decision it produced can no
longer be reconstructed from the audit log.

`RISK_PHILOSOPHY.md` section 9, `CONFIGURATION.md` section 8.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Literal

from fking.risk.ceilings import (
    HARD_CEILINGS,
    HARD_FLOORS,
    assert_above_floors,
    assert_within_ceilings,
)

__all__ = ["GATE_FIELDS", "RiskLimits"]

# The gates that are not numbers. Typed `Literal[True]` on the model so `mypy --strict`
# refuses `False` outright, and re-checked at runtime because a value arriving from an
# environment variable or a JSON payload carries no static type at all.
GATE_FIELDS: Final[tuple[str, ...]] = ("kill_switch_enabled", "require_invalidation_level")


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Risk limits as configured, refused at construction if any bound is crossed.

    Defaults are the safe baseline, not a convenient one: a process constructed with no
    arguments runs at the tightest limits this system ships, because a missing
    configuration file must never produce a less safe system than a present one.
    """

    # Bounded above by HARD_CEILINGS. Defaults mirror the configuration tree's own
    # RiskSettings defaults, which are themselves well inside every ceiling.
    max_portfolio_notional_usd: Decimal = Decimal("25000")
    max_position_notional_usd: Decimal = Decimal("5000")
    max_leverage: Decimal = Decimal("2")
    max_daily_drawdown_ratio: Decimal = Decimal("0.03")
    max_total_drawdown_ratio: Decimal = Decimal("0.10")
    max_open_positions: int = 5
    max_orders_per_minute: int = 10
    max_single_order_notional_usd: Decimal = Decimal("2000")
    max_correlated_exposure_ratio: Decimal = Decimal("0.25")

    # Bounded below by HARD_FLOORS. Sources for each default are on the floor beside it
    # in `fking.risk.ceilings`; all three are above their floor with room to tighten.
    min_free_margin_ratio: Decimal = Decimal("0.40")
    min_trades_for_kelly: int = 100
    conviction_floor: Decimal = Decimal("0.15")

    # CLAUDE.md section 11 names the anti-pattern directly: adding a config flag to
    # bypass a gate. Gates exist because someone will be in a hurry later, and that
    # someone is you. Kept visible in the model rather than omitted, so its value is
    # auditable in the boot log while being unchangeable.
    kill_switch_enabled: Literal[True] = True
    require_invalidation_level: Literal[True] = True

    def __post_init__(self) -> None:
        self._assert_gates_are_engaged()
        submitted = self.bounded_values()
        assert_within_ceilings(submitted, HARD_CEILINGS, scope="risk")
        assert_above_floors(submitted, HARD_FLOORS, scope="risk")

    def bounded_values(self) -> Mapping[str, Decimal]:
        """Every bounded limit as a `Decimal`, keyed by field name.

        The two integer-valued limits are widened here rather than compared as ints, so
        that one comparison implementation serves both and there is no second code path
        whose direction could be written backwards.
        """
        names = (*HARD_CEILINGS, *HARD_FLOORS)
        return MappingProxyType({name: Decimal(str(getattr(self, name))) for name in names})

    def _assert_gates_are_engaged(self) -> None:
        """Refuse a disabled gate at runtime as well as at type-check time.

        The values are read through an `object`-typed mapping on purpose. Reading the
        `Literal[True]` attributes directly would let mypy prove the comparison can
        never be false and report the refusal below as unreachable code -- which is true
        of every *typed* caller and false of the untyped ones this check exists for.
        """
        states: Mapping[str, object] = {name: getattr(self, name) for name in GATE_FIELDS}
        disabled = sorted(name for name, state in states.items() if state is not True)
        if disabled:
            raise ValueError(
                f"risk gates {disabled} are disabled; a gate is not configurable, "
                f"because gates exist for the moment somebody is in a hurry"
            )
