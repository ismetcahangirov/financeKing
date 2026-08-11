"""The registry is the only route a feature has into the system, and it is locked.

Two properties are asserted here that nothing else can assert:

**Every registered definition still hashes to the digest it was registered with.** A
`compute` edited under an unchanged `version` is a definition change attributed to
results that were produced by the previous one, and no other test in the repository
would notice. The failure message names the version to bump.

**Every registered feature is parametrised, not enumerated.** These tests iterate
`FEATURES`, so a feature added in a later pull request inherits them with no test-file
edit -- which is the same property the adversarial look-ahead probe (#31) relies on.

The compute tests below are behavioural: they assert that a value stamped at `t` does
not move when the future changes. That is a miniature of the probe, and it is here
rather than deferred because a trailing statistic that is not actually trailing is the
defect this whole package exists to make impossible.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from fking.data.features._definition_digests import DEFINITION_DIGESTS
from fking.data.features.registry import (
    FEATURES,
    evaluate,
    evaluate_settlement_rates,
    locked_digest,
    registered,
)
from fking.data.features.spec import (
    FeatureObservation,
    FeaturePoint,
    FeatureSpec,
    SettlementRateObservation,
    definition_digest,
)
from fking.platform.errors import FeatureContractError

pytestmark = pytest.mark.unit

_START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
_BAR_STEP = timedelta(minutes=15)
# Binance's perpetual funding cadence, and the grain every settlement-rate feature's
# lookback is expressed over (`fking.data.alt.registry.BINANCE_FUNDING_RATE`).
_SETTLEMENT_STEP = timedelta(hours=8)


def _series(
    closes: Sequence[str], *, step: timedelta = timedelta(minutes=15)
) -> tuple[FeatureObservation, ...]:
    """A minute-stamped series of closed bars, one observation per `step`.

    Each bar opens where the previous one closed, which is what a continuously traded
    market produces. No feature below reads the open; it is here because the type carries
    it for label alignment (`fking.data.features.labels`).
    """
    return tuple(
        FeatureObservation(
            event_time_utc=_START + step * index,
            open_quote_price=Decimal(closes[index - 1] if index else close),
            close_quote_price=Decimal(close),
        )
        for index, close in enumerate(closes)
    )


def _probe_closes() -> list[str]:
    """A series long enough that every registered feature emits over it.

    Its length is derived from the deepest declared lookback rather than written down, so
    a feature registered later with a longer window does not silently turn the probes
    below into assertions over an empty tuple -- which is the failure mode that leaves the
    look-ahead check passing while checking nothing.

    Alternating up and down by uneven amounts, so no window is degenerate: a constant
    stretch has no dispersion to standardise against and no channel to break out of, and
    both features answer that case by emitting nothing.
    """
    step = timedelta(minutes=15)
    deepest = max(spec.lookback for spec in FEATURES.values())
    observations_needed = deepest // step + 8
    swing = (Decimal("1.004"), Decimal("0.997"), Decimal("1.002"), Decimal("0.999"))
    closes = [Decimal("100")]
    while len(closes) < observations_needed:
        closes.append(closes[-1] * swing[len(closes) % len(swing)])
    return [format(close, "f") for close in closes]


def _settlements(rates: Sequence[str]) -> tuple[SettlementRateObservation, ...]:
    """A funding series on the venue's eight-hourly grid."""
    return tuple(
        SettlementRateObservation(
            event_time_utc=_START + _SETTLEMENT_STEP * index,
            settlement_rate=Decimal(rate),
        )
        for index, rate in enumerate(rates)
    )


def _probe_rates() -> list[str]:
    """Settlements long enough that every registered rate feature emits over them.

    Signed and unequal in magnitude on purpose. A series of one repeated rate would make
    the trailing mean equal to every element of it, so a mean computed over the wrong
    window -- or over the whole sample -- would agree with the correct one and the probe
    would pass while checking nothing. The sign changes matter for the same reason: a
    *signed* mean and a mean of magnitudes coincide on a series that never crosses zero.
    """
    deepest = max(
        spec.lookback for spec in FEATURES.values() if spec.settlement_rate_compute is not None
    )
    settlements_needed = deepest // _SETTLEMENT_STEP + 8
    swing = ("0.0001", "-0.00035", "0.0006", "0.00002")
    return [swing[index % len(swing)] for index in range(settlements_needed)]


