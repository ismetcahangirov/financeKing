# Rule — Quota Management

## The rule

**Free-tier LLM quota is an architectural constraint, not an operational detail** (`../../ARCHITECTURE.md` §9, §13).

The gateway in `fking.agents.gateway` is the only code in this repository that imports a provider SDK, and it owns admission control:

1. **A call reserves an estimated token cost before it is made, and reconciles to actual usage after.** A limit checked after the fact is a limit you have already breached.
2. **The ledger is persistent**, keyed by `(provider, model, window_kind, window_start)`, in PostgreSQL. Not an in-memory counter — an in-memory counter resets exactly when you are being rate limited and restarting.
3. **Priority classes decide who runs when quota is scarce.** Safety- and reconciliation-relevant agents run; exploratory ones do not.
4. **Exhaustion degrades to deterministic-only operation.** `complete()` returns a `Degraded` result object. It does not raise, does not stall, and does not produce an exception storm across every scheduled agent on the same beat.
5. **`429` handling honours `Retry-After` and never retries into the same exhausted window.** The window is marked cooled-down in the ledger, and the request either fails over to the other provider or returns `Degraded`.
6. **Responses are cached on `sha256(model | temperature | top_p | schema_hash | prompt)`**, and only when `temperature == 0`.
7. **Every agent declares a token budget, an invocation ceiling and a timeout in its own definition** (see `../../.claude/agents/quant.md`: "≤ 45k tokens, ≤ 5 invocations/day, 900s timeout").
8. **Configured limits can only lower the effective limit**, never raise it: the gateway takes `min(configured, HARD_CEILING)` against a compiled-in ceiling.

## Why

The system schedules dozens of agent invocations per day across research, evaluation, review and evolution, on free tiers, forever (`../../ARCHITECTURE.md` §13 lists "free tiers hold" as a standing assumption). That means quota is a shared, exhaustible, non-refundable resource with a hard cliff, and every design property follows from that:

**Reserve-then-reconcile, not measure-then-hope.** Token usage is only known after the response arrives. If you check the ledger before the call and add the actual cost after, then eight concurrent agents all see "plenty left", all fire, and the daily budget is gone by 09:14. Reserving an *estimate* under the same atomic statement that checks the limit is what makes the admission decision correct under concurrency. Reconciling afterwards is what keeps the estimate from drifting into fiction — an over-estimate returns unused quota, an under-estimate charges the difference.

**Persistence, because the failure mode is correlated.** The moment you are most likely to restart the process is the moment a provider started returning `429` and something crashed or was redeployed in response. An in-memory counter is zeroed by exactly that restart, so the system wakes up believing it has full quota, immediately fires the backlog, and gets rate-limited harder. The ledger has to survive the restart that the rate limiting caused.

**Degradation as a designed state, not an error path.** This system runs unattended. An exhausted quota that raises produces one exception per scheduled agent per beat, which fills the log with noise at exactly the moment you need the log to be readable, and does nothing useful. `Degraded` is a value the caller handles: the reconciler falls back to deterministic comparison, the evaluator falls back to the survival score without narrative commentary, and the exploratory agents skip the beat entirely. Nothing about the deterministic core requires an LLM — the agents sit on top of it (`../../ARCHITECTURE.md` §9) — so degradation is a real, complete mode of operation rather than a stub.

**Caching only at temperature zero.** Caching a sampled response is not a cache; it is a silent change to the agent's distribution. Judge and Critic agents are adversarial by construction and are supposed to disagree with each other (`../../CLAUDE.md` §10); serving one of them a cached answer from a sibling's identical prompt makes an agent panel converge for free, which is precisely the failure the panel exists to avoid.

## The numbers we do not have

**The published Gemini and Groq free-tier figures are unverified.** That research was cut short by a session limit on 2026-08-01 and is tracked as GitHub issue #19, recorded as **OQ-001** in `../../.claude/knowledge/open-questions.md`. Nothing in this repository states a vendor RPM, RPD or TPM as fact, and nothing should.

The consequence is a design property, not a caveat: **the ledger measures reality, and configuration supplies conservative floors below anything the vendor is believed to allow.**

