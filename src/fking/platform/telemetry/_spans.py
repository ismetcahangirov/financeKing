"""Manual spans, with the attributes that make them a record rather than a stopwatch.

`OBSERVABILITY.md` section 5: **a span whose attributes are empty is treated as no
span.** An untyped span is a timing measurement. What an investigation needs is which
correlation chain the timing belongs to, and that is why `fking.correlation_id` is
stamped by this helper rather than by each caller.

Span names are `module.operation`, lowercase and dot-separated, and are frozen for the
same reason metric names are: a renamed span breaks every historical query in the
direction of "no results".

What never goes on a span: prompts, responses, API keys, Ed25519 material, exchange
credentials, raw venue response bodies. Tempo has seven-day retention and no access
control worth the name. Spans carry the prompt hash and the audit row id; the text lives
in the append-only audit tables.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Final

from opentelemetry import trace
from opentelemetry.trace import Span, Tracer

from fking.platform.correlation import require_current_correlation_id

CORRELATION_ATTRIBUTE: Final[str] = "fking.correlation_id"

_SPAN_NAME: Final[re.Pattern[str]] = re.compile(r"\A[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+\Z")

# Substrings that must never appear in an attribute key. A denylist is the wrong shape
# for the log pipeline -- where the field set is open and an allowlist is the only stable
# answer -- but it is the right shape here, because span attributes are written by the
# same hand that writes the span name, and the failure being guarded is a slip rather
# than an omission.
_FORBIDDEN_ATTRIBUTE_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "credential",
        "passphrase",
        "password",
        "private_key",
        "prompt",
        "response_body",
        "secret",
        "signature",
        "token",
    }
)

_TRACER_NAME: Final[str] = "fking.platform.telemetry"


class SpanContractError(ValueError):
    """A span name or attribute set violates the span contract."""


def tracer() -> Tracer:
    """The project tracer, resolved against whatever provider is installed now."""
    return trace.get_tracer(_TRACER_NAME)


def _check_attributes(attributes: Mapping[str, str]) -> None:
    offending = sorted(
        key
        for key in attributes
        if any(token in key.lower() for token in _FORBIDDEN_ATTRIBUTE_TOKENS)
    )
    if offending:
        raise SpanContractError(
            f"span attribute(s) {offending} name material that must not reach Tempo; "
            f"record the audit row id and put the value in the audit table instead"
        )


@contextmanager
def traced(span_name: str, /, **attributes: str) -> Iterator[Span]:
    """Open a span called `span_name`, carrying the active correlation id.

    Raises `MissingCorrelationId` when no scope is active. That is deliberate and it is
    the whole value of the helper: a span with no chain to attach to is a timing
    measurement, and the correct fix is to open a scope at the top of the flow rather
    than to let this one emit an orphan.
    """
    if _SPAN_NAME.fullmatch(span_name) is None:
        raise SpanContractError(
            f"span name {span_name!r} is not module.operation in lower snake_case; span "
            f"names are frozen and a rename silently empties every query built on it"
        )
    _check_attributes(attributes)
    correlation_id = require_current_correlation_id(at=f"span {span_name!r}")
    with tracer().start_as_current_span(span_name) as span:
        span.set_attribute(CORRELATION_ATTRIBUTE, correlation_id)
        for key, mapped in attributes.items():
            span.set_attribute(f"fking.{key}", mapped)
        yield span
