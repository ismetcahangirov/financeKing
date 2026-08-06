"""A strategy built from a caller-supplied specification, for testing the registry.

The registry's whole job is to refuse specifications, so the tests need specifications the
shipped strategies cannot express -- one requiring a feature nobody registered, one whose
warm-up is shorter than its deepest lookback, one that emits a level its own declared rule
does not produce. Constructing those through `TrailingReturnContinuation` is impossible by
design: its spec is assembled from constants and bounded parameters.

This is not a mock. It implements the same `Strategy` protocol the real one does, against
the real types, and `mypy --strict` checks it -- so a change to the protocol breaks it in
the same way it would break a shipped strategy.
"""

from __future__ import annotations

from fking.domain import Bar, Signal
from fking.strategy import Clock, StrategySpec, StrategyState

__all__ = ["ScriptedStrategy", "SilentStrategy"]


class SilentStrategy:
    """Carries any specification and never emits. Used to test registration alone."""

    __slots__ = ("_spec",)

    def __init__(self, spec: StrategySpec) -> None:
        self._spec = spec

    @property
    def spec(self) -> StrategySpec:
        return self._spec

    # The parameter names are the protocol's, not this class's, so they stay even though
    # this implementation reads none of them.
    def evaluate(
        self,
        state: StrategyState,  # noqa: ARG002
        bar: Bar,  # noqa: ARG002
        clock: Clock,  # noqa: ARG002
    ) -> Signal | None:
        return None


class ScriptedStrategy:
    """Emits a caller-supplied signal on every bar past its warm-up.

    The signal is supplied rather than computed, so a test can hand the runner one that
    contradicts the specification and assert the runner refuses it. A strategy that could
    only ever emit a conforming signal would make those checks unreachable.
    """

    __slots__ = ("_spec", "_to_emit")

    def __init__(self, spec: StrategySpec, to_emit: Signal | None) -> None:
        self._spec = spec
        self._to_emit = to_emit

    @property
    def spec(self) -> StrategySpec:
        return self._spec

    def evaluate(
        self,
        state: StrategyState,  # noqa: ARG002
        bar: Bar,  # noqa: ARG002
        clock: Clock,  # noqa: ARG002
    ) -> Signal | None:
        return self._to_emit