```toml
# config/llm.toml
# These are OUR admission limits, not quoted vendor limits. They are deliberately
# low. The gateway's observed_limits view reports what the providers actually
# tolerated; raise these only from that evidence, and only in a pull request that
# cites it. Tracked as OQ-001 in .claude/knowledge/open-questions.md (issue #19).
[providers.gemini]
sdk = "google-genai"
role = "primary"
requests_per_minute = 4
requests_per_day = 80
tokens_per_minute = 60_000
tokens_per_day = 1_000_000

[providers.groq]
sdk = "groq"
role = "fallback"
requests_per_minute = 8
requests_per_day = 200
tokens_per_minute = 40_000
tokens_per_day = 400_000
```

The ledger's own history is the measurement instrument. Every `429` is recorded with the `(provider, model, window_kind, window_start)` that was live and the reserved/actual totals at that moment, so `observed_limits` reports the largest volume that was ever accepted and the smallest that was ever refused. Those two numbers bracket the real limit. Until they converge, the configured value stays below the lower bracket.

## Incorrect

```python
# src/fking/agents/quant/runner.py
import google.genai as genai

_CALLS_TODAY = 0
_DAILY_LIMIT = int(os.environ.get("GEMINI_DAILY_LIMIT", "1500"))


async def ask(prompt: str) -> str:
    global _CALLS_TODAY
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    for attempt in range(5):
        try:
            response = await client.aio.models.generate_content(
                model="gemini-2.5-flash", contents=prompt
            )
        except genai.errors.ClientError as exc:
            if exc.code == 429:
                await asyncio.sleep(2**attempt)
                continue
            raise
        _CALLS_TODAY += 1
        if _CALLS_TODAY > _DAILY_LIMIT:
            raise QuotaExhausted("daily limit reached")
        return response.text
    raise QuotaExhausted("rate limited after 5 attempts")
```

Five failures, in the order they bite:

- **The SDK is imported inside an agent module.** There is now no single place that knows what the project has spent, and the next agent will copy this file.
- **`_CALLS_TODAY` is process memory.** The container restarts at 03:00 for a deploy; the counter is zero; the day's budget is spent twice.
- **`_DAILY_LIMIT` is read from a mutable environment variable, with an invented default.** `1500` is not a measured number. Anyone can raise it by editing a `.env`, which is precisely the property `./safety-kernel.md` refuses for the same reason.
- **The counter is incremented *after* the call and checked *after* that.** By the time `QuotaExhausted` is raised, the over-limit request has already been sent and already counted against the provider. The check is decorative.
- **Exponential backoff into the same window.** `Retry-After` is ignored. On a per-day quota the retry at 1s, 2s, 4s, 8s, 16s all land inside the same exhausted 24-hour window, so all five fail, consume five requests' worth of the provider's rate-limit budget, and the function ends by raising into an unattended scheduler — which will call it again on the next beat, forever.

## Correct

