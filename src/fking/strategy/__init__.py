"""The strategy contract and its implementations. Knows about features and beliefs.

A strategy emits a `Signal` -- direction, conviction, horizon, invalidation -- and
says nothing about size. It cannot import `execution` or `risk`, and that is a
contract rather than a guideline: a strategy that sizes its own positions can
bankrupt the portfolio regardless of how good its signals are.

Functions here are pure. No I/O, no clock access, no randomness without an injected
seed, which is what makes a strategy deterministically replayable and safely
evolvable.
"""

__all__: tuple[str, ...] = ()
