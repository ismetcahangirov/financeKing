---
number: 0011
title: Decimal constructed from str for every monetary value, with float trapped
date: 2026-08-03
status: accepted
deciders: [ismetcahangirov, architect, code-reviewer]
supersedes: null
superseded_by: null
related_issues: ["#12", "#17", "#16"]
related_adrs: [ADR-0002, ADR-0003]
---

## Context

Prices, quantities, notionals, fees, funding payments, balances and PnL flow from a venue's JSON response, through position arithmetic, into a database column, and back out into a backtest. Every one of those hops has a numeric type.

```
Forces:
- Decimal(0.1) is Decimal('0.1000000000000000055511151231257827021181583404541015625').
  The error is baked in before the constructor runs, because the literal was
  rounded to the nearest double by the parser. Decimal("0.1") is exactly one
  tenth. Same-looking constructor, different number.
- Decimal("0.1") == 0.1 is False. Not an error -- a risk-limit check quietly
  taking the wrong branch. Arithmetic between the types raises TypeError;
  comparison is the hole, and comparison is what limit checks are made of.
- Positions are built by summing fills. Exchange state is the source of truth
  and reconciliation runs continuously (ARCHITECTURE.md 7), so a per-fill
  rounding residue becomes a recurring alert rather than a one-off.
- Binance enforces per-symbol step and tick filters. A quantity carrying float
  noise in the eighteenth decimal is rejected in the order path with a message
  about a value that looks correct when printed.
- Decimal is materially slower than float, and statistical computation --
  Sharpe ratios, covariance, regression, indicator math -- is a real workload
  in backtest and data.
- Money crosses a JSON boundary to a TypeScript dashboard (ADR-0002), and
  JSON.parse has no Decimal.

The constraint that forces a decision now:
#12 defines the domain types and #17 defines the schema. Every field's type is
fixed by those two issues, and a value that has already passed through a float
is not repairable -- it must be re-parsed from the source text or discarded.
```

## Decision

**Every price, quantity, notional, fee, funding payment, balance and PnL is a `decimal.Decimal` constructed from a `str`, from the venue's raw response text through to a `NUMERIC(38, 18)` column.** Money enters the process exactly once per boundary, via `json.loads(body, parse_float=Decimal)`, whose hook receives the original source substring rather than a parsed double. The process-wide decimal context is set once at bootstrap with `prec = 38` — matching the column, so the round trip cannot lose digits — and with `FloatOperation` trapped, which turns `Decimal(0.1)` and `Decimal("0.1") == 0.1` into exceptions at the point of the mistake. Rounding mode is chosen per quantity kind rather than globally: `ROUND_DOWN` for order quantities and buy prices, `ROUND_UP` for sell prices, `ROUND_HALF_EVEN` for reported PnL and fees, and **no quantization at all** for risk-limit thresholds. `float` is permitted only inside statistical computation in `fking.backtest` and `fking.data`, at a named boundary, under the constraints below.

## Alternatives considered

### Alternative 1 — integer minor units (satoshis, or a fixed 10^-18 scale) (strongest rejected)

**What it would have given us.** This is what payment systems do, and the reasons are good. Integers are exact by construction with no context, no precision setting and no trap configuration to get wrong — there is no `Decimal(0.1)` mistake available because there is no float constructor to reach for. They are fast, they hash and compare trivially, and they map onto `BIGINT`/`NUMERIC(38,0)` with no scale question. Crucially, they make the *scale* explicit in the type system rather than in a convention: a `Satoshis` newtype cannot be added to a `QuoteMinorUnits` without a conversion, which is a stronger guarantee than two `Decimal`s that differ only in what they mean.

**Why it lost.** Crypto has no fixed minor unit. Binance's `stepSize` and `tickSize` are per-symbol and change: BTCUSDT quantity steps at 1e-5 while other pairs step at 1e-8 or at 0.005, which is not even a power of ten. A single global scale must be chosen for the worst case (1e-18, to survive a future asset), at which point the integers are large, the conversions are everywhere, and every read and write is a scaling operation whose direction a reader must reconstruct. Worse, the scaling operations are where the bugs move to: a value scaled twice or not at all is off by 10^18 and is not obviously wrong in a log line, whereas a `Decimal` that is wrong is usually visibly wrong.

The second reason is the boundary. Every venue serialises money as a decimal **string** and every database column here is `NUMERIC(38, 18)`. Integers would mean converting at every boundary in both directions, and `docs/rules/decimal-and-money.md`'s central point is that money should enter the process once, from text, and never be transformed again. Adding a scaling step at each boundary reintroduces exactly the per-hop transformation the rule exists to remove.

**What survives the rejection, and is adopted.** The strongest part of the integer case is that scale and meaning should be visible in the type rather than in a comment. That is adopted through naming rather than through newtypes: `notional_usd`, `fee_quote_usd`, `base_quantity`, `slippage_bps` — units in the identifier, enforced by an AST check that rejects `size`, `price`, `amount` and `qty` outright (`docs/rules/naming.md`). It is a weaker guarantee than a distinct type and it is checked mechanically on every commit.

### Alternative 2 — float everywhere, with a tolerance-based comparison discipline

