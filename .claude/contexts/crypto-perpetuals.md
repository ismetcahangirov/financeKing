# Crypto Perpetuals

## What you need to hold in your head

A perpetual swap is a futures contract that never expires, which means it lost the one mechanism that normally forces a futures price to converge on spot. Everything strange about the instrument is the replacement for that mechanism or a consequence of it: funding is the tether, mark price is the referee, liquidation is the enforcement, and the insurance fund and auto-deleveraging are what happens when enforcement fails. Three of those four are invisible in a naive backtest, and all three are cash flows or forced trades that a live account experiences. If you take one thing from this document: **you are trading three different prices at once — last, mark, and index — and the one that decides whether you are liquidated is not the one you see printing.**

This project trades USDⓈ-M perpetuals on Binance futures testnet. See `./binance-testnet.md` for what that environment does and does not reproduce.

---

## 1. What a perpetual is, and the problem it solves

A traditional futures contract has an expiry. At expiry it settles against the spot index, and that fact alone drags the futures price toward spot as expiry approaches — arbitrageurs can buy one and sell the other and hold to a known settlement. Convergence is structural.

A perpetual removes expiry. Traders wanted permanent leveraged exposure without the operational cost of rolling quarterly contracts, and exchanges wanted a single deep book rather than fragmented expiries. The result is far more liquid than dated futures in crypto, and it is where essentially all the volume is.

But with no expiry there is no settlement, so nothing forces the perp price to equal spot. It could drift 20% above spot indefinitely. **Funding is the engineered replacement:** a periodic cash transfer between longs and shorts, sized to punish whichever side is causing the deviation. It converts "this contract will converge because it must settle" into "this contract converges because holding the crowded side costs you money every eight hours".

---

## 2. Funding

### Direction and mechanics

Funding is a payment **between traders**, settled on a schedule. It is not a fee: the exchange takes no cut of it, and it does not appear in a fee schedule.

| Funding rate | Perp trading | Who pays | Who receives |
|---|---|---|---|
| Positive | Above index (premium) | Longs | Shorts |
| Negative | Below index (discount) | Shorts | Longs |

The economic logic: if the perp is expensive relative to spot, longs are the ones bidding it there, so make them pay for the privilege. The payment attracts shorts, whose selling pushes the perp back down.

Payment size on Binance USDⓈ-M is the position's notional at the settlement mark price, times the rate:

```
payment = position_notional_at_mark * funding_rate
```

**You pay on notional, not on margin.** At 10x leverage a 0.01% rate costs 0.1% of your margin. This is the arithmetic that surprises people into thinking funding is trivial when it is not.

### The 8-hour interval convention

Binance USDⓈ-M perpetuals settle funding every 8 hours by default, at 00:00, 08:00 and 16:00 UTC. Some symbols run on 4-hour or 1-hour schedules, and the schedule can change per symbol. Read the interval from the venue (`fundingIntervalHours` in `premiumIndex` / `fundingInfo`); do not hardcode 8.

Two consequences that bite:

- **You only pay if you hold across the settlement timestamp.** A position opened at 08:01 and closed at 15:59 pays nothing. This makes funding a step function in your P&L, not a continuous carry, and strategies can be built to straddle or avoid the boundary. It also makes it a calendar effect: flow, spread and volume shift around settlement (`./market-microstructure.md` §10).
- **The rate you see quoted is per interval, not annualised.** A 0.01% 8-hourly rate is 0.03%/day, roughly 11%/year. A sustained 0.1% 8-hourly rate is roughly 110%/year, which is not an exotic scenario — it happens in a mania and it dominates every other term in the P&L.

### Premium index and the interest rate component

The rate is not chosen by anyone. It is computed as:

```
funding_rate = clamp(premium_index + clamp(interest_rate - premium_index, -0.05%, +0.05%), -cap, +cap)
```

- **Premium index** measures how far the perp's impact bid/ask sits from the index price, time-averaged over the interval. It is the part that actually responds to the market.
- **Interest rate** is a fixed component representing the borrowing cost differential between the two currencies of the pair (historically 0.03%/day, i.e. 0.01% per 8h, on USDT pairs). It gives the funding rate a small structural positive bias, which is why funding is positive more often than not, which is why perpetual longs pay a small ongoing carry in equilibrium.
- The inner clamp with a **dampener** of ±0.05% means small premiums produce a rate near the interest rate, and only meaningful premiums move the rate away from it.

### Caps and clamping

