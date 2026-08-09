"""Zero re-asks, enforced by a type, a signature and an absent metric.

ADR-0020 records the decision and the rejected one-repair design. This suite is what
makes the decision hold against the code that exists in eighteen months, written by a
session with no memory of this one.

Three independent layers, because each catches something the others cannot:

- **The type.** `mypy --strict` refuses any value but `0`, so the setting cannot be
  raised in a `.env`, a TOML file, or by an operator at 1am.
- **The signature.** `parse_or_fail` holds nothing that could issue a second call, so
  adding one is a change to the function's dependencies rather than a two-line patch.
- **The absent metric.** A repair counter would have nothing to count unless a repair
  had been reintroduced, so its absence is a cheaper signal than reading the parse path.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import tempfile
from typing import Final, Literal, get_type_hints

import pytest
from mypy import api as mypy_api
from pydantic import ValidationError

from fking.agents import AGENT_PARSE_FAILURES, parse
from fking.platform.config.settings import AgentSettings

pytestmark = pytest.mark.unit

REPO_ROOT: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parents[2]
AGENTS_ROOT: Final[pathlib.Path] = REPO_ROOT / "src" / "fking" / "agents"
PACKAGE_ROOT: Final[pathlib.Path] = REPO_ROOT / "src" / "fking"
ADR: Final[pathlib.Path] = REPO_ROOT / "docs" / "adr" / "0020-agent-reask-policy.md"

# Names that could only exist to count, configure or perform a repair.
REPAIR_SHAPED_NAMES: Final[tuple[str, ...]] = (
    "AGENT_SCHEMA_REPAIRS",
    "schema_repairs",
    "reask",
    "re_ask",
    "repair_attempts",
)

PROVIDER_SDKS: Final[frozenset[str]] = frozenset(
    {"google", "google.genai", "google.generativeai", "groq", "openai", "anthropic"}
)

# `docs/rules/llm-output-handling.md` clause 4: nothing derived from model output is
# passed to any of these. In this package none of them appears at all, which is the
# strongest available form of that guarantee.
FORBIDDEN_CALLS: Final[frozenset[str]] = frozenset(
    {"eval", "exec", "compile", "__import__", "system", "popen"}
)


def _agents_sources() -> list[tuple[pathlib.Path, str]]:
    return [(path, path.read_text(encoding="utf-8")) for path in sorted(AGENTS_ROOT.rglob("*.py"))]


def _bound_identifiers(source: str, *, label: str) -> list[tuple[str, int]]:
    """Every identifier the module binds, calls or reads, with its line number.

    Identifiers only, never raw text. The words below appear throughout this package's
    docstrings, explaining precisely why the mechanisms they name do not exist -- and a
    substring scan over source text would make writing that explanation impossible,
    which would delete the reasoning to satisfy the check that depends on it.
    """
    return [
        named
        for node in ast.walk(ast.parse(source, filename=label))
        if (named := _identifier_of(node)) is not None
    ]


def _identifier_of(node: ast.AST) -> tuple[str, int] | None:
    """The identifier a node introduces or reads, with its line, if it is that kind."""
    match node:
        case ast.Name():
            return node.id, node.lineno
        case ast.Attribute():
            return node.attr, node.lineno
        case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
            return node.name, node.lineno
        case ast.arg():
            return node.arg, node.lineno
        case ast.keyword() if node.arg is not None:
            return node.arg, node.lineno
        case _:
            return None


class TestTheSettingCannotBeRaised:
    def test_the_shipped_value_is_zero(self) -> None:
        assert AgentSettings().max_reask_attempts == 0

    def test_the_annotation_is_a_literal_rather_than_an_int(self) -> None:
        """An `int` is a value somebody edits. A `Literal[0]` is one `mypy` refuses."""
        assert get_type_hints(AgentSettings)["max_reask_attempts"] == Literal[0]

    def test_pydantic_refuses_another_value_at_construction(self) -> None:
        with pytest.raises(ValidationError):
            AgentSettings(max_reask_attempts=1)  # type: ignore[arg-type]  # the point of the test

    @pytest.mark.slow
    def test_mypy_strict_refuses_a_config_assigning_another_value(self) -> None:
        """The acceptance criterion, run rather than asserted.

        Both halves matter: a checker that rejects everything proves nothing, so the
        zero case must type-check cleanly in the same invocation shape.
        """
        with tempfile.TemporaryDirectory() as workspace:
            root = pathlib.Path(workspace)
            cache = root / "cache"
            shipped = root / "shipped_value.py"
            raised = root / "raised_value.py"
            shipped.write_text(
                "from fking.platform.config.settings import AgentSettings\n"
                "settings = AgentSettings(max_reask_attempts=0)\n",
                encoding="utf-8",
            )
            raised.write_text(
                "from fking.platform.config.settings import AgentSettings\n"
                "settings = AgentSettings(max_reask_attempts=1)\n",
                encoding="utf-8",
            )
            flags = [
                "--strict",
                "--no-incremental",
                "--no-error-summary",
                "--cache-dir",
                str(cache),
            ]

            shipped_out, _shipped_err, shipped_code = mypy_api.run([*flags, str(shipped)])
            assert shipped_code == 0, shipped_out

            raised_out, _raised_err, raised_code = mypy_api.run([*flags, str(raised)])
            assert raised_code != 0
            assert "max_reask_attempts" in raised_out
            assert "Literal[0]" in raised_out


class TestTheParsePathHoldsNothingThatCouldReAsk:
    def test_the_signature_carries_no_gateway_provider_or_client(self) -> None:
        """The signature is the enforcement: with nothing that can call a model in
        scope, a re-ask cannot be added without changing the dependencies."""
        signature = inspect.signature(parse.parse_or_fail)
        assert set(signature.parameters) == {"call", "schema", "completion", "recorder"}
        rendered = str(signature).casefold()
        for forbidden in ("gateway", "provider", "client", "session"):
            assert forbidden not in rendered

    def test_the_parse_path_is_synchronous(self) -> None:
        """An `async def` would be the first half of adding a second call."""
        assert not inspect.iscoroutinefunction(parse.parse_or_fail)

    def test_the_parse_module_contains_no_loop_over_the_validation(self) -> None:
        """A loop here is the shape the whole rule forbids, whatever it is called."""
        tree = ast.parse((AGENTS_ROOT / "parse.py").read_text(encoding="utf-8"))
        loops = [
            node for node in ast.walk(tree) if isinstance(node, ast.For | ast.While | ast.AsyncFor)
        ]
        assert loops == []


class TestNoRepairMechanismExists:
    @pytest.mark.parametrize("name", REPAIR_SHAPED_NAMES)
    def test_no_repair_shaped_identifier_is_bound_in_the_package(self, name: str) -> None:
        """A repair counter has nothing to count unless a repair was reintroduced."""
        lowered = name.casefold()
        offenders = [
            f"{path.name}:{lineno} {identifier}"
            for path, source in _agents_sources()
            for identifier, lineno in _bound_identifiers(source, label=str(path))
            if lowered in identifier.casefold()
        ]
        assert offenders == [], f"{name} is bound at {offenders}"

    def test_the_declared_metric_counts_failures_rather_than_repairs(self) -> None:
        assert AGENT_PARSE_FAILURES.name == "fking_agents_parse_failures_total"
        assert AGENT_PARSE_FAILURES.labels == ("agent", "provider")

    def test_no_metric_name_in_the_package_ends_in_repairs_total(self) -> None:
        """A string literal check, because a metric name *is* a string literal."""
        offenders = [
            f"{path.name}:{node.lineno}"
            for path, source in _agents_sources()
            for node in ast.walk(ast.parse(source, filename=str(path)))
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.endswith("repairs_total")
        ]
        assert offenders == []


class TestTheContractLayerReachesNoProvider:
    def test_no_module_imports_a_provider_sdk(self) -> None:
        """The gateway (#72) is the only sanctioned importer, and it does not exist yet.
        `import-linter` states the same contract for the whole tree; this names the
        package and runs without the graph."""
        offenders: list[str] = []
        for path, source in _agents_sources():
            for node in ast.walk(ast.parse(source, filename=str(path))):
                if isinstance(node, ast.Import):
                    offenders.extend(
                        f"{path.name}: {alias.name}"
                        for alias in node.names
                        if alias.name.split(".")[0] in PROVIDER_SDKS
                    )
                elif (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.split(".")[0] in PROVIDER_SDKS
                ):
                    offenders.append(f"{path.name}: {node.module}")
        assert offenders == []

    @pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_CALLS))
    def test_no_module_calls_an_arbitrary_execution_primitive(self, forbidden: str) -> None:
        """Clause 4: nothing derived from model output reaches `eval`, `exec`,
        `__import__`, a subprocess, a SQL string, a file path or a URL. None of them
        appears here at all, which is stronger than a per-call-site argument."""
        offenders: list[str] = []
        for path, source in _agents_sources():
            for node in ast.walk(ast.parse(source, filename=str(path))):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                named = (
                    function.id
                    if isinstance(function, ast.Name)
                    else function.attr
                    if isinstance(function, ast.Attribute)
                    else ""
                )
                if named == forbidden:
                    offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == []


class TestTheDecisionIsRecorded:
    def test_the_adr_exists_and_is_accepted(self) -> None:
        assert ADR.is_file()
        assert "status: accepted" in ADR.read_text(encoding="utf-8")

    def test_the_adr_states_the_shipped_value(self) -> None:
        assert "max_reask_attempts` is `Literal[0]`" in ADR.read_text(encoding="utf-8")

    def test_the_adr_records_the_one_repair_design_as_the_rejected_alternative(self) -> None:
        """An ADR whose rejected-alternatives section is empty is a decision nobody can
        re-open honestly, and the record of rejected paths is the valuable part."""
        body = ADR.read_text(encoding="utf-8")
        assert "strongest rejected" in body
        assert "ValidationError" in body
