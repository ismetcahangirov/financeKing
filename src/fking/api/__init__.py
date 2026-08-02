"""The FastAPI application. Knows about HTTP shapes.

Translation between wire representation and domain types happens here and nowhere
else. Money crosses this boundary as a JSON string, never a JSON number: a number has
already been through a binary double by the time any validator sees it.
"""

__all__: tuple[str, ...] = ()
