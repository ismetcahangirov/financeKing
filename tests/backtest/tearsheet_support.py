"""Builders and a parser shared by the tearsheet suites.

No tests of its own. `ElementIndex` is a `html.parser`-based reader rather than a regular
expression, because the acceptance criterion for suppression is that the *element* is
absent -- a grep for the string `equity-curve` would also match the suppression notice
that must be present, and a grep is exactly the "eyeballing it" the issue rules out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from typing import Final

from fking.backtest.cpcv import PathDistribution
from fking.backtest.feed import CoverageGap, PartitionFormat, SymbolCoverage
from fking.backtest.portfolio import EquityPath, EquityPoint
from fking.backtest.results import BacktestResult
from fking.backtest.tearsheet import EngineBuild, HeldOutStatus, TearsheetInputs
from fking.data.format_resolver import EpochUnit, Market
from tests.backtest.results_support import result_for

ENGINE_SHA: Final[str] = "0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"
EQUITY_START_UTC: Final[datetime] = datetime(2026, 1, 1, tzinfo=UTC)
POINT_TOTAL: Final[int] = 40


def equity_path(
    *, point_total: int = POINT_TOTAL, daily_growth: Decimal = Decimal("1.004")
) -> EquityPath:
    """A contiguous daily curve that alternates its step, so it is not a straight line."""
    equity_usd = Decimal("10000")
    points: list[EquityPoint] = []
    for ordinal in range(point_total):
        points.append(
            EquityPoint(
                as_of_utc=EQUITY_START_UTC + timedelta(days=ordinal),
                equity_usd=equity_usd,
                is_in_market=ordinal % 3 != 0,
                regime="trend",
            )
        )
        step = daily_growth if ordinal % 2 == 0 else Decimal("2") - daily_growth
        equity_usd = (equity_usd * step).quantize(Decimal("0.01"))
    return EquityPath(points=tuple(points))


def flat_equity_path(*, point_total: int = 4) -> EquityPath:
    """A curve that never moves: the degenerate vertical domain."""
    return EquityPath(
        points=tuple(
            EquityPoint(
                as_of_utc=EQUITY_START_UTC + timedelta(days=ordinal),
                equity_usd=Decimal("10000"),
                is_in_market=False,
                regime="chop",
            )
            for ordinal in range(point_total)
        )
    )


def path_distribution(
    *, sharpe_p05: Decimal = Decimal("-0.9"), sharpe_p95: Decimal = Decimal("3.0")
) -> PathDistribution:
    """A CPCV distribution shaped like the one issue #45 quotes: -0.9 to 3.0."""
    return PathDistribution(
        path_total=28,
        included_path_total=26,
        insufficient_path_total=2,
        insufficient_path_indices=(11, 19),
        sharpe_mean=Decimal("0.8"),
        sharpe_p05=sharpe_p05,
        sharpe_p95=sharpe_p95,
        sharpe_spread=sharpe_p95 - sharpe_p05,
        fraction_of_paths_positive=Decimal("0.65"),
        trade_counts=tuple(40 + ordinal for ordinal in range(28)),
    )


def coverage() -> tuple[SymbolCoverage, ...]:
    """Two series, one complete and one with a gap, so both branches render."""
    return (
        SymbolCoverage(
            market=Market.FUTURES_UM,
            symbol="ETHUSDT",
            observed_bar_count=57_600,
            expected_bar_count=57_600,
            first_open_time_utc=EQUITY_START_UTC,
            last_open_time_utc=EQUITY_START_UTC + timedelta(days=40),
            gaps=(),
            partition_formats=(
                PartitionFormat(year_month="2026-01", epoch_unit=EpochUnit.MILLISECONDS),
            ),
        ),
        SymbolCoverage(
            market=Market.SPOT,
            symbol="BTCUSDT",
            observed_bar_count=57_580,
            expected_bar_count=57_600,
            first_open_time_utc=EQUITY_START_UTC,
            last_open_time_utc=EQUITY_START_UTC + timedelta(days=40),
            gaps=(
                CoverageGap(
                    gap_start_utc=datetime(2026, 1, 9, 3, 0, tzinfo=UTC),
                    gap_end_utc=datetime(2026, 1, 9, 3, 20, tzinfo=UTC),
                    missing_bar_count=20,
                ),
            ),
            partition_formats=(
                PartitionFormat(year_month="2026-01", epoch_unit=EpochUnit.MICROSECONDS),
            ),
        ),
    )


def inputs_for(  # noqa: PLR0913 - one keyword per field a tearsheet suite commonly varies
    *,
    backtest_result: BacktestResult | None = None,
    distribution: PathDistribution | None = None,
    path: EquityPath | None = None,
    parameters: dict[str, Decimal] | None = None,
    feature_versions: dict[str, str] | None = None,
    held_out: HeldOutStatus | None = None,
    is_working_tree_dirty: bool = False,
    coverage_series: tuple[SymbolCoverage, ...] | None = None,
) -> TearsheetInputs:
    return TearsheetInputs(
        backtest_result=backtest_result if backtest_result is not None else result_for(),
        engine=EngineBuild(git_sha=ENGINE_SHA, is_working_tree_dirty=is_working_tree_dirty),
        equity_path=path if path is not None else equity_path(),
        coverage=coverage_series if coverage_series is not None else coverage(),
        parameters=(
            parameters
            if parameters is not None
            else {"breakout_atr_multiple": Decimal("2.50"), "lookback_bars": Decimal("96")}
        ),
        feature_versions=(
            feature_versions
            if feature_versions is not None
            else {"atr_14": "v3", "realised_volatility_24h": "v1"}
        ),
        held_out=(
            held_out
            if held_out is not None
            else HeldOutStatus(start=date(2026, 6, 1), end=date(2026, 8, 1), is_burned=False)
        ),
        cpcv_distribution=distribution,
    )


@dataclass
class ElementIndex(HTMLParser):
    """Every element the document opens, in document order, with its attributes."""

    opened: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(convert_charrefs=True)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.opened.append((tag, {name: (found or "") for name, found in attrs}))

    @property
    def element_ids(self) -> list[str]:
        """The `id` of every element that carries one, in document order."""
        return [attributes["id"] for _, attributes in self.opened if attributes.get("id", "") != ""]

    def tags_named(self, tag: str) -> list[dict[str, str]]:
        return [attributes for found, attributes in self.opened if found == tag]

    def by_id(self, element_id: str) -> tuple[str, dict[str, str]] | None:
        for tag, attributes in self.opened:
            if attributes.get("id") == element_id:
                return tag, attributes
        return None


def parse(document: str) -> ElementIndex:
    """Read a rendered tearsheet into an element index."""
    index = ElementIndex()
    index.feed(document)
    index.close()
    return index