def _probe_over(spec: FeatureSpec, *, poisoned_tail: int = 0) -> tuple[FeaturePoint, ...]:
    """Evaluate `spec` over the probe series its declared observation kind consumes.

    Dispatching here rather than parametrising each test twice: the properties below hold
    for every registered feature, and a test that enumerated the bar features by name
    would stop covering the next kind the store learns to hold.
    """
    if spec.settlement_rate_compute is not None:
        rates = _probe_rates()
        if poisoned_tail:
            rates = [*rates[:-poisoned_tail], *(["0.5", "-0.9", "0.75", "-0.4"][:poisoned_tail])]
        return evaluate_settlement_rates(spec, _settlements(rates))
    closes = _probe_closes()
    if poisoned_tail:
        closes = [*closes[:-poisoned_tail], *(["1000", "1", "5000", "2"][:poisoned_tail])]
    return evaluate(spec, _series(closes))


def _probe_step(spec: FeatureSpec) -> timedelta:
    """The grain the probe series for `spec` is stamped on."""
    return _SETTLEMENT_STEP if spec.settlement_rate_compute is not None else _BAR_STEP


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("feature_key", sorted(FEATURES), ids=str)
def test_a_registered_definition_still_matches_its_locked_digest(
    feature_key: tuple[str, int],
) -> None:
    """The one test that makes `version` mean anything.

    If this fails, `compute` changed under a version number that earlier results are
    attributed to. The fix is a new `FeatureSpec` at `version + 1` with its own digest,
    never an edited digest at the same version.
    """
    spec = FEATURES[feature_key]
    assert definition_digest(spec.computation) == locked_digest(spec.name, spec.version), (
        f"{spec.name} v{spec.version} was edited without a version bump; register "
        f"version {spec.version + 1} instead of updating its digest"
    )


def test_every_registered_feature_is_locked() -> None:
    unlocked = sorted(key for key in FEATURES if key not in DEFINITION_DIGESTS)
    assert unlocked == [], f"add these to DEFINITION_DIGESTS: {unlocked}"


def test_asking_for_a_digest_that_was_never_locked_refuses() -> None:
    with pytest.raises(FeatureContractError, match="no entry in DEFINITION_DIGESTS"):
        locked_digest("a_feature_nobody_registered", 1)


def test_every_registered_key_agrees_with_the_spec_it_maps_to() -> None:
    """A mapping built by hand can key a spec under somebody else's name."""
    mismatched = sorted(key for key, spec in FEATURES.items() if key != spec.key())
    assert mismatched == []


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_an_unregistered_feature_refuses_and_says_what_exists() -> None:
    """The refusal redirects rather than dead-ends.

    The common cause is an LLM-authored strategy asking for a feature that exists in the
    literature it was trained on rather than in this corpus, and a bare `KeyError` sends
    somebody looking for a bug (`DATA_PIPELINE.md` section 8).
    """
    with pytest.raises(FeatureContractError, match="registered features are"):
        registered("order_book_imbalance_top_10_levels", 1)


def test_a_registered_name_at_an_unregistered_version_still_refuses() -> None:
    """Name is not identity. Two versions coexist and one of them may not exist yet."""
    with pytest.raises(FeatureContractError, match="version 99"):
        registered("trailing_return_fraction", 99)


# ---------------------------------------------------------------------------
# evaluate: the input boundary
# ---------------------------------------------------------------------------


def test_duplicate_event_times_are_refused_rather_than_deduplicated() -> None:
    """Two observations claiming one instant means two sources disagree.

    Picking one here would record that disagreement as a fact nobody can see afterwards,
    which is the same reason the seam reconciliation refuses to merge a mismatch.
    """
    spec = registered("trailing_return_fraction", 1)
    duplicated = (
        FeatureObservation(
            event_time_utc=_START,
            open_quote_price=Decimal("100"),
            close_quote_price=Decimal("100"),
        ),
        FeatureObservation(
            event_time_utc=_START,
            open_quote_price=Decimal("100"),
            close_quote_price=Decimal("101"),
        ),
    )
    with pytest.raises(FeatureContractError, match="ascend strictly"):
        evaluate(spec, duplicated)


