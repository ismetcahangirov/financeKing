"""The audit row refuses to describe a call that could not have happened.

Every guard here exists because the row is the only evidence that survives log
retention. A row whose timestamp is naive is a row nobody can place on a timeline
months later, and a row claiming a validated response with no response text is a row
asserting a decision with nothing behind it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fking.agents import AgentCallContext, AgentCallRecord, CompletionResult
from tests.agents.conftest import (
    CALL_ID,
    CALLED_AT_UTC,
    CORRELATION_ID,
    PROMPT_HASH,
    RUN_ID,
    completion,
)

pytestmark = pytest.mark.unit

BAKU = timezone(timedelta(hours=4))


def a_context(**overrides: object) -> AgentCallContext:
    fields: dict[str, object] = {
        "call_id": CALL_ID,
        "run_id": RUN_ID,
        "correlation_id": CORRELATION_ID,
        "agent_id": "sentiment",
        "provider": "gemini",
        "model_id": "gemini-2.5-flash-002",
        "temperature": Decimal("0"),
        "prompt_hash": PROMPT_HASH,
        "prompt_variables": {"role": "analyst"},
        "prompt_text": "You are an analyst.",
        "called_at_utc": CALLED_AT_UTC,
    }
    return AgentCallContext(**{**fields, **overrides})  # type: ignore[arg-type]  # validated in __post_init__


class TestTimestampsAreAwareUtc:
    def test_a_naive_called_at_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            a_context(called_at_utc=datetime(2026, 8, 5, 9, 30))  # noqa: DTZ001 - the value under test

    def test_an_aware_non_utc_called_at_is_rejected_rather_than_converted(self) -> None:
        """`astimezone(UTC)` would launder a guess made three modules ago into a
        confident value."""
        with pytest.raises(ValueError, match="must be UTC"):
            a_context(called_at_utc=datetime(2026, 8, 5, 13, 30, tzinfo=BAKU))

    def test_a_record_applies_the_same_guard(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            AgentCallRecord(
                call_id=CALL_ID,
                run_id=RUN_ID,
                correlation_id=CORRELATION_ID,
                agent_id="sentiment",
                provider="gemini",
                model_id="gemini-2.5-flash-002",
                temperature=Decimal("0"),
                prompt_hash=PROMPT_HASH,
                prompt_variables={},
                prompt_text="p",
                response_text="r",
                prompt_token_count=1,
                completion_token_count=1,
                latency_ms=1,
                cache_hit=False,
                schema_valid=True,
                called_at_utc=datetime(2026, 8, 5),  # noqa: DTZ001 - the value under test
            )


class TestImplausibleValuesAreRefused:
    def test_a_negative_temperature_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            a_context(temperature=Decimal("-0.1"))

    @pytest.mark.parametrize(
        "field_name", ["prompt_token_count", "completion_token_count", "latency_ms"]
    )
    def test_a_negative_count_is_rejected_and_named(self, field_name: str) -> None:
        """Named rather than counted: a negative latency is an NTP step or a wall-clock
        subtraction, and knowing which field carried it is the whole diagnosis."""
        fields: dict[str, object] = {
            "text": "{}",
            "prompt_token_count": 1,
            "completion_token_count": 1,
            "latency_ms": 1,
            "cache_hit": False,
        }
        fields[field_name] = -1
        with pytest.raises(ValueError, match=field_name):
            CompletionResult(**fields)  # type: ignore[arg-type]  # validated in __post_init__

    def test_a_validated_row_with_no_response_text_is_refused(self) -> None:
        """The `agent_call` table encodes the same rule as a CHECK constraint."""
        with pytest.raises(ValueError, match="no evidence behind it"):
            AgentCallRecord(
                call_id=CALL_ID,
                run_id=RUN_ID,
                correlation_id=CORRELATION_ID,
                agent_id="sentiment",
                provider="gemini",
                model_id="gemini-2.5-flash-002",
                temperature=Decimal("0"),
                prompt_hash=PROMPT_HASH,
                prompt_variables={},
                prompt_text="p",
                response_text=None,
                prompt_token_count=1,
                completion_token_count=1,
                latency_ms=1,
                cache_hit=False,
                schema_valid=True,
                called_at_utc=CALLED_AT_UTC,
            )


class TestTheRowIsAssembledFromTheCall:
    def test_from_call_copies_every_field_without_translation(self) -> None:
        """The record mirrors `agent_call` column for column, so there is no mapping
        dictionary to pair the wrong two names."""
        context = a_context()
        record = AgentCallRecord.from_call(context, completion("{}"), schema_valid=False)
        assert record.response_text == "{}"
        assert record.prompt_text == context.prompt_text
        assert record.model_id == context.model_id
        assert record.cache_hit is False

    def test_a_record_is_immutable(self) -> None:
        record = AgentCallRecord.from_call(a_context(), completion("{}"), schema_valid=False)
        with pytest.raises((AttributeError, TypeError)):
            record.schema_valid = True  # type: ignore[misc]  # frozen dataclass, by design

    def test_a_utc_call_at_the_epoch_boundary_is_accepted(self) -> None:
        assert a_context(called_at_utc=datetime(2026, 1, 1, tzinfo=UTC)).called_at_utc.tzinfo is UTC