Funding is capped, typically at ±0.75% per interval on standard USDⓈ-M symbols, with the cap tied to the maintenance margin rate of the symbol's lowest leverage tier. Non-obvious consequence: **when the market dislocates hard enough that the true premium exceeds the cap, funding stops being an arbitrage-enforcing mechanism.** The perp can then sustain a basis wider than funding can punish, for as long as the dislocation lasts. Any strategy whose thesis is "funding forces convergence" has its thesis suspended in exactly the regime where the trade would have paid most. Model the cap; do not model funding as unbounded.

### Funding is not a fee

Restating because it changes the accounting. Fees leave the system to the exchange; funding moves between participants. A funding-harvesting strategy is not exploiting a pricing error, it is being paid to take the unpopular side of a crowded position. The payment is compensation for a risk, and §7 describes the risk.

### Computing a funding payment

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class FundingEvent:
    """One settlement of one symbol. Immutable, as all domain objects are."""

    symbol: str
    settled_at: datetime      # timezone-aware UTC; 00/08/16 on the default schedule
    rate: Decimal             # per-interval, signed. Positive: longs pay shorts.
    mark_price: Decimal       # mark at settlement, NOT last traded price

    def __post_init__(self) -> None:
        if self.settled_at.tzinfo is None:
            raise ValueError("settled_at must be timezone-aware UTC")


def funding_cash_flow(
    *, position_base_quantity: Decimal, event: FundingEvent
) -> Decimal:
    """Signed quote-currency cash flow TO the position holder.

    Negative means the holder pays. Position quantity is signed: positive long,
    negative short. Notional is taken at the settlement mark price because that
    is what the venue uses, and last price can differ materially.
    """
    notional = position_base_quantity * event.mark_price
    return -(notional * event.rate)


event = FundingEvent(
    symbol="BTCUSDT",
    settled_at=datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc),
    rate=Decimal("0.0001"),                 # 0.01% for this 8h interval
    mark_price=Decimal("64250.30"),
)

