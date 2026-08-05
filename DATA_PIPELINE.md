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
- **`date_range` and `destination` are absent.** They belong to the backfill (`fking.data.backfill`), not to a parse of one file: a pure function has no use for the range it was drawn from, and a destination a parser cannot write to is a field something that *can* write will eventually read.

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

`gaps_detected` is **not** on this record. Gap detection spans files — a gap can sit between the last bar of one archive and the first of the next — so it belongs to the coverage registry rather than to a pure function that has seen one file. Putting it here would make every single-file result claim there were no gaps.

### The bulk backfill

`make ingest SYMBOLS=BTCUSDT,ETHUSDT INTERVAL=1m` walks each symbol's discovered range to T-1 and prints rows in / out / rejected with per-reason counts, gap count and total gapped duration. `fking.data.backfill` implements it; `make data-coverage` prints the coverage report below.

Four properties are worth knowing before changing it, because each closes a failure that is silent without it.

**An archive is not a Parquet file.** Bars are partitioned monthly (§6) and published daily for the current and previous month (§2), so a recent month arrives as up to thirty-one archives that all belong in one partition. `write_records` writes a partition whole, so writing each archive as it arrives leaves the month holding whichever one was written last — a file with a plausible name, a plausible schema, and one day in it. The partition is therefore the unit of work: `quality.ingest_partition` gates every archive, concatenates what survived, and writes once. This is also the only arrangement under which gate 8 can see a day that is missing entirely, because each surviving file is individually complete.

**Resume is derived from the registry and the corpus, never from a progress file.** A partition is skipped only when `ingest_partition` says its coverage reaches the run's target date, its `absent_archive_count` is zero, *and* the Parquet file on disk still carries the `content_digest_hex` the registry recorded. A progress file is a third opinion that can disagree with both.

**An absent archive is re-probed on every run.** Absence is a claim about upstream, and upstream does publish a missing day later. Caching a 404 makes that fix permanently invisible, so a partition that met one is never marked complete — one eighty-byte request per absent day, which is the only route by which a corrected archive is ever picked up.

**A rewrite that would cover less than the corpus already holds is refused.** A partition is written whole, so re-deriving one from fewer archives than last time *deletes* the difference — and the only surviving evidence would be a row count nobody is comparing against last week's. The reachable path is not operator error: a partition that met an absent archive is deliberately re-derived, and a narrower `--through` or a tail day that stopped being published then truncates it. The run raises and names both ranges.

**Per-symbol earliest dates are discovered by probing, never assumed** (§2). The probe is a binary search over `.CHECKSUM` siblings, so a hundred months costs seven requests. It assumes publication is contiguous from a symbol's listing; a hole after that date is not the search's problem — the backfill meets it as an absent archive and it becomes a gap. The run summary states the consequence explicitly, because it is the one a researcher forgets: **a hypothesis inherits the shortest history among its inputs**, which is usually far shorter than the BTC history that made the idea look testable.

### Gaps are data, not defects to be patched

**No interpolation. No forward-fill. No synthesised bars. Ever.**

A gap is information about the world: an exchange outage, a maintenance window, a delisting, a genuine archive hole. Filling it manufactures a price path that never traded, and a strategy will find that path and trade it — a synthetic bar has zero realised volatility and perfect mean reversion, which is catnip to exactly the strategies this system is trying to reject.

Gaps propagate to the availability declaration. A backtest whose window contains a gap either narrows its window or refuses to run. That decision belongs to `BACKTEST_ENGINE.md`, not to the loader.

`coverage_gap` stores a gap as the half-open **missing region** `[gap_start_utc, gap_end_utc)` — not the observations bracketing it, which would overstate every gap by two bars and make `sum(gap_end - gap_start)` a number nobody could use. Three kinds, and the distinction follows from whether the dataset has a cadence at all:

| Kind | Means | `missing_bar_count` |
|---|---|---|
| `cadence` | Bars missing inside one partition | exact |
| `seam` | Bars missing between two partitions | exact |
| `absent_archive` | The host does not publish this period, for a dataset with no declared cadence | `NULL` |

`absent_archive` carries no count on purpose. A trades archive that was never published says nothing about how many prints are missing, and a zero there would read as "none" — a stronger claim than the evidence supports. For bars the absence needs no separate kind: a missing day between two present ones *is* a cadence or seam gap, with the exact bounds and the exact count.

**Gaps are recorded as discovered and never merged.** Two adjacent absent days stay two rows. Merging means rewriting a row to widen its bounds, which destroys the earlier `discovered_at_utc` — and that column is the whole point of the table, because a gap found inside a range a completed backtest already consumed is what makes those results suspect (§11). The duration total is the same either way.

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

A disconnect gap's bounds are **the last event observed before the drop, and the instant listening resumed** — the second being a wall-clock reading, not the event time of the first frame after the reconnect. The distinction is not cosmetic. A kline's event time is the bar's *open*, up to a full interval before the frame carrying it arrived, so closing the gap on it would routinely produce a region that ends before it starts. Taking the left edge from the last observation rather than from the error instant matters for the opposite reason: a connection killed by a missed pong surfaces as an error up to ten minutes after the last frame, and anchoring there would report ten unobserved minutes as observed.

Only exceptions in the transport's own failure vocabulary end a session for a retry — `fking.platform.safety.TRANSPORT_ERRORS`, which is the one place allowed to know what the transport is. A frame the parser cannot understand is not in that set and stops the process: a venue whose payload shape changed is not something to retry into for a week.

### Gap detection

Two independent detectors, because they fail differently:

1. **Sequence detector** — for `aggTrade`, consecutive `a` (aggregate trade id) values must be contiguous. A jump is a gap with an exact size, detected within one message.
2. **Cadence detector** — for `kline_1m`, a closed bar must arrive for every minute. A minute with no closed bar within a 90s grace window is a gap. This catches the case the sequence detector cannot: a stream that is connected and silent.

Both write the same `gaps_detected` structure the archive loader writes. Downstream code cannot tell whether a gap came from a live outage or a missing archive, and should not need to.

The 90 second window is measured from the interval's **open**, which is the only edge a detector knows about before anything arrives. A 1m bar is published within about a second of its close, so the effective slack is roughly thirty seconds; measuring from the close instead would put the deadline a full interval later and let a silent stream run for two and a half minutes unreported.

An outage therefore produces two rows rather than one: a `disconnect` gap over the unobserved window, and — once their grace windows expire — a `cadence` gap naming the exact minutes whose bars are absent. They answer different questions, "when were we not listening" and "which bars are missing", and the availability contract (§8) refuses a window intersecting either. The cost is that `data_coverage.total_gapped_duration` counts an outage twice, which is why that column is a diagnostic aggregate and not an accounting figure.

`missing_bar_count` on a `disconnect` gap is **NULL, never zero**. A 400ms reconnect inside one minute loses no bar at all, and a zero would claim we checked and found nothing absent when in fact nothing was being checked.

**The live `aggTrade` tape is spooled, not buffered.** There is no operational trade table, because §6 takes the tape only as a whole daily Parquet partition — so a print cannot become a row until its day is over. `fking.data.live.tape` appends each print to a line-buffered NDJSON spool keyed by `(series, UTC day)` and seals the day into its partition fifteen minutes past the following midnight. The memory bound is **one open file handle per subscribed symbol**, and it does not grow with the day, the print rate, or the session's uptime: a day of BTCUSDT prints runs to millions of rows, and buffering a day per symbol is the design that stops working at exactly the scale this system is for.

