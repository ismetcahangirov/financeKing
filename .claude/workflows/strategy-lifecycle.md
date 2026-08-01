# Workflow — Strategy Lifecycle

From hypothesis to retirement. Most strategies die at stage 3, and that is the system working — the hard part is not generating strategies, it is rejecting them correctly.

Every stage transition is recorded against the strategy's **version hash** in the append-only strategy record. A strategy without a version hash is unattributable and cannot be intelligently retired later.

---

## Stage 1 — Hypothesis

Comes from `.claude/workflows/research.md` or from an agent proposal.

Requirements to proceed:
- A falsifiable statement of what would prove it wrong
- A mechanism: **who is on the other side and why they keep taking that trade**
- Every required feature declared available by the feature store — remember free full-depth L2 history does not exist

**Exit**: a written thesis with a candidate invalidation level.

---

## Stage 2 — Implementation

Run `/add-strategy <name> "<thesis>"`.

Hard constraints, structurally enforced:
- Emits `Signal` only — never `Order`, never a quantity, notional, or leverage
- `invalidation` populated on every non-flat signal, derived from the setup's structure rather than a round-number percentage bolted on
- Pure: no I/O, no clock access, no unseeded randomness
- Parameters declared with explicit bounds, because the evolution engine will mutate them and an unbounded parameter is an unbounded search space

**Exit**: `make check` green, including the no-look-ahead and invalidation property tests.

---

## Stage 3 — Validation (where most strategies die)

Run `/backtest`. Answer the full skeptical checklist — trial count incremented, deflated Sharpe computed, held-out period untouched, and the rest.

Requirements to pass:
- Deflated Sharpe above zero at the required confidence, with the **global** trial count
- Walk-forward plus combinatorial purged CV with an embargo exceeding max feature lookback plus max holding horizon
- Cross-fold dispersion acceptable — excellent in two folds and flat in six is one regime, not an edge
- Gross edge at least ~2x modelled cost, with the cost model calibrated on **production** data (testnet's ~7.5bp spread vs production's ~0.16bp makes testnet calibration fiction)
- Minimum trade count met

**Do not tune to pass.** Optimizing until the backtest looks good is the definition of overfitting, and each attempt increments the trial count and deflates the Sharpe you are chasing.

**Exit**: pass, or **reject** — which is the expected and successful outcome.

---

## Stage 4 — Paper

Live data, simulated fills, `PaperVenue`. Identical strategy code — only the venue swaps.

Watch for:
- Signals matching what the backtest produced on the same bars. A divergence is a parity bug and outranks everything else.
- Realized decision-price slippage against modelled slippage
- Signal frequency matching the backtest. A strategy firing three times as often live is reading data it did not have in backtest.

**Exit**: a minimum paper period with no parity divergence and slippage within model.

---

## Stage 5 — Demo (challenger)

Binance testnet, `DemoVenue`, small allocation, champion/challenger against the incumbent.

Promotion is on **forward performance**, never on backtest rank. Backtest rank is what selected it; using it again to promote is scoring the same evidence twice.

---

## Stage 6 — Champion

Run `/evaluate <version-hash>` on the review cadence. Report every survival score component separately — the aggregate hides what you need to see — and treat risk-limit violations as a hard negative. A strategy that made money by breaching limits scores worse than one that made less within them.

Additional checks the score cannot make:
- Are losers respecting the declared invalidation? If they routinely run past it, the strategy does not have the thesis it claims.
- Remove the best 5 trades — does the edge survive? If not, it is a story about five events.
- Has capacity been eaten by the size it is now running?

---

## Stage 7 — Retirement

Triggers, any one sufficient:
- Out-of-sample decay past the allowance it was promoted under
- Any hard risk-limit violation
- The mechanism stopped being true — a fee schedule changed, a venue changed its matching, the counterparty went away
- Capacity exhausted at current size

**Do not extend the decay allowance to keep a strategy alive.** Retiring strategies is the system working.

Retirement is permanent for that version hash. A fixed variant is a **new lineage entry with its own trial count**, not a resurrection — otherwise the trial count understates how many attempts the idea has actually consumed.

---

## The meta-check, every cycle

Compare validation rank against subsequent forward performance across the population.

If validation rank does not predict forward performance, **the scoring engine is lying**, and fixing it outranks every other piece of work in the project. This is the assumption most likely to be wrong in the whole architecture and the one to watch hardest.
