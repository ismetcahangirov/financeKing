---
description: Review a diff against this project's actual failure modes, blocking on the non-negotiables
argument-hint: [pr-number]
allowed-tools: Read, Grep, Glob, Bash
---

Review $ARGUMENTS (default: the working branch diff against `main`).

```bash
gh pr diff $1 2>/dev/null || git diff main...HEAD
gh pr view $1 --json title,body,labels,additions,deletions 2>/dev/null
```

Review for the defects this project actually produces, in this order. Findings are **Blocking**, **Should fix**, or **Note**, each with file:line.

## Blocking classes

**1. Money as `float`.** Any price, quantity, or monetary amount typed or constructed as `float`; any `Decimal(0.1)` constructed from a float literal instead of `str`; any `round()` on money without an explicit quantize.

```bash
git diff main...HEAD | grep -nE "float|\.0\b|Decimal\([^\"']"
```

**2. Naive datetimes / clock access.** Any `datetime` without tzinfo; any `datetime.now()` or `time.time()` inside `strategy/` or `risk/` — the clock is a parameter there, and reading it makes the code untestable and non-reproducible.

**3. Direct HTTP/WebSocket construction.** Anything in the execution path building its own client instead of `guarded_client()`. Also flag anything that widens the safety allowlist, adds an override flag, or moves an `import-linter` contract into `ignore_imports`.

**4. `strategy` importing `execution`,** or a strategy computing a quantity, notional, or leverage. A strategy that sizes its own positions can bankrupt the portfolio regardless of signal quality.

**5. Non-idempotent bus consumer.** Redis Streams delivers at least once. A consumer that applies a fill without a dedupe key produces a position bug that only reproduces under redelivery.

**6. Mutable domain object.** A `domain/` dataclass without `frozen=True`, or an in-place state mutation instead of returning a new object.

**7. Audit table mutability.** Any migration granting UPDATE or DELETE on an audit table, or dropping a rejecting trigger.

**8. Look-ahead.** In feature or backtest code: centred rolling windows, `shift(-n)`, normalization computed over the full range, joins that forward-fill from the future, labels leaking into features. This defect does not fail — it makes bad strategies look excellent.

**9. Swallowed errors.** Bare `except Exception` that continues, an error logged and ignored, an exchange response indexed optimistically. A trading system that continues after an unexpected state is more dangerous than one that stops.

**10. Fake implementation.** `NotImplementedError`, `TODO`, a stub that looks finished, documentation promising later work.

## Should-fix classes

- **Speculative abstraction**: a base class, protocol, or registry with one implementation. Two concrete callers before an abstraction exists.
- **Ambiguous names**: `size`, `price`, `timeout`, `amount` without units. `base_quantity`, `quote_price`, `timeout_seconds`.
- **Unsourced constant** in `risk/` or the cost model. A magic number with no provenance will be "cleaned up" by someone who does not know what it protects against.
- **Tests that only execute code**: no assertion on behaviour, or asserting on a private method's name.
- **Hand-written exchange fixtures** instead of recorded real responses; **mocked database** instead of the Postgres service container.
- **Missing Hypothesis properties** on new position or risk arithmetic.
- **Missing instrumentation**: a new cross-module hop with no event, no correlation ID propagation, or no audit row.
- **Commit hygiene**: a refactor mixed with a behaviour change in one commit — unreviewable and unrevertable.

## Verify rather than assume

Do not accept the PR body's verification claim. Check it:

```bash
gh pr checks $1
make check
make test ARGS="--cov=src/fking/<touched module> --cov-report=term-missing"
```

Compare against the floor for each touched module (`platform/safety` 100%, `risk` 95%, `domain` 95%, `execution` 90%, else 80%). A PR body claiming a green build that CI did not run is itself a blocking finding.

## Size

Over ~400 substantive changed lines: say so and propose the split. An unreviewable PR is an unreviewed PR.

## Verdict

**Approve** / **Approve with comments** / **Request changes**, and — if approving — one sentence on what you actually verified rather than read.