Three consequences worth stating, because each closes a failure that is otherwise silent. The spool **outlives the process**, so a session restarted at noon continues the same file and a spool left by a session that died is sealed by the next one — which is why the seal reads the spool directory rather than any in-memory state. A seal **may not shrink a partition**: the row count in the existing file's Parquet footer is compared against what is about to be written, and a rewrite that would delete prints is refused rather than resolved. And a **torn spool line stops the seal** instead of being skipped, because prints dropped there are an absence with no gap row, which is the one shape of hole the coverage registry cannot describe.

### Backfill, and the seam

After a gap is closed, backfill from public REST (`/api/v3/klines`, `/fapi/v1/klines`), then reconcile the overlap.

> **Reconcile the seam on exchange trade id, never on timestamp.**

This is the non-obvious constraint in this section. Timestamps at a reconnect seam are the least reliable field in the record: the live stream carries the exchange's event time, REST backfill carries the archive's time, clocks differ, and a microsecond/millisecond boundary can sit inside the window. Deduplicating on `(symbol, timestamp)` at a seam either drops a real trade or admits a duplicate, and both are invisible. `aggTrade.a` and `trade.id` are monotone integers assigned by the exchange and are the only join key that is actually authoritative.

For klines, which have no id, the seam key is `open_time` **plus** a full-field equality check on the overlapping bars. A mismatch in the overlap means the stream and the REST view disagree about a closed bar, which is an escalation, not a merge conflict to resolve by preference. Two fields are outside that comparison and each for a stated reason: `close_time` is a derived boundary whose representation differs by epoch unit (`.999` against `.999999`, the trap above), and `ignored_field` is the archive's trailing filler column, which is `"0"` from a CSV, `""` from the stream, and absent from the `bar` table. Everything that describes the market is compared exactly, as `Decimal`, with no tolerance — a tolerance would be a threshold below which two sources are allowed to disagree, and nobody can name that number for a volume field spanning eight orders of magnitude across symbols.

The fetch window is deliberately **wider than the gap** — one interval on each side — because that overlap *is* the seam. A fetch that covered only the missing minutes would give the reconciler nothing to compare, and a disagreement between the two views of a closed bar would go undetected forever.

**A gap narrows; it does not disappear.** If REST returns forty of sixty missing minutes, the original row is marked `superseded`, narrower rows are inserted for the twenty that remain — each carrying the *original* `discovered_at_utc`, because those minutes were found missing then and are missing still — and only a fully recovered range is marked `backfilled`. Nothing is deleted and no row's bounds are ever rewritten: a repaired range is still the answer to "which completed backtests consumed this range while it was holed", which is a more interesting question after a repair, not a less interesting one. `data_coverage` and every registry read count only unresolved rows. ADR-0018 carries the argument, including why narrowing in place was rejected.

**A `disconnect` gap is not repaired by fetching its records.** It is a claim about *observation* — nothing was being watched between these two instants — and recovering the klines does not make that false. It is the only kind that is never backfillable: `cadence` and `seam` are claims about bars and are repaired from `/klines`, `sequence` is a claim about the tape and is repaired from `/aggTrades`, and each repair sees only its own dataset's kinds.

**The trade half of the reconciler.** `reconcile_trades` joins on `venue_trade_id` and leaves `event_time_utc` out of the comparison entirely, which is the rule above made mechanical: two records under one id and a few milliseconds apart are one print and exactly one survives, and two records sharing a millisecond under different ids are two prints and both survive. `is_best_match` is outside the comparison too — the `aggTrade` stream carries no such flag and the record sets it `True` by construction, so comparing it would escalate about a field the stream never observed. The live corpus writer is its first caller: a print delivered twice across a reconnect is the same same-id case, so the seal deduplicates through the seam and a contradiction — one id, two prices — escalates there as loudly as it would in a repair.

The REST side pages on **`fromId`, never on time**, and that is not a performance preference. A loss between two prints in the same millisecond spans a one-millisecond window, and `startTime`/`endTime` have no finer resolution than the prints they would have to separate — so there is no time window that addresses those prints. What there is, is arithmetic: the detector recorded `missing_bar_count` from `next_id − previous_id − 1`, so the missing ids are exactly `previous_id + 1 … previous_id + N` and `/aggTrades?fromId=` takes that range directly.

