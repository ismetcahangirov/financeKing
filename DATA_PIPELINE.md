# Data Pipeline

Ingestion, normalization, storage and point-in-time semantics.

This document expands `ARCHITECTURE.md` §6. Read that first. The short version is that this pipeline has two jobs: get data in correctly, and **refuse to serve data the system does not actually have.** The second job is the one that gets skipped, and skipping it is how a strategy ends up backtested against a market that never existed.

---

## 1. What the rest of the system is owed

| Consumer | What it needs | What happens if this pipeline lies |
|---|---|---|
| `backtest` | Bars and trades with correct epoch units, no synthesised rows, declared gaps | Every result is unfalsifiable |
| `strategy` | Point-in-time features that could have been computed at *t* | Look-ahead: the strategy looks excellent and is not |
| `evolution` | Stable feature definitions with versions | Population evolves against a moving target |
| `risk` | Current mark price with a known staleness bound | Positions sized against a price that no longer exists |
| `observability` | "What data existed at decision time" | The first of the eight reconstruction facts is unanswerable |

Everything below exists to serve one of those five rows.

---

## 2. Sources, and the ceiling they impose

Zero budget is a hard architectural constraint, not a phase. Every source below is free and requires no authentication except where noted.

| Source | Auth | Covers | Used for |
|---|---|---|---|
| `data.binance.vision` | none | Spot and USDⓈ-M futures archives, daily and monthly | All bulk history |
| Binance public WebSocket streams | none | Live klines, aggTrades, trades, bookTicker | Live ingestion |
| Binance public REST (`/api/v3/klines`, `/fapi/v1/klines`) | none | Recent bars | Gap backfill only |
| Binance testnet (spot + futures) | GitHub OAuth / testnet keys | Order lifecycle, user data | **Execution mechanics only** |

The last row carries a rule that overrides convenience everywhere: **testnet is never a statistical source.** Measured on Binance futures testnet, the spread is 7.5bp against production's 0.16bp — a ratio of roughly 47x — with roughly 10x inflated volume. Testnet tells you whether an order round-trips. It tells you nothing true about cost, liquidity, fill probability, or realised volatility. `BACKTEST_ENGINE.md` §4 makes the corresponding rule structural in the cost model.

### The `data.binance.vision` layout

```
https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2025-01-02.zip
https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2025-01-02.zip.CHECKSUM
https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2024-11.zip
https://data.binance.vision/data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2025-01-02.zip
https://data.binance.vision/data/futures/um/daily/bookDepth/BTCUSDT/BTCUSDT-bookDepth-2025-01-02.zip
```

Every `.zip` has a `.zip.CHECKSUM` sibling containing a SHA-256 digest and the filename. The rule is absolute:

> **No archive is read before its checksum is verified.** Not "verified after a successful parse" — verified before the first byte is handed to a parser.

The failure this prevents is not a corrupt file that fails to open. It is a **truncated** archive that parses cleanly for 80% of its rows and then ends, producing a day with a plausible row count, a plausible price range, and eleven missing hours that nobody notices until a backtest six weeks later shows an implausibly clean edge in that window.

Prefer monthly archives where they exist (one request instead of thirty, one checksum instead of thirty) and fall back to daily for the current and previous month, which monthly archives lag.

`fking.data.archive` implements all of this: `resolve_granularity()` makes the monthly/daily choice from an injected `today_utc` rather than a clock read, and `ArchiveFetcher.fetch()` verifies before writing, caches the verified bytes with their `.CHECKSUM` sibling beside them, and re-verifies on every cache hit. It reaches the host over a **separate egress path** — `ARCHIVE_HOSTS` and `guarded_archive_client()`, not the trading allowlist — which holds no credential and which `fking.execution` cannot import. ADR 0017 records why the trading allowlist was not simply widened.

### Coverage reference points

| Series | Earliest |
|---|---|
| BTCUSDT spot 1m klines | **2017-08-17** |
| BTCUSDT spot trades | 2017-08-17 |
| BTCUSDT USDⓈ-M futures 1m klines | 2019-09-08 |
| USDⓈ-M `bookDepth` | 2023-01-01 (varies by symbol) |
| USDⓈ-M `bookTicker` | 2020-01-01 (varies by symbol) |

Earliest dates vary per symbol and per dataset. They are discovered by probing, recorded in the availability declaration, and never assumed.

---

## 3. THE THREE VERIFIED TRAPS

