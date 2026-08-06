"""Adverse moments lower the deflated Sharpe. Hold the Sharpe fixed and check the sign.

This is the clause most easily lost in a refactor, because the moments enter through a
denominator and an inverted sign there produces a number that still looks like a
probability. Negative skew must lower the deflated figure and fatter tails must lower it,
so a strategy that earns steadily and loses catastrophically is discounted by the
statistics for the same reason it is objectionable economically. The raw Sharpe rewards
that shape; this is the correction that stops it.

The direction only holds where the observed Sharpe exceeds the selection benchmark -- the
regime a candidate under consideration is in. Below the benchmark the numerator is
negative, and a larger denominator pulls the statistic *up* toward zero, which is
arithmetically correct and says nothing about skill. The properties condition on that
explicitly rather than papering over it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from fking.backtest.validation import (
    SharpeEvidence,
    SharpeEvidenceUnusableError,
    deflated_sharpe_ratio,
    expected_max_sharpe,
)

pytestmark = [pytest.mark.unit, pytest.mark.property]

# A material worsening rather than an epsilon: the reported probability is quantised to
# twelve places, so an infinitesimal nudge can legitimately round to the same value and a
# strictness assertion on it would be a flaky test rather than a property.
_SKEWNESS_WORSENING = Decimal("1.0")
_KURTOSIS_WORSENING = Decimal("2.0")

evidence_records = st.builds(
    SharpeEvidence,
    observed_sharpe=st.decimals(min_value="0.05", max_value="1.20", places=4),
    trials_at_time_of_run=st.integers(min_value=1, max_value=20_000),
    independent_episode_count=st.integers(min_value=5, max_value=2_000),
    skewness=st.decimals(min_value="-1.50", max_value="1.50", places=3),
    kurtosis=st.decimals(min_value="2.00", max_value="9.00", places=3),
    sharpe_variance_across_trials=st.decimals(min_value="0.0001", max_value="0.0500", places=6),
)


def _deflated_or_skip(evidence: SharpeEvidence) -> Decimal:
    """The deflated Sharpe, discarding draws whose variance term is non-positive.

    Skew and kurtosis are free parameters here, and some combinations make the statistic
    undefined. That refusal has its own example-based test; these properties are about
    the sign of a defined one.
    """
    try:
        return deflated_sharpe_ratio(evidence)
    except SharpeEvidenceUnusableError:
        assume(False)
        raise  # unreachable: assume(False) raises, and mypy needs the path closed


def _assume_above_the_selection_benchmark(evidence: SharpeEvidence) -> None:
    assume(
        evidence.observed_sharpe
        > expected_max_sharpe(
            evidence.trials_at_time_of_run, evidence.sharpe_variance_across_trials
        )
    )


@given(evidence=evidence_records)
def test_more_negative_skewness_never_raises_the_deflated_sharpe(
    evidence: SharpeEvidence,
) -> None:
    _assume_above_the_selection_benchmark(evidence)
    worsened = evidence.model_copy(update={"skewness": evidence.skewness - _SKEWNESS_WORSENING})

    assert _deflated_or_skip(worsened) <= _deflated_or_skip(evidence)


@given(evidence=evidence_records)
def test_fatter_kurtosis_never_raises_the_deflated_sharpe(
    evidence: SharpeEvidence,
) -> None:
    _assume_above_the_selection_benchmark(evidence)
    worsened = evidence.model_copy(update={"kurtosis": evidence.kurtosis + _KURTOSIS_WORSENING})

    assert _deflated_or_skip(worsened) <= _deflated_or_skip(evidence)


@given(evidence=evidence_records)
def test_adverse_moments_strictly_lower_an_unsaturated_deflated_sharpe(
    evidence: SharpeEvidence,
) -> None:
    """Strictness, conditioned on the probability not already sitting at a bound.

    The normal CDF saturates: past a statistic of about seven the reported probability is
    1 to twelve places, and no worsening of the moments can move it. That is a property
    of the CDF rather than of this gate, so the assertion is scoped to the band where a
    difference is representable at all.
    """
    _assume_above_the_selection_benchmark(evidence)
    baseline = _deflated_or_skip(evidence)
    assume(Decimal("0.01") < baseline < Decimal("0.99"))

    worsened = evidence.model_copy(
        update={
            "skewness": evidence.skewness - _SKEWNESS_WORSENING,
            "kurtosis": evidence.kurtosis + _KURTOSIS_WORSENING,
        }
    )

    assert _deflated_or_skip(worsened) < baseline


@given(evidence=evidence_records)
def test_a_larger_trial_count_never_raises_the_deflated_sharpe(
    evidence: SharpeEvidence,
) -> None:
    """Every test anyone runs makes every future result harder to prove.

    No conditioning on the benchmark here: a larger trial count raises `SR*`, which
    lowers the numerator whatever its sign, so the direction holds everywhere.
    """
    searched_harder = evidence.model_copy(
        update={"trials_at_time_of_run": evidence.trials_at_time_of_run * 2}
    )

    assert _deflated_or_skip(searched_harder) <= _deflated_or_skip(evidence)
