"""The agent layer's declared metrics.

Only one is declared, and only because something in this package emits it. Declaring a
name before anything increments it freezes a name nobody has yet had to use, which is
the failure `fking.platform.telemetry._registry` opens by naming.

The spec is validated at import by `MetricSpec.__post_init__`, so a name that violates
the frozen convention fails at import rather than at the first scrape.

**There is deliberately no repair counter.** If a metric named
`fking_agents_schema_repairs_total`, or a symbol named `AGENT_SCHEMA_REPAIRS`, ever
appears in this package, a re-ask has been reintroduced -- because there is nothing
else for such a counter to count. `tests/agents/test_no_reask.py` asserts its absence
over the source tree, which is a cheaper signal than reading the parse path.
"""

from __future__ import annotations

from typing import Final

from fking.platform.telemetry import MetricSpec, counter

__all__ = ["AGENT_PARSE_FAILURES", "PARSE_FAILURES"]

AGENT_PARSE_FAILURES: Final = MetricSpec(
    name="fking_agents_parse_failures_total",
    kind="counter",
    labels=("agent", "provider"),
    description=(
        "Agent responses that failed schema validation. With zero re-asks this is a "
        "direct measurement rather than a residual: every failure is exactly one call "
        "the agent could not contribute to, so the ratio against calls is the fraction "
        "of decisions made without it. A step change usually means the provider rolled "
        "the model despite a pinned id, or a prompt shipped without its schema."
    ),
)

# Both labels are bounded -- `agent` by the declaration registry, `provider` by the two
# configured providers -- so neither forks a series per request.
PARSE_FAILURES: Final = counter(AGENT_PARSE_FAILURES)
