"""What the tearsheet refuses to render, and what it says when it cannot draw the band.

Every refusal here exists because the alternative is an empty cell in a provenance
footer, and an empty provenance cell reads as "nothing to report" rather than "nobody
supplied it". The two are opposite claims and only one of them is true.

The envelope cases are the same argument applied to the chart: when CPCV ran but the band
cannot be projected, the document says which of the two happened and why, because a
reader who sees no band cannot otherwise tell "CPCV was not run" from "CPCV ran and this
run's volatility is zero".
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import cast

import pytest

from fking.backtest.results import ResultCredibility
from fking.backtest.tearsheet import (
    EngineBuild,
    EnvelopeProjection,
    HeldOutStatus,
    TearsheetInputError,
    project_envelope,
    render_tearsheet,
)
from tests.backtest.results_support import result_for
from tests.backtest.tearsheet_support import (
    ENGINE_SHA,
    equity_path,
    flat_equity_path,
    inputs_for,
    parse,
    path_distribution,
)


def test_a_short_engine_sha_is_refused() -> None:
    with pytest.raises(TearsheetInputError, match="at least 7"):
        EngineBuild(git_sha="0f1e")


def test_a_branch_name_is_not_an_engine_sha() -> None:
    with pytest.raises(TearsheetInputError, match="not hexadecimal"):
        EngineBuild(git_sha="feat/45-backtest-tearsheet")


def test_a_blank_engine_sha_is_refused() -> None:
    with pytest.raises(TearsheetInputError, match="must not be blank"):
        EngineBuild(git_sha="       ")


def test_a_held_out_window_must_be_ordered() -> None:
    with pytest.raises(TearsheetInputError, match="is not after start"):
        HeldOutStatus(start=date(2026, 8, 1), end=date(2026, 6, 1), is_burned=False)


def test_a_run_that_lists_no_series_is_refused() -> None:
    with pytest.raises(TearsheetInputError, match="coverage names no series"):
        inputs_for(coverage_series=())


def test_a_float_parameter_never_reaches_the_footer() -> None:
    # `docs/rules/decimal-and-money.md`: the footer prints the parameter set verbatim, so
    # a float arriving here would be printed as the binary double it already is.
    with pytest.raises(TearsheetInputError, match="is a float, not a Decimal"):
        inputs_for(parameters={"entry_threshold": cast(Decimal, 0.1)})


def test_a_blank_feature_version_is_refused() -> None:
    with pytest.raises(TearsheetInputError, match="version of feature"):
        inputs_for(feature_versions={"atr_14": ""})


def test_a_window_too_short_to_estimate_volatility_says_so_rather_than_drawing() -> None:
    refusal = project_envelope(path=equity_path(point_total=2), distribution=path_distribution())

    assert isinstance(refusal, str)
    assert "cannot be projected" in refusal


def test_a_zero_volatility_run_says_the_band_would_be_a_line() -> None:
    refusal = project_envelope(path=flat_equity_path(), distribution=path_distribution())

    assert isinstance(refusal, str)
    assert "realised volatility" in refusal


def test_the_band_is_projected_from_the_sharpe_quantiles_and_the_realised_volatility() -> None:
    projection = project_envelope(path=equity_path(), distribution=path_distribution())

    assert isinstance(projection, EnvelopeProjection)
    opening_equity_usd = equity_path().starting_equity_usd
    # Day zero is the origin for both bounds: no time has elapsed, so no drift has
    # accrued and the band has zero width where the curve starts.
    assert projection.lower_usd[0] == opening_equity_usd
    assert projection.upper_usd[0] == opening_equity_usd
    # p05 is negative in this distribution, so the lower bound falls away from the start
    # and the upper bound rises. A band that did not straddle the origin would mean the
    # projection had picked up the realised path's own shape.
    assert projection.lower_usd[-1] < opening_equity_usd < projection.upper_usd[-1]


def test_an_empty_battery_is_rendered_as_seven_unanswered_questions() -> None:
    unaudited = result_for(credibility=ResultCredibility.UNAUDITED, audit_findings=())

    document = render_tearsheet(inputs_for(backtest_result=unaudited))

    assert "An empty battery is seven unanswered questions" in document


def test_a_run_with_no_declared_features_or_parameters_says_so_explicitly() -> None:
    document = render_tearsheet(inputs_for(parameters={}, feature_versions={}))

    index = parse(document)

    assert index.by_id("feature-versions") is not None
    assert index.by_id("parameter-set") is not None
    assert "No feature-store features were declared" in document
    assert "takes no free parameters" in document


def test_the_engine_label_is_the_bare_sha_for_a_clean_tree() -> None:
    assert EngineBuild(git_sha=ENGINE_SHA).label == ENGINE_SHA
