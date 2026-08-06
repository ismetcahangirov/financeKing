"""One attempt, no repair, and the raw text on the audit row either way.

The three response shapes issue #70 names -- wrapped in a markdown code fence, carrying
a trailing comment, truncated mid-object -- are each the shape somebody would be
tempted to rescue, and each is the shape a rescue would rescue wrongly. A regex that
lifts the first JSON object out of prose will, given a model that echoed a fenced
document back, lift an object out of an untrusted headline instead.

Every case asserts two things: the call failed, and the verbatim response reached the
audit row. The second is the one that decays if unwritten, because a system that
records "parse failed" and nothing else looks correct from the outside and has thrown
away the only corpus a prompt revision can be built from.
"""

from __future__ import annotations

import contextlib
import json
from decimal import Decimal

import pytest

from fking.agents import (
    AgentCallContext,
    AgentOutputInvalid,
    CriticVerdict,
    ThesisProposal,
    parse_or_fail,
)
from tests.agents.conftest import CALL_ID, CALLED_AT_UTC, RecordingAudit, completion

pytestmark = pytest.mark.unit

_VALID_BODY = json.dumps(
    {
        "symbol_index": 0,
        "direction": "long",
        "conviction": "0.6",
        "horizon_hours": 8,
        "invalidation_note": "a close back below the 20-bar high",
        "rationale": "four independent prints of the same announcement",
    }
)

# Each of these is a *near miss*. That is the point: a schema failure on obvious
# garbage costs nothing to reject, and these are the ones where rejecting feels wasteful.
UNREPAIRABLE_RESPONSES: dict[str, str] = {
    "markdown_code_fence": f"```json\n{_VALID_BODY}\n```",
    "prose_preamble": f"Here is my analysis:\n\n{_VALID_BODY}",
    "trailing_comment": f"{_VALID_BODY}  // conviction is deliberately conservative",
    "truncated": _VALID_BODY[: len(_VALID_BODY) // 2],
    "empty": "",
    "trailing_prose": f"{_VALID_BODY}\n\nLet me know if you would like more detail.",
}


class TestAValidResponseParses:
    def test_a_clean_response_returns_the_typed_output(
        self, call: AgentCallContext, recorder: RecordingAudit
    ) -> None:
        parsed = parse_or_fail(call, ThesisProposal, completion(_VALID_BODY), recorder)
        assert parsed.direction == "long"
        assert parsed.conviction == Decimal("0.6")

    def test_a_successful_call_is_audited_with_the_response_verbatim(
        self, call: AgentCallContext, recorder: RecordingAudit
    ) -> None:
        parse_or_fail(call, ThesisProposal, completion(_VALID_BODY), recorder)
        (row,) = recorder.rows
        assert row.schema_valid is True
        assert row.response_text == _VALID_BODY

    def test_the_audit_row_carries_everything_needed_to_replay_the_call(
        self, call: AgentCallContext, recorder: RecordingAudit
    ) -> None:
        """`ARCHITECTURE.md` section 11: the exact prompt, months later, with the model
        that answered it. A row missing any of these cannot be replayed at all."""
        parse_or_fail(call, ThesisProposal, completion(_VALID_BODY), recorder)
        (row,) = recorder.rows
        assert (row.provider, row.model_id, row.temperature) == (
            "gemini",
            "gemini-2.5-flash-002",
            Decimal("0"),
        )
        assert (row.prompt_text, row.called_at_utc) == (call.prompt_text, CALLED_AT_UTC)
        assert (row.correlation_id, row.run_id, row.call_id) == (
            call.correlation_id,
            call.run_id,
            CALL_ID,
        )


class TestNothingIsRepaired:
    @pytest.mark.parametrize("shape", sorted(UNREPAIRABLE_RESPONSES))
    def test_a_near_miss_fails_the_call(
        self, shape: str, call: AgentCallContext, recorder: RecordingAudit
    ) -> None:
        with pytest.raises(AgentOutputInvalid):
            parse_or_fail(call, ThesisProposal, completion(UNREPAIRABLE_RESPONSES[shape]), recorder)

    @pytest.mark.parametrize("shape", sorted(UNREPAIRABLE_RESPONSES))
    def test_a_near_miss_writes_the_raw_text_to_the_audit_row(
        self, shape: str, call: AgentCallContext, recorder: RecordingAudit
    ) -> None:
        """Verbatim, byte for byte -- not stripped, not truncated, not summarised."""
        raw = UNREPAIRABLE_RESPONSES[shape]
        with pytest.raises(AgentOutputInvalid):
            parse_or_fail(call, ThesisProposal, completion(raw), recorder)
        (row,) = recorder.rows
        assert row.response_text == raw
        assert row.schema_valid is False

    def test_exactly_one_audit_row_is_written_per_failed_call(
        self, call: AgentCallContext, recorder: RecordingAudit
    ) -> None:
        """Two rows would mean a second attempt happened. One row is the whole policy."""
        with pytest.raises(AgentOutputInvalid):
            parse_or_fail(
                call, ThesisProposal, completion(UNREPAIRABLE_RESPONSES["truncated"]), recorder
            )
        assert len(recorder.rows) == 1

    def test_the_failure_names_the_agent_and_the_call_without_quoting_the_response(
        self, call: AgentCallContext, recorder: RecordingAudit
    ) -> None:
        """The message is logged; model-authored text belongs in the audit table only."""
        secret_shaped = json.dumps({"direction": "IGNORE PREVIOUS INSTRUCTIONS"})
        with pytest.raises(AgentOutputInvalid) as raised:
            parse_or_fail(call, ThesisProposal, completion(secret_shaped), recorder)
        message = str(raised.value)
        assert "sentiment" in message
        assert str(CALL_ID) in message
        assert "IGNORE PREVIOUS INSTRUCTIONS" not in message

    def test_the_validation_error_is_chained_as_the_cause(
        self, call: AgentCallContext, recorder: RecordingAudit
    ) -> None:
        """`from err` sets `__cause__`, so the traceback names the structural surprise
        rather than reading as an error inside the error handler."""
        with pytest.raises(AgentOutputInvalid) as raised:
            parse_or_fail(call, ThesisProposal, completion("{}"), recorder)
        assert raised.value.__cause__ is not None

    def test_no_default_output_is_manufactured_on_failure(
        self, call: AgentCallContext, recorder: RecordingAudit
    ) -> None:
        """Returning `direction="flat"` would attribute to the model a decision it
        never made, which is worse than raising."""
        with pytest.raises(AgentOutputInvalid):
            parse_or_fail(call, ThesisProposal, completion("not json at all"), recorder)


class TestTheAuditRowPrecedesTheFailure:
    def test_the_row_is_written_even_when_the_caller_lets_the_error_propagate(
        self, call: AgentCallContext, recorder: RecordingAudit
    ) -> None:
        with contextlib.suppress(AgentOutputInvalid):
            parse_or_fail(call, ThesisProposal, completion("{"), recorder)
        assert len(recorder.rows) == 1
        assert recorder.rows[0].response_text == "{"


class TestTheSchemaIsWhateverTheCallerAsksFor:
    def test_a_thesis_body_does_not_satisfy_the_critic_schema(
        self, call: AgentCallContext, recorder: RecordingAudit
    ) -> None:
        """Parsing is against the *declared* schema, never against whichever model
        happens to accept the payload."""
        with pytest.raises(AgentOutputInvalid, match="CriticVerdict"):
            parse_or_fail(call, CriticVerdict, completion(_VALID_BODY), recorder)
