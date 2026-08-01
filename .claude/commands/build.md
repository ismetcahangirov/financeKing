---
description: Implement a planned change test-first, honouring the non-negotiables, ending in a green make check
argument-hint: <task or issue number>
allowed-tools: Read, Grep, Glob, Bash, Write, Edit
---

Implement: $ARGUMENTS

If there is no plan yet, run `/plan` first. Building without a stated contract in a codebase written mostly by AI across sessions with no shared memory is how the contracts drift.

## 1. Confirm you are on the right branch

```bash
git branch --show-current
```

Must be `<type>/<issue-number>-<kebab-slug>`. If it is `main`, stop and run `/new-task`.

## 2. Write the failing test first

Start from the contract in the plan. The test asserts **behaviour**, not implementation — a test that breaks when you rename a private method is a liability.

For anything in `risk`, `domain`, or any position arithmetic, a Hypothesis property test is mandatory, not optional:

```python
@given(
    entry=decimals(min_value="0.01", places=2),
    qty=decimals(min_value="0.0001", places=8),
    ...
)
def test_position_close_never_flips_sign_unintentionally(...): ...
```

Example-based tests confirm the cases you thought of. Position arithmetic fails on the cases you did not: partial closes, direction flips, zero-crossings, dust quantities.

Run it and watch it fail for the right reason:

```bash
make test ARGS="tests/<path> -x -v"
```

A test that passes before the implementation exists is testing nothing.

## 3. Implement, minimally

Non-negotiables while writing:

- `Decimal` for every price, quantity and monetary amount, constructed from `str`. `Decimal(0.1) != Decimal("0.1")`, and that error accumulates across thousands of fills into reconciliation drift that looks like an exchange bug.
- Timezone-aware UTC everywhere; reject naive datetimes at construction. Crypto has no session boundary to make a timezone error obvious.
- Domain objects immutable; transitions return new objects.
- Strategies emit `Signal`, never `Order`.
- Clock injected as a parameter; no `datetime.now()` in `strategy` or `risk`.
- No randomness without an injected seed.
- No direct `httpx`/`aiohttp`/`websockets`/`requests` construction — use `fking.platform.safety.guarded_client()`.
- Bus consumers idempotent. Redis Streams is at-least-once by design; write the dedupe on the way in, not after the first double-fill.
- `mypy --strict` clean. Any `# type: ignore` carries an inline comment saying why it is unavoidable.
- Errors fail loud: catch the specific exception you can handle, never bare `Exception` to keep going. Validate at boundaries — API, exchange responses, config, agent output — then trust internally. Exchange responses are hostile input; never index into them optimistically.
- Comment *why*, never *what*. Every non-obvious constant gets a source, e.g. `# Binance spot switched to microsecond timestamps on 2025-01-01; see docs/adr/0013`.

## 4. No fake implementations

No placeholder functions. No `raise NotImplementedError` left behind. No `TODO` standing in for the work. If something cannot be implemented because information is missing, ask for it. If it is genuinely out of scope, say so explicitly in the PR rather than leaving a stub that looks finished.

## 5. Instrument as you go

Emit the event, propagate the correlation ID that originated at the top of the flow, write the audit row. Deferring instrumentation until the end means it never gets added properly and is missing from exactly the history an investigation needs.

## 6. Verify for real

```bash
make check
```

Green, in this transcript, before you say anything works. Then check the floor for what you touched:

```bash
make test ARGS="--cov=src/fking/<module> --cov-report=term-missing"
```

Floors: `platform/safety` 100%, `risk` 95%, `domain` 95%, `execution` 90%, everything else 80%.

## 7. Self-review the diff

```bash
git diff
```

- Debug output, commented-out code, scratch files?
- Anything handling money as `float`?
- Any network call bypassing `guarded_client()`?
- Any mutable domain object added?
- Are the tests meaningful, or do they just execute the code?
- Would someone reading this in six months know *why*, not just *what*?

## 8. Report

What changed, the verification output, coverage against floors, and anything left out and why. If tests were not run, say they were not run.
