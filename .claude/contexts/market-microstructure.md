# Market Microstructure

## What you need to hold in your head

A price on a screen is not a price you can transact at. It is the best of two queues of unfilled promises, and the moment you try to take one of them you change it. Everything in this document follows from that single fact: the spread is a cost you pay before you have an opinion, impact is a cost you pay for being large, adverse selection is a cost you pay for being passive, and fees are a cost you pay for existing. A strategy's gross edge is a number about the market; its net edge is a number about you — your size, your urgency, your order type, and the venue's fee tier. In this project the gap between those two numbers is where every retracted result has lived, which is why the cost model is a first-class component with its own calibration provenance rather than a constant somewhere in the backtest loop.

---

## 1. The book is a queue of promises, not a price

A limit order book is two sorted lists of resting orders. Bids sit below, asks above, sorted by price and then by arrival time within a price. The best bid and best ask are the **touch** or **top of book** (L1). The full set of price levels with aggregated quantity is **L2**. The order-by-order view, where you can see individual orders and their position in each queue, is **L3** or MBO.

| Level | Content | Do we have it |
|---|---|---|
| L1 | Best bid, best ask, and their sizes | Yes, futures `bookTicker` |
| L2 | Per-price-level aggregated quantity | No. Only ~1/min cumulative bands at ±1–5% from mid |
| L3 / MBO | Individual orders, arrival order, queue | No, and not purchasable at this budget |

That table is the shape of the entire strategy space. See `../../DATA_PIPELINE.md` §9 for the exact schema of what Binance actually publishes and why `bookDepth` is not snapshots.

A **market order** consumes the book from the touch outward until filled. A **limit order** either crosses immediately (if marketable) or rests and joins a queue. The book is not static while this happens: it is being modified thousands of times per second by participants who see your order arrive.

**Mid price** is `(best_bid + best_ask) / 2`. It is a convenient number that nobody can trade at. **Microprice** — the size-weighted mid, `(bid_px·ask_qty + ask_px·bid_qty) / (bid_qty + ask_qty)` — is a better short-horizon predictor of the next trade price because it leans toward the side with less resting size. Neither is a transactable price.

---

## 2. Makers and takers, and why the fee sign differs

A **maker** adds liquidity: their order rests in the book and waits. A **taker** removes liquidity: their order crosses the spread and executes against resting orders. Every trade has exactly one of each.

Exchanges charge takers more than makers, and at high volume tiers pay makers a rebate, because a book with no resting orders is not a market. The fee schedule is the exchange buying inventory of resting liquidity from you.

| Role | What you do | Fee sign | What you receive | What you pay |
|---|---|---|---|---|
| Maker | Rest in the book | Low positive, or negative at high tiers | Half the spread, plus possible rebate | Adverse selection (§7), and uncertainty about whether you fill at all |
| Taker | Cross the spread | Higher positive | Certainty of immediate execution | Half the spread plus the fee |

Binance USDⓈ-M futures publishes a VIP 0 schedule of 0.0200% maker and 0.0500% taker at time of writing. Treat that as a vendor-published figure that changes, not as a constant: read it from the venue at startup (`fetch_trading_fee` / `commissionRate`) and store it with the timestamp it was read. A hardcoded fee that silently goes stale understates cost in exactly the direction that makes strategies look good.

**The rule this project applies:** a backtest may not book a maker fee unless it can also model whether the maker order would have filled. It cannot (§4). So the default cost assumption is taker on both legs, and any maker assumption is a claim requiring evidence, not a default requiring objection.

---

## 3. The spread is the first cost, and mid is a lie

The bid-ask spread is the price of immediacy. If the book is `bid 100.00 / ask 100.02`, you buy at 100.02 and sell at 100.00. A round trip costs you 2 ticks before fees, before impact, before you were right or wrong about anything.

