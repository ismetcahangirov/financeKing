"""No Sharpe without its trial count, and no benchmark invented from a missing one.

Two refusals are asserted here rather than left to review.

`SharpeEvidence` has no default for `trials_at_time_of_run`, so omitting it is a schema
failure rather than a value of zero. The omission is the dangerous one because it is
invisible afterwards: a stored Sharpe with no trial count cannot have one reconstructed,
and every downstream consumer then treats an unqualified number as a qualified one
(`BACKTEST_ENGINE.md` section 6.5).

A non-positive trial count is a refusal for the same reason from the other direction.
Zero is what an empty or unreachable ledger returns, and zero read as "no selection took
place" deflates by nothing at all.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from fking.backtest.validation import (
    SharpeEvidence,
    SharpeEvidenceUnusableError,
    TrialCountUnavailableError,
    deflated_sharpe_ratio,
    expected_max_sharpe,
)

pytestmark = pytest.mark.unit


def _evidence(**overrides: object) -> SharpeEvidence:
    fields: dict[str, object] = {
        "observed_sharpe": Decimal("0.5"),
        "trials_at_time_of_run": 100,
        "independent_episode_count": 120,
        "skewness": Decimal("0"),
        "kurtosis": Decimal("3"),
        "sharpe_variance_across_trials": Decimal("0.01"),
    }
    fields.update(overrides)
    return SharpeEvidence(**fields)  # type: ignore[arg-type]  # a heterogeneous override map cannot be typed per field without repeating the model


def test_evidence_without_a_trial_count_fails_schema_validation() -> None:
    with pytest.raises(ValidationError, match="trials_at_time_of_run"):
        SharpeEvidence(  # type: ignore[call-arg]  # the omission is what is under test
            observed_sharpe=Decimal("0.5"),
            independent_episode_count=120,
            skewness=Decimal("0"),
            kurtosis=Decimal("3"),
            sharpe_variance_across_trials=Decimal("0.01"),
        )


def test_evidence_rejects_an_unrecognised_field_rather_than_absorbing_it() -> None:
    with pytest.raises(ValidationError):
        _evidence(annualised_sharpe=Decimal("2.4"))


def test_evidence_rejects_a_nan_moment() -> None:
    """A NaN skewness would propagate to a deflated Sharpe that fails every comparison."""
    with pytest.raises(ValidationError):
        _evidence(skewness=Decimal("NaN"))


@pytest.mark.parametrize("trial_count", [0, -1])
def test_a_non_positive_trial_count_is_refused_rather_than_deflating_by_nothing(
    trial_count: int,
) -> None:
    with pytest.raises(TrialCountUnavailableError, match="unreadable ledger"):
        expected_max_sharpe(trial_count, Decimal("0.01"))


def test_one_trial_benchmarks_against_zero_because_nothing_was_selected() -> None:
    assert expected_max_sharpe(1, Decimal("0.01")) == Decimal("0")


def test_the_benchmark_rises_with_the_trial_count() -> None:
    variance = Decimal("0.01")
    benchmarks = [expected_max_sharpe(count, variance) for count in (2, 100, 10_000)]

    assert benchmarks == sorted(benchmarks)
    assert benchmarks[0] < benchmarks[-1]


def test_a_hundredfold_search_raises_the_bar_by_about_half() -> None:
    """`SR*` grows as `sqrt(2 ln K)`, so 100x the search buys a small constant factor.

    The asymptote predicts `sqrt(ln 10000 / ln 100) = sqrt(2)`, about 1.41x; the exact
    expression at these counts gives about 1.53x, because the asymptote is approached
    slowly. Either number is the point: a hundredfold increase in search effort raises
    the noise threshold by roughly half, which *feels* survivable, and that is precisely
    why the trial counter is allowed to drift. The band is asserted rather than the
    constant, because a change that made the correction materially steeper or flatter
    would be a change to what this project considers evidence.
    """
    variance = Decimal("0.01")
    ratio = expected_max_sharpe(10_000, variance) / expected_max_sharpe(100, variance)

    assert Decimal("1.4") < ratio < Decimal("1.7")


def test_a_degenerate_trial_distribution_is_refused() -> None:
    with pytest.raises(SharpeEvidenceUnusableError, match="deflates by nothing"):
        expected_max_sharpe(100, Decimal("0"))


def test_moments_that_drive_the_variance_term_non_positive_are_refused() -> None:
    """Undefined, not weak: the value would sit on the same axis as a valid one."""
    with pytest.raises(SharpeEvidenceUnusableError, match="undefined, not weak"):
        deflated_sharpe_ratio(
            _evidence(
                observed_sharpe=Decimal("1.4"),
                skewness=Decimal("2"),
                kurtosis=Decimal("1"),
            )
        )


def test_the_deflated_sharpe_is_a_probability() -> None:
    deflated = deflated_sharpe_ratio(_evidence())

    assert Decimal("0") <= deflated <= Decimal("1")
