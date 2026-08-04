"""`as_of` cannot be forgotten, defaulted, or slipped in positionally.

`mypy --strict` is the primary check -- `load()` without `as_of` does not type-check --
but a type error is only a gate while somebody runs the gate. These assertions are about
the shape of the signature itself, so a later edit that adds `as_of: datetime | None =
None` "for convenience" fails a test rather than passing review.

The value somebody would default it to is `now()`, and `now()` is the leak.
"""

from __future__ import annotations

import dataclasses
import inspect
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from fking.data.features.spec import FeatureRef
from fking.data.features.store import (
    FeatureSeries,
    FeatureStore,
    FeatureValue,
    FeatureValueWriter,
    PostgresFeatureStore,
)
from fking.data.format_resolver import Market
from fking.platform.errors import FeatureContractError

pytestmark = pytest.mark.unit

_REF = FeatureRef(name="trailing_return_fraction", version=1, market=Market.SPOT, symbol="BTCUSDT")


def _store() -> PostgresFeatureStore:
    """A store over an engine that is never connected.

    Every refusal asserted here happens before a connection is acquired, which is the
    point: a bad `as_of` is rejected by the caller's own process rather than by a
    database round trip that might succeed against the wrong instant.
    """
    return PostgresFeatureStore(create_async_engine("postgresql+asyncpg://unused/unused"))


@pytest.mark.parametrize("subject", [FeatureStore.load, PostgresFeatureStore.load])
def test_as_of_is_keyword_only_and_has_no_default(subject: object) -> None:
    parameter = inspect.signature(subject).parameters["as_of"]  # type: ignore[arg-type]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


@pytest.mark.parametrize("subject", [FeatureStore.load, PostgresFeatureStore.load])
def test_no_read_parameter_carries_a_default(subject: object) -> None:
    """Not just `as_of`. A defaulted `lookback` is the same failure one field over: it
    would silently decide how much history a decision was allowed to see."""
    defaulted = [
        name
        for name, parameter in inspect.signature(subject).parameters.items()  # type: ignore[arg-type]
        if parameter.default is not inspect.Parameter.empty
    ]
    assert defaulted == []


def test_the_concrete_store_satisfies_the_protocol() -> None:
    """Structural, so the assertion is that mypy accepts the assignment below."""
    store: FeatureStore = _store()
    assert isinstance(store, PostgresFeatureStore)


def test_what_comes_back_carries_no_available_at() -> None:
    """A caller cannot re-derive "what would this look like without the as-of bound".

    The reader function does not return `available_at_utc` and these types have no field
    for it, so the filtering is not a decision anybody downstream gets to revisit.
    """
    assert {field.name for field in dataclasses.fields(FeatureValue)} == {
        "event_time_utc",
        "feature_value",
    }
    assert "available_at_utc" not in {field.name for field in dataclasses.fields(FeatureSeries)}


@pytest.mark.asyncio
async def test_a_naive_as_of_is_refused_rather_than_localised() -> None:
    """Rejected, never converted.

    `astimezone(UTC)` would launder an offset that was guessed wrong upstream into a
    confident value, and crypto has no session boundary to make the resulting shift
    visible -- the backtest would just be quietly better.
    """
    with pytest.raises(FeatureContractError, match="must be timezone-aware"):
        await _store().load(
            _REF,
            as_of=datetime(2026, 3, 1, 12, 0),
            lookback=timedelta(hours=1),
        )


@pytest.mark.asyncio
async def test_an_aware_but_non_utc_as_of_is_refused() -> None:
    """An aware datetime in `Europe/Baku` compares fine, sorts fine, and is four hours
    wrong in every bar alignment. Converting it here would launder the wrong guess."""
    baku = timezone(timedelta(hours=4))
    with pytest.raises(FeatureContractError, match="must be UTC"):
        await _store().load(
            _REF,
            as_of=datetime(2026, 3, 1, 16, 0, tzinfo=baku),
            lookback=timedelta(hours=1),
        )


@pytest.mark.asyncio
async def test_a_zero_lookback_is_refused() -> None:
    """Zero returns nothing, and nothing reads as "this feature has no history" rather
    than as "you asked for a window of no width"."""
    with pytest.raises(FeatureContractError, match="lookback must be positive"):
        await _store().load(
            _REF, as_of=datetime(2026, 3, 1, 12, 0, tzinfo=UTC), lookback=timedelta(0)
        )


@pytest.mark.asyncio
async def test_appending_nothing_opens_no_transaction() -> None:
    """An empty recomputation is a no-op, not an empty transaction against a database
    this fixture is not even connected to -- which is what proves it short-circuits."""
    writer = FeatureValueWriter(create_async_engine("postgresql+asyncpg://unused/unused"))
    written = await writer.append(_REF, ())
    assert written == 0