```python
# src/fking/agents/gateway/quota.py
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


class Priority(enum.IntEnum):
    """Lower runs first and survives scarcity longest."""

    SAFETY = 0  # kill-switch explanation, incident triage
    RECONCILIATION = 1  # exchange/local divergence narration
    OPERATIONAL = 2  # risk commentary, trade supervision
    RESEARCH = 3  # hypothesis formulation, judging
    EXPLORATORY = 4  # mutation ideation, documentation drafting


# Fraction of the window's budget that must remain for a class to be admitted.
# SAFETY is admitted while any budget at all remains.
_ADMISSION_FLOOR: Final[dict[Priority, float]] = {
    Priority.SAFETY: 0.00,
    Priority.RECONCILIATION: 0.05,
    Priority.OPERATIONAL: 0.15,
    Priority.RESEARCH: 0.35,
    Priority.EXPLORATORY: 0.60,
}

# Compiled in, not configurable. config/llm.toml may only lower the effective
# limit; min() against this ceiling is what makes "raise the limit in config"
# structurally unavailable, in the same spirit as ./safety-kernel.md.
_HARD_CEILING: Final[dict[tuple[str, str], int]] = {
    ("gemini", "requests_per_day"): 200,
    ("gemini", "tokens_per_day"): 2_000_000,
    ("groq", "requests_per_day"): 400,
    ("groq", "tokens_per_day"): 800_000,
}


@dataclass(frozen=True, slots=True)
class Reservation:
    handle: UUID
    provider: str
    model: str
    window_start: datetime
    estimated_tokens: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Degraded:
    """Not an exception. A value the caller is expected to handle."""

    provider: str
    model: str
    reason: str  # "quota_exhausted" | "cooldown" | "priority_floor"
    retry_after: datetime | None


_RESERVE_SQL = text(
    """
    INSERT INTO llm_quota_ledger AS l (
        provider, model, window_kind, window_start,
        requests_reserved, tokens_reserved
    )
    VALUES (:provider, :model, :window_kind, :window_start, 1, :tokens)
    ON CONFLICT (provider, model, window_kind, window_start) DO UPDATE
       SET requests_reserved = l.requests_reserved + 1,
           tokens_reserved   = l.tokens_reserved + EXCLUDED.tokens_reserved
     WHERE l.cooldown_until IS NULL
       AND l.requests_reserved + 1 <= :request_limit
       AND l.tokens_reserved + EXCLUDED.tokens_reserved <= :token_limit
    RETURNING l.requests_reserved, l.tokens_reserved
    """
)


class QuotaProtocolError(RuntimeError):
    """A call was presented without a valid, unexpired reservation handle."""


class QuotaLedger:
    def __init__(self, conn: AsyncConnection, limits: ProviderLimits) -> None:
        self._conn = conn
        self._limits = limits

    def effective_limit(self, provider: str, field: str) -> int:
        configured = self._limits.get(provider, field)
        ceiling = _HARD_CEILING.get((provider, field))
        return configured if ceiling is None else min(configured, ceiling)

    async def reserve(
        self,
        *,
        provider: str,
        model: str,
        priority: Priority,
        estimated_tokens: int,
        now: datetime,
    ) -> Reservation | Degraded:
        request_limit = self.effective_limit(provider, "requests_per_day")
        token_limit = self.effective_limit(provider, "tokens_per_day")
        if estimated_tokens > token_limit:
            return Degraded(provider, model, "quota_exhausted", None)

        # Priority floors shrink the budget visible to lower classes, so an
        # exploratory agent is refused at 40% remaining while the incident
        # narrator is admitted at 2%.
        floor = _ADMISSION_FLOOR[priority]
        visible_requests = int(request_limit * (1.0 - floor))
        visible_tokens = int(token_limit * (1.0 - floor))

        window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        row = (
            await self._conn.execute(
                _RESERVE_SQL,
                {
                    "provider": provider,
                    "model": model,
                    "window_kind": "day",
                    "window_start": window_start,
                    "tokens": estimated_tokens,
                    "request_limit": visible_requests,
                    "token_limit": visible_tokens,
                },
            )
        ).first()
        if row is None:
            # ON CONFLICT ... DO UPDATE ... WHERE that matches nothing returns no
            # row and raises nothing. Zero rows IS the refusal.
            return Degraded(provider, model, "quota_exhausted", None)

        handle = uuid4()
        await self._conn.execute(
            text(
                """
                INSERT INTO llm_reservations
                    (handle, provider, model, window_kind, window_start,
                     estimated_tokens, expires_at)
                VALUES (:handle, :provider, :model, 'day', :window_start,
                        :tokens, :expires_at)
                """
            ),
            {
                "handle": handle,
                "provider": provider,
                "model": model,
                "window_start": window_start,
                "tokens": estimated_tokens,
                "expires_at": now + timedelta(seconds=900),
            },
        )
        return Reservation(
            handle=handle,
            provider=provider,
            model=model,
            window_start=window_start,
            estimated_tokens=estimated_tokens,
            expires_at=now + timedelta(seconds=900),
        )

    async def reconcile(
        self, reservation: Reservation, *, actual_tokens: int, now: datetime
    ) -> None:
        """Settle the estimate. Over-estimates return quota; under-estimates charge it."""
        deleted = (
            await self._conn.execute(
                text("DELETE FROM llm_reservations WHERE handle = :h RETURNING 1"),
                {"h": reservation.handle},
            )
        ).first()
        if deleted is None:
            raise QuotaProtocolError(
                f"reservation {reservation.handle} was already settled or never issued"
            )
        await self._conn.execute(
            text(
                """
                UPDATE llm_quota_ledger
                   SET tokens_actual   = tokens_actual + :actual,
                       tokens_reserved = tokens_reserved + :delta,
                       requests_actual = requests_actual + 1,
                       last_settled_at = :now
                 WHERE provider = :provider AND model = :model
                   AND window_kind = 'day' AND window_start = :window_start
                """
            ),
            {
                "actual": actual_tokens,
                "delta": actual_tokens - reservation.estimated_tokens,
                "now": now,
                "provider": reservation.provider,
                "model": reservation.model,
                "window_start": reservation.window_start,
            },
        )

    async def apply_cooldown(
        self, *, provider: str, model: str, window_start: datetime, until: datetime
    ) -> None:
        """Record a 429. The window is closed until `until`, and reserve() refuses."""
        await self._conn.execute(
            text(
                """
                UPDATE llm_quota_ledger
                   SET cooldown_until = greatest(
                           coalesce(cooldown_until, :until), :until),
                       rate_limit_hits = rate_limit_hits + 1
                 WHERE provider = :provider AND model = :model
                   AND window_kind = 'day' AND window_start = :window_start
                """
            ),
            {
                "provider": provider,
                "model": model,
                "window_start": window_start,
                "until": until,
            },
        )
```