Quoting a backtest in mid is the most common way to invent an edge that does not exist. If your strategy round-trips once a day on BTCUSDT and you mark entries and exits at mid, you have granted yourself the full spread per round trip as a gift. On a genuinely tight instrument that gift is small; on anything else it is the entire result.

**Half-spread** is the right unit for a single fill: crossing from mid to the touch costs you half the quoted spread. A round trip crossing both ways costs a full spread.

Measured for this project, and the reason `../../CLAUDE.md` §2 has a row about it:

| Venue | BTCUSDT spread | Volume |
|---|---|---|
| Binance futures **production** | 0.16 bp | Real |
| Binance futures **testnet** | 7.5 bp | Roughly 10x inflated |

Testnet's spread is about 47x wider and its volume is fiction. Calibrating a cost model on testnet does not produce a conservative model — it produces an unrelated one, wrong in both directions depending on which strategy you run. See `./binance-testnet.md` for what testnet is and is not good for.

Spread is regime-dependent. It widens on volatility spikes, on funding settlement (§10), and in the thin hours. A single median half-spread is a starting point; a spread conditioned on realised volatility is the honest version, and if your strategy trades preferentially in wide-spread conditions, the median will flatter it.

---

## 4. Queue position, and why it is unobservable to us

If you rest a limit order at the best bid, whether you fill depends on where you are in that price level's FIFO queue. Ten million dollars of resting size ahead of you means the price has to hold while all of it trades before you get anything. Queue position is the single most important variable in passive execution, and it determines fill probability far more than the price you quoted.

We cannot observe it. Computing queue position requires L3 order-by-order data, or at minimum L2 with every book event, so you can track how much size was ahead of you at insertion and how much of it cancelled versus traded. Our data ceiling is L1 plus coarse minute-sampled bands (§1). Reconstructing queue dynamics from that is not approximation, it is invention.

The consequences are enforced, not merely advised:

- The feature store **refuses** requests for queue-position and resting-liquidity features rather than substituting a proxy (`../../ARCHITECTURE.md` §6, `../../DATA_PIPELINE.md` §8). The refusal names what does exist.
- Passive fill probability is reported as `None`, never as a number. A fabricated number propagates into sizing and becomes invisible.
- A strategy whose thesis is "we earn the spread by providing liquidity" is not testable here. That is a data budget statement, not a judgement about the strategy.

If you find yourself writing a backtest where limit orders fill because the price touched your level, stop. That is the naive maker-fill assumption, and §7 explains precisely how it lies to you.

---

## 5. Market impact: temporary and permanent

Your order moves the price. Two components, and they behave differently:

- **Temporary impact** is the price concession you pay to consume liquidity faster than it replenishes. It decays after you stop trading. It is a cost.
- **Permanent impact** is the market's revision of fair value because your trade carried information. It does not decay. It is the market learning something from you.

The standard functional form for temporary impact is the square root law:

```
impact ≈ Y · σ · sqrt(Q / V)
```

where `σ` is volatility over the execution interval, `Q` is your quantity, `V` is market volume over that interval, and `Q/V` is your **participation rate**. The intuition: doubling your size does not double your cost, because liquidity replenishes while you work — but it does not leave cost flat either, because you are still ahead of the replenishment rate. The square root is the compromise, and it has survived a lot of empirical scrutiny across asset classes.

**The constant `Y` is where people go wrong.** Equity literature quotes values around 0.5 to 1 as an order of magnitude. That number is not transferable to a crypto perpetual, not transferable between BTCUSDT and a mid-cap alt, and not transferable between a calm Tuesday and a liquidation cascade. `Y` must be **measured from your own production fills** — regress realised slippage against `sqrt(participation) · σ` per instrument per regime — and until you have enough fills to measure it, the honest thing is a conservative assumed value stated as an assumption in the backtest report, not a number that looks calibrated.

Practical consequence: impact is why capacity is finite (see `./risk-vocabulary.md` §12). An edge of 5 bp per trade at 0.1% participation may be 0 bp at 5% participation, and the strategy that "works" at $10k notional is not a strategy at $1M.