These are not hypotheses. Each was observed, each silently produces plausible wrong data, and none of them raises an exception. They are listed first in this document because a loader written without knowing them will pass its tests.

### Trap 1 — Spot timestamps became microseconds on 2025-01-01. Futures did not.

From **2025-01-01**, Binance spot archives (klines, trades, aggTrades) emit **microsecond** epochs. USDⓈ-M futures archives remain in **milliseconds**. There is no field, header, or filename that announces this.

A loader with a global `// 1000` divisor applied to post-2025 spot data produces timestamps around the year **56,000**. A loader with a global `* 1000` multiplier applied to pre-2025 data lands in **1970**. Neither raises. If the divisor is right for one market and wrong for the other, a mixed-market backtest has one leg aligned and one leg shifted by three orders of magnitude, and it will look either brilliant or broken depending on which way the shift ran.

**The rule:** epoch unit is resolved per `(market, date)`. There is no global constant anywhere in the parsing path. The resolver **raises** on an undeclared `(market, dataset, date)` combination rather than defaulting — defaulting is the entire bug.

```python
# Binance spot archives switched to microsecond epochs on 2025-01-01;
# USDⓈ-M futures archives did not. See docs/adr/0013.
EPOCH_UNIT: Final[dict[tuple[Market, str], tuple[date, EpochUnit]]] = ...
```

A range that crosses 2025-01-01 is **two ingestion specs**, not one with a conditional inside the row loop.

### Trap 2 — Futures kline CSVs have a header row. Spot kline CSVs do not.

USDⓈ-M futures kline archives begin with `open_time,open,high,low,close,volume,close_time,...`. Spot kline archives begin with data.

Read a futures file assuming no header and the string `"open_time"` is parsed as a timestamp — which, depending on the parser, either raises (best case) or coerces to `NaT`/`0` and becomes a row at the Unix epoch. Read a spot file assuming a header and **the first real bar of the day is silently discarded**. That is the dangerous direction: a day with 1,439 minutes instead of 1,440 looks like nothing at all, and it is always the same minute — 00:00 UTC — which correlates with daily-boundary behaviour that strategies care about.

**The rule:** `has_header_row` is a declared field on the ingestion spec, not sniffed per file. A file whose first token does not match the declaration causes the **whole file to be rejected**, not a row to be skipped. A silent off-by-one-row load is discovered by a backtest months later; a failed load is discovered in ninety seconds.

### Trap 3 — Spot trade files serialize booleans Python-style.

Spot `trades` archives write `True` / `False`, not `true` / `false` and not `1` / `0`. The affected columns are `is_buyer_maker` and `is_best_match`.

A lowercase comparison (`value == "true"`), a `json.loads`, or any parser that treats an unrecognised token as falsy returns `False` for **every row in the file**. Row counts are right. Prices are right. Volumes are right. Only the trade *side* is wrong — uniformly, on every trade.

An order-flow imbalance feature built on that data is not noisy; it is **sign-inverted**, and it will backtest beautifully in one direction. This is the trap with the highest ratio of damage to visibility.

**The rule:** `boolean_encoding` is declared per `(market, dataset)`. An unexpected token **rejects the row and increments a counter**; a rejection rate above 0.1% fails the run. A drifting boolean encoding is this trap recurring on a new dataset, and a counter is how you find out.

### Why these three are stated together

They share a shape: each is a *format* assumption that a competent engineer would make correctly for one `(market, dataset, date)` combination and incorrectly for another. The generalisation is the design rule:

> **Format is a property of `(market, dataset, date)`, never of the codebase.**

Anything that looks like a module-level parsing constant is trap 1 waiting to recur.

---

## 4. Normalization contract

Every loader is a pure function of `(bytes, IngestionSpec) -> (records, NormalizationResult)`, which makes it testable against recorded archive fragments checked into the repository. Implemented in `fking.data.loaders`; the recorded corpus is `tests/fixtures/archives/`, written by `tools/record_archive_fragment.py` (`TESTING.md` §5.4).

```python
@dataclass(frozen=True, slots=True)
class IngestionSpec:
    coordinate: ArchiveCoordinate  # market, dataset, symbol, archive_date, interval
    archive_format: ArchiveFormat  # from resolve_archive_format(); epoch unit, header, booleans
    source_checksum_hex: str  # the verified SHA-256; there is no unverified path in
    now_utc: datetime  # aware UTC, one reference instant for the whole run
    max_rejection_fraction: Decimal = Decimal("0.001")  # 0.1%, trap 3 above
```

