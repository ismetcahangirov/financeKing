---
description: Behaviour-preserving refactor with a proof that behaviour was preserved
argument-hint: <module, file, or symbol>
allowed-tools: Read, Grep, Glob, Bash, Write, Edit
---

Refactor: $ARGUMENTS

A refactor changes structure and **nothing else**. If behaviour changes, it is not a refactor and must not be committed as one — a commit mixing a refactor with a behaviour change is unreviewable and unrevertable.

## 1. Justify it in one sentence

Valid reasons: a module boundary is being violated; a duplicated concept has now got its second concrete caller and can be extracted; a function knows about two things that should be separated; a name lies about units.

Not valid: it looks nicer; a pattern would be more elegant; it might be needed later. Speculative restructuring is the main way codebases become unnavigable.

## 2. Pin current behaviour first

```bash
make check
make test ARGS="tests/<area> --cov=src/fking/<module> --cov-report=term-missing"
```

If coverage on the target is below its floor, **write the missing tests before refactoring, in a separate commit.** Refactoring untested code is rewriting it blind.

For anything touching position or risk arithmetic, add a characterization property test first: generate inputs with Hypothesis, record the current outputs, assert they are unchanged after the change. That is the only honest proof of preservation for math with this many edge cases.

For anything touching a strategy or the backtest engine, capture a golden run:

```bash
make backtest CONFIG=configs/backtest/<pinned>.toml > /tmp/before.json
```

Trade-for-trade identity after the refactor is the acceptance criterion. Not "similar Sharpe" — identical fills, identical timestamps.

## 3. Refactor in reversible steps

One structural move per commit. Do not rename and move in the same commit; git will lose the history and the diff becomes unreadable.

While moving code, do not "fix" things you notice. Write them down and do them afterwards in their own commit.

Preserve on the way through:
- `Decimal` construction from `str` — a refactor that "simplifies" `Decimal("0.1")` to `Decimal(0.1)` silently corrupts money math.
- Timezone-aware UTC — do not let a helper drop `tzinfo`.
- Frozen domain objects.
- Injected clocks and seeds — do not "simplify" a clock parameter back to `datetime.now()`.
- Provenance comments on constants. If you move a magic number, move its comment with it, or the next reader deletes it.

## 4. Check the boundaries still hold

```bash
make check
```

`import-linter` must be green without any contract being relaxed. If the refactor requires weakening a contract, the refactor is wrong: the contracts that `strategy` cannot import `execution`, and that `execution` cannot import raw HTTP clients, are the architecture rather than lint noise.

## 5. Prove preservation

```bash
make backtest CONFIG=configs/backtest/<pinned>.toml > /tmp/after.json
diff /tmp/before.json /tmp/after.json
```

An empty diff, or a characterization test suite passing unchanged, is the proof. Paste it.

If there is a difference you believe is an improvement, stop: that is a behaviour change. Split it into its own PR with its own justification.

## 6. Report

What moved, why, and the preservation evidence. Explicitly state that no behaviour changed — or that it did, and that the work has been split.