long_2_btc = funding_cash_flow(position_base_quantity=Decimal("2"), event=event)
# Decimal('-12.850060')  -> the long pays 12.85 USDT
short_2_btc = funding_cash_flow(position_base_quantity=Decimal("-2"), event=event)
# Decimal('12.850060')   -> the short receives 12.85 USDT
```

Every literal is built from `str`, the timestamp is timezone-aware, and the object is frozen (`../rules/decimal-and-money.md`).

---

## 3. Three prices: last, mark, index

| Price | What it is | What it decides |
|---|---|---|
| **Last** | Most recent trade on this venue's perp book | Nothing structural. It is a print. |
| **Index** | Weighted spot price across several constituent exchanges | The anchor for premium index and mark |
| **Mark** | A manipulation-resistant fair value, derived from index plus a smoothed basis | **Unrealised P&L, margin ratio, and liquidation** |

The index price is composed from spot prices on multiple external venues, weighted, with rules for excluding a constituent whose price deviates too far or whose feed goes stale. That composition is the manipulation defence: moving one exchange's spot price does not move the index much, and moving it a lot gets that exchange excluded.

Mark price is then built from the index plus a moving-average basis (Binance uses the median of several candidate fair prices, including the index plus a smoothed funding basis and impact-book-derived prices). The point is that mark does not jump when someone sweeps the perp book.

**Liquidation is on mark.** This is the single most common source of confusion in crypto derivatives, and it produces the complaint "the exchange liquidated me at a price that never traded". Both halves of that sentence are true and there is no contradiction: mark price is not a traded price and was never claimed to be. It is also frequently true in the other direction — a wick on the perp book that dips well below mark does *not* liquidate you, which is the protection the design is buying.

Practical consequences for this codebase:

- Unrealised P&L, margin ratio, drawdown, and every risk limit that reads a current price must read **mark**, not last. Computing drawdown off last price makes the drawdown limit fire on prints that had no effect on your account.
- A backtest that models liquidation off OHLC lows models liquidations that would not have occurred, and misses ones that would. If mark price history is available for the symbol, use it; if it is not, state that liquidation modelling is approximate and treat every result near the liquidation boundary as unreliable.
- Stops and invalidation levels are a *strategy* concept and are evaluated on the price the strategy declared. Liquidation is a *venue* concept on mark. These are different mechanisms and conflating them produces a system that thinks it is protected when it is not.

---

## 4. Margin, leverage tiers, cross and isolated

**Initial margin** is what you must post to open: `notional / leverage`. **Maintenance margin** is the minimum equity the position must retain; drop below and you are liquidated. The gap between them is your room to be wrong.

```
margin_ratio = maintenance_margin / margin_balance
```

At 100% you are liquidated.

**Leverage tiers step with notional.** Maximum leverage is not a property of the symbol; it is a property of your position size in that symbol. A step schedule looks like the following — treat the exact bracket boundaries as **illustrative**, read the real ones from the venue's leverage-bracket endpoint per symbol:

| Position notional (USDT) | Max leverage | Maintenance margin rate |
|---|---|---|
| 0 – 50k | 125x | 0.40% |
| 50k – 500k | 100x | 0.50% |
| 500k – 5M | 50x | 1.00% |
| 5M+ | 20x | 2.50% |

The consequence people miss: **adding to a winning position can raise your maintenance margin rate on the entire position**, moving your liquidation price against you even though the trade is profitable. Any sizing logic that scales into positions must recompute the bracket, not assume the one it opened in.

**Cross vs isolated margin:**

| Mode | Collateral | Failure mode |
|---|---|---|
| Cross | Whole account balance backs every position | One position's loss can liquidate the account; correlated positions do not diversify your margin, they pool their risk |
| Isolated | A fixed margin allocation per position | Position dies alone; the rest of the account survives, but you cannot use spare equity to defend it |

Isolated makes per-position risk bounded and legible, which suits a system running many independent strategies whose attributed P&L must be meaningful. Cross is more capital-efficient and is how most discretionary traders run. Whichever this system configures, the important part is that **the backtest models the same one**, because they produce different liquidation prices from identical positions.

---

## 5. Liquidation cascades and the fat left tail

When margin ratio hits 100%, the venue's liquidation engine takes over the position and closes it in the market. That close is a market order into a book that is already moving against the position. It pushes price further in the same direction, which pushes other leveraged accounts into liquidation, whose liquidations push price further. That is a **cascade**, and it is the defining tail event of this asset class.

What a cascade does to your assumptions, all at once:

- Spread widens by an order of magnitude.
- Depth evaporates, so your participation rate spikes for the same order size and impact goes non-linear (`./market-microstructure.md` §5).
- Your stop, if it is a market order, executes into the worst of it.
- Correlations across every crypto asset go to 1, so your diversified book is one position (`../../RISK_PHILOSOPHY.md` §5).
- Funding can hit its cap and stop enforcing convergence (§2).

Every one of those is a cost model term that was fitted on the unconditional distribution and is now wrong in the same direction.

**Why this makes Sharpe misleading.** Sharpe assumes returns whose risk is fully described by their standard deviation. Perpetual returns are strongly negatively skewed and heavy-tailed: many small positive days from carry-like or mean-reverting behaviour, punctuated by a cascade that removes a year of them. A strategy that sells volatility, harvests funding, or mean-reverts into a trend is *manufacturing* that shape, and its Sharpe will look excellent for exactly as long as no cascade lands inside the sample. This is why the survival score is not Sharpe (`../../ARCHITECTURE.md` §10) and why drawdown, tail measures and cross-regime consistency carry weight — see `./risk-vocabulary.md` §5 and §6, and `./statistics-for-trading.md` for the distributional machinery.

Rule of thumb with teeth: if a strategy's backtest window contains no liquidation cascade, the backtest has not tested the strategy. Name the cascades in your sample. If there are none, say so in the result.

---

## 6. Insurance fund and auto-deleveraging

If a liquidation closes at a price worse than bankruptcy price, someone must absorb the shortfall. The **insurance fund**, accumulated from liquidations that closed better than bankruptcy price, covers it. When the fund cannot, the venue falls back to **auto-deleveraging (ADL)**: it forcibly closes profitable positions on the opposite side, ranked by profit and leverage, at the bankruptcy price.

ADL is rare and it is the thing that breaks hedged strategies. The scenario:

1. You are running a cash-and-carry: short perp, long spot, collecting funding. Directionally flat by construction.
2. A violent crash triggers mass liquidation of longs. Liquidations close below bankruptcy price and drain the insurance fund.
3. The venue needs counterparties for the shortfall and ranks the profitable side. Your short perp leg is deeply profitable, so it is high in the ADL queue.
4. Your short perp is force-closed at bankruptcy price. Your hedge is gone and your long spot leg is now naked into a crash — and you did not place a single order.

**ADL is a risk that no amount of your own discipline prevents.** A strategy whose thesis depends on a hedge surviving must state ADL as a named failure mode and must have a defined response — because "my hedge is always on" is false. The monitoring requirement follows: read your ADL quantile indicator from the venue's user data stream and treat a high quantile as a risk signal, not as trivia.

---

## 7. Basis, cash-and-carry, and the tail on the carry trade

**Basis** is `perp_price - index_price`, usually quoted in bps or annualised. Positive basis means the perp is at a premium, which pairs with positive funding.

**Cash-and-carry** is the canonical trade: buy spot, short the perp, collect funding. Your directional exposure is roughly zero, and you earn the carry. In crypto bull markets this has paid double-digit to triple-digit annualised rates, which is why it is a crowded, professionalised trade.

It is a carry trade, and carry trades have a characteristic P&L shape: a smooth ascending line, and then a cliff. The cliff is made of:

- **Funding regime flip.** The rate goes negative and you now pay to hold. The whole thesis was the sign of a number that is not guaranteed to keep its sign.
- **Basis widening before it converges.** Your short perp leg loses mark-to-market as basis widens, and margin is called on that leg *now* while the convergence profit arrives later. Being right eventually and liquidated meanwhile is the standard way this trade dies.
- **ADL** removing one leg (§6).
- **Execution cost** on two legs at entry and two at exit, plus spot-side custody and transfer constraints. At small size the round-trip cost can exceed weeks of carry.
- **Funding caps** (§2) meaning the mechanism you rely on can saturate.

The honest summary, and the one to carry into any funding-related hypothesis: **funding harvesting is short a tail.** Its Sharpe over any quiet sample is spectacular and uninformative. `quant`'s worked example in `../agents/quant.md` is exactly this shape — a real carry effect with a gross edge that our execution cost cannot afford.

---

## 8. Open interest

**Open interest (OI)** is the total notional of open contracts. It rises when a new long and a new short pair up, and falls when positions close. It is a stock; volume is a flow. A day with huge volume and flat OI was position churn; a day with modest volume and a sharp OI rise was new leverage entering.

The standard joint reading, useful and not rigorous:

| Price | OI | Common interpretation |
|---|---|---|
| Up | Up | New longs entering; trend with fresh leverage behind it |
| Up | Down | Shorts closing; a squeeze, and it exhausts when the shorts are gone |
| Down | Up | New shorts entering |
| Down | Down | Longs closing or being liquidated; deleveraging |

Where OI earns its keep here is as a **fragility measure**: high OI plus extreme funding plus compressed realised volatility is a loaded spring, because a lot of leveraged positioning is paying a lot of carry in a market with little room. That is a regime feature, not a signal, and it belongs in the conditioning set of a hypothesis rather than as a standalone predictor.

OI is available free from Binance futures data. Note that OI is per-venue, so "total crypto leverage" from one venue's OI is an inference, not a measurement.

---

## 9. USDT-margined, coin-margined, inverse, quanto

**USDⓈ-M (linear).** Collateral and P&L are in USDT. Contract value is in USD terms, so P&L is linear in price: `pnl = q_base * (p_exit - p_entry)`. Your margin does not move with the price of the asset you are trading.

**COIN-M (inverse).** Collateral and P&L are in the base asset, e.g. BTC. Contracts are denominated in USD notional and P&L is `q_usd * (1/p_entry - 1/p_exit)`. P&L is non-linear in price, and your collateral is itself volatile: a long BTC position losing money is also losing margin because the margin *is* BTC. That double exposure is exactly the wrong convexity for a risk system trying to hold a fixed fractional risk per trade.

**This project uses USDT-margined perpetuals**, for three reasons: P&L is linear so position arithmetic is simple and property-testable, collateral is stable so equity means one thing, and quoting everything in a single stable unit means `Decimal` money arithmetic never crosses a currency boundary implicitly. Nothing else about the design forbids COIN-M; it is a deliberate scope choice and adding it later would mean a second P&L formula in `fking.domain`, not a config flag.

**Quanto** contracts settle in a currency unrelated to either leg — a BTC/USD contract that pays out in ETH, say — so the payout carries a second, embedded FX exposure whose correlation with the underlying changes the position's effective delta. You will meet the term reading about other venues; it does not apply to anything we trade, and the reason to know it is to recognise instantly that a "cheap-looking" quanto basis is compensation for a correlation risk, not a free lunch.

---

## 10. Position mode: one-way vs hedge

**One-way mode:** one net position per symbol. Buying while short reduces the short. **Hedge mode:** simultaneous independent long and short positions in the same symbol, each with its own margin and liquidation price.

**This project uses one-way mode.** Hedge mode's appeal is per-strategy position isolation — each strategy gets its own leg. That appeal is exactly what the architecture already rejects: the risk engine nets opposing signals internally before an order goes out, precisely so you do not pay the spread twice for a zero net position (`../../RISK_PHILOSOPHY.md` §5). Hedge mode would move netting to the venue, hide the crossing, double the margin, and put position state in a shape the risk engine does not model. Internal netting with explicit crossing attribution is strictly better, and it keeps the reconciliation contract simple: one symbol, one position, and any divergence from the venue is unambiguous.

Reconciliation depends on this. Exchange state is the source of truth (`../../ARCHITECTURE.md` §7), and "the venue says one net position of X" is a comparison you can make; "the venue says a long and a short whose net matches but whose legs do not" is a comparison you then have to interpret.

---

## 11. Funding as a P&L line item

A perpetual position's realised P&L has three components, not one:

```
total_pnl = price_pnl + funding_cash_flows - fees_and_costs
```

A backtest that ignores funding does not have a slightly optimistic number — it is **missing a term whose sign is systematic**. Because the interest-rate component biases funding positive (§2), a long-biased strategy that ignores funding is overstated approximately always, and a short-biased one is understated. That is a bias, not noise, and no amount of sample length removes it.

Requirements this places on the engine:

- Funding events are **data**, ingested per symbol with their settlement timestamps, and replayed by the backtest at those timestamps against the position held at that instant. Not applied as an average, not amortised.
- Funding is booked to the strategy that held the position, in the same attribution scheme as fills, so survival scores measure the real cash flow.
- Funding is reported as its own line in results. A strategy whose entire net P&L is funding is a carry strategy regardless of the story it tells about itself, and §7 says what that means for its tail. This is exactly the edge-decomposition question `quant` asks (`../agents/quant.md`).
- Live and backtest read the same funding series through the same interface, per the parity rule in `../../ARCHITECTURE.md` §4.

---

## 12. Where this shows up in the codebase

| Concept | Module |
|---|---|
| `Position` with signed base quantity, entry price, margin mode; P&L arithmetic | `fking.domain` |
| `FundingEvent`, mark/index/last as distinct typed prices | `fking.domain` |
| Funding rate, mark price, index price, open interest ingestion | `fking.data` |
| Leverage bracket and maintenance margin schedule, cached from the venue | `fking.execution` |
| Funding replay at settlement timestamps; liquidation modelling on mark | `fking.backtest` |
| Margin ratio, liquidation distance, mark-based drawdown, cascade-aware limits | `fking.risk` |
| ADL quantile monitoring from the user data stream | `fking.execution` |
| Funding as an attributed P&L component in the survival score | `fking.evolution` |

Related: `./market-microstructure.md` for the cost side, `./binance-testnet.md` for the environment, `./backtest-pitfalls.md` for the replay rules, `./statistics-for-trading.md` for what skew and fat tails do to inference, `./risk-vocabulary.md` for the limit taxonomy, `../knowledge/glossary.md`, `../rules/no-lookahead.md`, `../../ARCHITECTURE.md` §6.

---

## 13. Traps

1. **Reading last price where the venue reads mark.** Your unrealised P&L, margin ratio, and liquidation distance are all wrong, and they are wrong in a way that only shows up during stress.
2. **Modelling liquidation off OHLC.** Liquidation is on mark. Wicks that never touched mark do not liquidate; mark moves without a print do.
3. **Ignoring funding in a backtest.** A systematically signed missing term. Long-biased strategies are overstated approximately always.
4. **Treating funding as continuous carry.** It settles at discrete timestamps. Hold across one or you pay nothing.
5. **Hardcoding an 8-hour interval.** Per-symbol and mutable. Read `fundingIntervalHours`.
6. **Treating funding as unbounded.** It is capped, and the cap binds exactly when the dislocation is largest — the moment your convergence thesis needed it most.
7. **Computing funding on margin instead of notional.** Off by your leverage factor.
8. **Assuming a fixed maintenance margin rate.** It steps with position notional, so scaling into a winner can move your liquidation price against you.
9. **Assuming a hedge survives.** ADL exists, is not under your control, and hits profitable legs first.
10. **Believing a funding-harvest Sharpe.** It is short a tail. Any sample without a cascade has not tested it.
11. **Confusing the strategy's invalidation level with the venue's liquidation price.** Different mechanisms, different price series, different owners. If your invalidation sits beyond your liquidation price, you do not have a stop — you have a liquidation.
