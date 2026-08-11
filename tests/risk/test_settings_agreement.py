"""`RiskSettings` and `RiskLimits` describe the same limits and must not diverge.

Two models of one limit is the actual finding behind issue #171. `fking.risk.limits`
carries every bound in both directions; `fking.platform.config.RiskSettings` carried
only the ceilings, and the field constraint it did carry for `conviction_floor` was
`ge=0` against a compiled-in floor of 0.10 -- so the permissive model was the one an
operator edits.

Divergence is silent by nature: nothing crashes, nothing logs, and the two agree on
every value anybody happens to test. So it is asserted structurally here, over the field
names, the shipped defaults, and acceptance itself. This file lives under `tests/risk`
rather than `tests/platform` because it is the only place allowed to look at both --
`risk` may import `platform`, never the reverse.

CONFIGURATION.md section 8, docs/rules/module-boundaries.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.platform.config import RiskSettings
from fking.risk.ceilings import HARD_CEILINGS, HARD_FLOORS
from fking.risk.limits import RiskLimits

pytestmark = [pytest.mark.property, pytest.mark.unit]

BOUNDED_NAMES: Final[tuple[str, ...]] = tuple(sorted((*HARD_CEILINGS, *HARD_FLOORS)))

INTEGER_FIELDS: Final[frozenset[str]] = frozenset(
    name for name in BOUNDED_NAMES if RiskSettings.model_fields[name].annotation is int
)

# Every bounded limit at or inside its bound, with the two cross-field relations
# `RiskSettings` additionally enforces satisfied by pinning the outer limit of each
# pair. A draw refused for a cross-field reason would say nothing about agreement.
_CONFORMING: Final[st.SearchStrategy[dict[str, Decimal]]] = st.fixed_dictionaries(
    {
        **{
            name: st.decimals(
                min_value=Decimal("1") if name in INTEGER_FIELDS else Decimal("0.01"),
                max_value=ceiling.bound,
                places=2,
                allow_nan=False,
                allow_infinity=False,
            )
            for name, ceiling in HARD_CEILINGS.items()
        },
        **{
            name: st.decimals(
                min_value=floor.bound,
                max_value=floor.bound * 2,
                places=2,
                allow_nan=False,
                allow_infinity=False,
            )
            for name, floor in HARD_FLOORS.items()
        },
        "max_position_notional_usd": st.just(HARD_CEILINGS["max_position_notional_usd"].bound),
        "max_total_drawdown_ratio": st.just(HARD_CEILINGS["max_total_drawdown_ratio"].bound),
    }
)


def _settings(drawn: Mapping[str, Decimal]) -> RiskSettings:
    payload: dict[str, object] = {
        name: int(value) if name in INTEGER_FIELDS else value for name, value in drawn.items()
    }
    return RiskSettings.model_validate(payload)


def _limits(settings: RiskSettings) -> RiskLimits:
    """The risk-side model built from the configuration tree's values.

    Written out field by field rather than splatted: a bounded field added to one model
    and not the other is then a type error here, which is the divergence this file
    exists to catch.
    """
    return RiskLimits(
        max_portfolio_notional_usd=settings.max_portfolio_notional_usd,
        max_position_notional_usd=settings.max_position_notional_usd,
        max_leverage=settings.max_leverage,
        max_daily_drawdown_ratio=settings.max_daily_drawdown_ratio,
        max_total_drawdown_ratio=settings.max_total_drawdown_ratio,
        max_open_positions=settings.max_open_positions,
        max_orders_per_minute=settings.max_orders_per_minute,
        max_single_order_notional_usd=settings.max_single_order_notional_usd,
        max_correlated_exposure_ratio=settings.max_correlated_exposure_ratio,
        min_free_margin_ratio=settings.min_free_margin_ratio,
        min_trades_for_kelly=settings.min_trades_for_kelly,
        conviction_floor=settings.conviction_floor,
    )


def test_the_configuration_tree_carries_every_bounded_limit() -> None:
    """A bound with no configuration field cannot be tightened by an operator and is not
    checked when the process boots -- which is how two of the three floors sat unenforced
    until issue #171."""
    missing = [name for name in BOUNDED_NAMES if name not in RiskSettings.model_fields]
    assert missing == []


@pytest.mark.parametrize("name", BOUNDED_NAMES)
def test_the_two_models_ship_the_same_default(name: str) -> None:
    """Same limit, same shipped value. A default that differs between the two means the
    limit in force depends on which object the caller happened to be handed."""
    assert Decimal(str(getattr(RiskSettings(), name))) == Decimal(str(getattr(RiskLimits(), name)))


@given(drawn=_CONFORMING)
def test_a_configuration_the_tree_accepts_is_one_the_risk_model_accepts(
    drawn: Mapping[str, Decimal],
) -> None:
    """The dangerous direction, asserted over arbitrary configurations.

    If the configuration tree could accept something `RiskLimits` refuses, the process
    would boot reporting a valid configuration and then fail -- or worse, not fail --
    when the risk engine constructed its own view of the same numbers.
    """
    settings = _settings(drawn)
    assert _limits(settings).bounded_values() == {
        name: Decimal(str(getattr(settings, name))) for name in BOUNDED_NAMES
    }