---

## 6. Slippage against decision price, not arrival price

Slippage is the difference between the price you assumed and the price you got. Which price you assumed is the entire question.

| Benchmark | Definition | What it hides |
|---|---|---|
| **Decision price** | Mid at the instant the `Signal` was emitted | Nothing. This is the honest one. |
| Arrival price | Mid when the order reached the venue | Every millisecond and every price move between decision and submission: risk computation, netting, queueing, network |
| VWAP over the fill window | Volume-weighted average during execution | Whether the whole window was a bad place to trade |
| Fill price against itself | Nothing at all | Everything |

This system measures against **decision price**, and the decision price is stamped into the audit trail at `Signal` construction so it cannot be chosen after the fact (`../../ARCHITECTURE.md` §11).

The reason is that the gap between decision and arrival is exactly where self-deception lives. If you measure against arrival price, then any latency in your own pipeline — a slow risk computation, an event bus backlog, a retry — becomes invisible. Your execution looks excellent while your system loses money to its own sluggishness. Worse, in a strategy that trades momentum, the price moves *toward* your direction between decision and arrival, and measuring against arrival price systematically reports negative slippage (apparent price improvement) while you are being run over. Decision-price slippage catches both.

`decision_price` is a required field, not an optional one, and a fill that cannot be joined back to its decision price is an audit defect.

---

## 7. Adverse selection: you get filled precisely when you were wrong

This is the concept that kills naive maker-fill backtests, and it deserves the sharpest statement in this document.

You rest a bid at 100.00. There are two worlds in which you get filled:

1. Price is oscillating randomly and someone needed immediacy. You buy at 100.00, price returns to 100.01 mid, you earned the half-spread. This is the world your backtest imagines.
2. Someone with better information, or simply someone with a large sell to complete, is pushing through the book. You buy at 100.00 and the next print is 99.94. You did not earn the spread; you were the exit liquidity.

The fills you receive are not a random sample of the times price touched your level. They are conditioned on there having been a seller motivated enough to reach you. **Your fill probability is correlated with the subsequent price move against you.** That correlation is adverse selection, and it is the maker's true cost — the thing the maker rebate is compensating you for.

The naive backtest rule "if `low <= my_limit_price` then filled" gets this exactly backwards. It grants you a fill in world 2 *and* prices it as world 1. Because the rule fills you on every adverse move and only sometimes on favourable ones, it manufactures an edge out of the selection itself. A momentum strategy backtested with naive maker fills will look like a market-making strategy that also happens to be right about direction.

There is no correct fix at our data budget. The available honest options, in descending order of preference:

1. Assume taker execution and pay the spread. This is the default here.
2. Model maker fills with an explicitly stated fill probability and an explicitly stated post-fill drift penalty, both labelled as assumptions and both varied in sensitivity analysis. The result is a range, not a number.
3. Do not test the strategy.

See `./backtest-pitfalls.md` for the fill-simulation rules the engine enforces.

---

## 8. Latency, and what this system explicitly is not

Latency matters when your edge is a race. In HFT and latency arbitrage, the participant who acts first captures the whole opportunity and everyone else captures a loss, so the game is measured in microseconds and won with colocation, kernel bypass, and FPGAs.

**This system is not built for that and must not pretend to be** (`../../ARCHITECTURE.md` §13). It is a Python modular monolith on a single node, talking to Binance over the public internet through `ccxt`, with an event bus in the path. Round trips are tens to hundreds of milliseconds and jitter is worse than the mean. That is a perfectly good budget for strategies whose horizon is minutes to days. It is a catastrophic budget for anything whose edge decays in under a second.

The practical rules:

