"""The two checks issue #70 adds must catch their violation and pass clean code.

The second half matters as much as the first. A check that flags everything gets
disabled within a week, and a check that flags nothing is indistinguishable from one
that is not wired up -- which is exactly how `make check` ends up green while the rule
it claims to enforce has been dead for months.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable, Mapping, Sequence

import pytest

from tools.checks import agent_schema_fields, rationale_untouched

pytestmark = pytest.mark.unit


def write_package(root: pathlib.Path, modules: Mapping[str, str]) -> pathlib.Path:
    """A throwaway package tree, so a check is exercised against files it must reject."""
    for relative, source in modules.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return root


class TestAgentSchemaFields:
    def test_a_sizing_field_on_an_output_model_is_rejected(self, tmp_path: pathlib.Path) -> None:
        package = write_package(
            tmp_path,
            {
                "contracts.py": (
                    "class AgentOutput(BaseModel): ...\n"
                    "class ThesisProposal(AgentOutput):\n"
                    "    direction: str\n"
                    "    notional_usd: Decimal\n"
                )
            },
        )
        failures = agent_schema_fields.check_package(package)
        assert len(failures) == 1
        assert "notional_usd" in failures[0]

    @pytest.mark.parametrize(
        "field_name",
        [
            "size",
            "base_size",
            "notional_usd",
            "leverage",
            "max_leverage",
            "base_quantity",
            "margin_usd",
            "collateral",
        ],
    )
    def test_every_sizing_spelling_is_rejected(
        self, field_name: str, tmp_path: pathlib.Path
    ) -> None:
        """Sizing authority belongs to the risk engine, and the schema is where the
        prohibition is cheapest to enforce."""
        package = write_package(
            tmp_path,
            {"m.py": f"class Proposal(AgentOutput):\n    {field_name}: Decimal\n"},
        )
        assert agent_schema_fields.check_package(package)

    @pytest.mark.parametrize(
        "field_name", ["venue_host", "hostname", "base_url", "rest_endpoint", "api_key", "secret"]
    )
    def test_every_egress_spelling_is_rejected(
        self, field_name: str, tmp_path: pathlib.Path
    ) -> None:
        """An agent may not propose a change to which hosts the system may contact, in
        any form, for any reason -- so it may not name one."""
        package = write_package(
            tmp_path,
            {"m.py": f"class Proposal(AgentOutput):\n    {field_name}: str\n"},
        )
        assert agent_schema_fields.check_package(package)

    def test_a_field_declared_on_a_grandchild_model_is_still_seen(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The two-pass closure is the point: a single-pass check would see an unknown
        base and skip the class, which is a check that inspects nothing."""
        package = write_package(
            tmp_path,
            {
                "base.py": "class AgentOutput(BaseModel): ...\n",
                "middle.py": "class Verdict(AgentOutput):\n    decision: str\n",
                "leaf.py": "class SizedVerdict(Verdict):\n    leverage: int\n",
            },
        )
        failures = agent_schema_fields.check_package(package)
        assert len(failures) == 1
        assert "SizedVerdict.leverage" in failures[0]

    def test_a_class_outside_the_schema_tree_is_not_scanned(self, tmp_path: pathlib.Path) -> None:
        """An internal record is not a decision surface, and flagging one is how a
        check earns a blanket suppression."""
        package = write_package(
            tmp_path,
            {"m.py": "class InternalRecord:\n    base_quantity: Decimal\n"},
        )
        assert agent_schema_fields.check_package(package) == []

    def test_a_legitimate_agent_field_is_accepted(self, tmp_path: pathlib.Path) -> None:
        package = write_package(
            tmp_path,
            {
                "m.py": (
                    "class Proposal(AgentOutput):\n"
                    "    symbol_index: int\n"
                    "    conviction: Decimal\n"
                    "    horizon_hours: int\n"
                    "    max_input_tokens: int\n"
                    "    rationale: str\n"
                )
            },
        )
        assert agent_schema_fields.check_package(package) == []

    def test_a_class_level_constant_is_not_treated_as_a_schema_field(
        self, tmp_path: pathlib.Path
    ) -> None:
        package = write_package(
            tmp_path,
            {"m.py": "class Proposal(AgentOutput):\n    model_config = dict(size=1)\n"},
        )
        assert agent_schema_fields.check_package(package) == []

    def test_the_real_agent_package_is_clean(self) -> None:
        assert agent_schema_fields.main(["src/fking/agents"]) == 0

    def test_an_empty_argv_exits_zero(self) -> None:
        assert agent_schema_fields.main([]) == 0

    def test_a_missing_directory_is_a_failure_rather_than_a_silent_pass(self) -> None:
        """A check pointed at a path that does not exist reports success otherwise,
        which is the failure mode where the gate has been dead for months."""
        assert agent_schema_fields.main(["src/fking/does_not_exist"]) == 1


