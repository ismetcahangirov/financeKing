"""The metric name registry, validated at import time.

`OBSERVABILITY.md` section 4 makes the case that this file exists to enforce: renaming a
metric breaks every dashboard, every alert and every historical query simultaneously,
silently, and in the direction of "no data" rather than "error". A panel goes blank and
an alert stops firing -- and an alert that stops firing looks exactly like an alert with
nothing to report, so a rename can disable a safety alert invisibly until the thing it
watched for happens. Prometheus cannot rename history either: old samples stay under the
old name, so a rename forks the series rather than migrating it.

So names are declared here, as data, and validated when this module is imported. A
misnamed instrument fails at import rather than at the first scrape, which is the
difference between a failed test run and a dashboard nobody notices is empty.

Three things are frozen here, not one:

- **The name.** Validated against `OBSERVABILITY.md` section 4's grammar at construction.
- **The label set, and the size of each label's domain.** `LABEL_DOMAINS` is an
  allowlist: a label that is not in it cannot be declared at all, so "unbounded label"
  is not a review finding, it is an import error. The declared domains multiply out to a
  ceiling on the series one metric can build, and that ceiling is checked here rather
  than discovered when Prometheus stops answering.
- **The authoritative record for monetary values.** A metric carrying `_usd` or
  `_basis_points` names the audit column holding the exact decimal, because a float
  sample under 15-day retention is a graph, not a record.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final, Literal

METRIC_PREFIX: Final[str] = "fking_"

# Matches the module map in `docs/rules/module-boundaries.md`, plus `telemetry` for
# the SDK's own self-reporting, which belongs to no trading module.
PERMITTED_SUBSYSTEMS: Final[frozenset[str]] = frozenset(
    {
        "agents",
        "backtest",
        "data",
        "evolution",
        "execution",
        "platform",
        "risk",
        "strategy",
        "telemetry",
    }
)

# Base units only. `_milliseconds` and `_bp` are absent on purpose: a dashboard that
# mixes `_seconds` and `_milliseconds` panels produces a 1000x reading error that looks
# like a plausible number, which is worse than one that looks wrong.
PERMITTED_UNIT_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        "basis_points",
        "bytes",
        "count",
        "fields",
        "messages",
        "ratio",
        "seconds",
        "usd",
    }
)

# A metric whose measurement is money or a money ratio may carry a float approximation
# for graphing, and must name the column that holds the exact decimal. `OBSERVABILITY.md`
# section 4: reconstructing a fill price from a Prometheus sample -- float precision,
# 15-day retention, subject to scrape gaps -- is not reconstruction.
MONETARY_UNIT_SUFFIXES: Final[frozenset[str]] = frozenset({"basis_points", "usd"})

# Suffixes that are a unit spelled in a non-base form. Named explicitly so the failure
# message can say what to write instead, rather than "not a permitted suffix".
_REJECTED_UNIT_SUFFIXES: Final[dict[str, str]] = {
    "bp": "basis_points",
    "bps": "basis_points",
    "kb": "bytes",
    "mb": "bytes",
    "microseconds": "seconds",
    "millis": "seconds",
    "milliseconds": "seconds",
    "ms": "seconds",
    "nanoseconds": "seconds",
    "secs": "seconds",
}

# `OBSERVABILITY.md` section 4: an unbounded label takes down Prometheus, and it does it
# during an incident, because an incident is when the unbounded thing gets interesting.
# High-cardinality identifiers belong on log lines and span attributes, which are indexed
# for exactly that.
FORBIDDEN_LABELS: Final[frozenset[str]] = frozenset(
    {
        "audit_ref",
        "client_order_id",
        "correlation_id",
        "event_id",
        "fill_id",
        "message_id",
        "order_id",
        "prompt",
        "trace_id",
        "trade_id",
    }
)

_SNAKE_CASE: Final[re.Pattern[str]] = re.compile(r"\A[a-z][a-z0-9_]*[a-z0-9]\Z")

# Every label this system is permitted to use, with the largest number of distinct
# values it may take. This is an allowlist, and that is the whole mechanism: a label
# nobody has bounded cannot be declared, so adding an unbounded label is not something
# that happens by accident during a hurried change -- it requires writing a number here
# and defending it in review.
#
# Each bound cites what caps it. A bound with no cap behind it is a guess, and a guess
# is how `symbol` becomes "whatever the exchange returned".
LABEL_DOMAINS: Final[Mapping[str, int]] = {
    "agent": 16,  # the agent catalogue under .claude/agents/ plus room to grow
    "binding_limit": 16,  # the named limits in fking.risk
    # Correlation clusters are named by their lexicographically smallest member, so the
    # domain can never exceed the symbol domain: the degenerate assignment is one cluster
    # per symbol. Bounded at the same number for that reason rather than by a guess at how
    # many clusters a crypto universe actually has.
    "cluster_id": 60,
    "component": 6,  # shortfall decomposition: spread, impact, fee, delay, timing
    "consumer_group": 16,  # one per consuming service, registered at startup
    "dataset": 6,  # klines, trades, aggtrades, funding, open_interest, metrics
    "detector": 4,  # gap detectors in fking.data
    "direction": 3,  # long, short, flat -- also prompt/completion for token counts
    "event_type": 48,  # the registered bus event catalogue
    "feature_name": 64,  # the feature registry, which the look-ahead probe iterates
    "feature_version": 4,  # versions kept live at once; older ones are retired
    "field_name": 64,  # the log field allowlist
    "host": 32,  # allowlist rejections only; see the note on that metric
    "interval": 6,  # 1m, 5m, 15m, 1h, 4h, 1d
    "job_id": 32,  # the registered scheduler job catalogue, ADR-0019
    "kind": 8,  # reconciliation divergence kinds
    "limit_name": 16,  # the named limits in fking.risk
    "liquidity": 2,  # maker, taker
    "logger": 64,  # module count
    "market": 2,  # spot, futures
    "order_type": 4,  # market, limit, stop_market, stop_limit
    "outcome": 6,  # a closed enum per metric, never free text
    "provider": 3,  # gemini, groq, and one spare
    "reason": 24,  # a closed enum per metric, never a venue message
    "side": 2,  # buy, sell
    "source": 2,  # archive, websocket
    "stage": 8,  # the order path's named stages
    "strategy_id": 200,  # the live population cap in EVOLUTION_ENGINE.md
    "stream": 24,  # the registered stream catalogue
    # Validated against the resolved universe before it is ever used as a label. A raw
    # symbol string from an exchange response is forbidden -- see FORBIDDEN_VALUE_SOURCES.
    "symbol": 60,
    "table": 24,  # the audited table set
    "venue": 4,  # the venue profiles in fking.execution
}

# A histogram is not one series. Prometheus stores one series per bucket boundary plus
# `_sum` and `_count`, so a histogram with the default fourteen buckets is sixteen
# series per label combination. Multiplying label domains without this factor
# understates a histogram's cost by an order of magnitude, which is exactly the
# arithmetic that gets skipped.
SERIES_PER_INSTRUMENT: Final[Mapping[str, int]] = {
    "counter": 1,
    "gauge": 1,
    "histogram": 16,
}

# Per-metric ceiling. `OBSERVABILITY.md` section 4: if the product is over a few
# thousand, redesign now. Set above "a few thousand" only because a histogram's bucket
# factor is counted here and is not a cardinality decision anyone makes per metric.
MAX_SERIES_PER_METRIC: Final[int] = 8_000

# Whole-process ceiling for the declared set, checked by tools/checks/metric_cardinality.py.
# A single-node Prometheus on the local stack is comfortable here and is not comfortable
# an order of magnitude above it.
ACTIVE_SERIES_BUDGET: Final[int] = 40_000

# Expressions that must never produce a label *value*, checked at the call site by
# tools/checks/metric_cardinality.py. The label name being bounded is not sufficient:
# `symbol=response["symbol"]` declares a bounded label and fills it from an exchange
# response, which is unbounded in practice and hostile input by definition.
FORBIDDEN_VALUE_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "audit_ref",
        "client_order_id",
        "clientOrderId",
        "correlation_id",
        "event_id",
        "fill_id",
        "message_id",
        "order_id",
        "orderId",
        "trace_id",
        "trade_id",
        "tradeId",
    }
)

# A rename forks the series rather than migrating it, so both names are emitted for at
# least one full metrics retention window before the old one is dropped. 15 days is the
# retention configured for Prometheus in DEPLOYMENT.md; anything shorter means a query
# over the retention window sees a hole it cannot union away.
MIN_DUAL_EMISSION_DAYS: Final[int] = 15

type InstrumentKind = Literal["counter", "gauge", "histogram"]


class MetricNameError(ValueError):
    """A declared metric name violates the frozen naming convention."""


class MetricRenameError(ValueError):
    """The declared metric name set changed without a valid dual-emission declaration."""


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """One metric, named once and never renamed.

    `description` is not decoration: it is what a reader sees in Grafana's metric browser
    when deciding whether this is the series they meant, and the alternative to writing
    it here is writing it in a dashboard JSON that diverges.
    """

    name: str
    kind: InstrumentKind
    labels: tuple[str, ...]
    description: str
    # `table.column` references into the audit schema, required when the metric's unit is
    # monetary and forbidden otherwise. The metric graphs a float; these columns hold the
    # decimal a reconstruction reads.
    authoritative_audit_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate(self)

    @property
    def declared_series_ceiling(self) -> int:
        """Series this metric can build if every declared label domain is saturated."""
        ceiling = SERIES_PER_INSTRUMENT[self.kind]
        for label in self.labels:
            ceiling *= LABEL_DOMAINS[label]
        return ceiling


def _validate(spec: MetricSpec) -> None:
    _validate_name(spec)
    _validate_labels(spec)
    _validate_monetary_record(spec)


def _validate_name(spec: MetricSpec) -> None:
    name = spec.name
    if not name.startswith(METRIC_PREFIX):
        raise MetricNameError(
            f"{name!r} does not start with {METRIC_PREFIX!r}; the prefix is what makes "
            f"every metric this system emits selectable with one regex and separates it "
            f"from the exporters sharing the same Prometheus"
        )
    if _SNAKE_CASE.fullmatch(name) is None:
        raise MetricNameError(f"{name!r} is not lower snake_case")

    remainder = name.removeprefix(METRIC_PREFIX)
    subsystem = remainder.split("_", 1)[0]
    if subsystem not in PERMITTED_SUBSYSTEMS:
        raise MetricNameError(
            f"{name!r} names subsystem {subsystem!r}, which is not a module in this "
            f"system; permitted: {sorted(PERMITTED_SUBSYSTEMS)}"
        )

    is_counter = spec.kind == "counter"
    if is_counter and not name.endswith("_total"):
        raise MetricNameError(f"counter {name!r} must end with _total (Prometheus convention)")
    if not is_counter and name.endswith("_total"):
        raise MetricNameError(
            f"{spec.kind} {name!r} ends with _total, which every reader and every "
            f"recording rule will take to mean a monotonic counter"
        )

    measured = name.removesuffix("_total")
    trailing = measured.rsplit("_", 1)[-1]
    if trailing in _REJECTED_UNIT_SUFFIXES:
        raise MetricNameError(
            f"{name!r} ends in the non-base unit {trailing!r}; write "
            f"{_REJECTED_UNIT_SUFFIXES[trailing]!r} and convert at the call site"
        )
    # A counter's `_total` satisfies the unit requirement; everything else states its
    # unit in the name, because `fking_execution_order_size` could be a quantity, a
    # notional or a byte count and somebody will alert on it having guessed wrong.
    if not is_counter and not any(
        measured.endswith(f"_{suffix}") for suffix in PERMITTED_UNIT_SUFFIXES
    ):
        raise MetricNameError(
            f"{spec.kind} {name!r} states no unit; end it with one of "
            f"{sorted(PERMITTED_UNIT_SUFFIXES)}"
        )


def _validate_labels(spec: MetricSpec) -> None:
    name = spec.name
    forbidden = sorted(set(spec.labels) & FORBIDDEN_LABELS)
    if forbidden:
        raise MetricNameError(
            f"{name!r} declares unbounded label(s) {forbidden}; an unbounded label "
            f"builds one time series per value. Put the identifier on the log line and "
            f"the span attribute instead"
        )
    duplicated = sorted({label for label in spec.labels if spec.labels.count(label) > 1})
    if duplicated:
        raise MetricNameError(f"{name!r} declares label(s) {duplicated} more than once")

    undeclared = sorted(set(spec.labels) - set(LABEL_DOMAINS))
    if undeclared:
        raise MetricNameError(
            f"{name!r} declares label(s) {undeclared} with no entry in LABEL_DOMAINS. "
            f"A label whose domain nobody has bounded is an unbounded label; add it "
            f"there with the number of distinct values it can take and what caps it"
        )

    ceiling = spec.declared_series_ceiling
    if ceiling > MAX_SERIES_PER_METRIC:
        breakdown = " x ".join(f"{label}={LABEL_DOMAINS[label]}" for label in spec.labels)
        raise MetricNameError(
            f"{name!r} can build {ceiling} series ({breakdown}, "
            f"{SERIES_PER_INSTRUMENT[spec.kind]} per {spec.kind}), over the "
            f"{MAX_SERIES_PER_METRIC} per-metric budget. Drop a label or split the "
            f"metric; the product does not get smaller in production"
        )


def _validate_monetary_record(spec: MetricSpec) -> None:
    name = spec.name
    is_monetary = any(name.endswith(f"_{suffix}") for suffix in MONETARY_UNIT_SUFFIXES)
    if is_monetary and not spec.authoritative_audit_columns:
        raise MetricNameError(
            f"{name!r} graphs a monetary value and names no audit column. A float "
            f"sample at 15-day retention is not the record; name the table.column "
            f"holding the exact decimal"
        )
    if not is_monetary and spec.authoritative_audit_columns:
        raise MetricNameError(
            f"{name!r} is not monetary but names audit columns "
            f"{list(spec.authoritative_audit_columns)}; the field exists to make the "
            f"decimal record findable, not to annotate every metric"
        )
    malformed = sorted(
        column for column in spec.authoritative_audit_columns if column.count(".") != 1
    )
    if malformed:
        raise MetricNameError(
            f"{name!r} names audit column(s) {malformed} that are not 'table.column'"
        )


BUS_EVENTS_PUBLISHED: Final = MetricSpec(
    name="fking_platform_bus_events_published_total",
    kind="counter",
    labels=("stream", "event_type"),
    description="Envelopes accepted by XADD, by stream and event type.",
)

BUS_EVENTS_CONSUMED: Final = MetricSpec(
    name="fking_platform_bus_events_consumed_total",
    kind="counter",
    labels=("stream", "consumer_group", "outcome"),
    description=(
        "Envelopes a consumer reached a terminal decision on. `outcome` is applied, "
        "duplicate or dead_lettered -- a duplicate is normal operation, not an error."
    ),
)

BUS_MESSAGES_RECLAIMED: Final = MetricSpec(
    name="fking_platform_bus_messages_reclaimed_total",
    kind="counter",
    labels=("stream", "consumer_group"),
    description=(
        "Messages taken from a dead consumer's pending-entries list by XAUTOCLAIM. "
        "Non-zero after every restart, by design."
    ),
)

BUS_DEAD_LETTERED: Final = MetricSpec(
    name="fking_platform_bus_dead_lettered_total",
    kind="counter",
    labels=("stream", "reason"),
    description=(
        "Messages routed to the dead-letter stream. `reason` is a closed enum: "
        "undecodable, unregistered_event_type, schema_invalid, missing_correlation_id."
    ),
)

LOG_FIELDS_DROPPED: Final = MetricSpec(
    name="fking_platform_log_fields_dropped_total",
    kind="counter",
    labels=("field_name",),
    # Labelled by the dropped field name, which looks like a cardinality violation and is
    # not, for the same reason fking_platform_allowlist_rejections_total is labelled by
    # host: in correct operation this metric is always zero, and knowing *which* field
    # was dropped is the entire value of it. A field being silently dropped is a bug; a
    # field being dropped and counted is a signal that gets it added deliberately.
    description="Log fields discarded because they are absent from the field allowlist.",
)

LOG_ORPHAN_RECORDS: Final = MetricSpec(
    name="fking_platform_log_orphan_records_total",
    kind="counter",
    labels=("logger",),
    description=(
        "Log records emitted with no correlation scope active, repaired to "
        "correlation_id=orphan. Non-zero means a flow is not opening a scope, which is "
        "a defect in the emitting module rather than a threshold to raise."
    ),
)

SCHEDULER_JOB_RUNS: Final = MetricSpec(
    name="fking_platform_scheduler_job_runs_total",
    kind="counter",
    labels=("job_id", "outcome"),
    description=(
        "Scheduled job executions that reached a terminal state, by job and outcome. "
        "`outcome` is a closed set: succeeded, failed or abandoned. Labelled by job_id, "
        "which is bounded by the registered job catalogue rather than by traffic."
    ),
)

SCHEDULER_OVERLAPS_REFUSED: Final = MetricSpec(
    name="fking_platform_scheduler_overlaps_refused_total",
    kind="counter",
    labels=("job_id",),
    description=(
        "Runs refused because the previous run of the same job was still in flight. "
        "Sustained non-zero means the job is slower than its cadence, and the refusal -- "
        "rather than a queued run against a window that has since moved -- is what keeps "
        "two writers off one partition."
    ),
)

TELEMETRY_SPANS_DROPPED: Final = MetricSpec(
    name="fking_telemetry_spans_dropped_total",
    kind="counter",
    labels=(),
    description=(
        "Spans the exporter failed to deliver. Silent trace loss during an incident is "
        "the worst possible moment to lose traces, so it is counted rather than logged."
    ),
)

ALLOWLIST_REJECTIONS: Final = MetricSpec(
    name="fking_platform_allowlist_rejections_total",
    kind="counter",
    labels=("host",),
    # Labelled by the rejected host, which looks like a cardinality violation and is not:
    # in correct operation this metric is always zero, a non-zero value is a critical
    # incident, and which host was attempted is the entire value of the metric. If it
    # ever goes high-cardinality the system is being probed and the series count is the
    # least of the problems.
    description="Requests refused because the host is outside the compiled-in allowlist.",
)

AUDIT_WRITE_FAILURES: Final = MetricSpec(
    name="fking_platform_audit_write_failures_total",
    kind="counter",
    labels=("table",),
    description=(
        "Audit inserts that did not commit. Non-zero means a decision happened that "
        "cannot be reconstructed, which is the failure the audit substrate exists to "
        "prevent -- it is an incident, not a data-quality ticket."
    ),
)

BUS_LAG: Final = MetricSpec(
    name="fking_platform_bus_lag_messages",
    kind="gauge",
    labels=("stream", "consumer_group"),
    description="Undelivered entries between the stream's tail and a group's cursor.",
)

BUS_DLQ_DEPTH: Final = MetricSpec(
    name="fking_platform_bus_dlq_depth_messages",
    kind="gauge",
    labels=("stream",),
    description=(
        "Entries resting in a stream's dead-letter partition. Named `_messages` rather "
        "than `_depth` because a bare depth could be bytes or entries."
    ),
)

DATA_BARS_INGESTED: Final = MetricSpec(
    name="fking_data_bars_ingested_total",
    kind="counter",
    labels=("market", "symbol", "interval", "source"),
    description="Bars written to the corpus, by market, symbol, interval and origin.",
)

DATA_ROWS_REJECTED: Final = MetricSpec(
    name="fking_data_rows_rejected_total",
    kind="counter",
    labels=("market", "dataset", "reason"),
    description=(
        "Rows refused at ingest. `reason` is a closed enum -- never a parser message, "
        "which is unbounded and belongs on the log line."
    ),
)

DATA_GAP_SECONDS: Final = MetricSpec(
    name="fking_data_gap_seconds_total",
    kind="counter",
    labels=("market", "symbol", "detector"),
    description=(
        "Cumulative gapped duration observed per series. A counter rather than a gauge "
        "because a gap that closed still happened and still disqualifies a window."
    ),
)

DATA_STREAM_STALENESS: Final = MetricSpec(
    name="fking_data_stream_staleness_seconds",
    kind="gauge",
    labels=("market", "symbol"),
    description="Age of the most recent event received on a live stream.",
)

DATA_CHECKSUM_FAILURES: Final = MetricSpec(
    name="fking_data_checksum_failures_total",
    kind="counter",
    labels=("market", "dataset"),
    description="Archive files whose published checksum did not match the bytes fetched.",
)

DATA_FEATURE_COMPUTE: Final = MetricSpec(
    name="fking_data_feature_compute_seconds",
    kind="histogram",
    labels=("feature_name", "feature_version"),
    description="Wall time to compute one feature value, by feature and version.",
)

STRATEGY_SIGNALS_EMITTED: Final = MetricSpec(
    name="fking_strategy_signals_emitted_total",
    kind="counter",
    labels=("strategy_id", "direction"),
    description="Signals a strategy emitted. A signal is a belief, never an order.",
)

RISK_DECISIONS: Final = MetricSpec(
    name="fking_risk_decisions_total",
    kind="counter",
    labels=("strategy_id", "outcome"),
    description=(
        "Risk decisions reaching a terminal outcome: sized or vetoed. Both arms are "
        "counted -- a system that instruments only the sized arm cannot answer 'why did "
        "we not trade that?', which is asked as often as its inverse."
    ),
)

RISK_VETOES: Final = MetricSpec(
    name="fking_risk_veto_total",
    kind="counter",
    labels=("strategy_id", "binding_limit"),
    description="Vetoes by the limit that bound. The rejection detail is in the audit row.",
)

RISK_POSITION_NOTIONAL: Final = MetricSpec(
    name="fking_risk_position_notional_usd",
    kind="gauge",
    labels=("symbol",),
    # Deliberately not labelled by strategy_id as well: strategy_id x symbol is
    # 200 x 60 = 12,000 series for one gauge, and it grows with the product rather than
    # the sum. Per-strategy exposure is answered from the audit rows, which carry the
    # exact decimal anyway.
    description=(
        "Open position notional per symbol, as a float approximation for graphing. The "
        "exact decimal is in the audit columns named below."
    ),
    authoritative_audit_columns=(
        "position_snapshot.base_quantity",
        "position_snapshot.average_entry_quote_price",
    ),
)

RISK_PORTFOLIO_DRAWDOWN: Final = MetricSpec(
    name="fking_risk_portfolio_drawdown_ratio",
    kind="gauge",
    labels=(),
    description="Drawdown from the equity high-water mark, as a 0-1 ratio, not a percent.",
)

RISK_LIMIT_UTILISATION: Final = MetricSpec(
    name="fking_risk_limit_utilisation_ratio",
    kind="gauge",
    labels=("limit_name",),
    description="Observed value over threshold per named limit, as a 0-1 ratio.",
)

RISK_PORTFOLIO_VOLATILITY: Final = MetricSpec(
    name="fking_risk_portfolio_volatility_ratio",
    kind="gauge",
    labels=(),
    description=(
        "sigma_p = sqrt(w' Sigma w), the daily volatility of the whole book as a 0-1 "
        "ratio of equity. The denominator of every risk-share limit, so a limit that "
        "suddenly binds is read against this before anything else."
    ),
)

RISK_ASSET_RISK_SHARE: Final = MetricSpec(
    name="fking_risk_asset_risk_share_ratio",
    kind="gauge",
    labels=("symbol",),
    description=(
        "CTR_i / sigma_p per symbol: share of portfolio risk, not of notional. Signed -- "
        "a hedge carries a negative share, and clipping it at zero would make the shares "
        "stop summing to one."
    ),
)

RISK_CLUSTER_RISK_SHARE: Final = MetricSpec(
    name="fking_risk_cluster_risk_share_ratio",
    kind="gauge",
    labels=("cluster_id",),
    description=(
        "Share of portfolio risk held by one correlation cluster. This is the term that "
        "binds in a book that every per-symbol notional limit passes, so it is exported "
        "rather than left to be reconstructed from logs."
    ),
)

RISK_HISTORICAL_VAR: Final = MetricSpec(
    name="fking_risk_historical_var_ratio",
    kind="gauge",
    labels=(),
    description=(
        "Empirical, historical-simulation VaR at the configured confidence level, as a "
        "0-1 loss ratio -- never a parametric-normal estimate (issue #56). Read beside "
        "fking_risk_historical_var_sample_observations_count: a VaR read off a short "
        "sample is not a number a panel should render to three decimal places."
    ),
)

RISK_HISTORICAL_VAR_SAMPLE_OBSERVATIONS: Final = MetricSpec(
    name="fking_risk_historical_var_sample_observations_count",
    kind="gauge",
    labels=(),
    description="Observations the VaR estimate beside this gauge was read off.",
)

RISK_HISTORICAL_CVAR: Final = MetricSpec(
    name="fking_risk_historical_cvar_ratio",
    kind="gauge",
    labels=(),
    description=(
        "Empirical CVaR (expected shortfall) at the configured confidence level: the mean "
        "loss conditional on being past the VaR threshold, as a 0-1 loss ratio. Coherent "
        "and sub-additive where VaR is neither -- this is the term a limit binds on."
    ),
)

RISK_HISTORICAL_CVAR_TAIL_OBSERVATIONS: Final = MetricSpec(
    name="fking_risk_historical_cvar_tail_observations_count",
    kind="gauge",
    labels=(),
    description=(
        "Observations the CVaR average beside this gauge was actually taken over -- the "
        "tail, not the whole sample. A CVaR averaged over one observation is the number "
        "most likely to look precise and be noise."
    ),
)

RISK_BETA_TO_BTC: Final = MetricSpec(
    name="fking_risk_beta_to_btc_ratio",
    kind="gauge",
    labels=(),
    description=(
        "Beta of the book to BTC: whichever of the full-sample and stress-window "
        "estimates carries the larger magnitude (issue #56) -- a beta measured calm and "
        "relied on in stress overstates a hedge exactly when the hedge is needed."
    ),
)

RISK_BETA_TO_BTC_SAMPLE_OBSERVATIONS: Final = MetricSpec(
    name="fking_risk_beta_to_btc_sample_observations_count",
    kind="gauge",
    labels=(),
    description=(
        "Observations in whichever window (full sample or stress) produced the beta "
        "beside this gauge."
    ),
)

RISK_VOLATILITY_TO_TARGET: Final = MetricSpec(
    name="fking_risk_volatility_to_target_ratio",
    kind="gauge",
    labels=(),
    description=(
        "Realised annualised volatility over the target the sizing engine aims at. Above "
        "one means the book is running hotter than it was sized to; below one is capacity "
        "left on the table, not merely a metric inside its band."
    ),
)

RISK_VOLATILITY_SAMPLE_OBSERVATIONS: Final = MetricSpec(
    name="fking_risk_volatility_sample_observations_count",
    kind="gauge",
    labels=(),
    description="Observations the realised volatility estimate beside this gauge was read off.",
)

RISK_CONCENTRATION_HERFINDAHL: Final = MetricSpec(
    name="fking_risk_concentration_herfindahl_ratio",
    kind="gauge",
    labels=(),
    description=(
        "Herfindahl index over the book's cluster risk shares, in [0, 1]. Rises as risk "
        "concentrates into fewer clusters at constant notional; the per-symbol and "
        "per-cluster share gauges above are the detail this one summarises."
    ),
)

RISK_CONCENTRATION_CLUSTER_COUNT: Final = MetricSpec(
    name="fking_risk_concentration_cluster_count",
    kind="gauge",
    labels=(),
    description=(
        "Correlation clusters actually carrying weight, which the Herfindahl gauge "
        "beside this one was computed over."
    ),
)

RISK_KILL_SWITCH_ENGAGED: Final = MetricSpec(
    name="fking_risk_kill_switch_engaged_count",
    kind="gauge",
    labels=(),
    description=(
        "1 while the kill switch is engaged, 0 otherwise. `_count` rather than a bare "
        "`_active` so the name states what the number is; a unitless gauge on a panel "
        "is a number whose meaning lives in someone's memory."
    ),
)

EXECUTION_ORDERS_SUBMITTED: Final = MetricSpec(
    name="fking_execution_orders_submitted_total",
    kind="counter",
    labels=("venue", "symbol", "side", "order_type"),
    description="Orders accepted by a venue's submission endpoint.",
)

EXECUTION_FILLS: Final = MetricSpec(
    name="fking_execution_fills_total",
    kind="counter",
    labels=("venue", "symbol", "side", "liquidity"),
    description="Fills applied, split by maker and taker because the fee differs.",
)

EXECUTION_REJECTIONS: Final = MetricSpec(
    name="fking_execution_rejections_total",
    kind="counter",
    labels=("venue", "reason"),
    description=(
        "Orders the venue refused. `reason` is our classification of the venue code, "
        "not the venue's message text."
    ),
)

EXECUTION_SHORTFALL: Final = MetricSpec(
    name="fking_execution_shortfall_basis_points",
    kind="histogram",
    labels=("symbol", "component"),
    description=(
        "Implementation shortfall against the decision price, decomposed. Graphing "
        "approximation only -- the exact per-fill figure is in the audit column below."
    ),
    authoritative_audit_columns=("fill.slippage_bp",),
)

EXECUTION_STAGE_LATENCY: Final = MetricSpec(
    name="fking_execution_stage_latency_seconds",
    kind="histogram",
    labels=("stage",),
    description="Time spent in each named stage between signal and fill.",
)

EXECUTION_RECONCILIATION_AGE: Final = MetricSpec(
    name="fking_execution_reconciliation_age_seconds",
    kind="gauge",
    labels=("venue",),
    description=(
        "Age of the last completed reconciliation pass. Rising means local state is "
        "drifting from the exchange, which is the source of truth."
    ),
)

EXECUTION_RECONCILIATION_DIVERGENCES: Final = MetricSpec(
    name="fking_execution_reconciliation_divergences_total",
    kind="counter",
    labels=("venue", "kind"),
    description=(
        "Divergences found between local and venue state. `kind` distinguishes a testnet "
        "wipe -- orders gone and balances reset -- from an unrecorded rejection, which "
        "is the worse condition."
    ),
)

AGENTS_CALLS: Final = MetricSpec(
    name="fking_agents_calls_total",
    kind="counter",
    labels=("agent", "provider", "outcome"),
    description="Agent calls reaching a terminal outcome, including degraded and refused.",
)

AGENTS_TOKENS_CONSUMED: Final = MetricSpec(
    name="fking_agents_tokens_consumed_total",
    kind="counter",
    labels=("agent", "provider", "direction"),
    description="Tokens billed, split prompt and completion, reconciled from actual usage.",
)

AGENTS_QUOTA_REMAINING: Final = MetricSpec(
    name="fking_agents_quota_remaining_ratio",
    kind="gauge",
    labels=("provider",),
    description="Fraction of the day's admission budget still available, as a 0-1 ratio.",
)

AGENTS_PARSE_FAILURES: Final = MetricSpec(
    name="fking_agents_parse_failures_total",
    kind="counter",
    labels=("agent", "provider"),
    description=(
        "Responses that failed schema validation. With zero re-asks this is a direct "
        "measurement of the fraction of decisions an agent could not contribute to, "
        "which is why the alert threshold on it can be as tight as it is."
    ),
)

AGENTS_ABSTENTIONS: Final = MetricSpec(
    name="fking_agents_abstentions_total",
    kind="counter",
    labels=("agent",),
    description="Calls where the agent validly declined to produce a decision.",
)

AGENTS_LATENCY: Final = MetricSpec(
    name="fking_agents_latency_seconds",
    kind="histogram",
    labels=("agent", "provider"),
    description="End-to-end provider latency, measured on the monotonic clock.",
)

REGISTERED_METRICS: Final[tuple[MetricSpec, ...]] = (
    AGENTS_ABSTENTIONS,
    AGENTS_CALLS,
    AGENTS_LATENCY,
    AGENTS_PARSE_FAILURES,
    AGENTS_QUOTA_REMAINING,
    AGENTS_TOKENS_CONSUMED,
    ALLOWLIST_REJECTIONS,
    AUDIT_WRITE_FAILURES,
    BUS_DEAD_LETTERED,
    BUS_DLQ_DEPTH,
    BUS_EVENTS_CONSUMED,
    BUS_EVENTS_PUBLISHED,
    BUS_LAG,
    BUS_MESSAGES_RECLAIMED,
    DATA_BARS_INGESTED,
    DATA_CHECKSUM_FAILURES,
    DATA_FEATURE_COMPUTE,
    DATA_GAP_SECONDS,
    DATA_ROWS_REJECTED,
    DATA_STREAM_STALENESS,
    EXECUTION_FILLS,
    EXECUTION_ORDERS_SUBMITTED,
    EXECUTION_RECONCILIATION_AGE,
    EXECUTION_RECONCILIATION_DIVERGENCES,
    EXECUTION_REJECTIONS,
    EXECUTION_SHORTFALL,
    EXECUTION_STAGE_LATENCY,
    LOG_FIELDS_DROPPED,
    LOG_ORPHAN_RECORDS,
    RISK_ASSET_RISK_SHARE,
    RISK_BETA_TO_BTC,
    RISK_BETA_TO_BTC_SAMPLE_OBSERVATIONS,
    RISK_CLUSTER_RISK_SHARE,
    RISK_CONCENTRATION_CLUSTER_COUNT,
    RISK_CONCENTRATION_HERFINDAHL,
    RISK_DECISIONS,
    RISK_HISTORICAL_CVAR,
    RISK_HISTORICAL_CVAR_TAIL_OBSERVATIONS,
    RISK_HISTORICAL_VAR,
    RISK_HISTORICAL_VAR_SAMPLE_OBSERVATIONS,
    RISK_KILL_SWITCH_ENGAGED,
    RISK_LIMIT_UTILISATION,
    RISK_PORTFOLIO_DRAWDOWN,
    RISK_PORTFOLIO_VOLATILITY,
    RISK_POSITION_NOTIONAL,
    RISK_VETOES,
    RISK_VOLATILITY_SAMPLE_OBSERVATIONS,
    RISK_VOLATILITY_TO_TARGET,
    SCHEDULER_JOB_RUNS,
    SCHEDULER_OVERLAPS_REFUSED,
    STRATEGY_SIGNALS_EMITTED,
    TELEMETRY_SPANS_DROPPED,
)


@dataclass(frozen=True, slots=True)
class MetricRename:
    """One in-flight rename, dual-emitting both names until `dual_emit_until`.

    Prometheus cannot rename history: old samples stay under the old name, so a rename
    forks the series rather than migrating it. Both names are therefore emitted for at
    least one full retention window, which is what lets a dashboard be moved onto the new
    name while queries over the retained window still resolve.
    """

    old_name: str
    new_name: str
    declared_on: date
    dual_emit_until: date
    reason: str

    def __post_init__(self) -> None:
        window = self.dual_emit_until - self.declared_on
        if window < timedelta(days=MIN_DUAL_EMISSION_DAYS):
            raise MetricRenameError(
                f"{self.old_name!r} -> {self.new_name!r} dual-emits for {window.days} "
                f"days, under the {MIN_DUAL_EMISSION_DAYS}-day metrics retention "
                f"window. A shorter window leaves a hole no query can union away"
            )
        if not self.reason.strip():
            raise MetricRenameError(
                f"{self.old_name!r} -> {self.new_name!r} states no reason; a rename "
                f"blanks every panel built on the old name and needs one"
            )


# Renames currently dual-emitting. Empty is the healthy state: every entry here is a
# fork in the series history that someone has to finish removing.
ACTIVE_RENAMES: Final[tuple[MetricRename, ...]] = ()


def reconcile_with_pin(
    *,
    registered_names: frozenset[str],
    pinned_names: frozenset[str],
    renames: tuple[MetricRename, ...],
) -> tuple[str, ...]:
    """Report every difference between the declared and pinned name sets not covered.

    A name may be *added* only by updating the pin, which is a reviewed edit. A name may
    *disappear* only under an active rename that still emits it, because a name that
    stops being emitted takes its alerts down silently -- an alert that stopped firing
    looks exactly like an alert with nothing to report.
    """
    violations: list[str] = []
    retired = {rename.old_name for rename in renames}

    for name in sorted(pinned_names - registered_names):
        if name in retired:
            violations.append(
                f"{name!r} is pinned and under an active rename but is no longer "
                f"registered; dual emission means both names are emitted, not that the "
                f"old one is dropped early"
            )
        else:
            violations.append(
                f"{name!r} is pinned but no longer registered, and no rename declares "
                f"it. Every dashboard and alert on it now reads no-data rather than "
                f"erroring"
            )
    for name in sorted(registered_names - pinned_names):
        violations.append(
            f"{name!r} is registered but not pinned; add it to the golden set so the "
            f"name is reviewed once and then frozen"
        )
    return tuple(violations)


_declared_names = [spec.name for spec in REGISTERED_METRICS]
_duplicates = sorted({name for name in _declared_names if _declared_names.count(name) > 1})
if _duplicates:  # pragma: no cover - a module-level guard against a copy-paste mistake
    raise MetricNameError(f"metric name(s) declared more than once: {_duplicates}")