- If a strategy's backtested edge disappears when you add 500 ms of decision-to-submission delay, it was a latency strategy wearing a disguise. The backtest engine injects a configurable latency and this sensitivity is a standard report line.
- Never build a strategy whose thesis is "we see the print before others react". We do not.
- Latency still matters defensively: stale marks, a signal computed on a bar that has since been superseded, and slow cancellation during a kill-switch trip are all real. The kill switch's cancellation SLA is 2 s p99 (`../../RISK_PHILOSOPHY.md` §8) because that is an operational bound, not an alpha bound.

---

## 9. Order types, and which ones this system may use

| Type | Semantics | Status here |
|---|---|---|
| `LIMIT` + `IOC`, priced through the touch | Marketable limit with a price cap. Fills what it can immediately, cancels the rest. | **Default entry and exit.** Bounded worst-case price. |
| `LIMIT` + `GTC` | Rests until filled or cancelled. | Allowed for resting protective exits at the invalidation level. |
| `MARKET` | Unbounded price. Consumes the book. | Forced-exit path only, and never as a strategy's chosen execution style. |
| `LIMIT` + `GTX` (post-only) | Rejects if it would cross; guarantees maker. | Allowed to *place*, forbidden as a *source of backtested edge*, because fill probability is unmeasurable (§4). |
| `FOK` | All or nothing, immediately. | Forbidden. On a thin book it converts a partial fill into no position while the risk engine believes a position exists. |
| `reduceOnly` | May only decrease an existing position. | **Mandatory on every closing order.** |
| `closePosition` | Venue-side flatten. | Not used. Flattening is a trading decision and belongs to the risk engine, not to a venue flag. |

Two rules with reasons:

**Every closing order carries `reduceOnly`.** Without it, a race between a reconciliation-driven close and a strategy-driven entry can flip you through zero into an unintended position on the far side. `reduceOnly` makes that state unreachable at the venue rather than merely unlikely in your code.

**The kill switch cancels; it does not flatten.** Tripping the switch therefore emits no market orders. The conditions that trip a kill switch are precisely the conditions in which market orders execute worst, so flattening on trip converts a risk event into a realised loss at the worst available price (`../../RISK_PHILOSOPHY.md` §8).

---

## 10. What is different about crypto

**24/7, no session boundary.** There is no open, no close, no auction, no overnight gap. This removes a whole family of equity strategies and removes a whole family of bugs' natural detectors: a timezone error in an equity system produces obviously wrong session times, while here it silently shifts your data by hours and everything still looks plausible. This is why timezone-aware UTC is a non-negotiable rather than a style preference (`../../docs/rules/decimal-and-money.md`).

**Fragmentation.** The same instrument trades on many venues with no consolidated tape and no best-execution obligation. There is no NBBO. "The price of BTC" is a per-venue quantity, and cross-venue price differences are real, persistent, and not always arbitrageable after transfer time and fees.

**Volume is not trustworthy.** Reported volume includes self-matching and wash trading, historically severe on smaller venues and still non-zero on major ones. Because participation rate (§5) has volume in the denominator, inflated volume understates your impact and overstates your capacity. Prefer trade-count and large-trade-share sanity checks alongside raw volume, and treat any venue's volume as a claim.

**Maker rebate tiers are steep and reachable.** Fee tiers scale with 30-day volume and with exchange-token holdings. This means the same strategy has different economics for different participants, and someone else's published result at their fee tier says nothing about yours.

**Funding-driven flow.** On perpetuals, funding settles on a fixed schedule (see `./crypto-perpetuals.md` §2). Around settlement, positioning shifts and spreads and volumes move in a way that is calendar-predictable. If your strategy's fills cluster near settlement timestamps, its cost profile is not the daily median and you must condition on it.

**Liquidity is regime-bimodal.** Books that are deep for weeks become paper-thin during a liquidation cascade, and cascades are exactly when your stops trigger. Cost models fitted on unconditional medians are wrong in the tail in the direction that hurts (`./crypto-perpetuals.md` §5).

---

## 11. Tick size, step size, `minNotional`: rejection is a first-class case