Three differences from the sketch this section previously carried, each load-bearing:

- **The format fields are one `ArchiveFormat`, not five loose ones.** A resolved format is refused at construction if it does not cover the coordinate's own date, or if its market and dataset disagree with the coordinate's. Loose fields make trap 1 constructible — you can hand last year's epoch unit to this year's file and nothing notices until a timestamp lands in 1970.
- **`checksum_verified: bool` became `source_checksum_hex: str`.** A boolean is satisfied by writing `True`. The only way to hold the digest of a verified archive is to have verified one, so the digest is evidence where the flag was a claim.
- **`date_range` and `destination` are absent.** They belong to the backfill (#26), not to a parse of one file: a pure function has no use for the range it was drawn from, and a destination a parser cannot write to is a field something that *can* write will eventually read.

Records are `KlineRecord` and `TradeRecord` in `fking.data.loaders.records` — deliberately **not** `fking.domain.Bar` and `Tick`. A `Bar` holds an `Instrument` carrying `tick_size`, `lot_step` and `min_notional_quote`, and no archive file contains those, so a loader that built one would have to invent them; a backtest filling an invented lattice measures trades the venue would have refused. The canonical bar schema in §6 also carries `quote_volume` and the taker-buy columns, which a `Bar` correctly does not.

### Normalization steps, in order

1. **Verify checksum.** SHA-256 against the `.CHECKSUM` sibling. Mismatch → re-download once → mismatch again → escalate. A checksum that fails twice is a data-integrity event that invalidates every backtest that consumed the previous copy of that file.
2. **Verify header expectation** against `has_header_row`. Mismatch → reject the file.
3. **Parse prices and quantities as `Decimal` from the raw string.** Never via `float`, and never from `ccxt`'s unified structure, which returns Python floats — take the value out of `info` where the raw string survives.
4. **Apply the declared epoch unit**, producing tz-aware UTC datetimes.
5. **Assert timestamp plausibility.** Every timestamp must fall in `[2010-01-01, now + 1 day)`, against one reference instant fixed for the whole run. Both failure directions land far outside the window — 1970 in one, roughly the year 56,000 in the other — so a wrong unit is caught on the first run, every time. Checked per row rather than only on the first: a wrong declaration then rejects *every* row and step 6's fraction gate turns that into a refusal naming `epoch_out_of_range=1440/1440`, which is one rule doing the work of two.
6. **Parse booleans by declared encoding.** Unknown token → reject row, count it. Rejections above `max_rejection_fraction` refuse the file and return nothing partial, because the failures this catches — a drifted boolean encoding, a wrong epoch unit — are uniform across a file rather than sporadic.
7. **Detect gaps.** Do not fill them.
8. **Write**, then record a `NormalizationResult` as an episodic row.

```python
@dataclass(frozen=True, slots=True)
class NormalizationResult:
    rows_in: int
    rows_out: int
    rows_rejected: int
    rejection_reasons: Mapping[RejectionReason, int]  # per reason, never just a total
    epoch_unit_applied: EpochUnit
    first_event_time_utc: datetime | None  # None only for a genuinely empty archive
    last_event_time_utc: datetime | None
    source_checksum_hex: str
```

A run that reports only `rows_out` has hidden its rejections. Rejections are the interesting half of the output, and `rows_in == rows_out + rows_rejected == rows_out + sum(rejection_reasons.values())` is asserted as a Hypothesis property rather than trusted.

`rejection_reasons` is keyed by a `RejectionReason` enum, not by free strings. A rejection counter becomes a Prometheus label the moment ingestion is instrumented, and a free string mints a new time series every time someone rephrases a message — which on a dashboard is indistinguishable from a new failure appearing while the old one stopped.

`gaps_detected` is **not** on this record. Gap detection spans files — a gap can sit between the last bar of one archive and the first of the next — so it belongs to the coverage registry (#26) rather than to a pure function that has seen one file. Putting it here would make every single-file result claim there were no gaps.

### Gaps are data, not defects to be patched

**No interpolation. No forward-fill. No synthesised bars. Ever.**

A gap is information about the world: an exchange outage, a maintenance window, a delisting, a genuine archive hole. Filling it manufactures a price path that never traded, and a strategy will find that path and trade it — a synthetic bar has zero realised volatility and perfect mean reversion, which is catnip to exactly the strategies this system is trying to reject.

Gaps propagate to the availability declaration. A backtest whose window contains a gap either narrows its window or refuses to run. That decision belongs to `BACKTEST_ENGINE.md`, not to the loader.

---

## 5. Live ingestion

Live and historical land in the same canonical schema. The only differences are latency and the failure modes below.

### Streams consumed

| Stream | Purpose | Market |
|---|---|---|
| `<symbol>@kline_1m` | Canonical bar, closed bars only | both |
| `<symbol>@aggTrade` | Trade tape | both |
| `<symbol>@bookTicker` | Top-of-book quote | futures |
| `<symbol>@markPrice@1s` | Mark price and funding rate | futures |

Only **closed** klines are persisted (`k.x == true`). An open kline is a partial aggregate that will change; persisting it and updating it later is a mutation of a time series and turns replay into a lie. Open klines may feed a live-only staleness monitor; they never reach the store.

### Reconnect

Binance closes a WebSocket connection after **24 hours** regardless of health, and sends a ping every ~3 minutes expecting a pong within 10 minutes. So a reconnect is a scheduled event, not an exception.

- Exponential backoff with full jitter: base 1s, cap 60s, no upper retry limit.
- A reconnect **always opens a gap** in the record, even if it lasts 400ms. The gap is recorded with its exact bounds. Reconnects that recover invisibly are how you end up unable to explain a missing minute nine months later.
- Connection state changes emit an event with the correlation ID of the ingestion session.
- **The stream is never restarted by catching an exception inside the read loop.** Per `CLAUDE.md` §4, a swallowed exception converts a visible failure into silent wrong data. The supervisor restarts the session; the loop does not defend itself.

### Gap detection

Two independent detectors, because they fail differently:

1. **Sequence detector** — for `aggTrade`, consecutive `a` (aggregate trade id) values must be contiguous. A jump is a gap with an exact size, detected within one message.
2. **Cadence detector** — for `kline_1m`, a closed bar must arrive for every minute. A minute with no closed bar within a 90s grace window is a gap. This catches the case the sequence detector cannot: a stream that is connected and silent.

Both write the same `gaps_detected` structure the archive loader writes. Downstream code cannot tell whether a gap came from a live outage or a missing archive, and should not need to.

### Backfill, and the seam

After a gap is closed, backfill from public REST (`/api/v3/klines`, `/fapi/v1/klines`), then reconcile the overlap.

> **Reconcile the seam on exchange trade id, never on timestamp.**

This is the non-obvious constraint in this section. Timestamps at a reconnect seam are the least reliable field in the record: the live stream carries the exchange's event time, REST backfill carries the archive's time, clocks differ, and a microsecond/millisecond boundary can sit inside the window. Deduplicating on `(symbol, timestamp)` at a seam either drops a real trade or admits a duplicate, and both are invisible. `aggTrade.a` and `trade.id` are monotone integers assigned by the exchange and are the only join key that is actually authoritative.

For klines, which have no id, the seam key is `open_time` **plus** a full-field equality check on the overlapping bars. A mismatch in the overlap means the stream and the REST view disagree about a closed bar, which is an escalation, not a merge conflict to resolve by preference.

---

## 6. Canonical storage layout

### Parquet — bulk historical, scanned by DuckDB

```
data/parquet/
  market=spot/dataset=klines/symbol=BTCUSDT/interval=1m/year=2025/month=01/
      part-2025-01.parquet
  market=futures_um/dataset=klines/symbol=BTCUSDT/interval=1m/year=2025/month=01/
      part-2025-01.parquet
  market=spot/dataset=trades/symbol=BTCUSDT/year=2025/month=01/day=02/
      part-2025-01-02.parquet
  market=futures_um/dataset=bookDepth/symbol=BTCUSDT/year=2025/month=01/
      part-2025-01.parquet
```

- Hive-style partition keys, so DuckDB prunes on `market`, `symbol`, `interval` and date without reading files.
- **Bars partitioned monthly, trades daily.** Trades are the volume driver; monthly trade files are multi-gigabyte and destroy partition pruning. Bars are small enough that daily files would produce tens of thousands of tiny files, which is worse.
- Target file size 128–512 MB. Below ~32 MB the per-file overhead dominates a scan.
- ZSTD level 3. Sorted by timestamp within the file, so predicate pushdown on row-group statistics actually eliminates row groups.
- `market` and `dataset` are partition keys rather than columns specifically so that a query cannot accidentally union spot and futures rows — the two markets had different epoch units for part of history and the partition boundary is a reminder that they are different animals.

### Canonical bar schema

Declared once in `fking.data.parquet.schema`, never inferred from the values being written — pyarrow derives a `Decimal` column's precision and scale from the batch it is handed, so a month whose prices all carry two decimal places produces `decimal128(8, 2)` and the next month produces something else. Two files, two schemas, one glob, and a scan that fails on a type mismatch nobody introduced.

| Column | Type | Note |
|---|---|---|
| `open_time_utc` | `timestamp[us, tz=UTC]` | Bar start, half-open `[open_time_utc, close_time_utc)` |
| `close_time_utc` | `timestamp[us, tz=UTC]` | As the archive filed it, not rounded up |
| `open_quote_price`, `high_quote_price`, `low_quote_price`, `close_quote_price` | `decimal128(38, 18)` | Never float |
| `base_volume`, `quote_volume` | `decimal128(38, 18)` | Base and quote |
| `trade_count` | `int64` | |
| `taker_buy_base_volume`, `taker_buy_quote_volume` | `decimal128(38, 18)` | |
| `source` | `string` | `archive` or `stream` |
| `ingested_at_utc` | `timestamp[us, tz=UTC]` | Wall time of write, injected rather than read |

### Canonical trade schema

| Column | Type | Note |
|---|---|---|
| `venue_trade_id` | `string` | An identifier, not a quantity. The seam reconciliation join key |
| `event_time_utc` | `timestamp[us, tz=UTC]` | |
| `quote_price`, `base_quantity`, `quote_quantity` | `decimal128(38, 18)` | |
| `is_buyer_maker`, `is_best_match` | `bool` | `is_buyer_maker` is the aggressor side inverted — trap 3 |
| `source` | `string` | `archive` or `stream` |
| `ingested_at_utc` | `timestamp[us, tz=UTC]` | |

**Column names mirror the record field names in `fking.data.loaders.records` character for character**, per `.claude/rules/naming.md`. `open` and `price` are ambiguous in a trading system, and — more mechanically — a column with no unit suffix is invisible to any check that keys on one. The test asserting that every money column reads back as `DECIMAL(38, 18)` selects its columns by the `_price` / `_volume` / `_quantity` suffix, so a column added next year is covered the moment it is named, rather than when someone remembers to extend a list.

`KlineRecord.ignored_field` is deliberately **not** stored. The parser retains Binance's trailing always-zero column so that the field count it checks is the file's field count — an 11-column row means a column was removed upstream, which is worth failing on. On disk it is a column of zeroes with no reader.

`source` and `ingested_at_utc` are not decoration. When a backtest result is disputed, the first question is which rows came from a live stream and when they landed, because a stream-sourced bar backfilled after the fact has a different provenance from one that arrived on time. Gate 11 below queries `source` to prove no synthesised rows exist, which is only possible because it is written here.

**Writes are idempotent by content digest.** A SHA-256 over the sorted records and their `source` — excluding `ingested_at_utc`, because when we happened to read a file is not part of what the file said — is stored in the file's own Parquet key-value metadata. A re-run whose digest matches declines to rewrite, so re-backfilling a month tomorrow is a no-op rather than a new set of bytes. `source` is inside the digest because a stream-written month later re-fetched from the archive is the one rewrite that must happen.

**Reads go through `fking.data.parquet.read_connection`, which pins `SET TimeZone = 'UTC'`.** DuckDB renders a `TIMESTAMP WITH TIME ZONE` in the session timezone, defaulting to the machine's local zone. The instant survives — an equality assertion against the same instant in UTC still passes — but the returned object carries a local offset, so `.hour`, `.date()` and anything that later drops the tzinfo are wrong by that offset, and a developer outside UTC disagrees with CI about which day a bar belongs to with no error on either side. Same pin, same reason, as `ALTER DATABASE fking SET timezone TO 'UTC'`.

### TimescaleDB — operational state and recent series

Postgres holds what the running system reads: recent bars (a rolling window, chunk interval 1 day), the feature store, positions, orders, fills, agent memory and the audit tables. It is not a backtest scan target — `ARCHITECTURE.md` §6 splits bulk analytics to Parquet/DuckDB precisely so a full-universe scan cannot make the operational store unusable.

Compression and retention policies apply to market-data hypertables. They are **forbidden** on any hypertable backing an audit trail: compression rewrites chunks, which is mutation of append-only data under a different name.

---

## 7. Point-in-time semantics

> A feature value computed at time *t* must be reproducible using **only data that existed at *t***.

Look-ahead is the most dangerous defect class in this project because it does not fail. It produces a strategy that looks excellent, passes review, gets promoted, and then loses money in a way that reads as regime change rather than as a bug.

### The five ways it gets in

1. **Using the bar's own close to decide an action within that bar.** The single most common leak. If a decision is made at time *t*, the bar containing *t* has not closed and its `close`, `high` and `low` are unknown.
2. **Right-edge resampling.** Resampling 1m → 5m with `label='right'` stamps a bar with the timestamp of its *end*, so a join on that timestamp hands you a bar built from the following five minutes. `pandas` defaults vary by rule; the only safe posture is to state `label` and `closed` explicitly at every resample and to assert the result.
3. **Joining on timestamp equality instead of as-of.** A feature joined with `==` on a timestamp grid silently pairs a decision with a value published at the same instant, which in practice means a value that was not yet available. Every cross-series join is `as-of`, backward, with **strict** inequality.
4. **Backfilling a changed feature definition into history.** Recomputing a feature under a new definition and overwriting old values makes every historical backtest a test of a definition that did not exist then. Definition changes create a **new feature version**; old values remain, tagged with the version that produced them.
5. **Warm-up from the test side of a fold boundary.** Covered in `BACKTEST_ENGINE.md` §6, but it originates here: a feature that needs 2 hours of history to warm up must get those 2 hours from the training side of the purge boundary or the fold starts later.

### The mandatory proof

Every `FeatureSpec` carries a `point_in_time_proof` string. It is a required schema field, not a docstring.

```python
FeatureSpec(
    name="order_flow_imbalance_1h",
    version=1,
    lookback=timedelta(hours=1),
    point_in_time_proof=(
        "Window is half-open [t-1h, t), joined as-of on trade timestamp with "
        "strict inequality; no trade at exactly t is included."
    ),
    ...
)
```

If the proof cannot be stated in one sentence, the feature is probably leaking. That is the point of requiring it — it converts an unverifiable intention into a checkable claim.

### Declared lookback feeds validation

`FeatureSpec.lookback` is not documentation. `BACKTEST_ENGINE.md` §6 derives the walk-forward embargo floor from `max_feature_lookback + max_holding_horizon`. **An understated lookback silently weakens every validation downstream** — the embargo is too short, adjacent folds share information, and CPCV reports a stable edge that is partly the same data seen twice. This is the quietest way to break the validation machinery, because nothing about it looks wrong.

### The adversarial leak test

A dedicated test injects future data into the feature computation path and asserts that the guard **raises**. It runs in CI on every commit.

The test itself is verified by breaking the guard on purpose and confirming the test goes red. A leak test that has never been observed to fail is not evidence of anything; it is a test that might be asserting `True == True`.

---

## 8. The feature availability contract

The feature store declares what exists. **Strategies cannot request data the system does not have, and the store refuses rather than substituting something adjacent.**

```python
class AvailabilityDeclaration(BaseModel):
    markets: list[str]
    earliest: date
    resolution: str
    known_gaps: list[tuple[date, date]]
    refuses_if_unavailable: Literal[True]             # not configurable
```

The `Literal[True]` is deliberate. There is no permissive mode, because the permissive mode is where a strategy gets a forward-filled approximation of depth and does not know it.

When the store refuses, it says what does exist. "No L2 depth history exists for free; you have tick trades, futures top-of-book, and ~1-minute aggregated depth bands" is a usable answer that redirects the research. A bare `KeyError` is not.

This matters most for LLM-authored strategies. An agent asked to write a mean-reversion strategy will cheerfully request `order_book_imbalance_top_10_levels` because that feature exists in the literature it was trained on. The refusal is the mechanism that keeps the strategy population inside the data the system actually possesses.

---

## 9. The honest L2 constraint

> **Free full-depth L2 order book history does not exist.**

Not on `data.binance.vision`, not on any free mirror. Reconstructing it from a live diff-depth stream requires running a collector continuously from now onward and gives you nothing for the past. This is the single hardest ceiling on the system's research space and it is stated plainly here so that nobody spends a week rediscovering it.

### What `bookDepth` actually is

`bookDepth` archives are frequently assumed to be order book snapshots. They are not. The schema is:

```
timestamp,percentage,depth,notional
1704153600000,-1,12.482,538201.44
1704153600000,-2,31.007,1336940.11
1704153600000,-3,58.113,2504118.90
1704153600000,-4,94.220,4059331.72
1704153600000,-5,141.556,6098005.63
1704153600000,1,11.903,513276.19
...
```

- One row per **depth band**, sampled roughly **once per minute** — not per book update, not per second.
- `percentage` ∈ `{-5,-4,-3,-2,-1,+1,+2,+3,+4,+5}`, meaning distance from mid in percent. Negative is the bid side.
- `depth` is cumulative base quantity within that band; `notional` is cumulative quote value.
- There is no price level, no per-level quantity, no queue, no book event, and no ordering.

Ten cumulative bands at ±1/2/3/4/5% from mid, once a minute, is a *shape* statistic. On BTCUSDT a ±1% band is roughly ±$1,000 at current prices — hundreds of ticks wide. It cannot tell you what is at the touch, cannot support queue-position modelling, and cannot support any microstructure signal that depends on level-by-level state.

### The actual ceiling

| Available | Not available |
|---|---|
| Tick trades (`trades`, `aggTrades`) with side and exchange trade id | Full-depth L2 book history |
| Futures top-of-book (`bookTicker`) best bid/ask and sizes | Per-level book state or book events |
| Coarse depth bands (`bookDepth`), ~1/min, ±1–5% | Queue position, order-by-order flow |
| Funding rate, open interest, mark price (futures) | Passive fill probability |
| Klines at every standard interval | Anything requiring L3 / MBO |

### What follows from it, everywhere else

- **Strategy space is bounded.** No queue-position strategies, no book-pressure microstructure, no passive-fill-probability optimisation. Those are not "not yet built"; they are not fundable at this budget.
- **The cost model cannot model depth-consuming slippage from data.** With top-of-book only, the honest assumption is that the quoted top-of-book quantity is all there is until a fill proves otherwise. `BACKTEST_ENGINE.md` §4 states the resulting slippage function and its parameter provenance.
- **`market-research` returns `None` for passive fill probability** rather than a number. A number would be fabricated, and a fabricated number is worse than an admitted absence because it propagates into sizing.
- **Depth bands are still useful** — as a regime feature. The ratio of ±1% notional to ±5% notional is a legitimate, point-in-time, once-a-minute measure of book concentration. It is just not a book.

---

## 10. Data quality gates

Gates run at ingestion and block the write. A gate that runs after the write is a report, not a gate.

| # | Gate | Threshold | On failure |
|---|---|---|---|
| 1 | Checksum verified | exact SHA-256 match | Reject file; re-download once; then escalate |
| 2 | Header expectation matches spec | exact | Reject **file**, never skip a row |
| 3 | First timestamp plausible | `[2010-01-01, now+1d)` | Stop the load; write nothing partial |
| 4 | Timestamps monotone non-decreasing | 0 violations | Reject file — out-of-order rows mean the wrong epoch unit or a merged file |
| 5 | Boolean tokens recognised | reject rate < 0.1% | Fail the run; a drifting encoding is trap 3 recurring |
| 6 | OHLC coherence | `low <= min(open,close) <= max(open,close) <= high` | Reject row, count; > 0.01% fails the run |
| 7 | Non-negative volume | 0 violations | Reject row, count |
| 8 | Bar cadence | expected bars per day for the interval | Record gap; never fill |
| 9 | Price continuity | \|log return\| between consecutive bars < 0.5 | Flag, do not reject — real crypto moves do this |
| 10 | Cross-source agreement | archive vs stream overlap identical on all fields | Escalate on mismatch |
| 11 | No synthesised rows | `SELECT count(*) WHERE source NOT IN ('archive','stream')` = 0 | Fail; interpolation has entered the store |

Gate 9 is deliberately a flag, not a rejection. A 50% single-minute move on a thin altcoin is a real event, and rejecting it removes exactly the tail the risk engine most needs to have seen. Gates that reject unusual-but-real data quietly bias every downstream volatility estimate toward calm.

Gate 10 is the one that finds problems nobody predicted. Where an archive and a live stream cover the same minute, every field must match. They usually do. When they do not, something upstream changed — a schema revision, a new epoch unit, a symbol renaming — and this gate is the earliest possible warning.

`fking.data.quality` implements all eleven. `gates` holds one function per gate 1–9 and a `Gate` enum whose members are the reason codes a refusal carries; `ingest.ingest_archive` is the ordering, and it is the only path from archive bytes to the Parquet corpus — the write is its last statement, so a gate that raises leaves no file behind. `cross_source` holds gate 10, which needs two sources and therefore cannot live inside one file's parse, and `standing` holds gate 11, which is a question about the whole store.

Three details of that implementation are worth knowing before changing it:

- **The parser reports and the gates adjudicate.** `ingest_archive` hands `parse_archive` a rejection ceiling of 1 and then applies the declared ceiling itself, split across gates 5, 6, 7 and a residual rule covering every `RejectionReason` no numbered gate owns. The refusals are identical; the messages are not. "0.4% of rows were rejected" cannot distinguish a drifted boolean encoding from a wrong epoch unit, and `boolean_unrecognised=1440/1440` can.
- **Gate 3 reads a declared column, not column 0.** A kline row opens with `open_time`; a trade row opens with `trade_id` and carries its epoch fifth. A gate 3 that assumed column 0 would normalise a nine-digit trade id as a timestamp, land in 1970, and refuse every genuine trades archive — a gate that rejects only correct files.
- **Gate 9 compares a `Decimal` price ratio against exp(±0.5)** rather than taking a logarithm. `|log(p₁/p₀)| < 0.5` and `exp(-0.5) < p₁/p₀ < exp(0.5)` are the same statement, and the second one is exact.

The corpus that proves each gate fires is `tests/fixtures/corrupt/`, derived from the recorded archives by `tools/corrupt_archive_fixture.py`. Every file there is a declared, deterministic mutation of a real recording with a `.corruption.json` sidecar naming its source, its mutation and both digests, and CI re-derives all of them: a corrupt fixture edited by hand to make a gate pass would otherwise be indistinguishable from one the generator wrote. `make check` runs the derivation check, and the corruption corpus is its own CI job so that its failure is not buried among eighteen hundred unit tests.

### Ongoing gates, not just ingestion

- **Weekly re-verification** of a random sample of archived Parquet against a fresh checksum. Free archives are not guaranteed to remain available or unchanged; a silently revised upstream file invalidates every backtest that read the old one.
- **Coverage report per `(market, symbol, dataset)`** showing first timestamp, last timestamp, gap count and total gapped duration. `backtest` reads this before every run.
- **Staleness monitor** on live streams: last closed bar age per symbol. Beyond 120 seconds the feature store marks the symbol stale and the risk engine treats its mark price as unusable rather than merely old.

---

## 11. Failure handling

| Failure | Response |
|---|---|
| Checksum fails twice | Escalate. Upstream archive may have changed — a data-integrity event affecting every backtest that used it |
| Timestamp plausibility assertion fails | Stop; write nothing partial. Report the raw magnitude observed and the unit applied. The fix is the resolver, not the data |
| Header assumption wrong | Reject the whole file. A silent off-by-one-row load surfaces six weeks later as a backtest anomaly |
| Boolean token unexpected | Reject row, count, fail run above 0.1%. Investigate as trap 3 on a new dataset |
| Gap found in a range already backtested | Notify with the affected run ids. Those results are suspect and are re-run or voided |
| Live stream disconnects | Mark gap, reconnect with jittered backoff, backfill from REST, reconcile on exchange trade id |
| Unknown `(market, dataset, date)` epoch unit | **Escalate. Do not infer.** Inferring is trap 1's exact mechanism |
| Strategy needs data beyond the free-tier ceiling | Escalate. That is a budget and architecture decision, not a pipeline one |

---

## 12. Cross-references

| For | See |
|---|---|
| Why point-in-time is structural, and the storage split | `ARCHITECTURE.md` §6 |
| Purge, embargo, and how `lookback` feeds them | `BACKTEST_ENGINE.md` §6 |
| Cost model calibration and why testnet is void | `BACKTEST_ENGINE.md` §4 |
| Provenance as one of the eight reconstruction facts | `OBSERVABILITY.md` §1 |
| Retention, compression, and the audit-table exemption | `DEPLOYMENT.md` §6 |
| Feature-store configuration surface | `CONFIGURATION.md` §5 |
| The epoch-unit decision record | `docs/adr/0013` |
