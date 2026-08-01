---
description: Audit the risk engine or a risk-affecting change — sizing, limits, netting, kill switch
argument-hint: [module path or PR number]
allowed-tools: Read, Grep, Glob, Bash
---

Risk audit of $ARGUMENTS (default: `src/fking/risk/` and the working diff).

The risk engine is the only component with authority to construct orders. A defect here is unbounded; a defect in a strategy is bounded by the risk engine. Review accordingly.

## 1. Authority is intact

```bash
grep -rn "Order(" src/fking/ --include=*.py | grep -v "^src/fking/risk/\|^src/fking/domain/\|^tests/"
```

Every `Order` construction outside `risk` (and the `domain` type definition itself) is a blocking finding. Then confirm the static contract:

```bash
make check
```

`import-linter` must still forbid `strategy` → `execution`. A relaxed contract here is the finding, whatever the commit message says.

## 2. Signal → Order translation

Read the sizing path and confirm:

- `conviction` (0..1) scales size within a **hard cap that conviction cannot exceed**. Conviction is the strategy's opinion of itself; it is not evidence and must not be able to unbound the position.
- `invalidation` is used. A signal with `invalidation` set must produce a stop, and distance to invalidation must be an input to size — risking a fixed notional regardless of stop distance means the actual risk per trade varies by an order of magnitude across setups.
- A directional signal with `invalidation is None` is rejected and the rejection is logged. Silently defaulting a stop hides a strategy defect.

## 3. Limits actually bind

For each limit — per-position, per-symbol, gross exposure, net exposure, drawdown, daily loss, order rate — verify:

- It is checked **before** the order is emitted, not after the fill.
- Breach produces a **logged rejection**, not a clamped order that quietly proceeds. A clamp turns a limit breach into an invisible one.
- Correlated exposure is netted before the check. Summing gross notional across correlated symbols lets two correlated longs consume one symbol's limit twice.
- The check is evaluated against the position state **including in-flight unfilled orders**, not just filled positions. Otherwise rapid signals stack past the limit in the window before fills return.

## 4. Purity

No I/O, no clock access, no unseeded randomness in `risk/`. `datetime.now()` in this module makes every risk decision non-reproducible, which means an incident can never be replayed.

```bash
grep -rn "datetime.now\|time.time\|random\.\|requests\.\|httpx\." src/fking/risk/
```

Any hit is a finding.

## 5. Money math

- `Decimal` throughout, constructed from `str`.
- Rounding is explicit and directionally safe: **round position size down, round required margin up**. Rounding a size up to satisfy an exchange lot step can push it past a limit that was just checked.
- Exchange lot step, tick size, and minimum notional applied — and the limit re-checked *after* rounding, not before.

## 6. Kill switch

```bash
grep -rn "kill\|halt\|degrade" src/fking/risk/ src/fking/execution/ | head -30
```

- Triggered state is persisted, so a process restart does not clear it. A kill switch that a crash-loop resets is not a kill switch.
- It blocks new orders **and** defines what happens to open positions — flatten, hold, or manual-only. That decision must be explicit in code, not implied.
- It is reachable when the event bus is down. A kill switch that depends on the thing that is broken is decorative.
- Re-arming is deliberate and audited, never automatic on a timer.

## 7. Property tests, not examples

```bash
make test ARGS="tests/risk --cov=src/fking/risk --cov-report=term-missing"
```

Floor is 95%. Hypothesis properties are mandatory here, and must cover:

- Partial closes, direction flips, zero-crossings, dust quantities.
- No sequence of valid signals produces exposure above the limit.
- Sizing is monotonic in conviction and inversely monotonic in stop distance.
- A closed position has exactly zero quantity — not `1E-18`.
- The engine never returns a negative quantity, and never a quantity below the exchange minimum without rejecting instead.

## 8. Report

Findings as Blocking / Should fix / Note with file:line, plus one explicit sentence on whether order-construction authority is still exclusively in `risk`.
