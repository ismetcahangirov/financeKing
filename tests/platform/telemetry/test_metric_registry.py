"""The metric name registry refuses the names that break dashboards silently.

Every case here is a name that would be accepted by the OpenTelemetry SDK without
complaint and would then produce a series nobody queries or a rename nobody notices.
"""

from __future__ import annotations

from datetime import date

import pytest

from fking.platform.telemetry import (
    ACTIVE_RENAMES,
    ACTIVE_SERIES_BUDGET,
    FORBIDDEN_LABELS,
    LABEL_DOMAINS,
    MAX_SERIES_PER_METRIC,
    METRIC_PREFIX,
    MONETARY_UNIT_SUFFIXES,
    REGISTERED_METRICS,
    MetricNameError,
    MetricRename,
    MetricRenameError,
    MetricSpec,
    reconcile_with_pin,
)

pytestmark = pytest.mark.unit

# The golden set. Every name this system emits, pinned so that a rename or a removal
# fails a test rather than blanking a panel. Adding a name requires editing this list,
# which is the review the name gets before it is frozen; changing one requires a
# MetricRename declaring dual emission for a full retention window.
PINNED_METRIC_NAMES: frozenset[str] = frozenset(
    {
        "fking_agents_abstentions_total",
        "fking_agents_calls_total",
        "fking_agents_latency_seconds",
        "fking_agents_parse_failures_total",
        "fking_agents_quota_remaining_ratio",
        "fking_agents_tokens_consumed_total",
        "fking_data_bars_ingested_total",
        "fking_data_checksum_failures_total",
        "fking_data_feature_compute_seconds",
        "fking_data_gap_seconds_total",
        "fking_data_rows_rejected_total",
        "fking_data_stream_staleness_seconds",
        "fking_execution_fills_total",
        "fking_execution_orders_submitted_total",
        "fking_execution_reconciliation_age_seconds",
        "fking_execution_reconciliation_divergences_total",
        "fking_execution_rejections_total",
        "fking_execution_shortfall_basis_points",
        "fking_execution_stage_latency_seconds",
        "fking_platform_allowlist_rejections_total",
        "fking_platform_audit_write_failures_total",
        "fking_platform_bus_dead_lettered_total",
        "fking_platform_bus_dlq_depth_messages",
        "fking_platform_bus_events_consumed_total",
        "fking_platform_bus_events_published_total",
        "fking_platform_bus_lag_messages",
        "fking_platform_bus_messages_reclaimed_total",
        "fking_platform_log_fields_dropped_total",
        "fking_platform_log_orphan_records_total",
        "fking_platform_scheduler_job_runs_total",
        "fking_platform_scheduler_overlaps_refused_total",
        "fking_risk_decisions_total",
        "fking_risk_kill_switch_engaged_count",
        "fking_risk_limit_utilisation_ratio",
        "fking_risk_portfolio_drawdown_ratio",
        "fking_risk_position_notional_usd",
        "fking_risk_veto_total",
        "fking_strategy_signals_emitted_total",
        "fking_telemetry_spans_dropped_total",
    }
)


def test_every_registered_metric_is_prefixed_and_described() -> None:
    """The prefix is what makes one Grafana regex select everything this system emits."""
    for spec in REGISTERED_METRICS:
        assert spec.name.startswith(METRIC_PREFIX), spec.name
        assert spec.description.strip(), f"{spec.name} has no description"


def test_registered_names_are_unique() -> None:
    names = [spec.name for spec in REGISTERED_METRICS]
    assert sorted(names) == sorted(set(names))


def test_a_missing_prefix_is_refused() -> None:
    with pytest.raises(MetricNameError, match="does not start with"):
        MetricSpec(name="platform_bus_events_total", kind="counter", labels=(), description="x")


def test_a_counter_without_the_total_suffix_is_refused() -> None:
    """Prometheus convention, and every recording rule written against it."""
    with pytest.raises(MetricNameError, match="must end with _total"):
        MetricSpec(name="fking_platform_bus_events", kind="counter", labels=(), description="x")


def test_a_gauge_ending_in_total_is_refused() -> None:
    """`_total` on a gauge reads as monotonic, so `rate()` over it returns nonsense."""
    with pytest.raises(MetricNameError, match="ends with _total"):
        MetricSpec(name="fking_risk_drawdown_total", kind="gauge", labels=(), description="x")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("fking_execution_stage_latency_ms", "seconds"),
        ("fking_execution_stage_latency_milliseconds", "seconds"),
        ("fking_execution_shortfall_bps", "basis_points"),
        ("fking_data_archive_mb", "bytes"),
    ],
)
def test_a_non_base_unit_suffix_is_refused_and_names_the_replacement(
    name: str, expected: str
) -> None:
    """A dashboard mixing `_seconds` and `_ms` panels misreads by 1000x and looks fine."""
    with pytest.raises(MetricNameError, match=expected):
        MetricSpec(name=name, kind="gauge", labels=(), description="x")