```python
# src/fking/agents/gateway/client.py
class LLMGateway:
    """The only importer of a provider SDK in this repository."""

    async def complete(
        self,
        *,
        reservation: Reservation,
        request: StructuredRequest,
        now: datetime,
    ) -> StructuredResponse | Degraded:
        # The reservation is a required, typed parameter. There is no code path
        # that reaches a provider without one, and an unknown or expired handle
        # is a protocol error rather than a soft failure.
        if not await self._ledger.is_open(reservation, now=now):
            raise QuotaProtocolError(
                f"reservation {reservation.handle} is expired or unknown"
            )

        if request.temperature == Decimal("0"):
            cached = await self._cache.get(request.cache_key())
            if cached is not None:
                await self._ledger.reconcile(reservation, actual_tokens=0, now=now)
                await self._audit(request, cached, cache_hit=True)
                return cached

        try:
            raw = await self._providers[reservation.provider].generate(request)
        except ProviderRateLimited as limited:
            # Honour the server's window. Do not retry inside it, and do not
            # invent a backoff schedule the provider did not ask for.
            await self._ledger.apply_cooldown(
                provider=reservation.provider,
                model=reservation.model,
                window_start=reservation.window_start,
                until=limited.retry_after or now + timedelta(seconds=60),
            )
            await self._ledger.reconcile(reservation, actual_tokens=0, now=now)
            return Degraded(
                reservation.provider,
                reservation.model,
                "cooldown",
                limited.retry_after,
            )

        await self._ledger.reconcile(
            reservation, actual_tokens=raw.total_tokens, now=now
        )
        response = self._parse_or_fail(request.schema, raw)  # unparseable => failure
        await self._audit(request, response, cache_hit=False)
        if request.temperature == Decimal("0"):
            await self._cache.put(request.cache_key(), response)
        return response
```

`_audit` writes the prompt, response, model id, provider, temperature and both token counts to the append-only audit log (`./append-only-audit.md`), which is also what makes `observed_limits` reconstructable months later.

## Enforcement

**Schema.** `window` is a reserved word in PostgreSQL, hence `window_kind`.