Every venue publishes filters per symbol. On Binance USDⓈ-M futures the relevant ones are `PRICE_FILTER` (`tickSize`), `LOT_SIZE` (`stepSize`, `minQty`, `maxQty`), `MARKET_LOT_SIZE`, `MIN_NOTIONAL`, and `PERCENT_PRICE`. Read them from `exchangeInfo` at startup and cache them with a timestamp. Do not hardcode them; Binance changes them, including on live symbols. The USDⓈ-M minimum notional is commonly on the order of a few USDT — treat any specific value you remember as illustrative until you have read it from the venue.

Quantization rules this project applies:

- **Quantity rounds toward zero** to `stepSize`. Rounding up is a silent size increase past a risk decision, and the risk engine's output is a ceiling, not a suggestion.
- **Price rounds to the passive side** of `tickSize`: down for buys, up for sells. Rounding the wrong way turns a limit into an accidentally-more-aggressive limit.
- **After quantization, re-check `minNotional`.** This is the trap. Rounding quantity down can drop the order below the venue minimum, so a perfectly valid risk decision becomes an unplaceable order. That is not an error condition to log and continue past — it is a decision outcome that must be modelled.

Which is the general point: **order rejection is a normal outcome, not an edge case.** Filters, insufficient margin, `reduceOnly` with no position, rate limits, and `PERCENT_PRICE` bands all produce rejections during ordinary operation. Two consequences:

1. The backtest must apply the same filters. A backtest that fills a 0.0003 BTC order the venue would reject overstates trade count, overstates the number of independent observations behind the Sharpe, and hides the fact that the strategy is uninvestable at small size.
2. The live path treats a rejection as a typed outcome that flows back through the audit trail with its reason code, never as an exception swallowed into a log line (`../../CLAUDE.md` §4).

---

## 12. How a cost model is composed

Four terms, each with its own provenance:

```
cost = fee + half_spread_if_crossing + impact + slippage_draw
```

| Term | Source | Regime-dependent |
|---|---|---|
| Fee | Read from the venue, stamped with read time | No, but tier-dependent |
| Half-spread | Median half-spread from **production** L1, conditioned on volatility bucket | Strongly |
| Impact | `Y · σ · sqrt(participation)`, `Y` regressed from our own production fills | Strongly |
| Slippage | A *distribution*, not a point estimate: at minimum p50 and p95 from realised decision-price slippage | Strongly |

Slippage is a distribution because the tail is the part that matters. A model that adds a constant 3 bp reproduces the mean and erases the 40 bp fill you got during a cascade, and it is the cascade fills that decide whether the drawdown limit is hit. The backtest draws from the calibrated distribution with a seeded RNG so results stay deterministic (`../../CLAUDE.md` §5).

**All four parameters come from production data. None come from testnet.** This is the rule with the numbers behind it in §3, and it is a non-negotiable in `../../CLAUDE.md` §2.

Applied to a fill:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

BPS = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class CostParameters:
    """Calibrated from production market data only.

    Testnet showed a 7.5bp BTCUSDT spread against production's 0.16bp with
    roughly 10x inflated volume; fitting any of these on testnet is fiction.
    """

    taker_fee_bps: Decimal
    maker_fee_bps: Decimal          # signed: negative is a rebate
    half_spread_bps: Decimal        # median for this symbol and volatility bucket
    impact_coefficient: Decimal     # Y in Y * sigma * sqrt(participation)
    calibrated_at: datetime         # timezone-aware UTC
    source: Literal["production"]   # the type has one member on purpose


@dataclass(frozen=True, slots=True)
class FillCost:
    fee_quote: Decimal
    spread_quote: Decimal
    impact_quote: Decimal
    slippage_quote: Decimal

    @property
    def total_quote(self) -> Decimal:
        return (
            self.fee_quote + self.spread_quote + self.impact_quote + self.slippage_quote
        )