`previous_id` is looked up in the corpus rather than approximated. The gap's left bound *is* that print's own event time, so the id is the largest one held at exactly that instant — largest, because several prints can share a millisecond and the detector measured from the newest of them. If the corpus holds none there, the print the gap was measured from is gone, every id derived from it would be wrong, and the gap is left open rather than repaired against a nearby range that looks plausible.

The fetch asks for `N + 2` prints so it overlaps **both** bracketing prints, and that overlap is the seam — the same decision as the kline repair's one interval on each side, for the same reason.

**Only a sealed day is repaired.** The corpus for a day still in progress is a spool, not a partition, and reconciling against a file that is still being appended to is reconciling against a moving target — worse, the rewrite would race the seal for the same path. Such a gap is reported as *deferred*, which is a different answer from *unrepaired*: unrepaired means the venue had nothing, deferred means we did not ask.

**A residual is bounded by the prints that bracket it, because an absent print has no instant.** This is the sharpest difference from the kline repair, which recomputes its residual from a minute lattice. A print still missing has a known id and an unknown time, so the only honest bounds for a run of absent ids are the event times of the prints either side of it — and "present" means what the corpus holds *now*, not what this pass recovered: a gap row is frozen when the detector writes it while the tape keeps arriving, so a print inside an open gap can already be on disk, and counting it as absent would re-declare a print missing that is there.

**A repaired partition holds two provenances.** Prints the socket delivered keep `stream` and prints REST returned carry `rest_backfill`, in the same file, because that is exactly the distinction `source` exists to record. The Parquet writer therefore takes provenance per row rather than per batch, and its content digest normalises a decimal's scale — a `decimal128(38, 18)` column returns every value at eighteen places, so without that a partition read back and merged into would report a rewrite on every pass over a range already repaired.

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
  market=spot/dataset=aggTrades/symbol=BTCUSDT/year=2026/month=08/day=03/
      part-2026-08-03.parquet
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

The same seven columns serve `trades` and `aggTrades`, because an aggregate print and a raw print carry the same seven observations and giving them separate schemas would mean a reader had to know which corpus it was in to name a column. They stay separate **datasets** — different partition trees, never unioned by accident — because one aggregate print covers a range of raw ones and summing volume across both would double-count. `aggTrades` is the one dataset whose schema is written from a *socket* recording rather than an archive one: its CSV boolean encoding has never been read, so `DECLARED_FORMATS` still refuses the archive and only the live tape produces its rows.

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

### Enforced by the storage layer, not by the caller

`feature_values` carries both times: `event_time_utc` is when the thing happened, `available_at_utc` is the earliest instant this system could have known it. `available_at_utc >= event_time_utc` is a `CHECK`, and only `available_at_utc` governs visibility.

**`fking_app` holds no privilege on that table — not even `SELECT`.** The role every strategy, backtest and risk process connects as reaches feature data only through `fking_feature_as_of(name, version, market, symbol, as_of, lookback)`, a `SECURITY DEFINER` function whose body carries the `available_at_utc <= as_of` predicate. A look-ahead defect is therefore `permission denied for table feature_values` rather than a review miss, and there is no parameter that relaxes the bound.

`as_of` is keyword-only, non-optional and has no default on the Python side either. A default is a value somebody forgets to override, and the value they would forget is `now()`.

Revisions are appended, never updated: a corrected value is a new row with a later `available_at_utc`, and `DISTINCT ON (event_time_utc) ... ORDER BY available_at_utc DESC` returns the value as it was *believed* at `as_of`. Both rows survive, which is what lets a disputed result be reconstructed rather than argued about.

### The mandatory proof

Every `FeatureSpec` carries a `point_in_time_proof` string. It is a required schema field, not a docstring.

