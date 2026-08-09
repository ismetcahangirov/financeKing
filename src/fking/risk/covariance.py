"""The correlation estimate the risk engine sizes against, and the clusters it groups by.

`RISK_PHILOSOPHY.md` section 5 specifies the pipeline; this module is it. Five decisions
here are load-bearing and none of them are obvious from the formulae.

**The estimate is deliberately pessimistic.** `rho_used_ij = max(rho_ewma_ij, rho_p95_ij)`,
where the second term is the 95th percentile of the rolling 60-day correlation over three
years, supplied by the caller. Correlations rise toward one during exactly the events these
limits exist to survive, so a book sized on today's calm matrix loses its diversification in
the same hour the positions start losing. The cost is permanent, small and ongoing -- a
book slightly under-diversified in calm markets. That trade is worth making every time.

**A missing stress correlation is a refusal, not a fallback to the calm estimate.** The
symbol with no three-year history is the newest listing, which is the one most likely to be
a beta proxy for the majors. Defaulting it to the calm number would apply the floor
everywhere except where it matters most, silently.

**The floor is applied after shrinkage, and the PSD repair after the floor.** Ledoit-Wolf
shrinkage pulls correlations toward their common mean, which lowers the high ones -- run
before the floor it would partially undo it. The repair then only ever raises correlations
(it blends toward a common level chosen to dominate every entry), so the floor survives it
too. That ordering is asserted as a property rather than argued for in a comment alone.

**Positive definiteness is established by Cholesky, not by an eigensolver.** For a
symmetric matrix the two are equivalent, and only one of them is implementable in `Decimal`
without iteration. An EWMA covariance over 60 days across more assets than observations is
singular or near-singular by construction: `w' Sigma w` then comes back slightly negative
from rounding, its square root raises `InvalidOperation` inside the order path, and a size
divided by that `sigma_p` is silently enormous. Neither outcome shows up against a 2x2
matrix.

**Correlations are capped strictly below one.** That cap is not cosmetic: it is what makes
`Sigma` positive definite, which makes `w' Sigma w > 0` for every non-zero weight vector,
which is what makes `CTR_i / sigma_p` defined without a special case in the order path. The
price is that a genuinely perfectly correlated pair is modelled at 0.999 -- 0.1 of a
correlation point of diversification credit that does not exist. `fking.risk.contribution`
depends on this and says so.

Pure throughout: no clock, no I/O, no randomness. Clustering takes no seed because it
contains no randomness -- reproducibility is structural, and `RiskModel.cluster_digest`
is how a nightly run proves it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from types import MappingProxyType
from typing import Final

from fking.domain import DomainError

__all__ = [
    "CLUSTER_CUT_CORRELATION",
    "EWMA_DECAY",
    "MATH_PRECISION",
    "MAX_ESTIMATED_CORRELATION",
    "MINIMUM_OBSERVATIONS",
    "CorrelationCluster",
    "CorrelationMatrix",
    "RiskModel",
    "RiskModelError",
    "cluster_by_correlation",
    "correlation_distance",
    "estimate_risk_model",
    "is_positive_definite",
]

_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")
_HALF: Final = Decimal("0.5")

# Working precision for the matrix arithmetic, set explicitly rather than inherited. The
# identities this module and `fking.risk.contribution` promise -- symmetry, a positive
# smallest pivot, `sum_i CTR_i == sigma_p` -- must not depend on whatever context the
# caller happens to be running under. 50 digits leaves ~12 of headroom over the
# NUMERIC(38, 18) the results are eventually stored at.
MATH_PRECISION: Final[int] = 50

# RiskMetrics' daily decay factor, cited in RISK_PHILOSOPHY.md section 3 for the volatility
# estimator and reused here for the covariance. An effective half-life of ~11 days and an
# effective window of ~33, which is what "60-day EWMA" means in practice.
EWMA_DECAY: Final = Decimal("0.94")

# Below this the 60-day estimate is not being computed at all, it is being extrapolated
# from a fortnight. Not configurable: a shorter window is not a wider confidence interval
# here, it is a covariance matrix whose off-diagonal is mostly noise, and the floor is
# what stops that reaching the order path.
MINIMUM_OBSERVATIONS: Final[int] = 30

# RISK_PHILOSOPHY.md section 5: clusters are cut at rho = 0.7.
CLUSTER_CUT_CORRELATION: Final = Decimal("0.70")

# See the module docstring. The exact value matters less than that it is strictly below
# one: the smallest eigenvalue of the repair target is 1 - this, so 0.999 guarantees a
# positive pivot with three digits of margin at 50 digits of working precision.
MAX_ESTIMATED_CORRELATION: Final = Decimal("0.999")

# A universe of one has no off-diagonal: nothing to average, nothing to shrink toward,
# and nothing to cluster. Named so the two guards below read as the same statement.
_PAIRWISE_MINIMUM: Final[int] = 2

# Blend fractions tried in order when the floored matrix is not positive definite. Coarse
# on purpose: every rung raises correlations and none lowers them, so overshooting costs
# diversification credit and can never grant any. A bisection would find a slightly less
# conservative matrix at the cost of several more O(n^3) Cholesky passes per rebalance,
# which is the wrong side of that trade for a term that is already deliberately pessimistic.
# The last rung is 1, which is the equicorrelation target itself and always succeeds.
_PSD_REPAIR_LADDER: Final[tuple[Decimal, ...]] = (
    Decimal("0"),
    Decimal("0.01"),
    Decimal("0.05"),
    Decimal("0.15"),
    Decimal("0.35"),
    Decimal("0.65"),
    Decimal("1"),
)


class RiskModelError(DomainError):
    """The inputs cannot produce a usable risk model, or the model itself is unusable.

    Distinct from a limit breach, which is a `Rejection` value rather than an exception.
    This is raised only for a model that must not exist -- a matrix that is not positive
    definite, a window too short to estimate from, a pair with no stress floor.
    """


@dataclass(frozen=True, slots=True)
class CorrelationMatrix:
    """A symmetric correlation matrix with a unit diagonal, keyed by symbol.

    Keyed by name rather than indexed by position because every caller here holds symbols,
    and an off-by-one in an index map is a correlation silently attributed to the wrong
    pair -- which produces a plausible number rather than an error. `symbols` still fixes a
    canonical order, so `rows()` is deterministic and the clustering is reproducible.
    """

    symbols: tuple[str, ...]
    entries: Mapping[str, Mapping[str, Decimal]]

    def __post_init__(self) -> None:
        if not self.symbols:
            raise DomainError("a correlation matrix over no symbols is not a matrix")
        if tuple(sorted(self.symbols)) != self.symbols:
            raise DomainError(f"symbols must be sorted for reproducibility, got {self.symbols}")
        if len(set(self.symbols)) != len(self.symbols):
            raise DomainError(f"symbols must be unique, got {self.symbols}")
        for symbol_a in self.symbols:
            row = self.entries.get(symbol_a)
            if row is None:
                raise DomainError(f"no correlation row for {symbol_a}")
            for symbol_b in self.symbols:
                self._validate_entry(row, symbol_a, symbol_b)
        object.__setattr__(
            self,
            "entries",
            MappingProxyType(
                {
                    symbol: MappingProxyType(
                        {other: self.entries[symbol][other] for other in self.symbols}
                    )
                    for symbol in self.symbols
                }
            ),
        )

    def _validate_entry(self, row: Mapping[str, Decimal], symbol_a: str, symbol_b: str) -> None:
        entry = row.get(symbol_b)
        if entry is None:
            raise DomainError(f"no correlation between {symbol_a} and {symbol_b}")
        if not entry.is_finite() or abs(entry) > _ONE:
            raise DomainError(f"correlation {symbol_a}/{symbol_b} is {entry}, outside [-1, 1]")
        if symbol_a == symbol_b and entry != _ONE:
            raise DomainError(f"the diagonal entry for {symbol_a} is {entry}, not 1")
        mirrored = self.entries.get(symbol_b, {}).get(symbol_a)
        if mirrored != entry:
            raise DomainError(
                f"correlation matrix is not symmetric: {symbol_a}/{symbol_b} is {entry} "
                f"and {symbol_b}/{symbol_a} is {mirrored}"
            )

    def between(self, symbol_a: str, symbol_b: str) -> Decimal:
        """The correlation between two symbols. Raises if either is outside the universe."""
        row = self.entries.get(symbol_a)
        if row is None or symbol_b not in row:
            raise DomainError(f"{symbol_a}/{symbol_b} is outside the estimated universe")
        return row[symbol_b]

    def rows(self) -> tuple[tuple[Decimal, ...], ...]:
        """The matrix in `symbols` order, for the numerical routines."""
        return tuple(
            tuple(self.entries[symbol_a][symbol_b] for symbol_b in self.symbols)
            for symbol_a in self.symbols
        )


@dataclass(frozen=True, slots=True)
class CorrelationCluster:
    """A set of symbols that move together above the cut level.

    `cluster_id` is the lexicographically smallest member rather than an ordinal. An
    ordinal would renumber every night as the universe changes, which makes a metric
    series and a stored assignment unjoinable across two consecutive days.
    """

    cluster_id: str
    symbols: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.symbols:
            raise DomainError("an empty cluster cannot exist")
        if tuple(sorted(self.symbols)) != self.symbols:
            raise DomainError(f"cluster members must be sorted, got {self.symbols}")
        if self.cluster_id != self.symbols[0]:
            raise DomainError(
                f"cluster_id {self.cluster_id} is not the smallest member {self.symbols[0]}"
            )


@dataclass(frozen=True, slots=True)
class RiskModel:
    """Everything `RiskEngine.decide()` needs to price portfolio risk, as one value.

    Constructed for a single instant and used for a whole decision. Passing the pieces
    separately would let a volatility read at one moment meet a correlation matrix
    estimated at another, and the resulting `sigma_p` would describe no portfolio that
    ever existed.

    Refuses a matrix that is not positive definite. That check lives here rather than only
    in the estimator so that a model assembled by any other route -- a replay, a fixture, a
    future calibration job -- cannot reach the order path in a state where `sigma_p` is
    imaginary.
    """

    symbols: tuple[str, ...]
    daily_volatility_ratio: Mapping[str, Decimal]
    correlations: CorrelationMatrix
    shrinkage_intensity_ratio: Decimal
    psd_repair_ratio: Decimal
    clusters: tuple[CorrelationCluster, ...]

    def __post_init__(self) -> None:
        if self.symbols != self.correlations.symbols:
            raise DomainError(
                f"model symbols {self.symbols} disagree with the correlation matrix "
                f"{self.correlations.symbols}"
            )
        for symbol in self.symbols:
            volatility = self.daily_volatility_ratio.get(symbol)
            if volatility is None or not volatility.is_finite() or volatility <= _ZERO:
                raise DomainError(
                    f"daily volatility for {symbol} is {volatility}; a symbol with no "
                    f"estimated volatility cannot be sized and must not be assumed calm"
                )
        clustered = sorted(symbol for cluster in self.clusters for symbol in cluster.symbols)
        if clustered != sorted(self.symbols):
            raise DomainError("clusters must partition the universe exactly once")
        if not is_positive_definite(self.correlations.rows()):
            raise RiskModelError(
                "the correlation matrix is not positive definite; sigma_p would be the "
                "square root of a negative number and every risk share derived from it "
                "would be nonsense rather than an error"
            )
        object.__setattr__(
            self, "daily_volatility_ratio", MappingProxyType(dict(self.daily_volatility_ratio))
        )

    def covariance_between(self, symbol_a: str, symbol_b: str) -> Decimal:
        """`Sigma_ab = sigma_a * sigma_b * rho_ab`, in daily ratio units squared."""
        return (
            self.daily_volatility_ratio[symbol_a]
            * self.daily_volatility_ratio[symbol_b]
            * self.correlations.between(symbol_a, symbol_b)
        )

    def cluster_of(self, symbol: str) -> str:
        """The id of the cluster holding `symbol`."""
        for cluster in self.clusters:
            if symbol in cluster.symbols:
                return cluster.cluster_id
        raise DomainError(f"{symbol} is outside the estimated universe")

    @property
    def cluster_digest(self) -> str:
        """A digest over the cluster assignment, for asserting nightly reproducibility.

        Over the assignment alone, not over the matrix: the acceptance criterion is that
        the same data produces the same *clusters*, and a digest that also covered the
        correlations would go red for a change of no operational consequence.
        """
        rendered = ";".join(
            f"{cluster.cluster_id}:{','.join(cluster.symbols)}" for cluster in self.clusters
        )
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def correlation_distance(correlation: Decimal) -> Decimal:
    """`d = sqrt(0.5 * (1 - rho))`, the standard correlation metric.

    A proper distance: for a positive semi-definite correlation matrix it is the Euclidean
    distance between unit vectors, so it obeys the triangle inequality. `1 - rho` does not,
    and hierarchical clustering on a non-metric produces assignments that change with
    linkage order -- which turns a nightly job into a source of unexplained churn.
    """
    if not correlation.is_finite() or abs(correlation) > _ONE:
        raise DomainError(f"correlation {correlation} is outside [-1, 1]")
    with localcontext() as context:
        context.prec = MATH_PRECISION
        return (_HALF * (_ONE - correlation)).sqrt()


def is_positive_definite(rows: Sequence[Sequence[Decimal]]) -> bool:
    """True when the symmetric matrix `rows` admits a Cholesky factorisation.

    Equivalent to a strictly positive smallest eigenvalue, and computable exactly in
    `Decimal`. The factorisation fails on the first non-positive pivot, which is also the
    cheapest possible answer for the near-singular matrices this is called on.
    """
    order = len(rows)
    with localcontext() as context:
        context.prec = MATH_PRECISION
        lower: list[list[Decimal]] = [[_ZERO] * order for _ in range(order)]
        for row_index in range(order):
            for column_index in range(row_index + 1):
                accumulated = sum(
                    (
                        lower[row_index][inner] * lower[column_index][inner]
                        for inner in range(column_index)
                    ),
                    start=_ZERO,
                )
                if row_index == column_index:
                    pivot = rows[row_index][row_index] - accumulated
                    if pivot <= _ZERO:
                        return False
                    lower[row_index][column_index] = pivot.sqrt()
                else:
                    lower[row_index][column_index] = (
                        rows[row_index][column_index] - accumulated
                    ) / lower[column_index][column_index]
    return True


def cluster_by_correlation(
    matrix: CorrelationMatrix, *, cut_correlation: Decimal
) -> tuple[CorrelationCluster, ...]:
    """Average-linkage hierarchical clustering on the correlation distance, cut at a level.

    Average linkage rather than single linkage: single linkage chains, so one pair at 0.71
    can drag two otherwise unrelated groups into one cluster, and the resulting limit binds
    on a grouping nobody can explain. Ties are broken by position in the sorted symbol
    order, so the assignment is a function of the data alone.
    """
    cut_distance = correlation_distance(cut_correlation)
    members: list[list[str]] = [[symbol] for symbol in matrix.symbols]
    with localcontext() as context:
        context.prec = MATH_PRECISION
        distances = [
            [
                correlation_distance(matrix.between(symbol_a, symbol_b))
                for symbol_b in matrix.symbols
            ]
            for symbol_a in matrix.symbols
        ]
        while len(members) > 1:
            merged = _closest_pair(distances)
            if distances[merged[0]][merged[1]] > cut_distance:
                break
            _merge_clusters(members, distances, merged)

    clusters = sorted((sorted(group) for group in members), key=lambda group: group[0])
    return tuple(
        CorrelationCluster(cluster_id=group[0], symbols=tuple(group)) for group in clusters
    )


def _closest_pair(distances: Sequence[Sequence[Decimal]]) -> tuple[int, int]:
    """The (row, column) of the smallest inter-cluster distance, lowest indices winning.

    Strict `<`, so a tie is resolved by position in the sorted symbol order rather than by
    iteration accident. Called only with two or more clusters, which is what makes (0, 1)
    a valid starting pair.
    """
    closest = (0, 1)
    smallest = distances[0][1]
    for row_index in range(len(distances)):
        for column_index in range(row_index + 1, len(distances)):
            if distances[row_index][column_index] < smallest:
                closest = (row_index, column_index)
                smallest = distances[row_index][column_index]
    return closest


def _merge_clusters(
    members: list[list[str]], distances: list[list[Decimal]], pair: tuple[int, int]
) -> None:
    """Fold the second cluster of `pair` into the first, UPGMA-averaging the distances."""
    first, second = pair
    first_weight = Decimal(len(members[first]))
    second_weight = Decimal(len(members[second]))
    total_weight = first_weight + second_weight
    for other in range(len(members)):
        if other in pair:
            continue
        blended = (
            first_weight * distances[first][other] + second_weight * distances[second][other]
        ) / total_weight
        distances[first][other] = blended
        distances[other][first] = blended
    members[first] = members[first] + members[second]
    del members[second]
    del distances[second]
    for row in distances:
        del row[second]


def estimate_risk_model(
    *,
    daily_return_ratio_by_symbol: Mapping[str, Sequence[Decimal]],
    stress_correlation_by_pair: Mapping[str, Mapping[str, Decimal]],
    cluster_cut_correlation: Decimal = CLUSTER_CUT_CORRELATION,
) -> RiskModel:
    """Estimate the model the risk engine sizes against, from point-in-time returns.

    `daily_return_ratio_by_symbol` holds aligned histories -- observation `t` is the same
    day for every symbol, oldest first. Alignment is the caller's responsibility and is
    checked here only for equal length, because a covariance between two series that are
    off by a day is a number this function cannot detect and cannot refuse.

    Returns are used with a zero mean, which is RiskMetrics' convention and not an
    oversight: over 60 days the sample mean of a daily return is dominated by volatility,
    so subtracting it removes more signal than bias.
    """
    symbols, series = _validated_series(daily_return_ratio_by_symbol)
    floors = _validated_stress_floors(symbols, stress_correlation_by_pair)

    with localcontext() as context:
        context.prec = MATH_PRECISION
        weights = _ewma_weights(len(series[0]))
        covariance = _ewma_covariance(series, weights)
        volatilities = _volatilities(symbols, covariance)
        sample = _sample_correlations(covariance, volatilities)
        mean_correlation = _mean_off_diagonal(sample)
        intensity = _shrinkage_intensity(
            series=series,
            weights=weights,
            covariance=covariance,
            volatilities=volatilities,
            mean_correlation=mean_correlation,
        )
        shrunk = _shrunk_correlations(sample, mean_correlation, intensity)
        floored = _floored_correlations(symbols, shrunk, floors)
        repaired, repair_ratio = _repaired_to_positive_definite(floored)

    matrix = CorrelationMatrix(
        symbols=symbols,
        entries={
            symbol_a: {
                symbol_b: repaired[row_index][column_index]
                for column_index, symbol_b in enumerate(symbols)
            }
            for row_index, symbol_a in enumerate(symbols)
        },
    )
    return RiskModel(
        symbols=symbols,
        daily_volatility_ratio=dict(zip(symbols, volatilities, strict=True)),
        correlations=matrix,
        shrinkage_intensity_ratio=intensity,
        psd_repair_ratio=repair_ratio,
        clusters=cluster_by_correlation(matrix, cut_correlation=cluster_cut_correlation),
    )


def _validated_series(
    daily_return_ratio_by_symbol: Mapping[str, Sequence[Decimal]],
) -> tuple[tuple[str, ...], tuple[tuple[Decimal, ...], ...]]:
    """Sorted symbols and their histories, refused unless every window is usable."""
    if not daily_return_ratio_by_symbol:
        raise RiskModelError("no return series supplied; there is no universe to estimate")
    symbols = tuple(sorted(daily_return_ratio_by_symbol))
    lengths = {len(daily_return_ratio_by_symbol[symbol]) for symbol in symbols}
    if len(lengths) != 1:
        raise RiskModelError(
            f"return series have differing lengths {sorted(lengths)}; an unaligned "
            f"covariance is a correlation between two different days"
        )
    observation_count = lengths.pop()
    if observation_count < MINIMUM_OBSERVATIONS:
        raise RiskModelError(
            f"{observation_count} observations is below the {MINIMUM_OBSERVATIONS} "
            f"minimum; the off-diagonal of a shorter window is mostly noise"
        )
    for symbol in symbols:
        for observation in daily_return_ratio_by_symbol[symbol]:
            if not observation.is_finite():
                raise RiskModelError(f"{symbol} has a non-finite return observation")
    return symbols, tuple(tuple(daily_return_ratio_by_symbol[symbol]) for symbol in symbols)


def _validated_stress_floors(
    symbols: tuple[str, ...], stress_correlation_by_pair: Mapping[str, Mapping[str, Decimal]]
) -> dict[tuple[str, str], Decimal]:
    """The p95 floor for every unordered pair, refused if any is missing or disagrees."""
    floors: dict[tuple[str, str], Decimal] = {}
    for first_index, symbol_a in enumerate(symbols):
        for symbol_b in symbols[first_index + 1 :]:
            forward = stress_correlation_by_pair.get(symbol_a, {}).get(symbol_b)
            reverse = stress_correlation_by_pair.get(symbol_b, {}).get(symbol_a)
            if forward is None and reverse is None:
                raise RiskModelError(
                    f"no stress correlation for {symbol_a}/{symbol_b}; the floor is the "
                    f"whole point of this estimate and must not fall back to the calm one"
                )
            if forward is not None and reverse is not None and forward != reverse:
                raise RiskModelError(
                    f"stress correlation for {symbol_a}/{symbol_b} disagrees with itself: "
                    f"{forward} against {reverse}"
                )
            floor = forward if forward is not None else reverse
            if floor is None or not floor.is_finite() or abs(floor) > _ONE:
                raise RiskModelError(
                    f"stress correlation for {symbol_a}/{symbol_b} is {floor}, outside [-1, 1]"
                )
            floors[symbol_a, symbol_b] = floor
    return floors


def _ewma_weights(observation_count: int) -> tuple[Decimal, ...]:
    """Normalised exponential weights, oldest first, summing to exactly one."""
    raw = tuple(EWMA_DECAY ** (observation_count - 1 - index) for index in range(observation_count))
    total = sum(raw, start=_ZERO)
    return tuple(weight / total for weight in raw)


def _ewma_covariance(
    series: Sequence[Sequence[Decimal]], weights: Sequence[Decimal]
) -> list[list[Decimal]]:
    """The exponentially weighted covariance about a zero mean."""
    order = len(series)
    covariance = [[_ZERO] * order for _ in range(order)]
    for row_index in range(order):
        for column_index in range(row_index, order):
            entry = sum(
                (
                    weight * series[row_index][step] * series[column_index][step]
                    for step, weight in enumerate(weights)
                ),
                start=_ZERO,
            )
            covariance[row_index][column_index] = entry
            covariance[column_index][row_index] = entry
    return covariance


def _volatilities(
    symbols: tuple[str, ...], covariance: Sequence[Sequence[Decimal]]
) -> tuple[Decimal, ...]:
    """Per-symbol daily volatility, refusing a series that did not move.

    A zero-variance 60-day window is a halted or delisted symbol, not a riskless one.
    Sizing it would divide by zero to get its correlations and then treat it as free
    capacity, which is the most dangerous possible reading of "it has not moved".
    """
    volatilities: list[Decimal] = []
    for index, symbol in enumerate(symbols):
        variance = covariance[index][index]
        if variance <= _ZERO:
            raise RiskModelError(
                f"{symbol} has zero estimated variance over the window; a symbol that "
                f"did not move is halted, not riskless"
            )
        volatilities.append(variance.sqrt())
    return tuple(volatilities)


def _sample_correlations(
    covariance: Sequence[Sequence[Decimal]], volatilities: Sequence[Decimal]
) -> list[list[Decimal]]:
    """The sample correlation matrix, clamped into [-1, 1] against rounding."""
    order = len(volatilities)
    correlations = [[_ONE] * order for _ in range(order)]
    for row_index in range(order):
        for column_index in range(row_index + 1, order):
            entry = covariance[row_index][column_index] / (
                volatilities[row_index] * volatilities[column_index]
            )
            entry = max(-_ONE, min(_ONE, entry))
            correlations[row_index][column_index] = entry
            correlations[column_index][row_index] = entry
    return correlations


def _mean_off_diagonal(correlations: Sequence[Sequence[Decimal]]) -> Decimal:
    """The average pairwise correlation -- the constant-correlation shrinkage target."""
    order = len(correlations)
    if order < _PAIRWISE_MINIMUM:
        return _ZERO
    total = sum(
        (
            correlations[row_index][column_index]
            for row_index in range(order)
            for column_index in range(row_index + 1, order)
        ),
        start=_ZERO,
    )
    pair_count = Decimal(order * (order - 1) // 2)
    return total / pair_count


def _shrinkage_intensity(
    *,
    series: Sequence[Sequence[Decimal]],
    weights: Sequence[Decimal],
    covariance: Sequence[Sequence[Decimal]],
    volatilities: Sequence[Decimal],
    mean_correlation: Decimal,
) -> Decimal:
    """Ledoit-Wolf's optimal intensity toward the constant-correlation target.

    Ledoit and Wolf (2004), "Honey, I Shrunk the Sample Covariance Matrix", equation 2 with
    the constant-correlation target of section 3. Two adaptations, both stated because
    neither is in the paper:

    * the moments are exponentially weighted rather than equally weighted, because the
      covariance being shrunk is the EWMA one and shrinking an EWMA estimate by an
      intensity calibrated on a flat window would mismatch the two;
    * `T` in `delta = kappa / T` becomes Kish's effective sample size `1 / sum(w^2)`, which
      is what an exponentially weighted window has instead of an observation count.

    Clamped into [0, 1]. The unclamped ratio is a quotient of two noisy estimates and
    leaves that interval routinely on short windows; an intensity above one extrapolates
    past the target and can produce a negative variance.
    """
    order = len(series)
    if order < _PAIRWISE_MINIMUM:
        return _ZERO

    dispersion = _ZERO
    for row_index in range(order):
        for column_index in range(order):
            if row_index == column_index:
                continue
            target_entry = mean_correlation * volatilities[row_index] * volatilities[column_index]
            difference = target_entry - covariance[row_index][column_index]
            dispersion += difference * difference
    if dispersion <= _ZERO:
        # The sample already is the target; there is nothing to shrink toward.
        return _ZERO

    estimation_error = _ZERO
    asymptotic = [[_ZERO] * order for _ in range(order)]
    for row_index in range(order):
        for column_index in range(order):
            centred_sum = _ZERO
            cross_sum = _ZERO
            for step, weight in enumerate(weights):
                product = series[row_index][step] * series[column_index][step]
                deviation = product - covariance[row_index][column_index]
                centred_sum += weight * deviation * deviation
                own = series[row_index][step] * series[row_index][step]
                cross_sum += weight * (own - covariance[row_index][row_index]) * deviation
            estimation_error += centred_sum
            asymptotic[row_index][column_index] = cross_sum

    covariance_of_target = _ZERO
    for row_index in range(order):
        covariance_of_target += asymptotic[row_index][row_index]
    for row_index in range(order):
        for column_index in range(order):
            if row_index == column_index:
                continue
            ratio = volatilities[column_index] / volatilities[row_index]
            covariance_of_target += (
                mean_correlation
                * _HALF
                * (
                    ratio * asymptotic[row_index][column_index]
                    + asymptotic[column_index][row_index] / ratio
                )
            )

    effective_observations = _ONE / sum((weight * weight for weight in weights), start=_ZERO)
    intensity = (estimation_error - covariance_of_target) / dispersion / effective_observations
    return max(_ZERO, min(_ONE, intensity))


def _shrunk_correlations(
    sample: Sequence[Sequence[Decimal]], mean_correlation: Decimal, intensity: Decimal
) -> list[list[Decimal]]:
    """Shrink toward constant correlation.

    Shrinking the covariance toward `F_ij = rho_bar * sigma_i * sigma_j` with `F_ii = S_ii`
    leaves the diagonal untouched, so the shrunk *correlation* is exactly the convex blend
    below and the volatilities are unchanged. Doing it in correlation space keeps the unit
    diagonal exact rather than approximately one after a divide.
    """
    order = len(sample)
    shrunk = [[_ONE] * order for _ in range(order)]
    for row_index in range(order):
        for column_index in range(row_index + 1, order):
            entry = (
                intensity * mean_correlation + (_ONE - intensity) * sample[row_index][column_index]
            )
            shrunk[row_index][column_index] = entry
            shrunk[column_index][row_index] = entry
    return shrunk


def _floored_correlations(
    symbols: tuple[str, ...],
    shrunk: Sequence[Sequence[Decimal]],
    floors: Mapping[tuple[str, str], Decimal],
) -> list[list[Decimal]]:
    """`rho_used = max(rho_shrunk, rho_p95)`, capped strictly below one."""
    order = len(symbols)
    floored = [[_ONE] * order for _ in range(order)]
    for row_index in range(order):
        for column_index in range(row_index + 1, order):
            floor = floors[symbols[row_index], symbols[column_index]]
            entry = max(shrunk[row_index][column_index], floor)
            entry = max(-MAX_ESTIMATED_CORRELATION, min(MAX_ESTIMATED_CORRELATION, entry))
            floored[row_index][column_index] = entry
            floored[column_index][row_index] = entry
    return floored


def _repaired_to_positive_definite(
    floored: Sequence[Sequence[Decimal]],
) -> tuple[list[list[Decimal]], Decimal]:
    """Blend toward equicorrelation until Cholesky succeeds, and report how far it went.

    The target level is the largest off-diagonal entry, floored at zero, so the target
    dominates every entry elementwise and the blend can only *raise* correlations. That is
    what keeps the stress floor intact through the repair, and it is also the conservative
    direction: a repaired matrix credits less diversification than the floored one, never
    more. The target itself is positive definite with smallest eigenvalue
    `1 - MAX_ESTIMATED_CORRELATION`, so the last rung of the ladder always succeeds.
    """
    order = len(floored)
    if order == 1:
        return [[_ONE]], _ZERO
    target_level = max(
        (
            floored[row_index][column_index]
            for row_index in range(order)
            for column_index in range(row_index + 1, order)
        ),
        default=_ZERO,
    )
    target_level = max(target_level, _ZERO)

    for blend in _PSD_REPAIR_LADDER:
        candidate = [
            [
                _ONE
                if row_index == column_index
                else (_ONE - blend) * floored[row_index][column_index] + blend * target_level
                for column_index in range(order)
            ]
            for row_index in range(order)
        ]
        if is_positive_definite(candidate):
            return candidate, blend

    raise RiskModelError(  # pragma: no cover - the last rung is the target, which is PD
        "no blend toward the equicorrelation target produced a positive definite matrix"
    )