def test_an_unknown_subsystem_is_refused() -> None:
    """The subsystem segment is what makes the producing module readable from a panel."""
    with pytest.raises(MetricNameError, match="not a module in this system"):
        MetricSpec(name="fking_utils_things_total", kind="counter", labels=(), description="x")


@pytest.mark.parametrize("label", sorted(FORBIDDEN_LABELS))
def test_every_unbounded_label_is_refused(label: str) -> None:
    """One time series per order id takes Prometheus down during the incident that
    generated the order ids."""
    with pytest.raises(MetricNameError, match="unbounded label"):
        MetricSpec(
            name="fking_execution_orders_total", kind="counter", labels=(label,), description="x"
        )


def test_a_duplicated_label_is_refused() -> None:
    with pytest.raises(MetricNameError, match="more than once"):
        MetricSpec(
            name="fking_execution_orders_total",
            kind="counter",
            labels=("venue", "venue"),
            description="x",
        )


def test_a_permitted_name_is_accepted() -> None:
    """Guards the tests above: a validator that rejected everything would pass them all."""
    spec = MetricSpec(
        name="fking_execution_stage_latency_seconds",
        kind="histogram",
        labels=("stage",),
        description="Per-stage latency in the order path.",
    )
    assert spec.name.endswith("_seconds")


def test_a_gauge_stating_no_unit_is_refused() -> None:
    """`fking_execution_order_size` could be a quantity, a notional or a byte count."""
    with pytest.raises(MetricNameError, match="states no unit"):
        MetricSpec(name="fking_execution_order_size", kind="gauge", labels=(), description="x")


# ---------------------------------------------------------------------------
# The cardinality budget
# ---------------------------------------------------------------------------


def test_a_label_with_no_declared_domain_is_refused() -> None:
    """`LABEL_DOMAINS` is an allowlist, so an unbounded label is an import error rather
    than something a reviewer has to notice."""
    with pytest.raises(MetricNameError, match="no entry in LABEL_DOMAINS"):
        MetricSpec(
            name="fking_execution_orders_total",
            kind="counter",
            labels=("venue", "counterparty_note"),
            description="x",
        )


def test_a_label_product_over_the_per_metric_budget_is_refused() -> None:
    """strategy_id x symbol is 12,000 series for one metric and grows with the product."""
    with pytest.raises(MetricNameError, match="over the"):
        MetricSpec(
            name="fking_risk_exposure_usd",
            kind="gauge",
            labels=("strategy_id", "symbol"),
            description="x",
            authoritative_audit_columns=("position_snapshot.base_quantity",),
        )


def test_every_registered_metric_stays_inside_the_per_metric_budget() -> None:
    for spec in REGISTERED_METRICS:
        assert spec.declared_series_ceiling <= MAX_SERIES_PER_METRIC, spec.name


def test_the_declared_set_stays_inside_the_active_series_budget() -> None:
    """Individually reasonable metrics still sum, and the sum is what Prometheus holds."""
    total = sum(spec.declared_series_ceiling for spec in REGISTERED_METRICS)
    assert total <= ACTIVE_SERIES_BUDGET, total


def test_a_histogram_is_counted_as_more_than_one_series_per_label_combination() -> None:
    """Buckets plus _sum and _count. Ignoring the factor understates a histogram ~16x."""
    histogram = MetricSpec(
        name="fking_execution_stage_latency_seconds",
        kind="histogram",
        labels=("stage",),
        description="x",
    )
    counter_spec = MetricSpec(
        name="fking_execution_stage_entries_total",
        kind="counter",
        labels=("stage",),
        description="x",
    )
    assert histogram.declared_series_ceiling > counter_spec.declared_series_ceiling


def test_no_forbidden_label_has_a_declared_domain() -> None:
    """The two tables must not disagree: a forbidden label with a domain would be
    accepted by the allowlist check and refused by the other, or vice versa after an
    edit to either."""
    assert not (FORBIDDEN_LABELS & set(LABEL_DOMAINS))


# ---------------------------------------------------------------------------
# Money in metrics
# ---------------------------------------------------------------------------


def test_a_monetary_metric_without_an_audit_column_is_refused() -> None:
    """A float sample at 15-day retention is a graph, not the record."""
    with pytest.raises(MetricNameError, match="names no audit column"):
        MetricSpec(name="fking_execution_realised_usd", kind="gauge", labels=(), description="x")