```sql
CREATE TABLE llm_quota_ledger (
    provider          text        NOT NULL,
    model             text        NOT NULL,
    window_kind       text        NOT NULL CHECK (window_kind IN ('minute', 'day')),
    window_start      timestamptz NOT NULL,
    requests_reserved bigint      NOT NULL DEFAULT 0 CHECK (requests_reserved >= 0),
    requests_actual   bigint      NOT NULL DEFAULT 0 CHECK (requests_actual >= 0),
    tokens_reserved   bigint      NOT NULL DEFAULT 0 CHECK (tokens_reserved >= 0),
    tokens_actual     bigint      NOT NULL DEFAULT 0 CHECK (tokens_actual >= 0),
    rate_limit_hits   integer     NOT NULL DEFAULT 0,
    cooldown_until    timestamptz,
    last_settled_at   timestamptz,
    PRIMARY KEY (provider, model, window_kind, window_start)
);

CREATE TABLE llm_reservations (
    handle           uuid        PRIMARY KEY,
    provider         text        NOT NULL,
    model            text        NOT NULL,
    window_kind      text        NOT NULL,
    window_start     timestamptz NOT NULL,
    estimated_tokens integer     NOT NULL CHECK (estimated_tokens > 0),
    expires_at       timestamptz NOT NULL
);

-- Brackets the real limit from observation instead of from a vendor page.
CREATE VIEW observed_limits AS
SELECT provider,
       model,
       window_kind,
       max(requests_actual) FILTER (WHERE rate_limit_hits = 0) AS max_accepted_requests,
       min(requests_actual) FILTER (WHERE rate_limit_hits > 0) AS min_refused_requests,
       max(tokens_actual)   FILTER (WHERE rate_limit_hits = 0) AS max_accepted_tokens,
       min(tokens_actual)   FILTER (WHERE rate_limit_hits > 0) AS min_refused_tokens
  FROM llm_quota_ledger
 GROUP BY provider, model, window_kind;
```

**`import-linter`**, so the gateway is structurally the only path to a provider:

```toml
[[tool.importlinter.contracts]]
name = "LLM provider SDKs are reachable only through the gateway"
type = "forbidden"
source_modules = [
    "fking.agents.memory",
    "fking.agents.runtime",
    "fking.agents.panel",
    "fking.api",
    "fking.backtest",
    "fking.data",
    "fking.domain",
    "fking.evolution",
    "fking.execution",
    "fking.platform",
    "fking.risk",
    "fking.strategy",
]
forbidden_modules = ["google.genai", "google.generativeai", "groq", "openai"]
allow_indirect_imports = true
```

**`ruff` banned API**, catching the same mistake at the call site with an actionable message:

```toml
[tool.ruff.lint.flake8-tidy-imports.banned-api]
"google.genai.Client".msg = "Go through fking.agents.gateway.LLMGateway."
"groq.AsyncGroq".msg = "Go through fking.agents.gateway.LLMGateway."

[tool.ruff.lint.per-file-ignores]
"src/fking/agents/gateway/providers/*.py" = ["TID251"]
```

**Tests** (`tests/agents/gateway/`, real Postgres via testcontainers):

