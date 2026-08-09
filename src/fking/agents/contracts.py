"""The typed boundary between a language model and the deterministic core.

Three rules shape every model in this file, and each has an obvious wrong answer.

**`extra="forbid"`, not `extra="ignore"`.** The asymmetry with venue payloads
(`fking.data.live.frames`, which ignores) is deliberate. Binance adds fields without
notice and breaking on one we do not read is a self-inflicted outage; a model emitting
a field the schema did not ask for means the schema was not respected, and the fields
it invents are exactly the ones it was forbidden to produce.

**Decimals arrive as JSON strings.** A JSON number has already been through a `float`
inside the parser before Pydantic sees it, so the value landing in a `Decimal`-annotated
field is wrong in a way `mypy` will never see and a reviewer will never spot -- the
annotation says `Decimal` and it is a `Decimal`. The refusal is the only honest
response; `Decimal(str(candidate))` over the damage would launder it.

**Anything that is a decision is a closed `Literal`.** A free-text field where a
decision belongs is a field the deterministic core will eventually string-match, and
string matching on model output is a hallucination pipeline with extra steps.

`rationale` is the single exception, and it is bounded on all four sides: stored,
displayed, length-capped, and never parsed, matched, branched on, or used as a cache
key. `tools/checks/rationale_untouched.py` enforces the last clause over `src/fking/**`.

### The prohibitions are structural

There is no `size`, `notional`, `leverage`, `quantity` or venue-host field on any model
here, so `AI_MANIFEST.md` section 3 items 2 and 5 are enforced by Pydantic rather than
by the model's willingness to comply. `tools/checks/agent_schema_fields.py` fails the
build if one is ever added.

Nor does any model name an instrument. `ThesisProposal.symbol_index` is an index into a
universe the deterministic core resolved (`docs/rules/exchange-integration.md`), so
the worst a hallucinated or injected value can do is fall outside the range and be
refused by `resolve_symbol`. A `symbol: str` field would let a model name a market this
system has never listed, and the string would look exactly like a real one.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

# One direction only: `fencing` knows nothing about these models, so the edge cannot
# become a cycle. Importing it here is what lets `SentimentRequest` refuse an unfenced
# document at construction rather than trusting its caller to have fenced one.
from fking.agents.fencing import UNTRUSTED_OPEN_PREFIX

__all__ = [
    "MAX_RATIONALE_CHARACTERS",
    "AgentDecimal",
    "AgentInput",
    "AgentOutput",
    "Conviction",
    "CriticVerdict",
    "CritiqueRequest",
    "RationaleText",
    "SentimentRequest",
    "ThesisProposal",
    "resolve_symbol",
]

# Long enough for a paragraph of reasoning, short enough that the one free-text channel
# in the system cannot become a payload channel. A rationale is read by a human
# reconstructing a decision; one that needs more than this is not being read.
MAX_RATIONALE_CHARACTERS: int = 2000

# One week. A thesis whose horizon exceeds it is a claim about a regime rather than a
# trade, and the evaluation window that would be needed to falsify it does not exist.
_MAX_HORIZON_HOURS: int = 168


def _decimal_from_json_string(raw_field: object) -> Decimal:
    """A `Decimal` from the exact characters the model emitted, or a refusal.

    Raises `ValueError` rather than the `TypeError` ruff's TRY004 would prefer:
    Pydantic v2 converts a `ValueError` raised inside a validator into a
    `ValidationError` and lets a `TypeError` escape as itself. A `TypeError` here would
    leave the boundary as an unhandled crash instead of the named field failure the
    parse path reports and records.
    """
    if not isinstance(raw_field, str):
        raise ValueError(  # noqa: TRY004
            f"decimal fields must be JSON strings, not JSON numbers; got "
            f"{type(raw_field).__name__} {raw_field!r}. A JSON number has already lost "
            f"precision in the parser, before this validator ran"
        )
    try:
        parsed = Decimal(raw_field)
    except InvalidOperation as malformed:
        raise ValueError(f"{raw_field!r} is not a decimal") from malformed
    if not parsed.is_finite():
        # NaN and the infinities are legal Decimals that propagate through arithmetic
        # without raising, and NaN != NaN -- so one of them turns every equality
        # downstream into a permanent, unexplained mismatch.
        raise ValueError(f"{raw_field!r} is not finite")
    return parsed


AgentDecimal = Annotated[Decimal, BeforeValidator(_decimal_from_json_string)]
"""A decimal a model emitted, taken from its string form and never from a JSON number."""

Conviction = Annotated[AgentDecimal, Field(ge=Decimal("0"), le=Decimal("1"))]
"""A bounded belief strength. Not a size, and no arithmetic here turns it into one:
the risk engine reads it as one input among several and holds sole authority over
quantity (`RISK_PHILOSOPHY.md`)."""

RationaleText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_RATIONALE_CHARACTERS, strip_whitespace=True),
]
"""Free text a model wrote. Stored and displayed; never parsed, matched or branched on."""


class AgentInput(BaseModel):
    """Base for everything handed *to* an agent.

    Frozen so that the payload audited is the payload sent: a request mutated between
    the audit write and the provider call makes the recorded prompt a description of
    something that did not happen.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AgentOutput(BaseModel):
    """Base for everything an agent produces.

    Every output model in this system inherits it, and `AgentDeclaration` refuses an
    `output_schema` that does not -- so `extra="forbid"`, `frozen=True` and
    `strict=True` are properties of the boundary rather than of each author's memory.

    `strict=True` matters most of the three. Without it Pydantic coerces `"7"` into
    `7`, `1` into `True` and `"long "` into a `Literal` mismatch that a lax mode would
    have papered over -- each of which is charitable interpretation performed by the
    validator instead of by a regex, which makes it no less a second decision layer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SentimentRequest(AgentInput):
    """Input to an agent that reads text and proposes a directional thesis.

    `universe` is the resolved tradable set and is the *only* way an instrument enters
    the conversation; the agent answers with an index into it. `fenced_documents` have
    already been through `fking.agents.fencing.fence` -- this model does not fence them,
    because a model that fenced its own inputs would make the unfenced spelling
    reachable by constructing the request some other way.
    """

    universe: tuple[str, ...] = Field(min_length=1)
    fenced_documents: tuple[str, ...] = Field(min_length=1)
    as_of_utc: AwareDatetime

    @model_validator(mode="after")
    def _documents_are_fenced(self) -> Self:
        unfenced = [
            index
            for index, document in enumerate(self.fenced_documents)
            if not document.startswith(UNTRUSTED_OPEN_PREFIX)
        ]
        if unfenced:
            raise ValueError(
                f"fenced_documents{unfenced} do not open with {UNTRUSTED_OPEN_PREFIX!r}; "
                f"untrusted text reaches a prompt through fence(), never by "
                f"concatenation"
            )
        return self


class CritiqueRequest(AgentInput):
    """Input to an adversarial reviewer.

    `claim_under_review` is a prior agent's output, which is untrusted text for exactly
    the same reason a headline is -- so it arrives fenced.
    """

    fenced_claim: str
    fenced_evidence: tuple[str, ...] = Field(min_length=1)
    as_of_utc: AwareDatetime


class ThesisProposal(AgentOutput):
    """A directional belief with a stated horizon and a stated falsifier.

    Note what is absent, and that the absence is the enforcement: no symbol string, no
    notional, no leverage, no order type, no venue. The agent proposes a direction and
    a conviction; the risk engine decides the position, or vetoes it.
    """

    # An index into the caller's resolved universe, never a name. See resolve_symbol.
    symbol_index: int = Field(ge=0)
    direction: Literal["long", "short", "flat"]
    conviction: Conviction
    horizon_hours: int = Field(ge=1, le=_MAX_HORIZON_HOURS)
    # What would prove this wrong. Required, and required *before* the model has
    # committed to a conviction it would then defend.
    invalidation_note: RationaleText
    rationale: RationaleText

    @model_validator(mode="after")
    def _flat_carries_no_conviction(self) -> Self:
        """`flat` with conviction 0.9 is two contradictory answers in one object.

        Refused rather than normalised: silently zeroing the conviction would hide a
        model that has misunderstood its own schema, and that misunderstanding is the
        signal the parse-failure rate exists to surface.
        """
        if self.direction == "flat" and self.conviction != Decimal("0"):
            raise ValueError(
                f"direction is 'flat' but conviction is {self.conviction}; a flat "
                f"thesis asserts no direction and cannot be held with strength"
            )
        return self


class CriticVerdict(AgentOutput):
    """An adversarial reviewer's finding.

    The field order is a behavioural decision, not a formatting one: a model that
    states a verdict first justifies it, and a model that enumerates flaws first
    reaches a verdict it can defend (`PROMPT_LIBRARY.md` section 5). Pydantic emits its
    JSON schema in declaration order, so the schema is what carries the ordering into
    the provider's structured-output mode.

    `insufficient_evidence` is a first-class member of the union rather than an
    optional field. An agent that never abstains is not agreeable, it is uncalibrated,
    and the gates downstream cannot tell a confident wrong answer from a confident right
    one.
    """

    flaws: tuple[RationaleText, ...] = Field(max_length=16)
    decision: Literal["reject", "accept", "insufficient_evidence"]
    confidence: Conviction
    # Required on every verdict, abstaining or not: a model forced to state its
    # falsifier discovers, while stating it, that it does not have one.
    what_would_change_my_mind: RationaleText
    rationale: RationaleText

    @model_validator(mode="after")
    def _a_rejection_names_a_flaw(self) -> Self:
        if self.decision == "reject" and not self.flaws:
            raise ValueError(
                "decision is 'reject' but no flaw was named; a rejection nobody can "
                "read is indistinguishable from an unexplained refusal"
            )
        return self


def resolve_symbol(proposal: ThesisProposal, universe: Sequence[str]) -> str:
    """The symbol a proposal refers to, resolved against the caller's universe.

    This is the whole of the enumerated-constant mechanism, and it is four lines
    because that is all it takes: model output selects among constants the
    deterministic core supplied, so an index outside the range is a refusal rather
    than a market nobody has listed.

    Raises `IndexError` rather than returning `None`. A caller that forgets to check a
    `None` trades nothing; a caller that forgets to check a `None` used as a dict key
    finds out later, which is the shape of failure this system exists to avoid.
    """
    if proposal.symbol_index >= len(universe):
        raise IndexError(
            f"symbol_index {proposal.symbol_index} is outside the resolved universe of "
            f"{len(universe)}; the model named an instrument that does not exist here"
        )
    return universe[proposal.symbol_index]