def test_a_non_monetary_metric_naming_an_audit_column_is_refused() -> None:
    with pytest.raises(MetricNameError, match="is not monetary"):
        MetricSpec(
            name="fking_execution_stage_latency_seconds",
            kind="histogram",
            labels=("stage",),
            description="x",
            authoritative_audit_columns=("fill.quote_price",),
        )


def test_an_audit_column_that_is_not_table_dot_column_is_refused() -> None:
    with pytest.raises(MetricNameError, match=r"not 'table\.column'"):
        MetricSpec(
            name="fking_execution_realised_usd",
            kind="gauge",
            labels=(),
            description="x",
            authoritative_audit_columns=("realised_pnl_quote",),
        )


def test_every_monetary_metric_names_where_the_exact_decimal_lives() -> None:
    """The reconstruction path reads these columns. Nothing reconstructs from a sample."""
    for spec in REGISTERED_METRICS:
        monetary = any(spec.name.endswith(f"_{suffix}") for suffix in MONETARY_UNIT_SUFFIXES)
        assert monetary == bool(spec.authoritative_audit_columns), spec.name


# ---------------------------------------------------------------------------
# The frozen name set
# ---------------------------------------------------------------------------


def test_the_emitted_name_set_matches_the_pin() -> None:
    """A rename does not raise: the panel goes blank and the alert stops firing, which
    looks exactly like an alert with nothing to report."""
    violations = reconcile_with_pin(
        registered_names=frozenset(spec.name for spec in REGISTERED_METRICS),
        pinned_names=PINNED_METRIC_NAMES,
        renames=ACTIVE_RENAMES,
    )
    assert violations == ()


def test_a_dropped_name_with_no_rename_is_reported() -> None:
    violations = reconcile_with_pin(
        registered_names=frozenset({"fking_risk_veto_total"}),
        pinned_names=frozenset({"fking_risk_veto_total", "fking_risk_decisions_total"}),
        renames=(),
    )
    assert len(violations) == 1
    assert "no rename declares it" in violations[0]


def test_a_renamed_metric_must_still_emit_the_old_name() -> None:
    """Dual emission means both names are emitted; dropping the old one early forks the
    series and leaves the retained window unqueryable."""
    rename = MetricRename(
        old_name="fking_risk_veto_total",
        new_name="fking_risk_vetoes_total",
        declared_on=date(2026, 8, 6),
        dual_emit_until=date(2026, 8, 25),
        reason="plural for consistency with the other counters",
    )
    violations = reconcile_with_pin(
        registered_names=frozenset({"fking_risk_vetoes_total"}),
        pinned_names=frozenset({"fking_risk_veto_total", "fking_risk_vetoes_total"}),
        renames=(rename,),
    )
    assert len(violations) == 1
    assert "dual emission" in violations[0]


def test_a_rename_dual_emitting_both_names_is_accepted() -> None:
    rename = MetricRename(
        old_name="fking_risk_veto_total",
        new_name="fking_risk_vetoes_total",
        declared_on=date(2026, 8, 6),
        dual_emit_until=date(2026, 8, 25),
        reason="plural for consistency with the other counters",
    )
    violations = reconcile_with_pin(
        registered_names=frozenset({"fking_risk_veto_total", "fking_risk_vetoes_total"}),
        pinned_names=frozenset({"fking_risk_veto_total", "fking_risk_vetoes_total"}),
        renames=(rename,),
    )
    assert violations == ()


def test_a_new_name_that_is_not_pinned_is_reported() -> None:
    violations = reconcile_with_pin(
        registered_names=frozenset({"fking_risk_veto_total", "fking_risk_new_total"}),
        pinned_names=frozenset({"fking_risk_veto_total"}),
        renames=(),
    )
    assert len(violations) == 1
    assert "not pinned" in violations[0]


def test_a_rename_shorter_than_the_retention_window_is_refused() -> None:
    """Prometheus cannot rename history; 14 days leaves a hole in a 15-day window."""
    with pytest.raises(MetricRenameError, match="under the 15-day"):
        MetricRename(
            old_name="fking_risk_veto_total",
            new_name="fking_risk_vetoes_total",
            declared_on=date(2026, 8, 6),
            dual_emit_until=date(2026, 8, 20),
            reason="plural",
        )


def test_a_rename_with_no_stated_reason_is_refused() -> None:
    with pytest.raises(MetricRenameError, match="states no reason"):
        MetricRename(
            old_name="fking_risk_veto_total",
            new_name="fking_risk_vetoes_total",
            declared_on=date(2026, 8, 6),
            dual_emit_until=date(2026, 8, 25),
            reason="   ",
        )