class TestRationaleUntouched:
    @pytest.mark.parametrize(
        "source",
        [
            "if 'increase exposure' in proposal.rationale:\n    pass\n",
            "key = hash(proposal.rationale)\n",
            "score = sentiment(thesis.rationale)\n",
            "flag = verdict.rationale.startswith('confident')\n",
            "thesis.rationale = 'edited'\n",
        ],
    )
    def test_reading_the_field_is_rejected(self, source: str) -> None:
        """The moment code branches on it, the free-text field is an untyped control
        channel from the model into the deterministic core."""
        assert rationale_untouched.check_source(source, label="x.py")

    def test_declaring_the_field_is_accepted(self) -> None:
        """`rationale: RationaleText` is an annotated assignment, not an attribute
        access, so the declaration this rule protects is never flagged by it."""
        source = "class Proposal(AgentOutput):\n    rationale: RationaleText\n"
        assert rationale_untouched.check_source(source, label="x.py") == []

    def test_passing_the_field_as_a_keyword_argument_is_accepted(self) -> None:
        source = "proposal = ThesisProposal(rationale='because', direction='long')\n"
        assert rationale_untouched.check_source(source, label="x.py") == []

    def test_construction_time_validation_on_self_is_accepted(self) -> None:
        """Length-bounding is one of the four permitted operations, and a
        `__post_init__` can only refuse to build the object -- there is no decision for
        it to reach."""
        source = (
            "class Signal:\n"
            "    def __post_init__(self) -> None:\n"
            "        require_text(self.rationale, 'rationale')\n"
        )
        assert rationale_untouched.check_source(source, label="x.py") == []

    def test_reading_another_objects_field_inside_post_init_is_still_rejected(self) -> None:
        """The exemption is for validating one's own field, not for a method name that
        happens to open a hole."""
        source = (
            "class Thing:\n"
            "    def __post_init__(self) -> None:\n"
            "        if 'buy' in other.rationale:\n"
            "            raise ValueError\n"
        )
        assert rationale_untouched.check_source(source, label="x.py")

    def test_reading_self_dot_rationale_outside_post_init_is_rejected(self) -> None:
        source = (
            "class Thing:\n    def decide(self) -> bool:\n        return 'buy' in self.rationale\n"
        )
        assert rationale_untouched.check_source(source, label="x.py")

    def test_the_allowlisted_serializer_may_read_it(self) -> None:
        """Display is one of the four permitted operations, and the renderer must read
        the field to render it."""
        source = "def render(proposal):\n    return escape(proposal.rationale)\n"
        assert rationale_untouched.check_source(source, label="x.py", permitted=True) == []

    def test_the_failure_names_the_line_and_the_rule(self) -> None:
        failures = rationale_untouched.check_source("\nif x.rationale:\n    pass\n", label="x.py")
        assert failures[0].startswith("x.py:2")
        assert "llm-output-handling" in failures[0]

    def test_a_tree_scan_exempts_only_the_allowlisted_path(self, tmp_path: pathlib.Path) -> None:
        package = write_package(
            tmp_path,
            {
                "api/serializers.py": "def render(p):\n    return p.rationale\n",
                "risk/engine.py": "def decide(p):\n    return p.rationale\n",
            },
        )
        failures = rationale_untouched.check_tree(package)
        assert len(failures) == 1
        assert "engine.py" in failures[0]

    def test_the_real_source_tree_is_clean(self) -> None:
        """The acceptance criterion: no branch anywhere in src/fking reads .rationale."""
        assert rationale_untouched.main(["src/fking"]) == 0

    def test_an_empty_argv_exits_zero(self) -> None:
        assert rationale_untouched.main([]) == 0

    def test_a_missing_directory_is_a_failure_rather_than_a_silent_pass(self) -> None:
        assert rationale_untouched.main(["src/fking/does_not_exist"]) == 1


CHECK_ENTRY_POINTS: Mapping[str, Callable[[Sequence[str]], int]] = {
    "agent_schema_fields": agent_schema_fields.main,
    "rationale_untouched": rationale_untouched.main,
}


@pytest.mark.parametrize("check_name", sorted(CHECK_ENTRY_POINTS))
def test_check_passes_over_the_committed_tree(check_name: str) -> None:
    """`make check` is green on a lie if either of these has never been run for real."""
    assert CHECK_ENTRY_POINTS[check_name](["src/fking/agents"]) == 0