def test_a_descending_series_is_refused() -> None:
    spec = registered("trailing_return_fraction", 1)
    with pytest.raises(FeatureContractError, match="ascend strictly"):
        evaluate(spec, tuple(reversed(_series(["100", "101", "102"]))))


@pytest.mark.parametrize("corrupt_field", ["open_quote_price", "close_quote_price"])
def test_a_non_positive_price_is_refused(corrupt_field: str) -> None:
    """Both prices, not just the one the features happen to read.

    An open of zero is corrupt input whether or not today's feature set consumes it, and a
    label divides by it.
    """
    spec = registered("trailing_return_fraction", 1)
    prices = {"open_quote_price": Decimal("100"), "close_quote_price": Decimal("100")}
    prices[corrupt_field] = Decimal("0")
    corrupt = (FeatureObservation(event_time_utc=_START, **prices),)
    with pytest.raises(FeatureContractError, match=f"{corrupt_field} at .* non-positive price"):
        evaluate(spec, corrupt)


# ---------------------------------------------------------------------------
# The computations themselves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("feature_key", sorted(FEATURES), ids=str)
def test_no_partial_window_is_emitted(feature_key: tuple[str, int]) -> None:
    """A series shorter than the declared lookback produces nothing at all.

    A partial-window value is not a smaller version of the real one: it is a different
    statistic, computed from a sample size that varies with wherever the caller happened
    to start reading, and no live run would ever have had it.
    """
    spec = FEATURES[feature_key]
    if spec.settlement_rate_compute is not None:
        # Two settlements sixteen hours apart sit well inside any declared funding window.
        assert evaluate_settlement_rates(spec, _settlements(["0.0001", "0.0002"])) == ()
        return
    assert evaluate(spec, _series(["100", "101"], step=timedelta(minutes=1))) == ()


@pytest.mark.parametrize("feature_key", sorted(FEATURES), ids=str)
def test_a_value_does_not_move_when_the_future_is_replaced(feature_key: tuple[str, int]) -> None:
    """The miniature of the adversarial probe, run over every registered feature.

    The tail of the series is replaced with values that bear no relationship to the
    original -- tripled and then inverted, so a subtle perturbation cannot be absorbed by
    rounding -- and every point at or before the cut must be identical. Anything that
    moves read the future.
    """
    spec = FEATURES[feature_key]
    poisoned_tail = 4
    baseline = _probe_over(spec)
    perturbed = _probe_over(spec, poisoned_tail=poisoned_tail)
    cut = max(point.event_time_utc for point in baseline) - _probe_step(spec) * poisoned_tail

    before_cut = tuple(point for point in baseline if point.event_time_utc <= cut)
    after_poison = tuple(point for point in perturbed if point.event_time_utc <= cut)
    assert before_cut == after_poison
    assert before_cut, "the comparison matched nothing, so it verified nothing"


@pytest.mark.parametrize("feature_key", sorted(FEATURES), ids=str)
def test_available_at_is_the_event_time_plus_the_declared_lag(feature_key: tuple[str, int]) -> None:
    """The writer cannot claim a value was knowable earlier than the declaration says,
    because it does not supply `available_at_utc` at all -- `compute` derives it."""
    spec = FEATURES[feature_key]
    points = _probe_over(spec)
    assert points
    for point in points:
        assert point.available_at_utc == point.event_time_utc + spec.availability_lag


def test_a_trailing_return_is_measured_against_the_bar_at_the_window_start() -> None:
    """An exact value, so the window's endpoints are pinned rather than described.

    Four 15-minute steps span the declared one-hour lookback, so the point stamped at
    the fifth observation is measured against the first: 121 / 100 - 1.
    """
    spec = registered("trailing_return_fraction", 1)
    points = evaluate(spec, _series(["100", "105", "110", "115", "121"]))
    assert len(points) == 1
    assert points[0].event_time_utc == _START + timedelta(hours=1)
    assert points[0].feature_value == Decimal("0.21")


