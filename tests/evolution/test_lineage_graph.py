"""The genealogy graph: ancestry, descendants, cycle refusal, and the collapse report.

The two collapse cases are the acceptance criteria stated as arithmetic: a population in
which 51% of live strategies share an ancestor within 5 generations is flagged, and one
at 49% is not. They are built as explicit chains rather than randomly, because the number
being asserted is the boundary and a generator would occasionally straddle it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

import pytest

from fking.evolution import (
    DEFAULT_COLLAPSE_MAX_GENERATIONS,
    LineageCycleError,
    LineageError,
    LineageGraph,
    lineage_collapse_report,
)

pytestmark = pytest.mark.unit

# One hundred live strategies, so a share is a whole number of percent and the two
# boundary cases below read as the rule they encode rather than as arithmetic.
_POPULATION: Final[int] = 100

# Deeper than any bound the ancestor walk applies, which is the point: a defect must
# reach every descendant however far downstream.
_DEEP_CHAIN: Final[int] = 40


def chain(root: str, depth: int) -> dict[str, tuple[str, ...]]:
    """`root` -> `root.1` -> ... -> `root.depth`, one parent each."""
    edges: dict[str, tuple[str, ...]] = {}
    previous = root
    for step in range(1, depth + 1):
        child = f"{root}.{step}"
        edges[child] = (previous,)
        previous = child
    return edges


def test_ancestors_include_the_genome_itself_at_distance_zero() -> None:
    graph = LineageGraph(chain("a", 3))

    ancestors = graph.ancestors_within("a.3", max_generations=5)

    assert ancestors == {"a.3": 0, "a.2": 1, "a.1": 2, "a": 3}


def test_the_ancestor_walk_stops_at_the_declared_depth() -> None:
    graph = LineageGraph(chain("a", 9))

    ancestors = graph.ancestors_within("a.9", max_generations=DEFAULT_COLLAPSE_MAX_GENERATIONS)

    assert max(ancestors.values()) == DEFAULT_COLLAPSE_MAX_GENERATIONS
    assert "a" not in ancestors


def test_a_crossover_child_reaches_both_parents() -> None:
    graph = LineageGraph({"child": ("mother", "father")})

    assert graph.ancestors_within("child", max_generations=1) == {
        "child": 0,
        "mother": 1,
        "father": 1,
    }


def test_the_shortest_path_wins_when_a_genome_is_reachable_two_ways() -> None:
    """A grandchild of a founder that is also its child is one generation away."""
    graph = LineageGraph({"child": ("founder",), "grandchild": ("child", "founder")})

    assert graph.ancestors_within("grandchild", max_generations=5)["founder"] == 1


def test_descendants_are_unbounded_because_a_defect_reaches_all_of_them() -> None:
    graph = LineageGraph(chain("a", _DEEP_CHAIN))

    assert len(graph.descendants_of("a")) == _DEEP_CHAIN
    assert "a" not in graph.descendants_of("a")


def test_a_cycle_in_the_edge_set_is_refused_at_construction() -> None:
    with pytest.raises(LineageCycleError, match="cycle"):
        LineageGraph({"a": ("b",), "b": ("c",), "c": ("a",)})


def test_a_two_hop_cycle_would_be_created_and_is_detected_before_the_insert() -> None:
    """The database CHECK sees depth one only; this is what sees the rest."""
    graph = LineageGraph({"child": ("parent",), "grandchild": ("child",)})

    assert graph.would_create_cycle(child_genome_hash="parent", parent_genome_hash="grandchild")
    assert not graph.would_create_cycle(child_genome_hash="sibling", parent_genome_hash="parent")


def test_a_self_parent_would_create_a_cycle() -> None:
    graph = LineageGraph({})

    assert graph.would_create_cycle(child_genome_hash="a", parent_genome_hash="a")


def test_a_negative_generation_bound_is_refused() -> None:
    graph = LineageGraph({})

    with pytest.raises(LineageError, match="cannot be negative"):
        graph.ancestors_within("a", max_generations=-1)


def _population(*, descended: int, independent: int) -> tuple[LineageGraph, tuple[str, ...]]:
    """`descended` live strategies within 5 generations of one founder, plus loners.

    The descendants sit at depths 1..5 below `founder`, so every one of them is inside
    the window. The independents are their own founders and share nothing, which is what
    makes the denominator the whole live set and the numerator exactly `descended`.
    """
    edges: dict[str, tuple[str, ...]] = {}
    live: list[str] = []
    for index in range(descended):
        depth = index % DEFAULT_COLLAPSE_MAX_GENERATIONS + 1
        previous = "founder"
        for step in range(1, depth + 1):
            node = f"d{index:03d}.{step}"
            edges[node] = (previous,)
            previous = node
        live.append(previous)
    for index in range(independent):
        node = f"loner{index:03d}"
        edges[node] = ()
        live.append(node)
    return LineageGraph(edges), tuple(live)


def test_fifty_one_percent_sharing_an_ancestor_within_five_generations_is_flagged() -> None:
    graph, live = _population(descended=51, independent=49)

    report = lineage_collapse_report(graph, live)

    assert report.live_strategy_count == _POPULATION
    assert report.dominant_ancestor_genome_hash == "founder"
    assert report.dominant_share_fraction == Decimal("0.51")
    assert report.is_collapsed


def test_forty_nine_percent_sharing_an_ancestor_is_not_flagged() -> None:
    graph, live = _population(descended=49, independent=51)

    report = lineage_collapse_report(graph, live)

    assert report.live_strategy_count == _POPULATION
    assert report.dominant_share_fraction == Decimal("0.49")
    assert not report.is_collapsed


def test_exactly_half_is_not_a_collapse_because_the_rule_says_more_than() -> None:
    graph, live = _population(descended=50, independent=50)

    report = lineage_collapse_report(graph, live)

    assert report.dominant_share_fraction == Decimal("0.5")
    assert not report.is_collapsed


def test_an_ancestor_outside_the_window_does_not_count_toward_a_collapse() -> None:
    """Six generations back is six generations back, however many descendants it has."""
    edges: dict[str, tuple[str, ...]] = {}
    live: list[str] = []
    for index in range(10):
        previous = "ancient"
        for step in range(1, 7):
            node = f"line{index}.{step}"
            edges[node] = (previous,)
            previous = node
        live.append(previous)

    report = lineage_collapse_report(LineageGraph(edges), live)

    assert report.dominant_ancestor_genome_hash != "ancient"
    assert not report.is_collapsed


def test_an_empty_live_population_reports_no_collapse_and_no_ancestor() -> None:
    report = lineage_collapse_report(LineageGraph({}), ())

    assert report.live_strategy_count == 0
    assert report.dominant_ancestor_genome_hash is None
    assert not report.is_collapsed


def test_a_single_live_strategy_is_not_its_own_collapse() -> None:
    """Every genome is its own ancestor at distance zero; one of them is not a family."""
    report = lineage_collapse_report(LineageGraph({}), ("only",))

    assert report.live_strategy_count == 1
    assert report.dominant_ancestor_genome_hash is None
    assert not report.is_collapsed


def test_the_threshold_is_a_parameter_so_a_stricter_gate_can_be_asked_for() -> None:
    graph, live = _population(descended=40, independent=60)

    assert lineage_collapse_report(graph, live, threshold_fraction=Decimal("0.30")).is_collapsed
    assert not lineage_collapse_report(graph, live).is_collapsed


def test_the_report_names_the_same_ancestor_on_every_run() -> None:
    """Two ancestors tied on share must not resolve by dict ordering."""
    edges = {"x1": ("alpha",), "x2": ("alpha",), "y1": ("beta",), "y2": ("beta",)}
    live = ("x1", "x2", "y1", "y2")

    first = lineage_collapse_report(LineageGraph(edges), live)
    shuffled = lineage_collapse_report(LineageGraph(dict(reversed(list(edges.items())))), live)

    assert first.dominant_ancestor_genome_hash == shuffled.dominant_ancestor_genome_hash
    assert first.dominant_share_fraction == Decimal("0.5")
