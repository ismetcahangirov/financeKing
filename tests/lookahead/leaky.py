"""Five features that leak on purpose, one per known leak shape.

**This file lives under `tests/` and never under `src/`.** Nothing here is importable by
the registry, so none of it can reach a strategy; the point is to have a leak the probe
can be *observed* catching. A leak test that has never been seen to fail is not evidence
of anything -- it might be asserting `True == True` (`DATA_PIPELINE.md` section 7).

The five shapes, and why each is on the list rather than a different one:

1. **A full-sample z-score.** The leak that most often survives review, because the slice
   handed to the function *is* bounded by `t` and looks point-in-time -- while inside it,
   every row sees every other row.
2. **A value stamped as available at the instant it happened, when the declaration says
   otherwise.** The one that no amount of future-poisoning reveals: the arithmetic is
   honest and the *claim about when it was knowable* is not, so the store's
   `available_at_utc <= as_of` filter admits it to a decision that could not have seen it.
3. **A right-labelled window.** The value stamped at `t` is computed from the bars that
   follow `t`. Resampling 1m to 5m with `label='right'` produces exactly this, and pandas'
   defaults vary by rule, which is why the posture is to state it and assert it.
4. **A label measured from the decision bar's own close.** Inflates the measured edge by
   precisely the move the feature was computed from.
5. **A settlement rate joined to the interval that begins at its stamp.** The venue stamps
   a funding row with the instant of the settlement it *closes*, so reading it as the rate
   for the interval starting there is a plausible misreading worth one settlement of
   foresight -- which, on a strategy whose whole thesis is the funding rate, is the entire
   edge. It is the first shape that is specific to an observation kind rather than to a
   window, and it exists so the probe's settlement-rate branch has been observed failing.

Adding a shape to `LEAKY_CASES` is how a newly discovered leak class gets permanently
guarded: `test_probe_detects_a_known_leak.py` parametrises over it, so a probe that stops
catching one fails rather than going quiet.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from functools import partial
from typing import Final

from fking.data.features.labels import LabelPoint
from fking.data.features.spec import (
    FeatureCompute,
    FeatureObservation,
    FeaturePoint,
    FeatureSpec,
    FeatureWindow,
    SettlementRateCompute,
    SettlementRateObservation,
)
from tests.lookahead.harness import bars, probe_feature, probe_label, settlements

__all__ = ["LEAKY_CASES", "LeakyCase"]

_HORIZON: Final[timedelta] = timedelta(minutes=30)

# Long enough that every probe has points on both sides of its cut, and irregular enough
# that a leak cannot hide behind a constant series.
_CLOSES: Final[tuple[str, ...]] = (
    "100", "104", "99", "108", "97", "112", "94", "118", "91", "124", "88", "131",
)  # fmt: skip

# Twelve settlements, three of which fill the sixteen-hour window the leaky settlement spec
# declares. Signed and unequal for the same reason the closes are irregular: a repeated rate
# lets a feature that read one settlement ahead report the correct number anyway.
_RATES: Final[tuple[str, ...]] = (
    "0.00031", "-0.00072", "0.00013", "-0.00049", "0.00088", "-0.00021",
    "0.00054", "-0.00095", "0.00007", "-0.00038", "0.00066", "-0.00012",
)  # fmt: skip


# ---------------------------------------------------------------------------
# Leak 1: a full-sample statistic
# ---------------------------------------------------------------------------


def full_sample_zscore(
    observations: Sequence[FeatureObservation], window: FeatureWindow
) -> tuple[FeaturePoint, ...]:
    """LEAKY: normalised against the mean and standard deviation of the whole slice.

    The slice is bounded by `t`, so this passes a reading that only checks "does it use
    data after the cut". It does not: every row inside the slice sees every other row, so
    the value at row 10 depends on row 40,000.
    """
    closes = [float(observation.close_quote_price) for observation in observations]
    mean = statistics.fmean(closes)
    deviation = statistics.pstdev(closes) or 1.0
    return tuple(
        FeaturePoint(
            event_time_utc=observation.event_time_utc,
            available_at_utc=observation.event_time_utc + window.availability_lag,
            feature_value=Decimal(str((closes[index] - mean) / deviation)),
        )
        for index, observation in enumerate(observations)
    )


# ---------------------------------------------------------------------------
# Leak 2: an availability claim the declaration does not support
# ---------------------------------------------------------------------------


def available_at_the_event(
    observations: Sequence[FeatureObservation],
    window: FeatureWindow,  # noqa: ARG001 - ignoring it is precisely the defect
) -> tuple[FeaturePoint, ...]:
    """LEAKY: stamps `available_at_utc = event_time_utc`, ignoring the declared lag.

    Arithmetically honest and completely trailing, which is why the future-poisoning
    clause of the probe cannot see it. The store filters on `available_at_utc`, so every
    one of these values is visible to a decision taken `window.availability_lag` before
    the venue published it -- the storage-layer twin of filtering on `event_time`.
    """
    return tuple(
        FeaturePoint(
            event_time_utc=observation.event_time_utc,
            available_at_utc=observation.event_time_utc,
            feature_value=observation.close_quote_price,
        )
        for observation in observations
    )


# ---------------------------------------------------------------------------
# Leak 3: a right-labelled window
# ---------------------------------------------------------------------------


def right_labelled_return(
    observations: Sequence[FeatureObservation], window: FeatureWindow
) -> tuple[FeaturePoint, ...]:
    """LEAKY: the value stamped at `t` is the return over the bars that follow `t`.

    The shape `resample(...).agg(...)` produces when `label='right'` is left to the
    library's default: the bar carries the timestamp of its own *end*, so a join on that
    timestamp hands a decision the interval that came after it.
    """
    points: list[FeaturePoint] = []
    for index, observation in enumerate(observations):
        deadline = observation.event_time_utc + window.lookback
        ahead: FeatureObservation | None = None
        for candidate in observations[index + 1 :]:
            if candidate.event_time_utc <= deadline:
                ahead = candidate
        if ahead is None:
            continue
        points.append(
            FeaturePoint(
                event_time_utc=observation.event_time_utc,
                available_at_utc=observation.event_time_utc + window.availability_lag,
                feature_value=ahead.close_quote_price / observation.close_quote_price
                - Decimal("1"),
            )
        )
    return tuple(points)


# ---------------------------------------------------------------------------
# Leak 4: a label entered at the decision bar's own close
# ---------------------------------------------------------------------------


def decision_close_entry_label(
    observations: Sequence[FeatureObservation], *, horizon: timedelta
) -> tuple[LabelPoint, ...]:
    """LEAKY: entry is `close[i]`, a price the decision taken on it could not have hit.

    Identical to `forward_return_label` in every other respect, so the diff between them
    is exactly the leak -- which is the property that makes this a useful specimen rather
    than a straw man.
    """
    points: list[LabelPoint] = []
    for index in range(len(observations) - 1):
        decision = observations[index]
        deadline = decision.event_time_utc + horizon
        if observations[index + 1].event_time_utc > deadline:
            continue
        exit_index = index + 1
        for position in range(index + 2, len(observations)):
            if observations[position].event_time_utc > deadline:
                break
            exit_index = position
        points.append(
            LabelPoint(
                decision_time_utc=decision.event_time_utc,
                entry_time_utc=decision.event_time_utc,
                exit_time_utc=observations[exit_index].event_time_utc,
                return_fraction=(
                    observations[exit_index].close_quote_price / decision.close_quote_price
                    - Decimal("1")
                ),
            )
        )
    return tuple(points)


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------


def next_settlements_rate(
    observations: Sequence[SettlementRateObservation], window: FeatureWindow
) -> tuple[FeaturePoint, ...]:
    """Leak 5: a settlement-rate feature that reports the *next* settlement's rate.

    The shape a funding join takes when the settlement stamped at `t` is matched to the
    interval that *begins* at `t` rather than the one that ended there. Binance stamps a
    funding row with the instant of the settlement it closes, so the off-by-one is a
    plausible reading of the column rather than a careless one -- and it is worth exactly
    one settlement of foresight, which on a carry strategy is the entire edge.

    Same lag and same window as the honest version, so the probe's second clause passes and
    only the perturbation clause can fail. A leak that fails both tells you less about
    which clause is doing the work.
    """
    points: list[FeaturePoint] = []
    for index, observation in enumerate(observations):
        if index == 0 or index + 1 >= len(observations):
            continue
        points.append(
            FeaturePoint(
                event_time_utc=observation.event_time_utc,
                available_at_utc=observation.event_time_utc + window.availability_lag,
                feature_value=observations[index + 1].settlement_rate,
            )
        )
    return tuple(points)


def _leaky_settlement_spec(name: str, compute: SettlementRateCompute) -> FeatureSpec:
    """The settlement-rate twin of `_leaky_spec`, declared over `fundingRate`."""
    return FeatureSpec(
        name=name,
        version=1,
        settlement_rate_compute=compute,
        inputs=frozenset({"fundingRate"}),
        lookback=timedelta(hours=16),
        availability_lag=timedelta(minutes=1),
        label_horizon=_HORIZON,
        point_in_time_proof=(
            "DELIBERATELY FALSE. This spec exists under tests/ so the probe can be "
            "observed catching it, and is never registered."
        ),
        uses_trailing_statistics_only=True,
    )


def _leaky_spec(name: str, compute: FeatureCompute, *, availability_lag: timedelta) -> FeatureSpec:
    """A registrable-looking spec around a broken function.

    `uses_trailing_statistics_only=True` is a lie in three of the five cases, and it has to
    be: `FeatureSpec` refuses `False` outright, so the flag is a claim the author makes and
    the probe is what checks it. A leak that could not be declared could not be caught.
    """
    return FeatureSpec(
        name=name,
        version=1,
        compute=compute,
        inputs=frozenset({"klines"}),
        lookback=timedelta(hours=1),
        availability_lag=availability_lag,
        label_horizon=_HORIZON,
        point_in_time_proof=(
            "DELIBERATELY FALSE. This spec exists under tests/ so the probe can be "
            "observed catching it, and is never registered."
        ),
        uses_trailing_statistics_only=True,
    )


@dataclass(frozen=True, slots=True)
class LeakyCase:
    """One deliberate leak, and the probe invocation that must reject it."""

    leak_shape: str
    run_probe: Callable[[], None]


LEAKY_CASES: Final[tuple[LeakyCase, ...]] = (
    LeakyCase(
        leak_shape="full-sample z-score",
        run_probe=partial(
            probe_feature,
            _leaky_spec("full_sample_zscore", full_sample_zscore, availability_lag=timedelta(0)),
            bars(_CLOSES),
        ),
    ),
    LeakyCase(
        leak_shape="available_at stamped at the event, ignoring the declared lag",
        run_probe=partial(
            probe_feature,
            _leaky_spec(
                "available_at_the_event",
                available_at_the_event,
                availability_lag=timedelta(minutes=15),
            ),
            bars(_CLOSES),
        ),
    ),
    LeakyCase(
        leak_shape="right-labelled rolling window",
        run_probe=partial(
            probe_feature,
            _leaky_spec(
                "right_labelled_return", right_labelled_return, availability_lag=timedelta(0)
            ),
            bars(_CLOSES),
        ),
    ),
    LeakyCase(
        leak_shape="label entered at the decision bar's own close",
        run_probe=partial(probe_label, decision_close_entry_label, bars(_CLOSES), horizon=_HORIZON),
    ),
    LeakyCase(
        leak_shape="settlement rate joined to the interval that begins at its stamp",
        run_probe=partial(
            probe_feature,
            _leaky_settlement_spec("next_settlements_rate", next_settlements_rate),
            settlements(_RATES),
        ),
    ),
)
