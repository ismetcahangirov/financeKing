"""A feature that does not declare its timing cannot be constructed.

The failures asserted here are the ones that produce a *quiet* wrong answer rather than
a crash. An understated lookback shortens the walk-forward embargo, lets adjacent folds
share information, and makes cross-validation report a stable edge that is partly the
same data seen twice -- and nothing about it looks wrong. A blank
`point_in_time_proof` is the declaration nobody could state, which is the strongest
available signal that the feature leaks.

`FeatureSpec` is keyword-only and fully required, so omission is a `TypeError` naming
the field rather than a default that silently declares no lag. Both halves are asserted:
the ones Python refuses, and the ones only the validator can catch.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from fking.data.features.spec import (
    FeatureObservation,
    FeaturePoint,
    FeatureSpec,
    FeatureWindow,
    definition_digest,
)
from fking.platform.errors import FeatureContractError

pytestmark = pytest.mark.unit


def _noop(
    observations: Sequence[FeatureObservation], window: FeatureWindow
) -> tuple[FeaturePoint, ...]:
    """A compute that emits nothing, used where the subject under test is the spec."""
    del observations, window
    return ()


def _valid_fields() -> dict[str, object]:
    return {
        "name": "example",
        "version": 1,
        "compute": _noop,
        "inputs": frozenset({"klines"}),
        "lookback": timedelta(hours=1),
        "availability_lag": timedelta(0),
        "label_horizon": timedelta(hours=1),
        "point_in_time_proof": "Half-open (t-1h, t] over closed bars only.",
        "uses_trailing_statistics_only": True,
    }


@pytest.mark.parametrize(
    "omitted", ["lookback", "availability_lag", "point_in_time_proof", "label_horizon"]
)
def test_omitting_a_timing_declaration_names_the_missing_field(omitted: str) -> None:
    """No defaults, so Python itself refuses and says which field is absent.

    The alternative -- a default of `timedelta(0)` -- would be the permissive answer,
    and it would be chosen by silence rather than by anybody deciding it.
    """
    fields = _valid_fields()
    del fields[omitted]
    with pytest.raises(TypeError, match=omitted):
        FeatureSpec(**fields)  # type: ignore[arg-type]  # a factory over a closed field set


@pytest.mark.parametrize(
    ("field_name", "bad_declaration", "expected"),
    [
        ("lookback", timedelta(0), "lookback must be positive"),
        ("lookback", timedelta(hours=-1), "lookback must be positive"),
        ("label_horizon", timedelta(0), "label_horizon must be positive"),
        ("availability_lag", timedelta(seconds=-1), "availability_lag must not be negative"),
        ("version", 0, "version must be at least 1"),
        ("point_in_time_proof", "   ", "must not be blank"),
        ("point_in_time_proof", "trailing", "states nothing checkable"),
        ("name", "", "must not be blank"),
        ("inputs", frozenset(), "declares no inputs"),
        ("uses_trailing_statistics_only", False, "cannot be point-in-time"),
    ],
)
def test_an_undeclarable_value_is_refused_at_construction(
    field_name: str, bad_declaration: object, expected: str
) -> None:
    fields = _valid_fields()
    fields[field_name] = bad_declaration
    with pytest.raises(FeatureContractError, match=expected):
        FeatureSpec(**fields)  # type: ignore[arg-type]  # the wrong declaration is the test


def test_a_negative_availability_lag_is_refused_because_of_what_it_claims() -> None:
    """The message is the test.

    A negative lag claims the value was knowable before it happened, which is the
    inverse of the one invariant the whole store rests on -- and the database's
    `available_at_utc >= event_time_utc` CHECK would reject the rows it produced anyway,
    far downstream of the declaration that caused it.
    """
    fields = _valid_fields()
    fields["availability_lag"] = timedelta(minutes=-5)
    with pytest.raises(FeatureContractError, match="knowable before it happened"):
        FeatureSpec(**fields)  # type: ignore[arg-type]  # the wrong declaration is the test


def test_a_spec_is_frozen() -> None:
    spec = FeatureSpec(**_valid_fields())  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.lookback = timedelta(days=1)  # type: ignore[misc]  # the point of the test


def test_the_window_handed_to_compute_is_the_window_that_was_declared() -> None:
    """The declaration and the computation cannot drift apart.

    A window baked into the function as a constant can differ from the number the spec
    declares, and the difference is invisible to every test that only exercises the
    function -- while the embargo derived from the declaration is wrong.
    """
    spec = FeatureSpec(**_valid_fields())  # type: ignore[arg-type]
    assert spec.window() == FeatureWindow(
        lookback=spec.lookback, availability_lag=spec.availability_lag
    )


# ---------------------------------------------------------------------------
# definition_digest
# ---------------------------------------------------------------------------


def _original(
    observations: Sequence[FeatureObservation], window: FeatureWindow
) -> tuple[FeaturePoint, ...]:
    del window
    return tuple(
        FeaturePoint(
            event_time_utc=observation.event_time_utc,
            available_at_utc=observation.event_time_utc,
            feature_value=observation.close_quote_price * Decimal("2"),
        )
        for observation in observations
    )


def _reformatted(
    observations: Sequence[FeatureObservation],
    window: FeatureWindow,
) -> tuple[FeaturePoint, ...]:
    del window
    # A comment the original does not carry, and a different line layout.
    return tuple(
        FeaturePoint(
            event_time_utc=observation.event_time_utc,
            available_at_utc=observation.event_time_utc,
            feature_value=observation.close_quote_price * Decimal("2"),
        )
        for observation in observations
    )


def _changed_constant(
    observations: Sequence[FeatureObservation], window: FeatureWindow
) -> tuple[FeaturePoint, ...]:
    del window
    return tuple(
        FeaturePoint(
            event_time_utc=observation.event_time_utc,
            available_at_utc=observation.event_time_utc,
            feature_value=observation.close_quote_price * Decimal("3"),
        )
        for observation in observations
    )


def test_reformatting_a_definition_does_not_read_as_a_change() -> None:
    """The digest is over the parsed syntax, not the source text.

    A digest that moves on whitespace or a comment is a digest people learn to update
    without looking at what moved, which is the failure the lock file exists to prevent.
    """
    assert definition_digest(_original) == definition_digest(_reformatted)


def test_changing_one_constant_changes_the_digest() -> None:
    assert definition_digest(_original) != definition_digest(_changed_constant)


def test_the_digest_is_stable_across_calls() -> None:
    assert definition_digest(_original) == definition_digest(_original)


def test_an_observation_carries_the_instant_the_value_became_true() -> None:
    """A `FeatureObservation` has no field that could hold an open bar's close.

    The single most common look-ahead defect is using the close of the bar a decision is
    made inside. There is no `open_time_utc` here, so the shape that produces it cannot
    be constructed.
    """
    field_names = {field.name for field in dataclasses.fields(FeatureObservation)}
    assert field_names == {"event_time_utc", "close_quote_price"}
    observation = FeatureObservation(
        event_time_utc=datetime(2026, 1, 1, tzinfo=UTC), close_quote_price=Decimal("1")
    )
    assert observation.event_time_utc.tzinfo is UTC


def test_a_lambda_cannot_be_registered_as_a_definition() -> None:
    """A definition has to be diffable against a version number.

    A lambda, a `functools.partial` or a callable object has no source a reviewer can
    read next to the version it is registered under, so the digest refuses rather than
    hashing whatever line the expression happened to sit on.
    """
    with pytest.raises(FeatureContractError, match="must be a plain function"):
        definition_digest(lambda _observations, _window: ())
