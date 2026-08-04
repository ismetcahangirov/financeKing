"""Every registered feature, poisoned from the future, must not have moved in the past.

Parametrised over `FEATURES`, so a feature added in a later pull request inherits this
with no edit here. That is the property the registry exists for: a computation that lives
next to the code consuming it is never declared, never states a lookback, and is never
probed (`fking.data.features.registry`). `tools/checks/feature_registry.py` closes the
remaining route by failing the build on a compute function the registry does not carry.

The companion file, `test_probe_detects_a_known_leak.py`, is the one that makes this file
mean anything. A probe that has never been observed to fail might be asserting
`True == True`.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from fking.data.features.labels import forward_return_label
from fking.data.features.registry import FEATURES, evaluate
from fking.data.features.spec import FeaturePoint
from tests.lookahead.harness import (
    bars,
    canonical_digest,
    poison_after,
    poison_one_close,
    probe_feature,
    probe_label,
)

pytestmark = pytest.mark.unit

# Twelve 15-minute bars: three hours, so a one-hour lookback leaves points on both sides
# of the probe's cut. Irregular on purpose -- a monotone series lets a leak that reads one
# bar ahead produce a plausible value, and a constant one lets it produce an identical one.
_CLOSES = (
    "100", "104", "99", "108", "97", "112", "94", "118", "91", "124", "88", "131",
)  # fmt: skip
_HORIZON = timedelta(minutes=30)


@pytest.mark.parametrize("feature_key", sorted(FEATURES), ids=str)
def test_no_registered_feature_reads_the_future(feature_key: tuple[str, int]) -> None:
    """The probe itself: replace everything after the cut, require the past byte-identical.

    Also asserts the availability claim, because the two failures are disjoint. A value
    can be perfectly trailing and still be stamped as knowable before the venue published
    it, and the store filters on that stamp.
    """
    probe_feature(FEATURES[feature_key], bars(_CLOSES))


def test_the_label_is_entered_at_a_price_the_decision_could_have_transacted_at() -> None:
    """Perturb the decision bar's own close; the label for that bar must not move."""
    probe_label(forward_return_label, bars(_CLOSES), horizon=_HORIZON)


def test_the_label_enters_on_the_bar_after_the_decision() -> None:
    """The alignment, asserted directly as well as through the perturbation.

    Two statements of one rule, deliberately: the perturbation proves the label does not
    *depend* on `close[i]`, and this proves it entered where it claims to have. A label
    could satisfy the first by accident on a series where the numbers happen to agree.
    """
    series = bars(_CLOSES)
    labels = forward_return_label(series, horizon=_HORIZON)
    assert labels
    stamps = {observation.event_time_utc for observation in series}
    for label in labels:
        assert label.entry_time_utc > label.decision_time_utc
        assert label.exit_time_utc >= label.entry_time_utc
        assert label.entry_time_utc in stamps


# ---------------------------------------------------------------------------
# The comparison is only worth what its sensitivity is worth
# ---------------------------------------------------------------------------


def test_the_digest_rejects_a_one_femto_perturbation() -> None:
    """`1e-15` is a failure, not a rounding difference.

    A leak that only moves the fifteenth digit today moves the third digit on a different
    fold, and a comparison that tolerates the first will pass the second.
    """
    original = evaluate(FEATURES["trailing_return_fraction", 1], bars(_CLOSES))
    assert original
    nudged = (
        FeaturePoint(
            event_time_utc=original[0].event_time_utc,
            available_at_utc=original[0].available_at_utc,
            feature_value=original[0].feature_value + Decimal("1e-15"),
        ),
        *original[1:],
    )
    assert canonical_digest(original) != canonical_digest(nudged)


def test_the_digest_distinguishes_values_that_compare_equal() -> None:
    """`Decimal("0.1") == Decimal("0.10")` is `True`, and they are not the same value here.

    A rescaling upstream that changed a quantum without changing a quantity is a change to
    what was stored, and the probe's job is to notice changes rather than to agree with
    `__eq__`.
    """
    stamped = evaluate(FEATURES["trailing_return_fraction", 1], bars(_CLOSES))[0]
    tenth = FeaturePoint(
        event_time_utc=stamped.event_time_utc,
        available_at_utc=stamped.available_at_utc,
        feature_value=Decimal("0.1"),
    )
    padded = FeaturePoint(
        event_time_utc=stamped.event_time_utc,
        available_at_utc=stamped.available_at_utc,
        feature_value=Decimal("0.10"),
    )
    assert tenth.feature_value == padded.feature_value
    assert canonical_digest([tenth]) != canonical_digest([padded])


# ---------------------------------------------------------------------------
# The poison itself has to be gross, or a false pass is possible
# ---------------------------------------------------------------------------


def test_the_poison_actually_replaces_the_future() -> None:
    """A probe whose poison is absorbed by rounding reports a pass having tested nothing.

    Every close after the cut must differ by a wide margin, and every close at or before it
    must be untouched.
    """
    series = bars(_CLOSES)
    cut = series[len(series) // 2].event_time_utc
    poisoned = poison_after(series, cut=cut)

    unchanged = [
        (before, after)
        for before, after in zip(series, poisoned, strict=True)
        if before.event_time_utc <= cut
    ]
    assert unchanged
    assert all(before == after for before, after in unchanged)

    replaced = [
        (before, after)
        for before, after in zip(series, poisoned, strict=True)
        if before.event_time_utc > cut
    ]
    assert replaced
    for before, after in replaced:
        ratio = after.close_quote_price / before.close_quote_price
        assert ratio >= Decimal("3") or ratio <= Decimal("0.34")


def test_poisoning_one_close_touches_exactly_one_field_of_one_bar() -> None:
    """The label probe's instrument, checked before it is trusted.

    If it moved the next bar's open too, a correctly aligned label would move and the
    probe would report a leak that is not there -- which is how a real check gets deleted.
    """
    series = bars(_CLOSES)
    target = series[len(series) // 2].event_time_utc
    poisoned = poison_one_close(series, at=target)

    differing = [before for before, after in zip(series, poisoned, strict=True) if before != after]
    assert [observation.event_time_utc for observation in differing] == [target]
    assert all(
        before.open_quote_price == after.open_quote_price
        for before, after in zip(series, poisoned, strict=True)
    )
