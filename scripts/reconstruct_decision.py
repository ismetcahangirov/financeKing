"""Print the full agent contribution to one decision, from audit rows alone.

    uv run python scripts/reconstruct_decision.py --correlation-id <uuid>

`ARCHITECTURE.md` section 11 requires that any trade be reconstructable months later with
no access to application memory. This script is the test of that claim rather than a
convenience: it opens a connection, reads `agent_call` by `correlation_id`, and prints
every field an investigator needs -- verbatim prompt, verbatim response, model id,
provider, temperature, token counts, latency, cache-served flag and the schema verdict.

It joins nothing and imports no runtime. If a field is missing from this output, the
audit row is incomplete, and no amount of log retention will fix that after the fact.

`--verify-prompt` additionally resolves each `prompt_hash` in `prompt_template` and
re-renders it. An unresolvable address exits 2: the decision printed above it is one whose
prompt version can no longer be identified, and printing it without saying so would be
worse than printing nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

from sqlalchemy import text

from fking.agents import (
    PromptTemplate,
    RecordedPrompt,
    ReplayFinding,
    agent_contribution,
    verify_recorded_prompt,
)
from fking.platform.config import load_settings
from fking.platform.persistence.engine import build_engine

_UNRESOLVABLE_PROMPT_EXIT_CODE = 2


def _parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--correlation-id",
        required=True,
        type=UUID,
        help="the id minted at the top of the flow",
    )
    parser.add_argument(
        "--verify-prompt",
        action="store_true",
        help="resolve each prompt_hash and re-render it; exit 2 on a P0 finding",
    )
    return parser.parse_args(argv)


async def _run(correlation_id: UUID, *, verify_prompt: bool) -> int:
    engine = build_engine(load_settings().database)
    try:
        async with engine.connect() as connection:
            contributions = await agent_contribution(connection, correlation_id)
            findings: list[ReplayFinding] = []
            if verify_prompt:
                for contribution in contributions:
                    row = (
                        await connection.execute(
                            text(
                                "SELECT prompt_hash, agent_id, template_text FROM prompt_template "
                                "WHERE prompt_hash = :h"
                            ),
                            {"h": contribution.prompt_hash},
                        )
                    ).first()
                    template = (
                        PromptTemplate(
                            prompt_hash=bytes(row.prompt_hash),
                            agent_id=row.agent_id,
                            template_text=row.template_text,
                        )
                        if row is not None
                        else None
                    )
                    finding = verify_recorded_prompt(
                        RecordedPrompt(
                            call_id=contribution.call_id,
                            correlation_id=contribution.correlation_id,
                            agent_id=contribution.agent_id,
                            prompt_hash=contribution.prompt_hash,
                            prompt_variables=contribution.prompt_variables,
                            prompt_text=contribution.prompt_text,
                        ),
                        template,
                    )
                    if finding is not None:
                        findings.append(finding)
    finally:
        await engine.dispose()

    if not contributions:
        print(f"no agent calls recorded for correlation_id {correlation_id}")
        return 0

    for contribution in contributions:
        print("=" * 78)
        print(f"seq              {contribution.seq}")
        print(f"call_id          {contribution.call_id}")
        print(f"run_id           {contribution.run_id}")
        print(f"correlation_id   {contribution.correlation_id}")
        print(f"agent_id         {contribution.agent_id}")
        print(f"provider/model   {contribution.provider} / {contribution.model_id}")
        print(f"temperature      {contribution.temperature}")
        print(f"called_at_utc    {contribution.called_at_utc.isoformat()}")
        print(
            f"tokens           prompt={contribution.prompt_token_count} "
            f"completion={contribution.completion_token_count}"
        )
        print(f"latency_ms       {contribution.latency_ms}")
        print(f"cache_hit        {contribution.cache_hit}")
        print(f"schema_valid     {contribution.schema_valid}")
        print(f"prompt_hash      {contribution.prompt_hash.hex()}")
        print(f"prompt_variables {dict(contribution.prompt_variables)}")
        print("-- prompt (verbatim) " + "-" * 57)
        print(contribution.prompt_text)
        print("-- response (verbatim, untrusted model output) " + "-" * 31)
        print(contribution.response_text or "<no response>")

    for finding in findings:
        print(f"\n[{finding.severity.upper()}] {finding.reason}: {finding.detail}", file=sys.stderr)
    return _UNRESOLVABLE_PROMPT_EXIT_CODE if findings else 0


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    return asyncio.run(_run(arguments.correlation_id, verify_prompt=arguments.verify_prompt))


if __name__ == "__main__":
    raise SystemExit(main())