def test_realised_volatility_of_a_constant_series_is_zero() -> None:
    """Zero, not absent: a flat market is a real observation, and the feature has to be
    able to say so. What it must not do is report zero because the window was short,
    which is what the two-return minimum is for."""
    spec = registered("trailing_realised_volatility", 1)
    points = evaluate(spec, _series(["100", "100", "100", "100", "100"]))
    assert len(points) == 1
    assert points[0].feature_value == Decimal("0")


def test_a_window_holding_one_return_emits_nothing_rather_than_a_zero() -> None:
    """A standard deviation of one observation is undefined, and the substitute is worse.

    Two observations two hours apart put exactly one return inside a one-hour window.
    Reporting zero there would say "this market was perfectly calm" on the thinnest
    possible evidence, and it would say it most often exactly where the data is
    sparsest.
    """
    spec = registered("trailing_realised_volatility", 1)
    sparse = _series(["100", "140"], step=timedelta(hours=2))
    assert evaluate(spec, sparse) == ()


def test_the_channel_state_marks_a_new_high_a_new_low_and_neither() -> None:
    """The three states, pinned on one series rather than described.

    Twenty 15-minute steps span the declared five-hour lookback, so the point stamped at
    the twenty-first observation is the first one with a full window. The tail here walks
    to a new high, then to a new low, then back inside the channel.
    """
    spec = registered("donchian_channel_breakout_state", 1)
    closes = [*(["100"] * 20), "130", "70", "100"]
    points = evaluate(spec, _series(closes))

    assert [point.feature_value for point in points] == [
        Decimal("1"),
        Decimal("-1"),
        Decimal("0"),
    ]


def test_a_channel_with_no_width_is_not_a_breakout_in_both_directions() -> None:
    """A flat window's close is simultaneously the high and the low.

    Reporting a breakout there -- in whichever direction the branch order happened to
    test first -- would manufacture a directional signal out of an absence of movement,
    and it would do it precisely on the illiquid symbols whose closes repeat.
    """
    spec = registered("donchian_channel_breakout_state", 1)
    points = evaluate(spec, _series(["100"] * 22))
    assert points
    assert {point.feature_value for point in points} == {Decimal("0")}


def test_the_z_score_is_the_close_standardised_against_its_own_window() -> None:
    """A window whose moments are computable by hand.

    The window is half-open, so the point stamped at the twenty-first close standardises
    against the twenty closes inside `(t-5h, t]` and not against the observation sitting
    exactly on the boundary: nineteen at 100 and the stamped one at 110. The sample mean
    is 100.5, the sample standard deviation exactly 5**0.5, and the score 9.5/5**0.5.
    The band asserted here fails on a *population* standard deviation, which would put
    the score at 4.3589 -- the substitution that looks like a rounding choice and is a
    different statistic.
    """
    spec = registered("bollinger_z_score", 1)
    points = evaluate(spec, _series([*(["100"] * 20), "110"]))

    assert len(points) == 1
    assert points[0].feature_value > Decimal("4.248")
    assert points[0].feature_value < Decimal("4.249")


def test_a_window_with_no_dispersion_has_no_z_score_at_all() -> None:
    """Zero would assert "exactly at the mean", which is a claim the data cannot support.

    It is also the value that would divide a position size by nothing one layer down:
    `InvalidationRule` scales its distance off a dispersion, and a fabricated zero there
    is an unbounded quantity.
    """
    spec = registered("bollinger_z_score", 1)
    assert evaluate(spec, _series(["100"] * 22)) == ()


def test_the_band_width_is_the_dispersion_relative_to_the_price_level() -> None:
    """Dimensionless, so it can be compared across instruments and used as a distance."""
    spec = registered("bollinger_band_width_fraction", 1)
    points = evaluate(spec, _series([*(["100"] * 20), "110"]))

    assert len(points) == 1
    # 5**0.5 / 100.5, one sample standard deviation of the window's closes over their
    # mean, on the same twenty-close window the z-score test pins.
    assert points[0].feature_value > Decimal("0.02224")
    assert points[0].feature_value < Decimal("0.02226")


