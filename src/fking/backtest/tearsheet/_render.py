"""The document itself: one self-contained HTML string, ordered against the reader.

**Section order is the point, not the styling.** Header, credibility banner, audit
findings, economics, and only then the equity curve. Anchoring is real and it is not
defeated by knowing about it: a reader who has already seen an attractive curve reads the
audit findings looking for reasons they are too strict. So the numbers most likely to
invalidate the result are placed where they cannot be skipped, and the picture is last.

**A result that has not earned the picture does not get it.** The curve is suppressed for
`not_credible` *and* for `unaudited`, which is a deliberate strengthening of issue #45's
wording. `unaudited` means the battery is incomplete (`fking.backtest.results`), and a
curve shown beside an incomplete audit is a curve shown beside nothing. The banner
distinguishes the two -- "checked and failed" and "not finished checking" are different
claims -- but neither is a licence to look.

**Nothing is fetched.** No `<script>`, no `<link>`, no `@import`, no web font, no chart
library: the styling is one inline `<style>` block, the chart is an inline `<svg>`, and
the type is whatever the reading machine already has. Enforced by
`tests/backtest/test_tearsheet_offline.py`, which renders with `socket.socket` disabled
and then greps the emitted document for every way a browser can be made to make a
request.

**Nothing is read at render time.** No clock, no `git`, no environment, no filesystem.
Every fact comes from `TearsheetInputs`, so two renders of one stored result are the same
bytes -- which is what makes the stored artefact a record of the run rather than a
document about the day it was regenerated.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from html import escape
from typing import Final

from fking.backtest.feed import SymbolCoverage
from fking.backtest.results import AUDIT_ORDER, AuditFinding, AuditStatus, ResultCredibility
from fking.backtest.tearsheet._chart import ChartGeometry, chart_geometry, project_envelope
from fking.backtest.tearsheet._inputs import TearsheetInputs

__all__ = ["SECTION_IDS", "render_tearsheet"]

#: The order the document is written in, and the order the layout suite asserts. Fixed
#: here rather than implied by the order of calls below, so that reordering the sections
#: is a visible edit to a named constant and not a quiet move of a function call.
SECTION_IDS: Final[tuple[str, ...]] = (
    "header",
    "credibility",
    "audit-findings",
    "economics",
    "equity-curve",
    "provenance",
)

_EQUITY_CURVE_SUPPRESSED_ID: Final[str] = "equity-curve-suppressed"

_STYLE: Final[str] = """
:root { color-scheme: light dark; }
body { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
       margin: 0 auto; max-width: 1040px; padding: 24px; line-height: 1.45; }
h1 { font-size: 1.15rem; margin: 0 0 4px; }
h2 { font-size: 0.95rem; letter-spacing: 0.08em; text-transform: uppercase;
     margin: 28px 0 8px; }
table { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
th, td { border-bottom: 1px solid #d8d8d8; padding: 4px 8px; text-align: left;
         vertical-align: top; }
th { font-weight: 600; white-space: nowrap; width: 15em; }
.banner { border-radius: 4px; padding: 12px 16px; margin: 16px 0; font-weight: 600; }
.banner--not-credible { background: #7f1d1d; color: #fff; border: 2px solid #450a0a; }
.banner--unaudited { background: #78350f; color: #fff; border: 2px solid #451a03; }
.banner--credible { background: #14532d; color: #fff; border: 2px solid #052e16; }
.banner p { font-weight: 400; margin: 6px 0 0; }
.status-fail { color: #7f1d1d; font-weight: 700; }
.status-inconclusive { color: #78350f; font-weight: 700; }
.status-pass { color: #14532d; }
.note { font-size: 0.85rem; padding: 10px 12px; border-left: 4px solid #a3a3a3;
        background: #f4f4f4; margin: 12px 0; }
.evidence { white-space: pre-wrap; font-size: 0.8rem; }
.chart-envelope { fill: #94a3b8; fill-opacity: 0.35; stroke: none; }
.chart-curve { fill: none; stroke: #1d4ed8; stroke-width: 2; }
footer { margin-top: 32px; font-size: 0.8rem; }
"""


def render_tearsheet(inputs: TearsheetInputs) -> str:
    """The complete document for one run, as a UTF-8 string with `\\n` line endings.

    Pure: no clock, no filesystem, no network, no randomness. Called twice with the same
    inputs it returns the same string, character for character.
    """
    outcome = inputs.backtest_result
    lines: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(_document_title(inputs))}</title>",
        f"<style>{_STYLE}</style>",
        "</head>",
        "<body>",
        "<main>",
    ]
    lines.extend(_header_section(inputs))
    lines.extend(_credibility_section(inputs))
    lines.extend(_audit_section(outcome.audit_findings))
    lines.extend(_economics_section(inputs))
    lines.extend(_equity_section(inputs))
    lines.extend(_provenance_section(inputs))
    lines.extend(["</main>", "</body>", "</html>"])
    return "\n".join(lines) + "\n"


def _document_title(inputs: TearsheetInputs) -> str:
    outcome = inputs.backtest_result
    return (
        f"{outcome.strategy_id} {outcome.strategy_version} -- "
        f"{outcome.credibility.value} -- run {outcome.run_id}"
    )


def _header_section(inputs: TearsheetInputs) -> list[str]:
    outcome = inputs.backtest_result
    rows: tuple[tuple[str, str], ...] = (
        ("run_id", str(outcome.run_id)),
        ("config_hash", outcome.config_hash),
        ("strategy", f"{outcome.strategy_id} @ {outcome.strategy_version}"),
        ("window (UTC)", f"{_moment(outcome.window_start)} .. {_moment(outcome.window_end)}"),
        ("cost model", outcome.cost_model_version),
        # Version and provenance are separate rows because they answer different
        # questions and only one of them can void the run: a cost model calibrated on
        # testnet is fiction regardless of how current its version string is
        # (`CLAUDE.md` section 2).
        ("cost model calibration", outcome.cost_model_calibration_source),
        ("trials at time of run", str(outcome.trials_at_time_of_run)),
        ("engine git SHA", inputs.engine.label),
    )
    return [
        '<header id="header">',
        f"<h1>{escape(outcome.strategy_id)} {escape(outcome.strategy_version)}"
        f" &mdash; backtest tearsheet</h1>",
        *_definition_table(rows),
        "</header>",
    ]


def _credibility_section(inputs: TearsheetInputs) -> list[str]:
    outcome = inputs.backtest_result
    failing = tuple(
        found for found in outcome.audit_findings if found.status is not AuditStatus.PASS
    )
    modifier = outcome.credibility.value.replace("_", "-")
    headline, explanation = _banner_text(outcome.credibility, failing)
    lines = [
        f'<section id="credibility" class="banner banner--{modifier}"'
        f' data-credibility="{escape(outcome.credibility.value)}">',
        f"<div>{escape(headline)}</div>",
        f"<p>{escape(explanation)}</p>",
    ]
    if failing:
        lines.append("<ul>")
        lines.extend(
            f"<li>{escape(found.check.value)}: {escape(found.status.value)}</li>"
            for found in _in_audit_order(failing)
        )
        lines.append("</ul>")
    lines.append("</section>")
    return lines


def _banner_text(
    credibility: ResultCredibility, failing: tuple[AuditFinding, ...]
) -> tuple[str, str]:
    if credibility is ResultCredibility.NOT_CREDIBLE:
        return (
            f"NOT CREDIBLE -- {len(failing)} check(s) did not pass",
            "The equity curve is suppressed below. A curve read before its audit is a "
            "curve that makes the audit look too strict.",
        )
    if credibility is ResultCredibility.UNAUDITED:
        return (
            "UNAUDITED -- the seven-check battery is incomplete",
            "Not the same claim as 'not credible': nothing here has been refuted, and "
            "nothing here has been established. The equity curve is suppressed for the "
            "same reason it is suppressed for a failed audit.",
        )
    return (
        "CREDIBLE -- all seven checks pass on this window",
        "One window. Not evidence on its own: this result is admissible to walk-forward "
        "and combinatorial purged cross-validation, and to nothing else.",
    )


def _audit_section(findings: tuple[AuditFinding, ...]) -> list[str]:
    lines = [
        '<section id="audit-findings">',
        "<h2>Audit findings</h2>",
    ]
    if not findings:
        lines.extend(
            [
                (
                    '<p class="note">No audit findings were recorded. An empty battery '
                    "is seven unanswered questions, not seven passes.</p>"
                ),
                "</section>",
            ]
        )
        return lines
    lines.extend(
        [
            "<table>",
            "<thead><tr><th>check</th><th>status</th><th>evidence</th></tr></thead>",
            "<tbody>",
        ]
    )
    for found in _in_audit_order(findings):
        lines.append(
            f"<tr><td>{escape(found.check.value)}</td>"
            f'<td class="status-{found.status.value}">{escape(found.status.value)}</td>'
            f'<td class="evidence">{escape(found.evidence)}</td></tr>'
        )
    lines.extend(["</tbody>", "</table>", "</section>"])
    return lines


def _economics_section(inputs: TearsheetInputs) -> list[str]:
    outcome = inputs.backtest_result
    economics: tuple[tuple[str, str], ...] = (
        ("gross return", _fraction(outcome.gross_return)),
        ("total cost", _fraction(outcome.total_cost)),
        ("net return", _fraction(outcome.net_return)),
        ("gross edge / trade", f"{outcome.gross_edge_per_trade_bp:f} bp"),
        ("round-trip cost", f"{outcome.round_trip_cost_bp:f} bp"),
        ("edge / cost", f"{outcome.edge_to_cost_ratio:f}"),
        ("trades", str(outcome.trade_count)),
        ("max drawdown", _fraction(outcome.max_drawdown)),
        ("risk-limit breaches", str(outcome.risk_limit_breaches)),
        # A Sharpe never appears without the trial count that produced the configuration
        # it describes (`docs/rules/overfitting-defences.md`). They are one row, not two
        # adjacent ones, so that quoting the number out of the table takes the denominator
        # with it.
        (
            "Sharpe (trials)",
            f"{outcome.sharpe:f} over {outcome.trials_at_time_of_run} trials",
        ),
        ("deflated Sharpe", f"{outcome.deflated_sharpe:f}"),
    )
    return [
        '<section id="economics">',
        "<h2>Economics and statistics</h2>",
        *_definition_table(economics),
        "</section>",
    ]


def _equity_section(inputs: TearsheetInputs) -> list[str]:
    outcome = inputs.backtest_result
    if outcome.credibility is not ResultCredibility.CREDIBLE:
        return [
            f'<section id="{_EQUITY_CURVE_SUPPRESSED_ID}">',
            "<h2>Equity curve</h2>",
            '<p class="note">Suppressed. This run is '
            f"{escape(outcome.credibility.value)}, and the curve of a result that has "
            "not passed its audit is a picture with no claim behind it. It is not "
            "elsewhere in this document and it is not recoverable from it; re-run the "
            "audit, do not re-render the report.</p>",
            "</section>",
        ]

    envelope = (
        None
        if inputs.cpcv_distribution is None
        else project_envelope(path=inputs.equity_path, distribution=inputs.cpcv_distribution)
    )
    geometry = chart_geometry(path=inputs.equity_path, envelope=envelope)
    lines = [
        '<section id="equity-curve">',
        "<h2>Equity curve</h2>",
        *_svg(geometry),
    ]
    if geometry.envelope_polygon:
        lines.append(_envelope_caption(inputs))
    else:
        lines.append(f'<p class="note" id="cpcv-not-run">{escape(_cpcv_note(geometry))}</p>')
    lines.append("</section>")
    return lines


def _cpcv_note(geometry: ChartGeometry) -> str:
    return f"CPCV not run: {geometry.envelope_absence_reason}"


def _envelope_caption(inputs: TearsheetInputs) -> str:
    distribution = inputs.cpcv_distribution
    if distribution is None:  # pragma: no cover - the polygon cannot exist without one
        raise AssertionError("an envelope polygon was drawn without a CPCV distribution")
    return (
        '<p class="note" id="cpcv-envelope-caption">'
        f"Shaded band: CPCV Sharpe p05 {escape(f'{distribution.sharpe_p05:f}')} to p95 "
        f"{escape(f'{distribution.sharpe_p95:f}')} across "
        f"{distribution.included_path_total} of {distribution.path_total} paths, "
        "projected at this run's realised annualised volatility as constant drift. It is "
        "not twenty-eight stored equity curves; it is what the paths said about the "
        "Sharpe, drawn in equity terms.</p>"
    )


def _svg(geometry: ChartGeometry) -> list[str]:
    lines = [
        f'<svg id="equity-chart" width="{geometry.width_px}" height="{geometry.height_px}" '
        f'viewBox="0 0 {geometry.width_px} {geometry.height_px}" role="img" '
        f'aria-label="Equity in USD over the backtest window, from '
        f'{escape(f"{geometry.y_floor_usd:f}")} to {escape(f"{geometry.y_ceiling_usd:f}")}">',
    ]
    if geometry.envelope_polygon:
        lines.append(
            f'<polygon id="cpcv-envelope" class="chart-envelope" '
            f'points="{_points(geometry.envelope_polygon)}"></polygon>'
        )
    lines.extend(
        [
            f'<polyline id="equity-line" class="chart-curve" '
            f'points="{_points(geometry.curve)}"></polyline>',
            f'<text x="4" y="20" font-size="11">{escape(f"{geometry.y_ceiling_usd:f}")}</text>',
            f'<text x="4" y="{geometry.height_px - 16}" font-size="11">'
            f"{escape(f'{geometry.y_floor_usd:f}')}</text>",
            "</svg>",
        ]
    )
    return lines


def _points(coordinates: Iterable[tuple[Decimal, Decimal]]) -> str:
    return " ".join(f"{x_px:f},{y_px:f}" for x_px, y_px in coordinates)


def _provenance_section(inputs: TearsheetInputs) -> list[str]:
    lines = [
        '<footer id="provenance">',
        "<h2>Provenance</h2>",
        '<h3 id="provenance-coverage">Data coverage</h3>',
        "<table>",
        "<thead><tr><th>series</th><th>bars</th><th>window</th><th>gaps</th></tr></thead>",
        "<tbody>",
    ]
    # Sorted by label rather than left in the caller's order: the footer is diffed between
    # runs, and a reordered symbol list would show up as a coverage change.
    for series in sorted(inputs.coverage, key=lambda entry: entry.label):
        lines.append(_coverage_row(series))
    lines.extend(["</tbody>", "</table>"])
    lines.extend(_feature_versions(inputs.feature_versions))
    lines.extend(_parameter_set(inputs.parameters))
    lines.extend(
        [
            '<h3 id="provenance-held-out">Held-out period</h3>',
            f'<p id="held-out-status">{escape(inputs.held_out.label)}</p>',
            "</footer>",
        ]
    )
    return lines


def _coverage_row(series: SymbolCoverage) -> str:
    first = "-" if series.first_open_time_utc is None else _moment(series.first_open_time_utc)
    last = "-" if series.last_open_time_utc is None else _moment(series.last_open_time_utc)
    # Every gap range, not a count. A reader who sees "3 gaps" and cannot tell whether they
    # straddle the entry signals has lost the diagnosis, which is the same reason
    # `SymbolCoverage.render` prints them one per line.
    gaps = "no gaps" if series.is_complete else "; ".join(gap.render() for gap in series.gaps)
    return (
        f"<tr><td>{escape(series.label)}</td>"
        f"<td>{series.observed_bar_count}/{series.expected_bar_count}</td>"
        f"<td>{escape(first)} .. {escape(last)}</td>"
        f"<td>{escape(gaps)}</td></tr>"
    )


def _feature_versions(feature_versions: Mapping[str, str]) -> list[str]:
    lines = ['<h3 id="provenance-features">Feature versions</h3>']
    if not feature_versions:
        lines.append(
            '<p id="feature-versions">No feature-store features were declared for this '
            "run; every input came from the bar archive directly.</p>"
        )
        return lines
    rows = tuple((name, feature_versions[name]) for name in sorted(feature_versions))
    lines.append('<table id="feature-versions">')
    lines.append("<tbody>")
    lines.extend(
        f"<tr><th>{escape(name)}</th><td>{escape(version)}</td></tr>" for name, version in rows
    )
    lines.extend(["</tbody>", "</table>"])
    return lines


def _parameter_set(parameters: Mapping[str, Decimal]) -> list[str]:
    lines = ['<h3 id="provenance-parameters">Parameter set</h3>']
    if not parameters:
        lines.append('<p id="parameter-set">This strategy takes no free parameters.</p>')
        return lines
    lines.extend(['<table id="parameter-set">', "<tbody>"])
    # `f"{parameter:f}"` rather than `str()`: it renders the `Decimal` at its own exponent
    # with no scientific notation, so `Decimal("0.00001000")` prints the trailing zeros
    # the caller specified rather than `1E-5`. The trailing zeros are the caller's
    # statement about precision (`docs/rules/decimal-and-money.md`).
    lines.extend(
        f"<tr><th>{escape(name)}</th><td>{escape(f'{parameters[name]:f}')}</td></tr>"
        for name in sorted(parameters)
    )
    lines.extend(["</tbody>", "</table>"])
    return lines


def _definition_table(rows: tuple[tuple[str, str], ...]) -> list[str]:
    return [
        "<table>",
        "<tbody>",
        *(f"<tr><th>{escape(label)}</th><td>{escape(cell)}</td></tr>" for label, cell in rows),
        "</tbody>",
        "</table>",
    ]


def _in_audit_order(findings: Iterable[AuditFinding]) -> tuple[AuditFinding, ...]:
    """Findings in `AUDIT_ORDER`, with any unrecognised check kept at the end.

    The order is the battery's, not the caller's: look-ahead first because it is the only
    defect class that does not fail (`fking.backtest.results`). Sorting rather than
    trusting the incoming order also removes a source of byte-level nondeterminism from a
    document that has to render identically twice.
    """
    ranking = {check: ordinal for ordinal, check in enumerate(AUDIT_ORDER)}
    return tuple(sorted(findings, key=lambda found: ranking.get(found.check, len(ranking))))


def _fraction(value_fraction: Decimal) -> str:
    """A return fraction as a percentage string, at the precision it was given."""
    return f"{value_fraction * Decimal('100'):f}%"


def _moment(moment: datetime) -> str:
    return moment.isoformat()