```python
import pytest


async def test_exhausted_ledger_returns_degraded_rather_than_raising(
    ledger, clock
) -> None:
    limit = ledger.effective_limit("gemini", "requests_per_day")
    for _ in range(limit):
        assert isinstance(
            await ledger.reserve(
                provider="gemini", model="gemini-2.5-flash",
                priority=Priority.SAFETY, estimated_tokens=100, now=clock.now(),
            ),
            Reservation,
        )
    result = await ledger.reserve(
        provider="gemini", model="gemini-2.5-flash",
        priority=Priority.SAFETY, estimated_tokens=100, now=clock.now(),
    )
    assert isinstance(result, Degraded)
    assert result.reason == "quota_exhausted"


async def test_ledger_survives_a_process_restart(pg_dsn, clock) -> None:
    async with new_gateway(pg_dsn) as first:
        await first.ledger.reserve(
            provider="groq", model="llama-3.3-70b-versatile",
            priority=Priority.RESEARCH, estimated_tokens=5_000, now=clock.now(),
        )
    async with new_gateway(pg_dsn) as second:  # fresh process, fresh memory
        spent = await second.ledger.tokens_reserved(
            provider="groq", model="llama-3.3-70b-versatile", now=clock.now()
        )
    assert spent == 5_000


async def test_call_without_a_reservation_handle_is_a_protocol_error(gateway) -> None:
    stale = Reservation(handle=uuid4(), provider="gemini", model="m",
                        window_start=..., estimated_tokens=1, expires_at=...)
    with pytest.raises(QuotaProtocolError, match="expired or unknown"):
        await gateway.complete(reservation=stale, request=any_request(), now=now())


async def test_429_does_not_retry_inside_the_same_window(gateway, provider, clock):
    provider.fail_next_with_429(retry_after=clock.now() + timedelta(seconds=300))
    result = await gateway.complete(reservation=res, request=req, now=clock.now())
    assert isinstance(result, Degraded) and result.reason == "cooldown"
    assert provider.call_count == 1  # exactly one attempt, no backoff loop

    clock.advance(seconds=120)  # still inside the Retry-After window
    assert isinstance(
        await gateway.ledger.reserve(
            provider="gemini", model="m", priority=Priority.SAFETY,
            estimated_tokens=10, now=clock.now(),
        ),
        Degraded,
    )


@pytest.mark.parametrize(
    ("priority", "consumed_fraction", "admitted"),
    [
        (Priority.EXPLORATORY, 0.45, False),
        (Priority.RESEARCH, 0.45, True),
        (Priority.RESEARCH, 0.70, False),
        (Priority.SAFETY, 0.99, True),
    ],
)
async def test_priority_floors_ration_scarce_quota(
    ledger, clock, priority, consumed_fraction, admitted
) -> None:
    await ledger.seed_consumption("gemini", fraction=consumed_fraction, now=clock.now())
    result = await ledger.reserve(
        provider="gemini", model="gemini-2.5-flash", priority=priority,
        estimated_tokens=100, now=clock.now(),
    )
    assert isinstance(result, Reservation) is admitted


def test_configuration_cannot_raise_a_limit_above_the_compiled_ceiling(ledger):
    ledger._limits.override("gemini", "requests_per_day", 100_000)
    assert ledger.effective_limit("gemini", "requests_per_day") == 200


async def test_sampled_responses_are_never_cached(gateway, cache) -> None:
    await gateway.complete(
        reservation=res, request=request_at_temperature("0.7"), now=now()
    )
    assert cache.size == 0
```

**Per-agent budgets** are declared in the agent definition and enforced by the runtime before the reservation is requested — an agent that has spent its daily invocation ceiling never reaches the ledger at all, so its exhaustion is local and does not consume project-wide quota accounting. `../../.claude/agents/quant.md` is the reference form: token budget, invocation ceiling, timeout, and a stated behaviour under exhaustion mid-task.

## The one exception

**A single retry after a `Retry-After` window has actually elapsed is not a quota bypass.**

Precisely: the gateway may re-attempt a request once, if and only if all four hold — the provider returned `429` with a `Retry-After` header; the clock has passed `cooldown_until`; the ledger's `reserve()` grants a *fresh* reservation for the now-current window; and the caller's deadline still permits it. That is not evasion, it is compliance — the provider told you when to come back, and coming back then is the cooperative behaviour.

Everything else is a bypass, and the sharpest example is the one that looks most reasonable: **reading the limit from a mutable config in order to raise it.** `GEMINI_DAILY_LIMIT=5000` in a `.env`, an operator bumping `requests_per_day` in `config/llm.toml` because a research run stalled, a "temporary" override for a backfill. All of these are the same move, and they are refused by the same argument `./safety-kernel.md` makes about the allowlist: configuration is precisely what changes, so a limit that lives only in configuration is not a limit. `min(configured, _HARD_CEILING)` is what makes the refusal structural — the config file can lower the limit and can never raise it, and widening the ceiling requires a source edit and a reviewed pull request.

Also bypasses, for the avoidance of doubt: sleeping and retrying without a `Retry-After` to justify the interval; rotating to a second API key on the same free tier; splitting one prompt into three to slip under a per-request token cap; and catching `Degraded` and looping. `Degraded` is a terminal answer for this beat. The correct response to it is to run the deterministic path and try again on the next schedule.