def test_a_flat_band_has_zero_width_rather_than_no_value() -> None:
    """Unlike the score, nothing divides by the width here, and "the price did not move"
    is a true statement a consumer can act on -- by refusing to size against it."""
    spec = registered("bollinger_band_width_fraction", 1)
    points = evaluate(spec, _series(["100"] * 22))
    assert points
    assert {point.feature_value for point in points} == {Decimal("0")}


# ---------------------------------------------------------------------------
# The settlement-rate kind
# ---------------------------------------------------------------------------


def test_a_settlement_feature_handed_closed_bars_refuses_rather_than_computing() -> None:
    """The wrong series under the right name is worse than an exception.

    A funding feature computed from closes would emit a number whose declared
    point-in-time proof describes an input it never saw, and the value would carry a
    funding availability lag it did not earn.
    """
    spec = registered("settled_funding_rate", 1)
    with pytest.raises(FeatureContractError, match="declared over settlement rates"):
        evaluate(spec, _series(["100", "101"]))


def test_a_bar_feature_handed_settlement_rates_refuses_rather_than_computing() -> None:
    spec = registered("bollinger_z_score", 1)
    with pytest.raises(FeatureContractError, match="declared over closed bars"):
        evaluate_settlement_rates(spec, _settlements(["0.0001", "0.0002"]))


def test_duplicate_settlement_instants_are_refused() -> None:
    """Two rates claiming one settlement means two sources disagree about what was paid."""
    spec = registered("settled_funding_rate", 1)
    duplicated = (
        SettlementRateObservation(event_time_utc=_START, settlement_rate=Decimal("0.0001")),
        SettlementRateObservation(event_time_utc=_START, settlement_rate=Decimal("-0.0001")),
    )
    with pytest.raises(FeatureContractError, match="ascend strictly"):
        evaluate_settlement_rates(spec, duplicated)


def test_a_negative_settlement_rate_is_a_real_print_and_is_not_refused() -> None:
    """Shorts pay longs whenever the perpetual trades below the index, which is common.

    Asserted explicitly because the bar series *does* refuse a non-positive value, and the
    reflex that copies that guard across would silently drop every inverted-funding regime
    -- which is precisely the regime the carry baseline goes long in.
    """
    spec = registered("settled_funding_rate", 1)
    rates = ["-0.0004"] * (len(_probe_rates()))
    points = evaluate_settlement_rates(spec, _settlements(rates))
    assert points
    assert {point.feature_value for point in points} == {Decimal("-0.0004")}


def test_the_settled_rate_is_republished_one_publication_lag_after_settlement() -> None:
    """The value is the venue's; the instant it becomes readable is this store's claim."""
    spec = registered("settled_funding_rate", 1)
    rates = _probe_rates()
    points = evaluate_settlement_rates(spec, _settlements(rates))
    assert points
    first = points[0]
    # Twenty-one settlements span the declared seven-day lookback, so the earliest point
    # is stamped at the twenty-second, and it carries that settlement's own rate.
    assert first.event_time_utc == _START + _SETTLEMENT_STEP * 21
    assert first.feature_value == Decimal(rates[21])
    assert first.available_at_utc == first.event_time_utc + timedelta(minutes=1)


def test_the_trailing_mean_discards_sign_rather_than_netting_it() -> None:
    """A window that paid and then received exchanged a great deal; its signed mean is nil.

    Twenty-two settlements alternating +4bp and -4bp: the signed mean over any full window
    is within one settlement of zero, and the magnitude mean is exactly 4bp. A
    near-zero distance is the denominator of every position sized off this feature, so the
    difference between the two statistics is an unbounded quantity rather than a nuance.
    """
    spec = registered("trailing_mean_absolute_funding_rate", 1)
    alternating = ["0.0004" if index % 2 == 0 else "-0.0004" for index in range(24)]
    points = evaluate_settlement_rates(spec, _settlements(alternating))
    assert points
    assert {point.feature_value for point in points} == {Decimal("0.0004")}
