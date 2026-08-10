"""Chart geometry: an equity curve and the CPCV band behind it, as plain coordinates.

No chart library, at render time or ever. The output of this module is a tuple of
`(x, y)` pairs that `_render` writes into an inline `<svg>`, because a tearsheet that
needs a CDN is a tearsheet you cannot read during the incident that made you open it.

**The band is a projection of what the paths said, not twenty-eight stored equity
curves.** CPCV records one Sharpe per path (`fking.backtest.cpcv.PathPerformance`), not
one equity curve per path, so there is nothing to draw directly. What is drawn is the
terminal-return line a run of Sharpe p05 (respectively p95) would have traced at *this*
run's realised volatility:

    return_q(t) = sharpe_q * annualised_volatility * years_elapsed(t)

Constant drift, because that is exactly what a Sharpe ratio states -- annualised excess
return divided by annualised volatility -- and anything shaped more interestingly would
be borrowing the realised curve's own shape and presenting it as evidence from the
paths. The document labels the band with this derivation rather than leaving a reader to
assume the band is twenty-eight overlaid histories.

**The arithmetic runs in an explicit `Decimal` context.** The process-wide context is set
at bootstrap (`docs/rules/decimal-and-money.md`), and a tearsheet rendered in a process
that never bootstrapped would otherwise compute its coordinates at a different precision
and produce different bytes from the same stored result. Byte-identity is an acceptance
criterion, so the precision is pinned here rather than inherited.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Context, Decimal, localcontext
from typing import Final

from fking.backtest.cpcv import PathDistribution
from fking.backtest.portfolio import ANNUALISATION_DAYS, EquityPath, MetricInputError, risk_profile

__all__ = [
    "CHART_HEIGHT_PX",
    "CHART_WIDTH_PX",
    "ChartGeometry",
    "EnvelopeProjection",
    "chart_geometry",
    "project_envelope",
]

# Fixed, not responsive. The document is compared byte-for-byte between renders and
# pasted into incident notes; a viewport-dependent chart would make two readers of the
# same artefact see two different pictures.
CHART_WIDTH_PX: Final[int] = 960
CHART_HEIGHT_PX: Final[int] = 320
_PADDING_LEFT_PX: Final[Decimal] = Decimal("64")
_PADDING_RIGHT_PX: Final[Decimal] = Decimal("16")
_PADDING_TOP_PX: Final[Decimal] = Decimal("16")
_PADDING_BOTTOM_PX: Final[Decimal] = Decimal("28")

# Enough digits that the quantised pixel coordinate below is never the tie-break, and few
# enough that the whole computation stays exact in the cases that matter.
_PROJECTION_PRECISION: Final[int] = 34

# Sub-hundredth-of-a-pixel differences are not visible and not worth carrying into a
# byte-comparison.
_PIXEL_QUANTUM: Final[Decimal] = Decimal("0.01")
_USD_QUANTUM: Final[Decimal] = Decimal("0.01")

# One fiftieth of the plotted span, top and bottom, so the extreme point is not welded to
# the frame edge.
_VERTICAL_PADDING_DIVISOR: Final[Decimal] = Decimal("50")

# The span a flat curve is given so the line lands mid-frame rather than dividing by zero.
_FLAT_CURVE_SPAN_USD: Final[Decimal] = Decimal("1")

_ZERO: Final[Decimal] = Decimal("0")
_ONE: Final[Decimal] = Decimal("1")


@dataclass(frozen=True, slots=True)
class EnvelopeProjection:
    """The p05 and p95 equity bounds, one pair per point of the realised path.

    `annualised_volatility_fraction` travels with the bounds because it is half of the
    derivation: the same Sharpe quantiles projected at a different volatility describe a
    different band, and a reader who cannot see the volatility cannot check the band.
    """

    lower_usd: tuple[Decimal, ...]
    upper_usd: tuple[Decimal, ...]
    sharpe_p05: Decimal
    sharpe_p95: Decimal
    annualised_volatility_fraction: Decimal
    included_path_total: int
    path_total: int


@dataclass(frozen=True, slots=True)
class ChartGeometry:
    """Pixel coordinates for the curve, the band, and the labels around them.

    `envelope_polygon` is empty when no band is drawn, and `envelope_absence_reason` says
    why in words a reader can act on. The two are never both empty and never both full.
    """

    curve: tuple[tuple[Decimal, Decimal], ...]
    envelope_polygon: tuple[tuple[Decimal, Decimal], ...]
    envelope_absence_reason: str
    y_floor_usd: Decimal
    y_ceiling_usd: Decimal
    width_px: int = CHART_WIDTH_PX
    height_px: int = CHART_HEIGHT_PX


def project_envelope(
    *, path: EquityPath, distribution: PathDistribution
) -> EnvelopeProjection | str:
    """The p05--p95 band in equity terms, or the reason it cannot be drawn.

    Returns a string rather than `None` on failure so the caller has something to print.
    A band that is absent for a stated reason is a finding; a band that is absent for no
    stated reason is a missing element, which is what this whole feature exists to avoid.
    """
    try:
        volatility = risk_profile(path.daily_return_fractions).annualised_volatility_fraction
    except MetricInputError as too_short:
        return (
            f"CPCV ran ({distribution.included_path_total} of {distribution.path_total} "
            f"paths admitted) but the band cannot be projected: {too_short}"
        )

    if volatility <= _ZERO:
        return (
            f"CPCV ran ({distribution.included_path_total} of {distribution.path_total} "
            f"paths admitted) but this run's realised volatility is {volatility}, so a "
            f"Sharpe quantile projects to no equity band at all"
        )

    with localcontext(Context(prec=_PROJECTION_PRECISION)):
        opening_equity_usd = path.starting_equity_usd
        lower: list[Decimal] = []
        upper: list[Decimal] = []
        for ordinal in range(len(path.points)):
            years_elapsed = Decimal(ordinal) / Decimal(ANNUALISATION_DAYS)
            drift = volatility * years_elapsed
            lower.append(opening_equity_usd * (_ONE + distribution.sharpe_p05 * drift))
            upper.append(opening_equity_usd * (_ONE + distribution.sharpe_p95 * drift))

    return EnvelopeProjection(
        lower_usd=tuple(bound.quantize(_USD_QUANTUM) for bound in lower),
        upper_usd=tuple(bound.quantize(_USD_QUANTUM) for bound in upper),
        sharpe_p05=distribution.sharpe_p05,
        sharpe_p95=distribution.sharpe_p95,
        annualised_volatility_fraction=volatility,
        included_path_total=distribution.included_path_total,
        path_total=distribution.path_total,
    )


def chart_geometry(*, path: EquityPath, envelope: EnvelopeProjection | str | None) -> ChartGeometry:
    """Map one equity path, and any band behind it, onto the fixed pixel frame.

    `envelope` is the projection to draw, the reason there is none, or `None` when CPCV
    did not run at all -- the three states the document distinguishes.
    """
    equity_series = tuple(point.equity_usd for point in path.points)
    band = envelope if isinstance(envelope, EnvelopeProjection) else None

    plotted: list[Decimal] = list(equity_series)
    if band is not None:
        plotted.extend(band.lower_usd)
        plotted.extend(band.upper_usd)

    with localcontext(Context(prec=_PROJECTION_PRECISION)):
        floor_usd, ceiling_usd = _vertical_domain(plotted)
        curve = tuple(
            _to_pixels(ordinal, len(equity_series), equity_usd, floor_usd, ceiling_usd)
            for ordinal, equity_usd in enumerate(equity_series)
        )
        polygon: tuple[tuple[Decimal, Decimal], ...] = ()
        if band is not None:
            forward = [
                _to_pixels(ordinal, len(band.upper_usd), bound, floor_usd, ceiling_usd)
                for ordinal, bound in enumerate(band.upper_usd)
            ]
            backward = [
                _to_pixels(ordinal, len(band.lower_usd), bound, floor_usd, ceiling_usd)
                for ordinal, bound in reversed(tuple(enumerate(band.lower_usd)))
            ]
            polygon = tuple(forward + backward)

    return ChartGeometry(
        curve=curve,
        envelope_polygon=polygon,
        envelope_absence_reason="" if band is not None else _absence_reason(envelope),
        y_floor_usd=floor_usd.quantize(_USD_QUANTUM),
        y_ceiling_usd=ceiling_usd.quantize(_USD_QUANTUM),
    )


def _absence_reason(envelope: EnvelopeProjection | str | None) -> str:
    if isinstance(envelope, str):
        return envelope
    return (
        "CPCV not run for this configuration, so there is no path envelope behind the "
        "curve. A single equity curve is one draw from a distribution nobody sampled"
    )


def _vertical_domain(plotted: list[Decimal]) -> tuple[Decimal, Decimal]:
    """The equity range the frame spans, padded so no point sits on the border."""
    floor_usd = min(plotted)
    ceiling_usd = max(plotted)
    span = ceiling_usd - floor_usd
    if span == _ZERO:
        # A perfectly flat curve. Centre it rather than dividing by a zero span; the
        # alternative is a chart that renders as a crash for the one run that never
        # traded.
        return floor_usd - _FLAT_CURVE_SPAN_USD, ceiling_usd + _FLAT_CURVE_SPAN_USD
    padding = span / _VERTICAL_PADDING_DIVISOR
    return floor_usd - padding, ceiling_usd + padding


def _to_pixels(
    ordinal: int,
    point_total: int,
    equity_usd: Decimal,
    floor_usd: Decimal,
    ceiling_usd: Decimal,
) -> tuple[Decimal, Decimal]:
    inner_width = Decimal(CHART_WIDTH_PX) - _PADDING_LEFT_PX - _PADDING_RIGHT_PX
    inner_height = Decimal(CHART_HEIGHT_PX) - _PADDING_TOP_PX - _PADDING_BOTTOM_PX
    horizontal_span = Decimal(max(point_total - 1, 1))
    x_px = _PADDING_LEFT_PX + inner_width * Decimal(ordinal) / horizontal_span
    y_fraction = (ceiling_usd - equity_usd) / (ceiling_usd - floor_usd)
    y_px = _PADDING_TOP_PX + inner_height * y_fraction
    return x_px.quantize(_PIXEL_QUANTUM), y_px.quantize(_PIXEL_QUANTUM)
