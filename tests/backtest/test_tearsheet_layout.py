"""Layout, suppression and the CPCV envelope, asserted by parsing the emitted HTML.

The suppression criterion is the reason this suite parses rather than greps. "The
document contains no equity curve" is a statement about elements: the string
`equity-curve` appears in a suppressed document too, inside the notice that explains the
suppression, so a substring assertion would pass a document that still drew the chart.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from fking.backtest.results import AuditCheck, AuditStatus, BacktestResult, ResultCredibility
from fking.backtest.tearsheet import SECTION_IDS, HeldOutStatus, render_tearsheet
from tests.backtest.results_support import complete_battery, finding, result_for
from tests.backtest.tearsheet_support import (
    ENGINE_SHA,
    equity_path,
    flat_equity_path,
    inputs_for,
    parse,
    path_distribution,
)


def _not_credible_result() -> BacktestResult:
    failed = complete_battery(
        overrides={
            AuditCheck.LOOK_AHEAD: finding(
                AuditCheck.LOOK_AHEAD,
                status=AuditStatus.FAIL,
                evidence=(
                    "entry rule reads high of the signal bar; deferring to next open "
                    "moves sharpe 3.4 -> 0.9"
                ),
            ),
            AuditCheck.COST_MODEL: finding(
                AuditCheck.COST_MODEL,
                status=AuditStatus.FAIL,
                evidence="cost_model_calibration_source=binance_futures_testnet_2026-05",
            ),
        }
    )
    return result_for(credibility=ResultCredibility.NOT_CREDIBLE, audit_findings=failed)


def test_sections_are_ordered_against_the_reader() -> None:
    document = render_tearsheet(inputs_for(distribution=path_distribution()))

    ids = parse(document).element_ids
    ordered = [element_id for element_id in ids if element_id in set(SECTION_IDS)]

    # Asserted against the exported constant rather than a literal repeated here: the
    # order is a published property of the renderer, and a test carrying its own copy
    # would let the two drift apart while both stayed green.
    assert ordered == list(SECTION_IDS)
    assert SECTION_IDS.index("equity-curve") == len(SECTION_IDS) - 2
    assert SECTION_IDS[-1] == "provenance"


def test_the_header_carries_every_identity_field() -> None:
    inputs = inputs_for(distribution=path_distribution())
    outcome = inputs.backtest_result

    document = render_tearsheet(inputs)

    for expected in (
        str(outcome.run_id),
        outcome.config_hash,
        outcome.strategy_id,
        outcome.strategy_version,
        outcome.window_start.isoformat(),
        outcome.window_end.isoformat(),
        outcome.cost_model_version,
        outcome.cost_model_calibration_source,
        str(outcome.trials_at_time_of_run),
        ENGINE_SHA,
    ):
        assert expected in document, f"the header omits {expected!r}"


def test_a_dirty_working_tree_is_stated_rather_than_implied_clean() -> None:
    document = render_tearsheet(inputs_for(is_working_tree_dirty=True))

    assert f"{ENGINE_SHA}-dirty" in document


def test_a_not_credible_run_has_no_equity_curve_element() -> None:
    document = render_tearsheet(
        inputs_for(backtest_result=_not_credible_result(), distribution=path_distribution())
    )

    index = parse(document)

    assert index.by_id("equity-curve") is None
    assert index.by_id("equity-line") is None
    assert index.by_id("cpcv-envelope") is None
    assert index.tags_named("svg") == []
    assert index.by_id("equity-curve-suppressed") is not None


def test_a_not_credible_run_renders_red_and_lists_its_failing_checks() -> None:
    document = render_tearsheet(inputs_for(backtest_result=_not_credible_result()))

    banner = parse(document).by_id("credibility")

    assert banner is not None
    _, attributes = banner
    assert attributes["data-credibility"] == "not_credible"
    assert "banner--not-credible" in attributes["class"]
    assert "<li>look_ahead: fail</li>" in document
    assert "<li>cost_model: fail</li>" in document


def test_an_unaudited_run_also_loses_the_curve_and_says_which_claim_it_is() -> None:
    unaudited = result_for(
        credibility=ResultCredibility.UNAUDITED,
        audit_findings=complete_battery(
            overrides={
                AuditCheck.PARITY: finding(AuditCheck.PARITY, status=AuditStatus.INCONCLUSIVE)
            }
        ),
    )

    document = render_tearsheet(inputs_for(backtest_result=unaudited))
    index = parse(document)

    assert index.tags_named("svg") == []
    banner = index.by_id("credibility")
    assert banner is not None
    assert banner[1]["data-credibility"] == "unaudited"
    assert "UNAUDITED" in document


def test_a_credible_run_draws_the_curve_inside_the_cpcv_envelope() -> None:
    document = render_tearsheet(inputs_for(distribution=path_distribution()))

    index = parse(document)
    envelope = index.by_id("cpcv-envelope")
    curve = index.by_id("equity-line")

    assert envelope is not None
    assert curve is not None
    assert envelope[0] == "polygon"
    assert curve[0] == "polyline"
    # Behind, not in front: the band must be painted before the line it contains.
    assert index.element_ids.index("cpcv-envelope") < index.element_ids.index("equity-line")
    assert index.by_id("cpcv-not-run") is None
    assert "p05 -0.9 to p95 3.0" in document


def test_no_cpcv_emits_an_explicit_note_rather_than_a_missing_element() -> None:
    document = render_tearsheet(inputs_for(distribution=None))

    index = parse(document)

    assert index.by_id("equity-line") is not None
    assert index.by_id("cpcv-envelope") is None
    note = index.by_id("cpcv-not-run")
    assert note is not None
    assert "CPCV not run" in document


def test_a_flat_curve_renders_rather_than_dividing_by_a_zero_span() -> None:
    document = render_tearsheet(
        inputs_for(path=flat_equity_path(), distribution=path_distribution())
    )

    assert parse(document).by_id("equity-line") is not None


@pytest.mark.parametrize(
    ("element_id", "expected"),
    [
        ("provenance-coverage", "spot/BTCUSDT"),
        ("provenance-features", "realised_volatility_24h"),
        ("provenance-parameters", "breakout_atr_multiple"),
        ("provenance-held-out", "2026-06-01 .. 2026-08-01"),
    ],
)
def test_the_provenance_footer_carries_each_required_block(element_id: str, expected: str) -> None:
    document = render_tearsheet(inputs_for(distribution=path_distribution()))

    assert parse(document).by_id(element_id) is not None
    assert expected in document


def test_coverage_gaps_are_listed_as_ranges_not_as_a_count() -> None:
    document = render_tearsheet(inputs_for(distribution=path_distribution()))

    assert "2026-01-09T03:00:00+00:00 .. 2026-01-09T03:20:00+00:00 (20 bars)" in document
    assert "no gaps" in document


def test_parameters_render_as_decimal_strings_at_their_own_exponent() -> None:
    document = render_tearsheet(inputs_for(parameters={"entry_threshold": Decimal("0.00001000")}))

    # `str()` would render this as `0.00001000` too, but a float that reached the footer
    # would render as `1e-05`; the assertion pins the spelling either way.
    assert "<td>0.00001000</td>" in document
    assert "1E-5" not in document


def test_the_sharpe_never_appears_without_its_trial_count() -> None:
    document = render_tearsheet(
        inputs_for(
            backtest_result=result_for(sharpe=Decimal("1.7"), trials_at_time_of_run=612),
            distribution=path_distribution(),
        )
    )

    assert "1.7 over 612 trials" in document


def test_a_held_out_period_that_was_burned_says_so() -> None:
    document = render_tearsheet(
        inputs_for(
            held_out=HeldOutStatus(start=date(2026, 6, 1), end=date(2026, 8, 1), is_burned=True)
        )
    )

    assert "BURNED" in document


def test_an_equity_path_is_required_so_suppression_cannot_pass_vacuously() -> None:
    # Not a rendering assertion: the point is that `TearsheetInputs` has no way to omit
    # the path, so "no curve was drawn" can never mean "there was nothing to draw".
    point_total = 5
    inputs = inputs_for(path=equity_path(point_total=point_total))

    assert inputs.equity_path.observation_count == point_total - 1
