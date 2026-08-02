"""Ingestion, storage and the feature store. Knows about sources, schemas and
point-in-time semantics.

The distinction this module exists to keep straight is `event_time` versus
`available_at`: when a thing happened, versus the earliest instant this system could
have known it. Only the second governs visibility, and filtering on the first is the
single most common form of look-ahead.
"""

__all__: tuple[str, ...] = ()
