"""Venues, the order management system and reconciliation. Knows about order types
and venue protocols.

Exchange state is the source of truth; local state converges to it. Every response
crossing this boundary is hostile input -- parsed into a typed model, never indexed
into optimistically.

No module here constructs its own HTTP or WebSocket client. Network access goes
through the safety kernel in `fking.platform.safety`.
"""

__all__: tuple[str, ...] = ()
