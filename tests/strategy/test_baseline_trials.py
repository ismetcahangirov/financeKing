"""No shipped strategy has ever been searched, and the ledger is asked rather than assumed.

The baselines are a control group. Their value is entirely in having been fixed a priori:
`SURVIVAL_PROTOCOL.md` section 10 only lets an evolved strategy's Sharpe be read against a
strategy whose parameters were chosen before any of this data was seen. Tuning one destroys
that twice -- it stops being a fixed reference, *and* the trials land in the global counter
on behalf of something nobody intends to promote, so every future deflated Sharpe pays for
a search that was never meant to produce a strategy.

"Nobody tuned it" is not a checkable claim about intent, so this checks the artefact the
tuning would have left behind. Each strategy's declared defaults are expressed as the
single-point search they are, and the ledger is asked what has been charged against that
`spec_hash`. A sweep would have registered a grid with more than one candidate per
parameter and a different hash, so this is not a complete proof that no search ever ran --
what it does prove is that the configuration actually shipped carries no charge, which is
the state the deflation arithmetic downstream assumes.

Against the real ledger, not a double. A mocked ledger returning zero would assert that
the mock returns zero.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.evolution import SearchContext, TrialLedger, TrialSpecification
from fking.strategy import SHIPPED_STRATEGIES, Strategy, StrategyBuilder
from tests.strategy.harness import BTCUSDT

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# A fixed instant and a fixed correlation id: the spec hash has to be reproducible from
# this file alone, and a clock read would make it a function of when the test ran.
_WINDOW_START_UTC = datetime(2024, 1, 1, tzinfo=UTC)
_WINDOW_END_UTC = datetime(2026, 1, 1, tzinfo=UTC)
_CORRELATION_ID = UUID("8f7d3d0e-6a1c-4c58-9a52-1f2b6c0d4e77")


def _strategy_id(build: StrategyBuilder) -> str:
    return str(getattr(build, "__name__", build))


def _single_point_search(strategy: Strategy) -> TrialSpecification:
    """The search this strategy's shipped defaults would be, if anybody registered one.

    One candidate per declared parameter, spelled as the exact decimal text the strategy
    binds. `TrialSpecification` takes candidates as strings for the same reason: a `0.1`
    arriving as a float has already lost the exactness the digest preserves, and two
    callers writing the same grid would produce two hashes.
    """
    spec = strategy.spec
    bound = spec.parameters.bind(None)
    return TrialSpecification(
        correlation_id=_CORRELATION_ID,
        statement=f"the shipped defaults of {spec.describe()}, as a one-point grid",
        registered_by="tests.strategy.test_baseline_trials",
        parameter_grid={
            name: (format(value, "f") if isinstance(value, Decimal) else str(value),)
            for name, value in bound.items()
        },
        search_context=SearchContext(
            symbol_universe=tuple(instrument.symbol for instrument in spec.instruments),
            window_start_utc=_WINDOW_START_UTC,
            window_end_utc=_WINDOW_END_UTC,
            feature_ids=frozenset(requirement.describe() for requirement in spec.required_features),
        ),
        lineage_id=spec.strategy_id,
    )


@pytest_asyncio.fixture
async def ledger(app_engine: AsyncEngine) -> TrialLedger:
    """The ledger as `fking_app` -- INSERT and SELECT, and nothing else, ever."""
    return TrialLedger(app_engine)


@pytest.mark.asyncio
@pytest.mark.parametrize("build", SHIPPED_STRATEGIES, ids=_strategy_id)
async def test_no_shipped_strategy_carries_a_trial_charge(
    build: StrategyBuilder, ledger: TrialLedger
) -> None:
    strategy = build((BTCUSDT,))
    specification = _single_point_search(strategy)

    registration = await ledger.registration_for(specification.spec_hash)

    assert registration.trials_charged == 0, (
        f"{strategy.spec.describe()} has {registration.trials_charged} trials charged "
        f"against its shipped configuration; a searched control is not a control, and the "
        f"charge is already in every deflated Sharpe this project will report"
    )
    assert not registration.is_registered


@pytest.mark.parametrize("build", SHIPPED_STRATEGIES, ids=_strategy_id)
def test_a_shipped_strategy_declares_exactly_one_point(build: StrategyBuilder) -> None:
    """The grid a baseline represents multiplies out to one point per symbol.

    Without this, the clause above could pass while a strategy shipped with a parameter
    the evolution engine was already ranging over -- the charge would sit under a
    different `spec_hash` and the ledger would answer zero, correctly and uselessly.
    """
    strategy = build((BTCUSDT,))
    specification = _single_point_search(strategy)

    assert specification.declared_grid_point_count == len(strategy.spec.instruments)