```python
FeatureSpec(
    name="trailing_return_fraction",
    version=1,
    compute=trailing_return_fraction,
    inputs=frozenset({"klines"}),
    lookback=timedelta(hours=1),
    availability_lag=timedelta(0),
    label_horizon=timedelta(hours=1),
    point_in_time_proof=(
        "Window is half-open (t-1h, t]; the base is the newest closed bar at or "
        "before t-1h and the head is the bar closing at t, so both endpoints had "
        "already closed at t. No partial window is emitted."
    ),
    uses_trailing_statistics_only=True,
)
```

Every field is required and none has a default, because the values somebody would forget to override are `lookback=0` and `availability_lag=0` — both of which are the permissive answer. `uses_trailing_statistics_only=False` is not a configuration, it is a refusal: there is no supported way to register a full-sample statistic.

If the proof cannot be stated in one sentence, the feature is probably leaking. That is the point of requiring it — it converts an unverifiable intention into a checkable claim.

`lookback` is also the window the computation actually uses: the spec hands it to `compute` rather than the function carrying its own constant, so the declared number and the computed number cannot drift apart.

### A definition change is a new version, and the digest is what says so

`fking.data.features._definition_digests` holds one frozen digest per `(name, version)`, taken over the parsed syntax tree of the `compute` function — so reformatting and comments do not move it and a changed constant does. Entries are never edited and never removed. A `compute` edited under an unchanged `version` fails `tests/data/features/test_feature_registry.py`, which names the version to bump.

The reason a rename does not move the digest, while an edited body does: a digest that changes on cosmetics is a digest people learn to update without reading what changed, and the lock is worth exactly as much as the attention it gets.

### Declared lookback feeds validation

`FeatureSpec.lookback` is not documentation. `BACKTEST_ENGINE.md` §6 derives the walk-forward embargo floor from `max_feature_lookback + max_holding_horizon`. **An understated lookback silently weakens every validation downstream** — the embargo is too short, adjacent folds share information, and CPCV reports a stable edge that is partly the same data seen twice. This is the quietest way to break the validation machinery, because nothing about it looks wrong.

### The adversarial leak test

`tests/lookahead/` replaces everything after a cut with something unrecognisable — closes tripled and then alternately thirded, so every return's magnitude is multiplied by nine and its sign flipped — replays every registered feature, and requires every value at or before the cut to be **byte-identical**. `Decimal` values are digested in their exact positional form, so a `1e-15` difference is a failure; a leak that only moves the fifteenth digit today moves the third digit on a different fold. The probe is parametrised over `FEATURES`, so a feature added later inherits it with no test-file edit.

It runs as its own CI job with **no `paths:` filter**, deliberately: a change to the archive loader or to the store's read predicate can introduce a leak without touching a file under `data/features/`, and a path filter would report success by not running.

The probe has two clauses, because two disjoint failures both count as look-ahead. The first is reading the future. The second is *claiming* to have known something earlier than the venue published it — arithmetically honest, completely trailing, and invisible to any amount of future-poisoning, because the store filters on `available_at_utc` and admits an understated stamp to a decision that could not have seen it. So every emitted point's `available_at_utc` must equal its `event_time_utc` plus the **declared** lag.

Label alignment is part of the probe rather than a separate concern. The label for a decision at bar *i* is entered at the open of bar *i+1* — the price the decision could actually have transacted at — so perturbing `close[i]` alone must not move the label at *i*. Measuring from `close[i]` instead inflates the measured edge by exactly the move the feature was computed from (`fking.data.features.labels`).

`tests/lookahead/test_probe_detects_a_known_leak.py` is the file that makes the rest mean anything. `LEAKY_CASES` carries one deliberately broken definition per known leak shape — a full-sample z-score, an availability stamp that ignores the declared lag, a right-labelled window, and a label entered at the decision bar's own close — and every one of them **must** make the probe raise. It lives under `tests/` and never under `src/`, so nothing in it is reachable by the registry. A leak test that has never been observed to fail is not evidence of anything; it is a test that might be asserting `True == True`. Adding a shape to that tuple is how a newly discovered leak class gets permanently guarded.

