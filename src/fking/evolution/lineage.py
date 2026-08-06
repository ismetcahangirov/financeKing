"""The genealogy graph and the three questions it answers that correlation cannot.

**Defect propagation.** When a look-ahead leak is found in a genome, every descendant
carries it. Correlation cannot find them -- a child with an extra rule may correlate
weakly with its parent while sharing the leaking feature exactly -- so the graph is
walked instead. Without it you fix one strategy and leave nine of its children in
production carrying the same bug.

**Genealogical collapse.** Behavioural correlation is estimated over a finite window and
can look perfectly healthy while the population is inbred: the ancestors' shared
assumption has simply not been tested by the sample. If more than 50% of live strategies
share a common ancestor within 5 generations, diversity pressure escalates *regardless of
what the measured correlations say* -- the measurement is the thing under suspicion, so
it does not get a vote.

**Family-adjusted trials.** The 40th mutation of one parent is not an independent
hypothesis, and its family count feeds an additional deflation term.

Everything here is pure: a graph in, an answer out, no clock and no I/O. The store loads
the edges and calls these; that split is what makes a collapse report reproducible from
an archived edge list months later.

`EVOLUTION_ENGINE.md` sections 5.6, 7 and 8.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from fking.evolution._errors import LineageCycleError, LineageError

__all__ = [
    "DEFAULT_COLLAPSE_MAX_GENERATIONS",
    "DEFAULT_COLLAPSE_THRESHOLD_FRACTION",
    "LineageCollapseReport",
    "LineageGraph",
    "lineage_collapse_report",
]

# EVOLUTION_ENGINE.md section 5.6: "more than 50% of live strategies share a common
# ancestor within 5 generations". Both numbers are here, together, because they are one
# rule and separating them is how one of them gets tuned.
DEFAULT_COLLAPSE_MAX_GENERATIONS: Final[int] = 5
DEFAULT_COLLAPSE_THRESHOLD_FRACTION: Final[Decimal] = Decimal("0.5")

# A shared ancestor needs at least two descendants to be shared. Without this floor a
# population of one live strategy reports 100% collapse forever, because every genome is
# its own ancestor at distance zero.
_MIN_SHARING_STRATEGIES: Final[int] = 2


@dataclass(frozen=True, slots=True)
class LineageGraph:
    """Child genome hash -> its parents, in declaration order.

    Acyclicity is checked once here rather than at every walk. Content addressing does
    not prevent a cycle on its own -- a genome's digest covers its own expression, not
    its parents -- so a bad mutation operator can declare an ancestor as a child, and the
    resulting unbounded walk hangs whichever loop is walking it. The quarantine sweep is
    one of those loops.
    """

    parents_of: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        frozen = MappingProxyType(
            {child: tuple(parents) for child, parents in self.parents_of.items()}
        )
        object.__setattr__(self, "parents_of", frozen)
        self._require_acyclic()

    def _require_acyclic(self) -> None:
        """Iterative depth-first search with an explicit stack, reporting the back edge.

        Iterative rather than recursive: a deep lineage is exactly what this graph is for,
        and a `RecursionError` would report the wrong problem at the wrong depth.
        """
        settled: set[str] = set()
        for start in sorted(self.parents_of):
            if start in settled:
                continue
            on_path: list[str] = []
            in_path: set[str] = set()
            # (node, has_been_expanded)
            pending: list[tuple[str, bool]] = [(start, False)]
            while pending:
                node, expanded = pending.pop()
                if expanded:
                    on_path.pop()
                    in_path.discard(node)
                    settled.add(node)
                    continue
                if node in settled:
                    continue
                if node in in_path:
                    cycle = [*on_path[on_path.index(node) :], node]
                    raise LineageCycleError(
                        f"the lineage graph holds a cycle: {' -> '.join(cycle)}"
                    )
                on_path.append(node)
                in_path.add(node)
                pending.append((node, True))
                pending.extend((parent, False) for parent in self.parents_of.get(node, ()))

    def children_of(self, genome_hash: str) -> tuple[str, ...]:
        return tuple(
            sorted(child for child, parents in self.parents_of.items() if genome_hash in parents)
        )

    def ancestors_within(self, genome_hash: str, max_generations: int) -> Mapping[str, int]:
        """Every ancestor reachable in at most `max_generations` steps, with its distance.

        Distance zero is the genome itself, deliberately. "Share a common ancestor" has
        to be true for a parent and its own child, not only for two siblings -- otherwise
        a lineage that collapsed by one strategy simply outliving the rest reports no
        collapse at all.
        """
        if max_generations < 0:
            raise LineageError(f"max_generations cannot be negative, got {max_generations}")
        reached: dict[str, int] = {genome_hash: 0}
        frontier: tuple[str, ...] = (genome_hash,)
        for distance in range(1, max_generations + 1):
            next_frontier: list[str] = []
            for node in frontier:
                for parent in self.parents_of.get(node, ()):
                    if parent not in reached:
                        reached[parent] = distance
                        next_frontier.append(parent)
            if not next_frontier:
                break
            frontier = tuple(next_frontier)
        return MappingProxyType(reached)

    def descendants_of(self, genome_hash: str) -> frozenset[str]:
        """Everything downstream, at unbounded depth, excluding the genome itself.

        Unbounded on purpose, unlike the ancestor walk. This is what a `defect`
        retirement calls to decide what to quarantine, and a depth bound there would
        leave the great-grandchildren of a look-ahead leak in production.
        """
        found: set[str] = set()
        pending: list[str] = [genome_hash]
        while pending:
            node = pending.pop()
            for child in self.children_of(node):
                if child not in found:
                    found.add(child)
                    pending.append(child)
        return frozenset(found)

    def would_create_cycle(self, *, child_genome_hash: str, parent_genome_hash: str) -> bool:
        """True when declaring `parent` a parent of `child` would close a loop.

        Asked before the insert, not after: the acceptance criterion for #83 is that a
        cycle is *rejected by the ancestry walk rather than stored*, and a graph that
        already holds one cannot be walked to discover it.
        """
        if child_genome_hash == parent_genome_hash:
            return True
        return child_genome_hash in self.ancestors_within(
            parent_genome_hash, max_generations=len(self.parents_of) + 1
        )


@dataclass(frozen=True, slots=True)
class LineageCollapseReport:
    """How concentrated the live population's genealogy is, and whether that is a breach.

    `dominant_ancestor_genome_hash` is `None` exactly when no ancestor is shared by at
    least two live strategies -- which is a maximally diverse population, not a missing
    measurement, and the two are distinguished by `live_strategy_count`.
    """

    live_strategy_count: int
    max_generations: int
    threshold_fraction: Decimal
    dominant_ancestor_genome_hash: str | None
    dominant_share_fraction: Decimal
    is_collapsed: bool


def lineage_collapse_report(
    graph: LineageGraph,
    live_genome_hashes: Iterable[str],
    *,
    max_generations: int = DEFAULT_COLLAPSE_MAX_GENERATIONS,
    threshold_fraction: Decimal = DEFAULT_COLLAPSE_THRESHOLD_FRACTION,
) -> LineageCollapseReport:
    """Flag a population in which one ancestor accounts for too much of the live book.

    Strictly greater than the threshold, so exactly 50% is not a collapse: the rule reads
    "more than 50%", and a boundary that flags at the stated limit turns a documented
    number into an off-by-one that somebody eventually "fixes" in the other direction.

    The fraction is a `Decimal` and the division is exact for any population size whose
    denominator divides into 38 significant digits, which every population this engine
    caps at (12 paper, 6 challenger, 6 champion) does. A `float` here would put 0.5
    exactly on the boundary at some sizes and just below it at others.
    """
    live = tuple(dict.fromkeys(live_genome_hashes))
    if not live:
        return LineageCollapseReport(
            live_strategy_count=0,
            max_generations=max_generations,
            threshold_fraction=threshold_fraction,
            dominant_ancestor_genome_hash=None,
            dominant_share_fraction=Decimal(0),
            is_collapsed=False,
        )

    sharers: dict[str, set[str]] = {}
    for descendant in live:
        for ancestor in graph.ancestors_within(descendant, max_generations):
            sharers.setdefault(ancestor, set()).add(descendant)

    shared = {
        ancestor: descendants
        for ancestor, descendants in sharers.items()
        if len(descendants) >= _MIN_SHARING_STRATEGIES
    }
    if not shared:
        return LineageCollapseReport(
            live_strategy_count=len(live),
            max_generations=max_generations,
            threshold_fraction=threshold_fraction,
            dominant_ancestor_genome_hash=None,
            dominant_share_fraction=Decimal(0),
            is_collapsed=False,
        )

    # Sorted by share descending then hash ascending, so a tie resolves to the same
    # ancestor on every machine. A report whose subject depends on dict ordering is a
    # report two operators disagree about while both are looking at the same population.
    dominant = min(shared, key=lambda ancestor: (-len(shared[ancestor]), ancestor))
    share = Decimal(len(shared[dominant])) / Decimal(len(live))
    return LineageCollapseReport(
        live_strategy_count=len(live),
        max_generations=max_generations,
        threshold_fraction=threshold_fraction,
        dominant_ancestor_genome_hash=dominant,
        dominant_share_fraction=share,
        is_collapsed=share > threshold_fraction,
    )
