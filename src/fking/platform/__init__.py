"""Config, logging, telemetry, the event bus, persistence and the safety kernel.
Knows about mechanism, not policy.

This package is importable from anywhere, including `strategy` and `risk`, and is
exempt from the layering contract. That is safe precisely because it decides nothing
about trading: it knows how to send a request and has no opinion about whether one
should be sent, so importing it cannot smuggle a decision across a boundary.

Two constraints keep the exemption from becoming a loophole. It never imports another
`fking` module, including `domain`. And it gets no trading vocabulary -- a function
here named `size_position` or `should_trade` is a boundary violation whatever the
import graph says.
"""

__all__: tuple[str, ...] = ()
