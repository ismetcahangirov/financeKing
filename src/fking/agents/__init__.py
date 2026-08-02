"""LLM agents and their runtime. Knows about prompts, schemas and budgets.

Agents propose; deterministic code disposes. No agent output is acted on in the form
the model produced it: every response is parsed into a schema-validated structure,
and an unparseable response is a failure rather than something to interpret
charitably.

Exactly one module in this package may import a provider SDK -- the gateway, which
also owns quota admission control.
"""

__all__: tuple[str, ...] = ()