**What it would have given us.** Fast, native, no context to configure, and it interoperates with NumPy, pandas and every statistical library without a conversion at the boundary. IEEE 754 doubles carry roughly 15–16 significant decimal digits, which is far more than any real price needs — BTC at five decimal places uses ten. Written carefully, with comparisons through a tolerance and rounding at the venue's step before submission, it works. Most trading code in the world is float.

**Why it lost.** The failure is not size, it is accumulation with no error. A position is the sum of its fills, and reconciliation compares that sum against the exchange's number continuously. After a few thousand fills the two differ in the fifteenth decimal, the reconciler reports a mismatch, and a day disappears into reading `ccxt` source looking for an exchange bug that does not exist. Nothing crashed and nothing was logged, because a tolerance discipline is a convention and conventions are applied by whoever wrote the line.

The comparison hole makes it unrecoverable in a mixed codebase: `Decimal + float` raises `TypeError` immediately, but `Decimal("0.1") == 0.1` silently returns `False`. So the failure mode is not "we use float and it is slightly imprecise" — it is "one float leaks into a Decimal codebase and a limit check takes the wrong branch with no error anywhere". A tolerance discipline also has to answer "how much tolerance", and any answer is a number that becomes wrong for some symbol at some size.

### Alternative 3 — do nothing (decide per module as each is written)

```
Cost of the status quo: #12 and #17 both need a type today, and they fix the
domain objects and every column in the schema. Deciding per module means the
domain uses Decimal and a data loader uses float, and the conversion happens
implicitly at a boundary nobody wrote down. Repairing that later is not a
refactor -- a value that passed through a float cannot be recovered by
widening the type afterwards, so it means re-ingesting.
Why that is no longer payable: the schema is written once and audit rows are
never updated (docs/rules/append-only-audit.md). A wrong type in an
append-only table is permanent.
```

## Consequences

**What becomes easier**
- The two silent failures become loud at the point of the mistake: `FloatOperation` trapped makes `Decimal(0.1)` and `Decimal("0.1") == 0.1` raise, rather than producing a number that looks authoritative.
- `mypy --strict` catches the arithmetic half free, because typeshed types `Decimal.__mul__` as accepting `Decimal | int` — so `quote_price * 1.05` is a type error rather than a 03:00 `TypeError`.
- `prec = 38` matches `NUMERIC(38, 18)`, so a value representable in the database is representable in memory and the round trip is lossless.
- Rounding direction becomes a stated risk decision per quantity kind rather than an artefact of the default: `ROUND_DOWN` on quantities can only ever ask for less than the balance supports.

**What becomes harder**
- Every boundary needs an explicit parse. `json.loads(body, parse_float=Decimal)` is not the default and a plain `json.loads` anywhere on a money path silently discards the guarantee.
- Decimal arithmetic is roughly an order of magnitude slower than float, and it is in the fill-application path that every backtest runs millions of times — a real contributor to the budget #109 manages.
- The statistical exception has to be policed at its edges, because it is where float legitimately lives. Conversion happens at a named function, one direction at a time, never implicitly mid-expression, and what leaves `backtest` or `data` is always `Decimal`.
- Serialisation is asymmetric work: money crosses the wire as a JSON string in both directions, so a `PlainSerializer` on the way out and a `BeforeValidator` rejecting floats on the way in are mandatory, not stylistic.

**What we now cannot do**
- Hand a money value to NumPy or pandas and take the result back as money without an explicit, named conversion at each end. Reopening that — letting a float from a Parquet column become a fill quantity, say — is the exact leak the AST check and the float-free package list exist to prevent.

## What would make us revisit this

```
Trigger:   Profiling attributes more than 25% of backtest wall clock to
           decimal arithmetic in the fill-application path, after the #109
           optimisation work is complete.
Observed:  The py-spy profile recorded by the #109 performance harness.
Then:      Open a superseding ADR for a narrower change -- an integer minor-unit
           representation confined to the backtest engine's inner loop, with
           Decimal at its boundaries -- not a global switch to float.
```

## Verification

```
Confirmed if:  zero reconciliation discrepancies are attributed to numeric
               precision, and zero orders are rejected for a step- or
               tick-size violation caused by representation, measured by
               2027-02-01
Refuted if:    tools/checks/money_types.py is weakened, the FloatOperation trap
               is disabled, or any money-suffixed database column is found not
               to be NUMERIC(38, 18)
Checked by:    code-reviewer agent, via `make checks`, the Hypothesis
               round-trip properties in tests/property/, and the
               information_schema column-type test against real Postgres
Review date:   2027-02-01
```

## Definition of done

- [x] `number` is the next unused value in `docs/adr/` and the filename matches `NNNN-<kebab-slug>.md`
- [x] Context names one constraint that forces a decision
- [x] Decision is one paragraph, active voice, and names the owning module
- [x] The strongest rejected alternative is argued at its strongest, and the part of it that was correct is adopted rather than discarded
- [x] "Do nothing" is costed
- [x] All three Consequences lists are non-empty, including what we now cannot do
- [x] The revisit trigger is observable without judgement and names where it is observed
- [x] Verification states both a confirming and a refuting value, with a date and an owner
- [x] Linked from #16 and from `.claude/knowledge/decisions-log.md` (D-002)
