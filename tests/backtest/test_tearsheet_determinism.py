"""Two renders of one stored result are the same bytes, and a third write is refused.

A tearsheet that differs between renders is not a cosmetic problem. It means the artefact
is a document about the moment it was produced rather than about the run, which is the
one thing issue #45 says it must not be -- and in this codebase a result that differs
between two runs of the same `config_hash` outranks everything else on the queue.

The dict-ordering test permutes the *insertion* order of the parameter and feature
mappings rather than their contents. Python preserves insertion order, so a renderer that
iterated the mapping directly would produce two different documents for two callers who
built the same configuration in a different order -- the most likely way this file ever
goes red.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from fking.backtest.results import ResultCredibility
from fking.backtest.tearsheet import (
    TearsheetRegenerationError,
    render_tearsheet,
    write_tearsheet,
)
from tests.backtest.results_support import complete_battery, result_for
from tests.backtest.tearsheet_support import inputs_for, path_distribution


def test_rendering_twice_produces_the_same_string() -> None:
    inputs = inputs_for(distribution=path_distribution())

    assert render_tearsheet(inputs) == render_tearsheet(inputs)


def test_writing_twice_produces_a_byte_identical_file(tmp_path: Path) -> None:
    inputs = inputs_for(distribution=path_distribution())

    first = write_tearsheet(inputs, reports_root=tmp_path)
    first_bytes = first.read_bytes()
    second = write_tearsheet(inputs, reports_root=tmp_path)

    assert second == first
    assert second.read_bytes() == first_bytes


def test_mapping_insertion_order_does_not_change_the_document() -> None:
    forward = inputs_for(
        parameters={"alpha": Decimal("1.5"), "beta": Decimal("2.5"), "gamma": Decimal("3.5")},
        feature_versions={"atr_14": "v3", "rv_24h": "v1"},
    )
    reversed_order = inputs_for(
        parameters={"gamma": Decimal("3.5"), "beta": Decimal("2.5"), "alpha": Decimal("1.5")},
        feature_versions={"rv_24h": "v1", "atr_14": "v3"},
    )

    assert render_tearsheet(forward) == render_tearsheet(reversed_order)


def test_audit_findings_render_in_battery_order_not_caller_order() -> None:
    battery = complete_battery()
    forward = inputs_for(backtest_result=result_for(audit_findings=battery))
    shuffled = inputs_for(backtest_result=result_for(audit_findings=tuple(reversed(battery))))

    assert render_tearsheet(forward) == render_tearsheet(shuffled)


def test_coverage_order_does_not_change_the_document() -> None:
    inputs = inputs_for(distribution=path_distribution())
    reordered = inputs_for(
        distribution=path_distribution(), coverage_series=tuple(reversed(inputs.coverage))
    )

    assert render_tearsheet(reordered) == render_tearsheet(inputs)


def test_a_differing_rewrite_is_refused_rather_than_silently_applied(tmp_path: Path) -> None:
    inputs = inputs_for(distribution=path_distribution())
    write_tearsheet(inputs, reports_root=tmp_path)

    regenerated = inputs_for(
        backtest_result=result_for(credibility=ResultCredibility.NOT_CREDIBLE, trade_count=12),
        distribution=path_distribution(),
    )

    with pytest.raises(TearsheetRegenerationError, match="differ from this render"):
        write_tearsheet(regenerated, reports_root=tmp_path)
