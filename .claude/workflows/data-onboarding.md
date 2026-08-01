# Workflow — Data Onboarding

Adding a new data source, symbol, or historical archive. Assume every file is wrong until proven otherwise — three specific traps in this project's own data were found the expensive way, and they are listed below.

---

## 1. Declare what it is before downloading it

Write down: source, market (`spot` / `futures` / other), instrument, granularity, native timestamp unit, timezone, date coverage, licence, and cost. If it costs money, it does not go in — this is a zero-budget project.

---

## 2. Verify integrity before parsing

```bash
sha256sum -c <archive>.CHECKSUM
```

Every archive is checksum-verified before it is trusted. An archive that fails is discarded, not repaired — a partially corrupt bar file produces a plausible series with a hole in it, which is worse than an obviously broken one.

---

## 3. The three verified traps — check every one, every time

These are not hypothetical. They were all found in this project's actual data:

**A. Timestamp units change mid-history.** Binance **spot** switched to **microseconds from 2025-01-01**, while **futures** stayed in **milliseconds**.

Normalization is keyed on `(market, date)`, **never on a global constant**. A global constant is correct for exactly the range it was written against and silently wrong on both sides.

```bash
head -3 <file>   # look at the raw integer, not a formatted date
```

A 13-digit value is milliseconds; 16 digits is microseconds. Convert and assert the result lands in a plausible UTC window — a silent unit change puts bars in 1970 or the year 56000, and formatted output can hide it.

**B. Header rows are inconsistent.** Futures kline CSVs **have** a header row; spot ones **do not**. Consuming a header as data shifts every subsequent row by one and produces a series that looks fine.

**C. Booleans are Python-style.** Spot trade files serialize `True`/`False`, which most CSV readers hand back as non-empty strings — and every non-empty string is truthy. A `is_buyer_maker` column read this way is uniformly `True`, which quietly inverts trade-side attribution.

---

## 4. Sanity-check the series itself

- Duplicate timestamps
- Gaps — cross-reference known exchange outages before "fixing" them; a gap is data, a forward-fill over a gap is fabrication
- Zero or negative prices
- Volume discontinuities that are unit changes rather than events
- Monotonic timestamps after normalization
- Prices consistent in magnitude with a known reference point on a known date

---

## 5. Decide the storage tier

- **PostgreSQL + TimescaleDB** — operational state and time-series that must be queried transactionally
- **Partitioned Parquet on disk, queried in-process by DuckDB** — bulk historical bars for backtest scans

Bulk history goes to Parquet. Putting years of tick data in Postgres because it is already running is the mistake this split exists to prevent.

---

## 6. Declare availability, honestly

The feature store declares what actually exists, and refuses requests for what does not. This is deliberate: **free full-depth L2 order book history does not exist.** Binance `bookDepth` is aggregated depth bands sampled roughly once per minute, not snapshots.

When registering the new source, record its true resolution and its true earliest clean date. Overstating either lets a strategy silently assume richer data than exists, which is the failure the availability contract is designed to prevent.

---

## 7. Point-in-time semantics

Any feature computed from this source at time *t* must be reproducible from data that existed at *t*.

- Store the as-of time alongside every revisable value. Economic and fundamental series get **revised** — using the revised value at a historical timestamp is look-ahead wearing a respectable suit.
- No backfilling a corrected value into a historical row without recording both the original and the correction with their as-of times.
- Extend the adversarial leakage test to cover this source, and confirm it fails closed.

---

## 8. Test it

- Normalization test for this source's exact `(market, date)` unit behaviour
- Header/no-header parsing test
- Boolean parsing test asserting an actual boolean type, not truthiness
- A recorded-real-sample fixture, never hand-written
- Leakage test extended to the new features

```bash
make check
```

---

## 9. Document it

Add to `DATA_PIPELINE.md`: the source, its traps, its true availability, and its earliest clean date. The traps section is the most-read part of that document and it exists because each entry cost someone a day.

---

## 10. Backfill and verify

```bash
python -m fking.data.ingest --source <name> --from <date> --to <date>
python -m fking.data.verify --source <name>
```

Then re-run one pinned backtest and confirm results are unchanged where the new source is not used. If they moved, the ingestion touched something it should not have.
