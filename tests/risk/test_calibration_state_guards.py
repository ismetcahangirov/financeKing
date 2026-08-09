"""What the calibration map and its row codec refuse, and what the audit row carries.

Every refusal here is a row or an object that would otherwise produce a *plausible* map.
That is the shape of every failure in this module worth defending against: a map missing a
bucket still sizes signals, a map whose steps run backwards still sizes signals, and a map
rebuilt from a hand-edited row during an incident still sizes signals. None of them raises
on its own, and the only symptom is that a strategy is sized differently than its record
justifies -- which looks exactly like the calibration working.

`from_calibration_row` is therefore deliberately intolerant. A row missing a bucket's
`calibrated_fraction` is not a row to fill in with `0.5`: that substitution silently
flattens one strategy's map, and a correctly flattened map looks identical.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest

from fking.risk.calibration import (
    CalibrationBucket,
    CalibrationError,
    CalibrationMap,
    ClosedTrade,
    ConvictionAssessment,
    ConvictionParameters,
    fit_calibration,
    from_calibration_row,
    to_calibration_row,
)
from fking.risk.exposure import Rejection

pytestmark = pytest.mark.unit

_STRATEGY_ID: Final = "trailing-return-v3"
_EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)
_AS_OF: Final = datetime(2027, 1, 1, tzinfo=UTC)
_PARAMETERS: Final = ConvictionParameters()

# Ten distinct convictions whose realised returns rise with them, so the fit produces ten
# buckets with ten distinct calibrated fractions -- a map with something to corrupt.
_TRADE_COUNT: Final = 200


def _fitted() -> CalibrationMap:
    return fit_calibration(
        [
            ClosedTrade(
                strategy_id=_STRATEGY_ID,
                reported_conviction=Decimal(index % 10) / Decimal("10"),
                realised_return_fraction=(Decimal(index % 10) - Decimal("4")) / Decimal("100"),
                closed_at_utc=_EPOCH + timedelta(hours=index),
            )
            for index in range(_TRADE_COUNT)
        ],
        strategy_id=_STRATEGY_ID,
        as_of_utc=_AS_OF,
        parameters=_PARAMETERS,
    )


def _row() -> dict[str, object]:
    return dict(to_calibration_row(_fitted()))


def _buckets(row: Mapping[str, object]) -> list[dict[str, object]]:
    raw = row["buckets"]
    assert isinstance(raw, tuple)
    return [dict(bucket) for bucket in raw if isinstance(bucket, Mapping)]


def test_a_fitted_map_round_trips_through_its_persisted_form() -> None:
    fitted = _fitted()
    assert from_calibration_row(to_calibration_row(fitted)) == fitted


def test_the_audit_payload_renders_every_bucket_number_as_text() -> None:
    """A `Decimal` through a JSON encoder becomes a float, in a table nobody can correct."""
    payload = _fitted().audit_payload()

    assert payload["is_fitted"] is True
    assert payload["observation_count"] == _TRADE_COUNT
    buckets = payload["buckets"]
    assert isinstance(buckets, tuple)
    assert len(buckets) == 10  # noqa: PLR2004 - ten distinct convictions, ten deciles
    assert all(
        isinstance(bucket[name], str)
        for bucket in buckets
        if isinstance(bucket, Mapping)
        for name in ("conviction_upper_bound", "hit_rate_fraction", "calibrated_fraction")
    )


def test_a_row_whose_steps_run_backwards_is_refused() -> None:
    """The one corruption a per-row database CHECK cannot express.

    Monotonicity is a relation between rows, so `ck_..._fractions_are_in_range` accepts an
    inverted map without complaint. The type is the only place it can be caught, and this
    is the edit somebody makes to size a favoured strategy larger.
    """
    row = _row()
    buckets = _buckets(row)
    buckets[0]["calibrated_fraction"] = "1"
    row["buckets"] = tuple(buckets)

    with pytest.raises(CalibrationError, match="non-decreasing"):
        from_calibration_row(row)


def test_a_row_with_a_repeated_bucket_bound_is_refused() -> None:
    """Two buckets claiming the same upper bound is two answers for one conviction."""
    row = _row()
    buckets = _buckets(row)
    buckets[1]["conviction_upper_bound"] = buckets[0]["conviction_upper_bound"]
    row["buckets"] = tuple(buckets)

    with pytest.raises(CalibrationError, match="strictly increasing"):
        from_calibration_row(row)


def test_a_bucket_holding_no_trades_is_refused() -> None:
    """An empty bucket contributes a mean computed from nothing."""
    with pytest.raises(CalibrationError, match="at least one trade"):
        CalibrationBucket(
            conviction_upper_bound=Decimal("0.5"),
            trade_count=0,
            hit_rate_fraction=Decimal("0.5"),
            mean_return_fraction=Decimal("0.01"),
            fitted_return_fraction=Decimal("0.01"),
            calibrated_fraction=Decimal("0.5"),
        )


@pytest.mark.parametrize(
    ("field_name", "corrupt"),
    [
        ("conviction_upper_bound", "1.5"),
        ("hit_rate_fraction", "-0.1"),
        ("calibrated_fraction", "NaN"),
    ],
)
def test_a_bucket_fraction_outside_the_unit_interval_is_refused(
    field_name: str, corrupt: str
) -> None:
    row = _row()
    buckets = _buckets(row)
    buckets[0][field_name] = corrupt
    row["buckets"] = tuple(buckets)

    with pytest.raises(CalibrationError, match="finite fraction"):
        from_calibration_row(row)


def test_a_bucket_return_below_total_loss_is_refused() -> None:
    row = _row()
    buckets = _buckets(row)
    buckets[0]["mean_return_fraction"] = "-2"
    row["buckets"] = tuple(buckets)

    with pytest.raises(CalibrationError, match="at or above -1"):
        from_calibration_row(row)


@pytest.mark.parametrize("field_name", ["conviction_upper_bound", "calibrated_fraction"])
def test_a_bucket_number_arriving_as_a_number_is_refused(field_name: str) -> None:
    """A number here has already been through a float somewhere upstream.

    `Decimal(0.1)` is `0.1000000000000000055511151231257827...`, and the rounding is baked
    in before this line runs. The codec's contract is text on both sides so that there is
    no point at which a JSON parser without a `parse_float` hook can silently intervene.
    """
    row = _row()
    buckets = _buckets(row)
    buckets[0][field_name] = 0.5
    row["buckets"] = tuple(buckets)

    with pytest.raises(CalibrationError, match="already been through a float"):
        from_calibration_row(row)


def test_a_bucket_trade_count_arriving_as_a_boolean_is_refused() -> None:
    """`bool` is an `int` in Python, and `True` would decode as a bucket holding one trade."""
    row = _row()
    buckets = _buckets(row)
    buckets[0]["trade_count"] = True
    row["buckets"] = tuple(buckets)

    with pytest.raises(CalibrationError, match="must be an integer"):
        from_calibration_row(row)


def test_a_bucket_that_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(CalibrationError, match="bucket must be a mapping"):
        from_calibration_row(_row() | {"buckets": ("not-a-bucket",)})


def test_buckets_arriving_as_text_are_refused() -> None:
    """A string is a `Sequence`, so the type check has to exclude it by name."""
    with pytest.raises(CalibrationError, match="buckets must be a sequence"):
        from_calibration_row(_row() | {"buckets": "[]"})


def test_a_row_with_no_strategy_is_refused() -> None:
    with pytest.raises(CalibrationError, match="strategy_id must be text"):
        from_calibration_row(_row() | {"strategy_id": None})


def test_a_blank_strategy_is_refused() -> None:
    with pytest.raises(CalibrationError, match="non-empty text"):
        from_calibration_row(_row() | {"strategy_id": "   "})


def test_a_row_whose_available_at_is_not_an_instant_is_refused() -> None:
    with pytest.raises(CalibrationError, match="ISO-8601"):
        from_calibration_row(_row() | {"available_at_utc": 1_767_225_600})


def test_a_row_whose_available_at_carries_no_offset_is_refused() -> None:
    with pytest.raises(CalibrationError, match="timezone-aware UTC"):
        from_calibration_row(_row() | {"available_at_utc": "2027-01-01T00:00:00"})


def test_a_row_whose_available_at_is_in_another_offset_is_refused() -> None:
    """Rejected rather than converted: a converted instant is silently wrong by an offset."""
    with pytest.raises(CalibrationError, match="timezone-aware UTC"):
        from_calibration_row(_row() | {"available_at_utc": "2027-01-01T00:00:00+02:00"})


def test_a_row_with_a_negative_observation_count_is_refused() -> None:
    with pytest.raises(CalibrationError, match="at or above zero"):
        from_calibration_row(_row() | {"observation_count": -1})


def test_a_map_with_an_unknown_conviction_reads_the_highest_bucket_it_has_earned() -> None:
    """No extrapolation past the end of the record.

    A strategy that has never reported above 0.9 and now reports 1.0 has produced no
    evidence about 1.0. Reading the top bucket is the honest answer; extending the last
    step's slope would pay it for a claim nothing supports.
    """
    fitted = _fitted()

    assert fitted.buckets[-1].conviction_upper_bound == Decimal("0.9")
    assert fitted.calibrated(Decimal("1")) == fitted.buckets[-1].calibrated_fraction


def test_a_conviction_outside_the_unit_interval_is_refused_at_lookup() -> None:
    """The map is defined on `[0, 1]` and says so, rather than clamping quietly."""
    with pytest.raises(CalibrationError, match="finite fraction"):
        _fitted().calibrated(Decimal("1.5"))


def test_a_realised_return_carrying_more_precision_than_the_column_holds_is_refused() -> None:
    """The same guard as on conviction, on the field that produces the bucket means.

    A return quantized on the way into the database changes the mean it contributed to,
    which changes where the isotonic step pooled -- so the restored map differs from the
    one that sized live decisions, and only a restart makes that observable.
    """
    with pytest.raises(CalibrationError, match="18 decimal places"):
        ClosedTrade(
            strategy_id=_STRATEGY_ID,
            reported_conviction=Decimal("0.5"),
            realised_return_fraction=Decimal("1") / Decimal("3"),
            closed_at_utc=_EPOCH,
        )


@pytest.mark.parametrize(
    ("risk_fraction_used", "rejection"),
    [
        (Decimal("0.005"), Rejection("refused", "conviction_floor", _AS_OF)),
        (None, None),
    ],
    ids=["both", "neither"],
)
def test_an_assessment_carrying_both_or_neither_verdict_is_refused(
    risk_fraction_used: Decimal | None, rejection: Rejection | None
) -> None:
    """A refusal and a risk fraction in one object is a decision nobody can act on.

    Both halves matter. An assessment with both would let a caller that reads the fraction
    and a caller that reads the rejection take opposite actions from the same object; one
    with neither says a decision was reached and declines to say what it was.
    """
    with pytest.raises(CalibrationError, match="either a rejection or a risk fraction"):
        ConvictionAssessment(
            strategy_id=_STRATEGY_ID,
            reported_conviction=Decimal("0.8"),
            calibrated_conviction=Decimal("0.5"),
            risk_fraction_used=risk_fraction_used,
            conviction_floor=Decimal("0.15"),
            calibration_observation_count=0,
            calibration_available_at_utc=_AS_OF,
            is_calibrated=False,
            decided_at_utc=_AS_OF,
            rejection=rejection,
        )
