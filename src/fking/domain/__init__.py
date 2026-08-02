"""Pure types. Knows about instruments, bars, signals, orders, fills and positions.

Imports nothing but the standard library -- not pydantic, not sqlalchemy, not even
`fking.platform`. That is what makes a domain object free to construct anywhere: a
test can build a Position without dragging in config loading and telemetry, and a
pydantic major bump cannot change the meaning of a Fill.

Every type here is frozen and every state transition returns a new object.
"""

__all__: tuple[str, ...] = ()