def cost_of_fill(
    *,
    params: CostParameters,
    fill_price: Decimal,
    base_quantity: Decimal,
    liquidity: Literal["maker", "taker"],
    participation: Decimal,          # our quantity / interval volume, in [0, 1]
    interval_volatility_bps: Decimal,
    slippage_draw_bps: Decimal,      # sampled from the calibrated distribution, seeded
) -> FillCost:
    notional = fill_price * base_quantity
    fee_bps = params.taker_fee_bps if liquidity == "taker" else params.maker_fee_bps
    # A maker fill crosses no spread by construction. It pays adverse selection
    # instead, which belongs in the slippage distribution, not here.
    crossed_bps = params.half_spread_bps if liquidity == "taker" else Decimal("0")
    impact_bps = params.impact_coefficient * interval_volatility_bps * participation.sqrt()
    return FillCost(
        fee_quote=notional * fee_bps * BPS,
        spread_quote=notional * crossed_bps * BPS,
        impact_quote=notional * impact_bps * BPS,
        slippage_quote=notional * slippage_draw_bps * BPS,
    )


params = CostParameters(
    taker_fee_bps=Decimal("5.0"),
    maker_fee_bps=Decimal("2.0"),
    half_spread_bps=Decimal("0.08"),          # production BTCUSDT, calm regime
    impact_coefficient=Decimal("0.7"),        # measured, not assumed
    calibrated_at=datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc),
    source="production",
)
```

Every literal is constructed from `str`. `Decimal(0.08)` is not `Decimal("0.08")`, and the difference compounds across thousands of fills into reconciliation drift that looks like an exchange bug (`../../docs/rules/decimal-and-money.md`).

---

## 13. Where this shows up in the codebase

| Concept | Module |
|---|---|
| Cost model, its parameters, and their provenance | `fking.backtest.cost` |
| Fill simulation, latency injection, filter application | `fking.backtest` |
| Venue adapters, order type construction, `reduceOnly`, quantization | `fking.execution` |
| Exchange filter cache from `exchangeInfo`, rejection taxonomy | `fking.execution` |
| L1, tick trades, depth bands, and the availability refusal | `fking.data` |
| `decision_price` stamped at signal time | `fking.domain`, `fking.strategy` |
| Participation rate inputs (interval volume, realised volatility) | `fking.data` |
| Capacity ceiling derived from impact | `fking.evolution` scoring, see `./risk-vocabulary.md` §12 |

Related documents: `../../ARCHITECTURE.md` §6 and §7, `../../DATA_PIPELINE.md` §9, `./crypto-perpetuals.md`, `./backtest-pitfalls.md`, `./binance-testnet.md`, `./risk-vocabulary.md`, `../knowledge/glossary.md`, `../../docs/rules/no-lookahead.md`.

---

## 14. Traps

1. **Marking at mid.** The single most common invented edge. Half a spread per fill, every fill, forever.
2. **Naive maker fills.** `low <= limit_price` therefore filled is not a fill model; it is an adverse-selection generator (§7).
3. **Calibrating anything on testnet.** 7.5 bp against 0.16 bp, with 10x fake volume. Not conservative — unrelated.
4. **A constant slippage number.** Erases the tail, and the tail is what hits the drawdown limit.
5. **Assuming an impact constant from equity literature.** Wrong instrument, wrong regime, wrong by an unknown factor. Measure `Y` or state that you assumed it.
6. **Measuring slippage against arrival price.** Hides your own pipeline latency and inverts sign for momentum strategies.
7. **Skipping exchange filters in the backtest.** Manufactures trades the venue would reject and inflates the observation count behind every statistic.
8. **Rounding quantity up to satisfy `minNotional`.** You have just overridden the risk engine to make an order placeable. The correct outcome is no order.
9. **Trusting reported volume.** Wash volume understates your participation rate and overstates capacity.
10. **Treating rejection as an exception.** It is a routine outcome with a reason code that belongs in the audit trail.
11. **Any strategy that needs queue position.** Untestable here, by construction, and the feature store will refuse it (§4).
