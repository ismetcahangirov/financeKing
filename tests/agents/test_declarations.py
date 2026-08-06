"""No agent runs without all seven elements, and the forbidden list is the load-bearing one.

`CLAUDE.md` section 10 lists the seven; this suite is what makes the list a contract
rather than a checklist. The tests walk `DECLARED_AGENTS` rather than naming agents one
by one, so an agent added later inherits every assertion the moment it is declared --
which is the only version of this that survives a year.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from fking.agents import (
    CRITIC,
    DECLARED_AGENTS,
    SENTIMENT,
    UNIVERSAL_FORBIDDEN_DECISIONS,
    AgentDeclaration,
    AgentInput,
    AgentOutput,
    SentimentRequest,
    ThesisProposal,
)

pytestmark = pytest.mark.unit

DECLARATIONS = sorted(DECLARED_AGENTS.values(), key=lambda declaration: declaration.agent_id)
DECLARATION_IDS = [declaration.agent_id for declaration in DECLARATIONS]

# A verb, an object and a qualifier at minimum. Fewer words than that and the entry
# has named a category rather than an action, which is the thing a golden case cannot test.
MIN_ACTION_WORDS = 3


def a_declaration(**overrides: object) -> AgentDeclaration:
    """A valid declaration with `overrides` applied, for testing one refusal at a time."""
    fields: dict[str, object] = {
        "agent_id": "probe",
        "mission": "Do one narrow thing and say so in a single sentence.",
        "allowed_decisions": frozenset({"abstain"}),
        "forbidden_decisions": UNIVERSAL_FORBIDDEN_DECISIONS,
        "input_schema": SentimentRequest,
        "output_schema": ThesisProposal,
        "max_input_tokens": 1_000,
        "max_output_tokens": 100,
        "timeout_seconds": 30.0,
        "escalation": "needs-human",
        "temperature": Decimal("0"),
        "model_pin": "gemini-2.5-flash-002",
    }
    return AgentDeclaration(**{**fields, **overrides})  # type: ignore[arg-type]  # kwargs are validated by pydantic


class TestEverySevenElementIsPresent:
    @pytest.mark.parametrize("declaration", DECLARATIONS, ids=DECLARATION_IDS)
    def test_a_declaration_carries_all_seven_elements(self, declaration: AgentDeclaration) -> None:
        assert declaration.mission
        assert declaration.allowed_decisions
        assert declaration.forbidden_decisions
        assert issubclass(declaration.input_schema, AgentInput)
        assert issubclass(declaration.output_schema, AgentOutput)
        assert declaration.max_input_tokens > 0
        assert declaration.max_output_tokens > 0
        assert declaration.timeout_seconds > 0
        assert declaration.escalation

    @pytest.mark.parametrize("declaration", DECLARATIONS, ids=DECLARATION_IDS)
    def test_a_mission_is_one_sentence(self, declaration: AgentDeclaration) -> None:
        """An agent whose mission needs a paragraph is two agents."""
        assert "\n" not in declaration.mission

    def test_a_multi_line_mission_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="two agents"):
            a_declaration(mission="First it does one thing.\nThen it does another thing.")


class TestTheForbiddenListIsLoadBearing:
    def test_an_empty_forbidden_list_is_refused(self) -> None:
        """An author who cannot name a single thing their agent must not do has not
        designed it."""
        with pytest.raises(ValidationError):
            a_declaration(forbidden_decisions=frozenset())

    @pytest.mark.parametrize("declaration", DECLARATIONS, ids=DECLARATION_IDS)
    def test_every_declaration_carries_the_universal_prohibitions_in_full(
        self, declaration: AgentDeclaration
    ) -> None:
        assert declaration.forbidden_decisions >= UNIVERSAL_FORBIDDEN_DECISIONS

    def test_narrowing_the_universal_set_is_refused(self) -> None:
        """The failure mode is an author who names three prohibitions and omits the one
        about constructing an order."""
        narrowed = UNIVERSAL_FORBIDDEN_DECISIONS - {"construct, modify or cancel an order"}
        with pytest.raises(ValidationError, match="not a default an author may narrow"):
            a_declaration(forbidden_decisions=narrowed)

    def test_a_decision_that_is_both_allowed_and_forbidden_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="both allowed and forbidden"):
            a_declaration(
                allowed_decisions=frozenset({"construct, modify or cancel an order"}),
                forbidden_decisions=UNIVERSAL_FORBIDDEN_DECISIONS,
            )

    def test_the_universal_set_names_actions_rather_than_categories(self) -> None:
        """ "Do not make risky decisions" is unenforceable and unmeasurable; every entry
        here has to be something a golden case could test for."""
        assert all(
            len(entry.split()) >= MIN_ACTION_WORDS for entry in UNIVERSAL_FORBIDDEN_DECISIONS
        )
        assert not any(
            "risky" in entry or "dangerous" in entry for entry in UNIVERSAL_FORBIDDEN_DECISIONS
        )


class TestTheModelIsPinned:
    @pytest.mark.parametrize("declaration", DECLARATIONS, ids=DECLARATION_IDS)
    def test_a_declared_model_id_carries_a_version(self, declaration: AgentDeclaration) -> None:
        assert "latest" not in declaration.model_pin.casefold()

    @pytest.mark.parametrize(
        "floating",
        [
            "gemini-flash-latest",
            "Gemini-Flash-LATEST",
            "gemini-2.5-flash-preview",
            "llama-3.3-70b-*",
            "gpt-current",
            "claude-stable",
        ],
    )
    def test_a_floating_alias_is_refused(self, floating: str) -> None:
        """A provider rolling an alias forward changes every result with no diff, and
        the reconstructability requirement then fails for everything before the roll."""
        with pytest.raises(ValidationError, match="whatever the provider is serving today"):
            a_declaration(model_pin=floating)


class TestSchemasAreTypedAtBothEnds:
    def test_an_output_schema_that_is_not_an_agent_output_is_refused(self) -> None:
        """`AgentOutput` is where extra="forbid", frozen and strict live, so a schema
        outside that tree is a schema with none of them."""
        with pytest.raises(ValidationError):
            a_declaration(output_schema=SentimentRequest)

    def test_an_input_schema_that_is_not_an_agent_input_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            a_declaration(input_schema=ThesisProposal)


class TestTheRegistryIsConsistent:
    def test_the_registry_is_keyed_by_the_declarations_own_agent_id(self) -> None:
        assert all(key == declaration.agent_id for key, declaration in DECLARED_AGENTS.items())

    def test_the_registry_cannot_be_mutated_by_a_caller(self) -> None:
        with pytest.raises(TypeError):
            # The Mapping annotation forbids this statically; the assertion is that
            # the runtime proxy refuses it too, so a caller holding a `dict`-typed
            # alias cannot write through it either.
            DECLARED_AGENTS["forged"] = SENTIMENT  # type: ignore[index]

    def test_a_declaration_is_frozen(self) -> None:
        with pytest.raises(ValidationError):
            SENTIMENT.temperature = Decimal("1")  # frozen model refuses assignment

    def test_the_adversarial_agent_is_not_run_at_temperature_zero(self) -> None:
        """A Critic at temperature 0 produces the same critique every time, which looks
        stable on a dashboard and finds fewer flaws. Convergence is a defect signal."""
        assert CRITIC.temperature > Decimal("0")

    def test_each_agent_names_where_a_failure_goes(self) -> None:
        """An agent that hits a wall with no exit produces something anyway."""
        assert all(declaration.escalation for declaration in DECLARATIONS)