`tools/checks/feature_registry.py` closes the last route: it fails the build on a function with the shape of a feature computation that the registry does not carry — an unregistered computation is one the probe never runs — and on any import of a computation module from outside `fking.data`, because a computation called outside `evaluate` derives no availability stamp and reaches no store.

---

## 8. The feature availability contract

The feature store declares what exists. **Strategies cannot request data the system does not have, and the store refuses rather than substituting something adjacent.**

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class AvailabilityDeclaration:                        # fking.data.features.availability
    address: SeriesAddress                            # (market, symbol, dataset)
    resolutions: tuple[str, ...]                      # every interval held; () where the
                                                      # dataset has no cadence
    earliest_event_time_utc: datetime
    latest_event_time_utc: datetime
    known_gaps: tuple[AvailabilityGap, ...]           # each naming its own interval
    refuses_if_unavailable: Literal[True]             # not configurable
```

The `Literal[True]` is deliberate. There is no permissive mode, because the permissive mode is where a strategy gets a forward-filled approximation of depth and does not know it. `mypy --strict` rejects `False` at the call site and `__post_init__` rejects it at runtime — the construction that would carry `False` is the one built from an untyped mapping, a config file or an agent response, which no type checker ever saw.

**Declarations are derived from the ingestion registry, never written by hand.** `AvailabilityContract.snapshot()` reads `data_coverage` and `coverage_gap`, so ingesting a new dataset changes what the contract permits with no code edit. A hand-maintained list of what exists is wrong the first time a backfill runs and wrong in the optimistic direction, because nobody removes a symbol from a list.

It is a **snapshot** rather than a live query. A per-read lookup would make a refusal depend on whether a backfill happened to be mid-partition, so the same backtest would refuse and then not refuse with nothing in the code having changed.

`resolution` is plural here because the registry keys klines by interval and a symbol can hold 1m and 1h series beginning on different days. One declaration per `(market, symbol, dataset)`: `earliest` is the earliest across intervals and `known_gaps` is the union across them, each gap naming which interval has the hole. The union is the conservative direction, and the message is specific enough that the answer is to backfill the hole rather than to widen the check.

The store consults it on **every** read, because the as-of bound is not the only way a window goes wrong. A window opening before the corpus does, or running through a recorded gap, comes back *short* rather than empty — and a short series reads downstream as "no signal in this period" rather than as "no data in this period", which is a strategy scored on a window it never saw.

When the store refuses, it says what does exist. "No L2 depth history exists for free; you have tick trades, futures top-of-book, and ~1-minute aggregated depth bands" is a usable answer that redirects the research. A bare `KeyError` is not.

This matters most for LLM-authored strategies. An agent asked to write a mean-reversion strategy will cheerfully request `order_book_imbalance_top_10_levels` because that feature exists in the literature it was trained on. The refusal is the mechanism that keeps the strategy population inside the data the system actually possesses. Matching is on substrings of the requested name rather than on exact identifiers, because `order_book_imbalance_top_10_levels`, `l2_book_pressure` and `queue_position_estimate` are three spellings of two ceilings.

---

## 8a. Alternative sources, and the lag a publisher chose

Everything above §8 is market data, where a bar is late by the length of its own interval and no more. Funding rates, open interest, an index, news and macro releases are late by something a *publisher* decided, the gap is larger, and **it is visible in the source, which is what makes it easy to shrug off**. `fking.data.alt` is one adapter shape for all of them.

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class AltSourceSpec:                      # fking.data.alt.spec
    source_id: str
    delivery: Delivery                    # ARCHIVE | EGRESS_NOT_PROVISIONED
    availability_lag: timedelta           # strictly positive; no default
    cadence: timedelta
    unit: str                             # declared once per source, never per row
    requires_credential: bool
    revision: Revision                    # FINAL | REVISED
    terms_position: str                   # recorded BEFORE ingestion
    provenance: str                       # where the two durations came from
```

