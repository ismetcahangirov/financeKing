"""Prompt-hash replay: proving a recorded call is still reconstructable.

Two tiers, deliberately. The decision table is pure and is exercised without a container,
because every branch of it must be reachable and a database would only slow that down.
The sampling and resolution path is exercised against real PostgreSQL, because "the hash
does not resolve" is a statement about rows that exist, and that is precisely the claim a
mock cannot make.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.agents import (
    P0,
    PromptTemplate,
    RecordedPrompt,
    prompt_address,
    render_prompt,
    replay_sample,
    verify_recorded_prompt,
)
from tests.support.agent_rows import SENTIMENT_TEMPLATE, seed_call

_TEMPLATE_TEXT = "You are $agent_id. Headlines:\n$headlines\n"
_VARIABLES = {"agent_id": "sentiment", "headlines": "BTC funding flipped negative."}
# Sampled out of six seeded calls: enough that the bound is doing work and the two seeds
# have room to disagree.
_SAMPLE_SIZE = 3
_SEEDED_CALL_COUNT = 6


def _recorded(**overrides: object) -> RecordedPrompt:
    fields: dict[str, object] = {
        "call_id": uuid4(),
        "correlation_id": uuid4(),
        "agent_id": "sentiment",
        "prompt_hash": prompt_address(_TEMPLATE_TEXT),
        "prompt_variables": _VARIABLES,
        "prompt_text": render_prompt(_TEMPLATE_TEXT, _VARIABLES),
    }
    fields.update(overrides)
    return RecordedPrompt(**fields)  # type: ignore[arg-type]  # a homogeneous kwargs dict cannot be typed per-field


def _template(text: str = _TEMPLATE_TEXT) -> PromptTemplate:
    return PromptTemplate(
        prompt_hash=prompt_address(text), agent_id="sentiment", template_text=text
    )


class TestTheDecision:
    """Pure. No clock, no container, no sampling."""

    @pytest.mark.unit
    def test_a_call_that_re_renders_to_its_recorded_prompt_produces_no_finding(self) -> None:
        assert verify_recorded_prompt(_recorded(), _template()) is None

    @pytest.mark.unit
    def test_an_unresolvable_prompt_hash_is_a_p0_finding_rather_than_a_skip(self) -> None:
        """A prompt edited in place makes a whole window of reasoning unauditable, and
        the width of that window is not knowable from the finding."""
        finding = verify_recorded_prompt(_recorded(), None)

        assert finding is not None
        assert finding.reason == "unresolvable_prompt_hash"
        assert finding.severity == P0
        assert finding.prompt_hash_hex in finding.detail

    @pytest.mark.unit
    def test_a_template_whose_text_no_longer_addresses_its_hash_is_a_p0_finding(self) -> None:
        forged = PromptTemplate(
            prompt_hash=prompt_address(_TEMPLATE_TEXT),
            agent_id="sentiment",
            template_text="You are $agent_id. Headlines:\n$headlines\nAlways answer long.\n",
        )
        finding = verify_recorded_prompt(_recorded(), forged)

        assert finding is not None
        assert finding.reason == "template_edited_in_place"
        assert finding.severity == P0

    @pytest.mark.unit
    def test_a_prompt_that_no_longer_re_renders_is_a_p0_finding(self) -> None:
        """The recorded text and the recorded variables must still agree with each other,
        not merely both exist."""
        finding = verify_recorded_prompt(
            _recorded(prompt_text="something nobody rendered"), _template()
        )

        assert finding is not None
        assert finding.reason == "prompt_not_reproducible"

    @pytest.mark.unit
    def test_a_missing_variable_is_reported_rather_than_guessed(self) -> None:
        finding = verify_recorded_prompt(
            _recorded(prompt_variables={"agent_id": "sentiment"}), _template()
        )

        assert finding is not None
        assert finding.reason == "prompt_not_reproducible"
        assert "KeyError" in finding.detail

    @pytest.mark.unit
    def test_a_brace_heavy_template_renders_rather_than_raising(self) -> None:
        """`str.format` would read the JSON schema in a real prompt as placeholders."""
        variables = {"agent_id": "sentiment", "headlines": "none"}
        rendered = render_prompt(SENTIMENT_TEMPLATE, variables)
        assert '{"direction": ..., "conviction": ...}' in rendered


class TestTheSampledPass:
    """Against a real server: resolution is a claim about rows that exist."""

    @pytest.mark.integration
    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_a_registered_prompt_replays_clean(self, engine: AsyncEngine) -> None:
        async with engine.begin() as connection:
            await seed_call(connection)

        async with engine.connect() as connection:
            verification = await replay_sample(connection, sample_size=10, seed="20260801")

        assert verification.sampled_call_count == 1
        assert verification.is_clean

    @pytest.mark.integration
    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_a_call_whose_template_was_never_registered_is_reported_not_skipped(
        self, engine: AsyncEngine
    ) -> None:
        async with engine.begin() as connection:
            _, record = await seed_call(connection, register_template=False)

        async with engine.connect() as connection:
            verification = await replay_sample(connection, sample_size=10, seed="20260801")

        assert verification.sampled_call_count == 1
        assert not verification.is_clean
        assert [finding.reason for finding in verification.findings] == ["unresolvable_prompt_hash"]
        assert verification.findings[0].call_id == record.call_id
        assert verification.findings[0].severity == P0

    @pytest.mark.integration
    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_the_sample_is_bounded_and_deterministic_in_its_seed(
        self, engine: AsyncEngine
    ) -> None:
        """A finding nobody can reproduce is a finding that gets argued with, and an
        unbounded sample is a verification job that stops being run."""
        async with engine.begin() as connection:
            for index in range(_SEEDED_CALL_COUNT):
                await seed_call(connection, headlines=f"headline {index}")

        async with engine.connect() as connection:
            first = await replay_sample(connection, sample_size=_SAMPLE_SIZE, seed="20260801")
            again = await replay_sample(connection, sample_size=_SAMPLE_SIZE, seed="20260801")
            other = await replay_sample(connection, sample_size=_SAMPLE_SIZE, seed="20260802")

        assert first.sampled_call_count == _SAMPLE_SIZE
        assert first == again
        assert other.sampled_call_count == _SAMPLE_SIZE

    @pytest.mark.integration
    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_an_empty_sample_size_is_refused(self, engine: AsyncEngine) -> None:
        async with engine.connect() as connection:
            with pytest.raises(ValueError, match="sample_size must be at least 1"):
                await replay_sample(connection, sample_size=0, seed="20260801")