**`availability_lag` must be strictly positive here, unlike on `FeatureSpec`.** Zero is a legal declaration for a value derived only from a bar that has closed. Nothing in this package is such a value: every row is something a third party published *after* the instant it stamps. The constraint is enforced at both ends — the dataclass refuses a non-positive declaration and `alt_observations` carries `CHECK (available_at_utc > event_time_utc)`.

**`available_at_utc` is derived, never supplied.** `AltSourceSpec.point()` is the only construction path for an `AltPoint`, and it computes the instant itself. There is no `available_at_utc` parameter anywhere on the write path, so a writer cannot claim a value was knowable earlier than the declaration says.

**A source that publishes a release calendar overrides the lag with the real instant.** This is the macro case and no fixed offset can express it: Q2 GDP has an observation period ending in June and a release at 08:30 on 26 August. FRED's `release/dates` and BEA's forward-dated schedule give the instant directly, so `AltObservation.published_at_utc` carries it and the declared lag degrades to a floor that refuses a release predating its own observation period. **The release calendar is not metadata about the data; for a macro series it *is* the data this pipeline must ingest**, and a feature keyed on the observation period is look-ahead by weeks.

**A revision is a new row.** `alt_observations` closes its primary key with `available_at_utc`, so a first print and its restatement coexist and `fking_alt_as_of()` returns what was *believed* at the `as_of`. For a macro series, where restatements are routine and sometimes large, backfilling a correction over the original would make every historical backtest a test of a belief nobody held at the time.

**Where a symbol's history starts is probed, not assumed.** BTCUSDT's perpetual listed 2019-09-08; its funding archive begins 2020-01 and its open-interest archive 2020-09-01 (VF-028). The two boundaries differ from each other and neither reaches the listing, so `probe_earliest_archive_date` binary-searches the archive's `.CHECKSUM` siblings per `(source, symbol)` — about seven requests for a monthly series — and a window opening before the result is refused rather than answered short.

Two more traps of the §3 class, both measured on 2026-08-05 (VF-029):

| Dataset | Granularity | Timestamp |
|---|---|---|
| `fundingRate` | **monthly only** — every daily path 404s | `calc_time`, milliseconds, header row |
| `metrics` (open interest) | **daily only** — the monthly path 404s | `create_time` as `2024-01-02 00:00:00`, a **naive datetime string** |

The granularity rule that picks daily-versus-monthly by distance from today is therefore wrong for both, in opposite directions, on every date — so granularity is declared per dataset and every fetch states it. And `ArchiveFormat.epoch_unit` has no member for a datetime string, so `metrics` is fetchable and probeable today and **not parseable**: `PARSED_SOURCES` records that asymmetry rather than hiding it behind a parser that guesses.

Three of the five registered sources are `EGRESS_NOT_PROVISIONED`: their hosts are in no allowlist, and FRED's cannot be added to `ARCHIVE_HOSTS` at all because it is authenticated, which [`docs/adr/0017`](docs/adr/0017-separate-archive-egress-path.md) explicitly names as a refutation of that decision. Their measurements are registered anyway — the measurement is the expensive part, and a declared source that refuses to fetch is honest in a way a stub is not. [`SOURCES.md`](SOURCES.md) §4 carries the full register.

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
- **Coverage report per `(market, symbol, dataset)`** showing first timestamp, last timestamp, gap count and total gapped duration. `backtest` reads this before every run. It is the `data_coverage` view over `ingest_partition` and `coverage_gap`, printed by `make data-coverage`. The join is a `LEFT JOIN`: a series with no gaps must appear with `gap_count = 0`, and an inner join would drop exactly the series a reader hopes to see.
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
