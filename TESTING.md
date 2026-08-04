# Testing

`CLAUDE.md` §5 states the rules. This document is the working detail: what shape the suite takes, what the property tests must actually assert, why two specific things are never mocked, and the five adversarial tests without which this system is not trustworthy.

The framing that matters: **in most systems tests catch bugs that would otherwise crash. Here, the most expensive defects never crash.** Look-ahead bias makes a bad strategy look excellent. Float drift looks like an exchange problem. A non-idempotent consumer produces a position that is wrong by exactly one fill. None of these fail a smoke test. The suite is designed around that.

---

## 1. The pyramid, as it actually applies here

```
                    ┌───────────────────────────┐
                    │  Adversarial (5)          │  slow, hand-built,
                    │  actively try to break    │  highest value per test
                    │  the invariants           │  in the repository
                    ├───────────────────────────┤
                    │  End-to-end (~15)         │  full loop, testnet-shaped,
                    │  signal → order → fill    │  recorded venue
                    │  → audit → score          │
                    ├───────────────────────────┤
                    │  Integration (~150)       │  real Postgres+Timescale,
                    │  module ↔ module,         │  real Redis, recorded exchange
                    │  module ↔ datastore       │
                    ├───────────────────────────┤
                    │  Property (~60)           │  Hypothesis. Risk and position
                    │  invariants over          │  math. Mandatory, not optional.
                    │  generated input          │
                    ├───────────────────────────┤
                    │  Unit (~600)              │  pure functions, fast,
                    │  behaviour of one thing   │  no I/O at all
                    └───────────────────────────┘
```

Two deliberate deviations from the standard pyramid:

**The property tier is disproportionately large for its position.** Sixty property tests is a lot for a codebase this size. It is the correct amount, because position arithmetic has a combinatorial edge-case space — partial close × direction flip × dust quantity × fee asset — that example-based tests cannot cover by enumeration and nobody enumerates correctly by hand anyway.

**The adversarial tier is not defined by scope, it is defined by intent.** These five tests are not "big integration tests". They are tests whose job is to *attempt a violation* and assert that the system refuses. They are listed individually in §8 because each one has a specific named enemy.

### Target runtimes

| Tier | Budget | Runs on |
|---|---|---|
| Unit + property | < 60s total | Every save, every commit |
| Integration | < 5 min | Every push, every PR |
| End-to-end | < 10 min | Every PR |
| Adversarial | < 15 min | Every PR, and nightly on `main` |

If the unit tier exceeds a minute, something in it is doing I/O and belongs a tier up. That is the single most common way a test suite rots into something nobody runs locally.

---

## 2. Test behaviour, not implementation

A test that breaks when you rename a private method is a liability: it makes refactoring expensive, and expensive refactoring means the structure stops being maintained.

```python
# WRONG — asserts implementation
def test_sizing():
    engine = RiskEngine(limits)
    engine._compute_kelly_fraction = Mock(return_value=Decimal("0.02"))
    assert engine._apply_limits.call_count == 1

# RIGHT — asserts behaviour
def test_sizing_respects_per_symbol_notional_limit():
    engine = RiskEngine(limits=RiskLimits(max_notional_usd=Decimal("1000")), clock=FixedClock(T0))
    order = engine.size(signal=high_conviction_long("BTCUSDT"), equity_usd=Decimal("100000"))
    assert order.notional_usd <= Decimal("1000")
```

Corollary that is easy to miss: **a test that mocks a collaborator inside the module under test is an implementation test wearing an integration test's clothes.** Mock at the process boundary (the exchange, the LLM provider), not between two of our own classes.

---

## 3. Property-based testing with Hypothesis

Mandatory for all risk and position math. `CLAUDE.md` §5.

Example-based tests confirm the cases you thought of. Position arithmetic fails on the cases you did not.

### 3.1 The generator setup that everyone gets wrong first

```python
from decimal import Decimal
from hypothesis import given, settings, strategies as st

# WRONG — st.decimals() generates NaN and Infinity by default.
#         Your first failure will be Decimal('NaN'), which is a red herring
#         about your generator, not a finding about your code.
prices = st.decimals(min_value=1, max_value=100_000)

# RIGHT
prices = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("1000000"),
    places=2,                    # instrument tick precision
    allow_nan=False,
    allow_infinity=False,
)
quantities = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("10000"),
    places=8,                    # instrument lot precision
    allow_nan=False,
    allow_infinity=False,
)
```

Note `min_value=Decimal("0")` on quantities, not `Decimal("0.00000001")`. Zero and sub-lot dust are exactly the inputs you need generated; excluding them because they "aren't realistic" removes the cases the properties exist to catch.

### 3.2 The properties that must hold

These are not suggestions. Each corresponds to a class of bug this kind of code produces.

**Partial closes preserve cost basis.**
```python
@given(entry=prices, qty=quantities.filter(lambda q: q > 0), frac=st.decimals(min_value=0, max_value=1, places=4))
def test_partial_close_preserves_avg_entry(entry, qty, frac):
    p = Position.open(base_quantity=qty, avg_entry=entry, side=Side.LONG)
    closed = p.with_fill(Fill(quantity=-(qty * frac).quantize(LOT), price=entry * 2))
    if closed.base_quantity > 0:
        assert closed.avg_entry == entry     # closing part of a position does not move the entry
```
The bug this catches: recomputing the weighted average on a closing fill instead of only on an opening one. It inflates avg entry on every partial close, so realized PnL looks better than it is — and it looks *plausibly* better, which is worse.

**Direction flips pass through flat.**
```python
@given(qty=quantities.filter(lambda q: q > 0), over=quantities.filter(lambda q: q > 0))
def test_flip_passes_through_flat(qty, over):
    p = Position.open(base_quantity=qty, avg_entry=Decimal("100"), side=Side.LONG)
    states = p.apply_with_trace(Fill(quantity=-(qty + over), price=Decimal("110")))
    assert any(s.base_quantity == 0 for s in states)          # flat state exists
    assert states[-1].side is Side.SHORT
    assert states[-1].avg_entry == Decimal("110")             # new position, new basis
```
The bug: a sell larger than the long is applied as one arithmetic step, so the resulting short inherits the long's average entry and its unrealized PnL is nonsense from birth. The realized PnL from the closed long is never booked.

**Zero is exactly zero.**
```python
@given(qty=quantities.filter(lambda q: q > 0))
def test_full_close_is_exactly_zero(qty):
    p = Position.open(base_quantity=qty, avg_entry=Decimal("100"), side=Side.LONG)
    closed = p.with_fill(Fill(quantity=-qty, price=Decimal("100")))
    assert closed.base_quantity == Decimal("0")
    assert closed.base_quantity.as_tuple().exponent >= -LOT_PLACES   # not 0E-27
    assert closed.is_flat
```
`Decimal("1E-18")` is truthy, is not equal to `Decimal("0")` under `is_flat` implementations that compare exponents, and will cause the system to attempt to close a position that does not exist — which the exchange rejects, which trips error handling, at 3am.

**Dust quantities are refused, not rounded to zero silently.**
```python
@given(dust=st.decimals(min_value=Decimal("1E-12"), max_value=Decimal("1E-9"), places=12, allow_nan=False, allow_infinity=False))
def test_sub_lot_quantity_is_rejected_not_truncated(dust):
    with pytest.raises(SubLotQuantityError):
        Order.market(symbol=BTCUSDT, base_quantity=dust, side=Side.BUY)
```
Silently truncating a sub-lot quantity to zero produces an order for nothing, which the exchange rejects, which the retry logic re-sends. The failure mode is a retry loop against a rate-limited API.

**Idempotency: applying a fill twice equals applying it once.**
```python
@given(qty=quantities.filter(lambda q: q > 0), price=prices)
def test_fill_application_is_idempotent(qty, price):
    fill = Fill(fill_id=FillId("f-1"), quantity=qty, price=price)
    once = Position.flat(BTCUSDT).with_fill(fill)
    twice = once.with_fill(fill)
    assert twice == once
```
Redis Streams delivers at least once (`CLAUDE.md` §2). This is a requirement, not luck, and it must be asserted rather than assumed.

**Exposure never exceeds the configured limit, for any sequence of valid signals.**
```python
@given(signals=st.lists(valid_signals, min_size=1, max_size=50))
@settings(max_examples=500)
def test_no_signal_sequence_breaches_exposure_limit(signals):
    engine = RiskEngine(limits=LIMITS, clock=FixedClock(T0))
    book = Portfolio.empty()
    for s in signals:
        order = engine.size(s, book)
        if order is not None:
            book = book.with_order(order)
        assert book.gross_notional_usd <= LIMITS.max_gross_notional_usd
```
This is the highest-value property in the file. It asserts the risk engine's contract against sequences no human would write.

**Every `Decimal` result is quantized to instrument precision.**
```python
@given(...)
def test_all_outputs_are_tick_or_lot_aligned(...):
    assert order.limit_price == order.limit_price.quantize(symbol.price_tick)
    assert order.base_quantity == order.base_quantity.quantize(symbol.lot_size)
```
Unquantized values are rejected by the exchange with an error that names the wrong thing.

**PnL decomposition is invariant.** Realized + unrealized over any partition of the same fill sequence equals the total over the whole sequence. This catches double-booking, which is the bug that makes the survival score wrong without making anything crash.

### 3.3 Hypothesis operational rules

**Commit and cache the example database.** `.hypothesis/examples/` holds shrunk counterexamples. In CI it must be restored and saved via the cache action.

This is the non-obvious one. Without it, a rare counterexample found once in CI is discarded at the end of the job and the next run — with a different random seed — passes. You will see a single red build, re-run it, get green, and conclude it was flaky. It was not flaky; it found a real bug and then threw the evidence away. Cache key on the Python version and the test file hashes.

**`@settings(deadline=None)` for anything touching Postgres.** Hypothesis's default 200ms per-example deadline is measuring your test's I/O, not your code, and will flake on a cold container.

**`derandomize=True` in CI, `False` locally.** CI must be reproducible from the commit; local runs should explore. The `--hypothesis-seed` flag pins a specific run when reproducing a CI failure.

**Shrink the counterexample before you fix.** Hypothesis prints the minimal failing case. Fix against that, then add it as an explicit `@example()` so it is checked forever regardless of the random walk.

---

## 4. The database is never mocked

Use the real PostgreSQL 16 + TimescaleDB via `testcontainers`.

A mocked database proves the mock works. The bugs in this layer are not in our SQL strings — they are in:

- **Constraints.** A `CHECK (base_quantity >= 0)` that we believe exists and does not.
- **The append-only triggers on audit tables.** These are the entire enforcement mechanism for `CLAUDE.md` §2's audit rule. A mock cannot fire a trigger, so a mocked test suite gives 100% coverage on a system whose central integrity property is untested.
- **Transaction boundaries.** Whether a fill and its audit row commit atomically. A mock has no transactions.
- **Timescale-specific behaviour.** Hypertable chunk boundaries, `time_bucket` alignment, and what a continuous aggregate returns for a partially-filled bucket. All of that is real behaviour that a mock invents.
- **Type round-trips.** `NUMERIC` → `Decimal` preserves precision; `DOUBLE PRECISION` → `float` does not, and the column type is exactly the kind of thing that gets chosen wrong in a migration.

### 4.1 Container and isolation strategy

One container per test **session**, not per test — container startup dominates everything else.

Migrations run once, into a template database:

```python
@pytest.fixture(scope="session")
def pg_template(pg_container: PostgresContainer) -> str:
    run_migrations(pg_container.url, target="head")
    return "fking_template"

@pytest.fixture
def db(pg_container: PostgresContainer, pg_template: str) -> Iterator[Engine]:
    name = f"t_{uuid4().hex[:12]}"
    _createdb(pg_container, name, template=pg_template)   # ~30ms
    yield create_engine(pg_container.url_for(name))
    _dropdb(pg_container, name)
```

`CREATE DATABASE ... TEMPLATE` is far faster than re-running the migration chain, and gives true isolation — including of sequences and of `alembic_version`.

**Why not the faster savepoint-rollback pattern?** Because code that calls `commit()` explicitly escapes it, and worse, a rollback-per-test suite cannot test anything about commit behaviour — which for us includes the append-only triggers' interaction with transaction boundaries. The template approach costs ~30ms per test and tests the real thing.

**Never `TRUNCATE` a hypertable for cleanup.** It leaves chunk metadata and compression policies in a state that makes the next test's inserts behave differently from a fresh table. Drop the database.

---

## 5. Exchange fixtures must be recorded, never written

Do mock the exchange. Mock it **against recorded real responses**.

Hand-written fixtures encode what you assume the API returns. The tests then pass while production fails, and the failure is in exactly the assumption the fixture was built to express — so reading the test tells you nothing.

### 5.1 What "recorded" means precisely

Store the **raw response bytes**, plus status code and headers. Not a parsed dict.

```
tests/fixtures/recorded/
  binance/
    futures/
      fetch_order__filled.json          # {"status": 200, "headers": {...}, "body": "<raw bytes, base64>"}
      fetch_order__partially_filled.json
      create_order__err_-2019_margin_insufficient.json
      create_order__err_-1021_recv_window.json
      fetch_balance__after_testnet_wipe.json
    spot/
      ws_session_logon__ed25519_ok.json
```

Raw bytes matter because **the parse is the thing under test**. A stored dict has already had `json.loads` applied, so it cannot exercise the microsecond/millisecond timestamp discrimination, the `NaN` literal rejection (`CODING_STANDARDS.md` §1.3), or a number that arrived as a string in one field and a JSON number in another.

Headers matter because `ccxt`'s behaviour depends on them — `Content-Type` selects the parse path, and the `X-MBX-USED-WEIGHT-1M` header drives our rate limiter's state.

### 5.2 Record the errors deliberately

This is the part that gets skipped, and it is the part that matters.

A happy-path recording session captures none of the responses that cause incidents. The ones you need have to be **provoked**:

| Fixture | How to provoke it |
|---|---|
| `-2019` margin insufficient | Submit an order larger than testnet balance |
| `-1021` timestamp outside recvWindow | Skew the local clock by 10s and submit |
| `-1013` filter failure (LOT_SIZE / MIN_NOTIONAL) | Submit a sub-lot quantity, and a valid quantity below min notional |
| `-2011` unknown order cancel | Cancel an order id that was already filled |
| 429 / 418 rate limit | Burst `fetch_open_orders` past the weight limit |
| Empty balance after wipe | Capture the day the spot testnet wipes; otherwise simulate by using a fresh key |
| WS disconnect mid-stream | Kill the connection during an active order |

Each recorded error becomes a test asserting the parser produces the right typed exception (`CODING_STANDARDS.md` §6.2) rather than a `KeyError` three frames later.

### 5.3 Scrub on record, mechanically

The recorder strips `X-MBX-APIKEY`, the `signature` query parameter, `listenKey` values, and any Ed25519 key material before writing. This is a function in the recorder, not a manual step — a manual scrub is a scrub that gets forgotten once, and once is enough since git history is permanent.

A `pre-commit` hook greps `tests/fixtures/recorded/` for key-shaped strings and blocks the commit.

### 5.4 Archive fixtures are a second corpus, with a digest gate

Historical archive fixtures live in `tests/fixtures/archives/`, separately from the exchange-API corpus above, because they are a different kind of evidence: no credential is involved, the files are public, and the parse under test is a CSV parse rather than a JSON one.

```
tests/fixtures/archives/
  spot/klines/BTCUSDT/1m/
    BTCUSDT-1m-2025-01-02.zip                       # a whole verified archive, 72 KB
    BTCUSDT-1m-2025-01-02.zip.provenance.json
    BTCUSDT-1m-2025-01-02.head32.csv                # a byte-exact prefix of the member
    BTCUSDT-1m-2025-01-02.head32.csv.provenance.json
    BTCUSDT-1m-2024-12-31.head32.csv                # pre-cutover: milliseconds
  futures_um/klines/BTCUSDT/1m/                     # same date, still milliseconds, has a header
  spot/trades/BTCUSDT/                              # True/False booleans
```

Four properties make this corpus trustworthy, and each one closes a specific way a fixture corpus rots:

- **Recorded through the production fetcher.** `tools/record_archive_fragment.py` downloads via `GuardedArchiveEgress` and `ArchiveFetcher`, so every byte kept was checksum-verified against the archive's `.CHECKSUM` sibling before it was written. A fixture recorded from a truncated transfer would teach a parser that a short day is normal, which is indistinguishable from a real short day.
- **A provenance sidecar per file**, carrying the source URL, the archive digest, the member digest, and the member's line count. A fixture whose upstream cannot be named is a fixture nobody can re-record.
- **A digest gate.** `tests/data/test_archive_fixture_integrity.py` recomputes every digest, and additionally asserts the corpus still spans both epoch units, both header conventions, and still contains a Python-style boolean. If a fixture is ever "cleaned up", the traps in `DATA_PIPELINE.md` §3 lose their test data and every parser assertion becomes decoration — so the corpus's *coverage* is asserted, not just its integrity.
- **A mutation is either performed in the test or derived on disk with its recipe beside it — never authored.** Most trap-triggering inputs — a lowercase `true`, a dropped column, a header that reordered — are produced by transforming recorded bytes inside the test that asserts on them, because the transformation is then visible in the same diff as the assertion. The exception is §5.5's corrupted corpus, and its condition is exact: the file is committed only when a *generator* can re-derive it byte for byte from a pristine recording, and CI re-derives all of them. What is never acceptable in either place is a hand-authored file, because a hand-authored corrupt fixture looks recorded and six months later nobody remembers which of the two it is.

Whole archives are committed only where they are small (a daily 1m kline zip is ~70 KB) and only where a fragment cannot prove the assertion: 1,440 bars in a day, and a first bar at exactly 00:00 UTC, are the two facts a 32-row prefix cannot establish. Trades archives are 30 MB and stay fragments.

### 5.5 The corrupted corpus is derived, and CI re-derives it

`tests/fixtures/corrupt/` holds one archive per data-quality gate that a single file can trip (`DATA_PIPELINE.md` §10). Each is a `.zip`, because a `.zip` is what ingestion receives — gate 1 hashes the archive and gate 2 reads the member out of it, so a corpus of loose CSVs could exercise neither.

```
tests/fixtures/corrupt/
  spot_klines_truncated_archive.zip                 # gate 1
  spot_klines_truncated_archive.zip.corruption.json
  spot_klines_header_prepended.zip                  # gate 2, the direction that eats 00:00
  futures_klines_header_stripped.zip                # gate 2, the direction that files 1970
  spot_klines_first_epoch_zeroed.zip                # gate 3
  spot_klines_rows_out_of_order.zip                 # gate 4
  spot_trades_booleans_lowercased.zip               # gate 5
  spot_klines_high_below_close.zip                  # gate 6
  spot_klines_negative_volume.zip                   # gate 7
  spot_klines_gapped_bar_block.zip                  # gate 8 — reported, never refused
  spot_klines_08_log_return.zip                     # gate 9 — flagged, never refused
```

Why this corpus is committed rather than mutated per test, when §5.4's rule says the opposite: these mutations are not assertions about one parser call. They are assertions about **ordering** — that a gate refuses *before* a write, and that the file it would have written does not exist. That needs a whole archive travelling the real ingestion path, and it needs the same bytes to be reachable from a CI job, from `make check` and from a developer reproducing a failure. A transformation performed inside one test is none of those things.

What makes it safe is that nothing there is authored. `tools/corrupt_archive_fixture.py` declares each mutation as a named pure function of a pristine recording's bytes, writes a `.corruption.json` sidecar carrying the source recording, the mutation's name, the rationale, the gate, and both digests — and `tests/data/test_corrupt_fixture_integrity.py` re-runs every derivation and requires byte equality. An edit to a corrupt fixture fails, and so does an edit to *both* the fixture and its sidecar, because the derivation is what is checked rather than the record of it. The zip is built with a fixed member timestamp and stored rather than deflated, so the derivation is reproducible across machines and Python versions.

Two fixtures in that list are not corruptions at all in the refusal sense, and they are the most useful ones: `spot_klines_gapped_bar_block` and `spot_klines_08_log_return` must be **ingested successfully**, with the gap recorded and the move flagged. A corpus of only-refusals passes just as well against a pipeline that refuses everything.

### 5.6 Also assert on malformed input

For every recorded endpoint, one test mutates the payload — drops a field, changes a type, inserts a `NaN` — and asserts the parser raises `ExchangeProtocolError`. Exchange responses are hostile input and must never be indexed into optimistically.

---

## 6. Coverage floors

| Module | Floor | Metric |
|---|---|---|
| `platform/safety` | 100% | Branch, **plus mutation score ≥ 90%** |
| `risk` | 95% | Branch |
| `domain` | 95% | Branch |
| `execution` | 90% | Branch |
| Everything else | 80% | Line |

Per-module floors exist because a single global number lets well-tested utilities subsidize untested risk logic. A repository at 88% overall can have a risk engine at 40%.

**Branch coverage, not line coverage**, for the top four. `--cov-branch`. A line-covered `if` whose false arm never runs is exactly the untested rejection path — and in `risk/` the rejection paths are the product.

### 6.1 Why the safety kernel needs mutation testing on top of 100%

100% coverage of `platform/safety` is achievable by a test that calls `guarded_client()` with an allowlisted host and asserts it returns something. Every line executes. Nothing about the *rejection* is tested.

Coverage measures execution, not assertion. For the one module where a silent failure means trading real money, that gap is unacceptable, so the safety module additionally runs under `mutmut`:

```bash
make test-mutation MODULE=platform/safety
```

If mutating `if host not in ALLOWED_HOSTS` to `if host in ALLOWED_HOSTS` does not fail a test, the test suite does not check the thing it exists to check. Mutation score ≥ 90% is a required check on any PR touching that path — which is to say, on every `safety:critical` PR (`GIT_WORKFLOW.md` §5).

Mutation testing is slow. It runs on that one module only, and that is the right trade.

### 6.2 Reading the coverage report honestly

```bash
make test ARGS="--cov=src/fking/risk --cov-branch --cov-report=term-missing"
```

The uncovered lines that matter are rarely the ones listed first. They are the branches that only fire under partial fills, redelivery, reconnects, and rejected orders. If a report shows 95% and the missing 5% is entirely error handling, the module is not at 95% in any meaningful sense.

---

## 7. Determinism and seeding

Every test is deterministic. A flaky test in a trading system trains you to ignore failures, and one of those failures will be real.

**The four sources of nondeterminism and their fixes:**

| Source | Fix |
|---|---|
| Clock | Inject `FixedClock`. Never assert against `datetime.now()` |
| Randomness | Inject `np.random.default_rng(seed)`. Never `random.seed()` / `np.random.seed()` — global seeds are shared and any library can reset them |
| Dict/set ordering | `PYTHONHASHSEED=0` in CI, and never assert on set iteration order |
| Concurrency | Tests that exercise the bus assert on final state, never on interleaving |

Detect flakiness rather than waiting to be surprised by it:

```bash
make test ARGS="-p no:randomly --count=5"        # same order, five times
make test ARGS="-p randomly --count=5"           # shuffled order, five times
```

The second catches inter-test state leakage — a test that only passes when it runs after another. The most common cause here is a module-level singleton (`CODING_STANDARDS.md` §11.1).

**If a test is flaky, it has an unmodelled dependency. Find it.** Do not add a retry, a `sleep`, or a `@flaky` decorator. A retry on a test of position arithmetic is an admission that position arithmetic is nondeterministic, which is a far larger problem than the test.

Nightly, `main` runs the full suite with `--hypothesis-seed=random` and `-p randomly`. That job is allowed to find things the PR job does not; when it does, the counterexample is added as an explicit `@example()`.

---

## 8. The five adversarial tests

These are the tests without which nothing else in the suite means very much. Each one attempts a violation and asserts the system refuses. Most live in `tests/adversarial/`; the look-ahead probe below has its own directory and its own required CI job, for the reason stated there. All are named so that a failure is self-explaining.

### 8.1 Look-ahead leak — `tests/lookahead/`

**The enemy**: look-ahead bias. The most dangerous defect class in the project, because it does not fail — it makes bad strategies look excellent (`ARCHITECTURE.md` §6).

It gets its own directory and its own required CI job rather than a file under `tests/adversarial/`, because it is not a test of a module — it is the test of the guarantee that makes every other result in the repository worth reading, and a failure buried among two thousand unit tests is a failure somebody re-runs rather than reads.

**The test**: replace everything after a cut with something unrecognisable — closes tripled and then alternately thirded, so every return's magnitude is multiplied by nine and its sign flipped — replay every registered feature, and require every value at or before the cut to be **byte-identical**. The poison is gross on purpose: a small perturbation can be absorbed by rounding and produce a *false pass*, which is the worst available outcome for this particular test. `Decimal` values are digested in their exact positional form, so a `1e-15` difference fails — a leak that only moves the fifteenth digit today moves the third digit on a different fold.

```python
@pytest.mark.parametrize("feature_key", sorted(FEATURES), ids=str)
def test_no_registered_feature_reads_the_future(feature_key: tuple[str, int]) -> None:
    probe_feature(FEATURES[feature_key], bars(_CLOSES))
```

Parametrised over the **feature registry**, so a newly added feature is automatically covered, and `tools/checks/feature_registry.py` fails the build on a computation the registry does not carry. That coupling is the point.

It catches the real culprits: centred and right-labelled rolling windows, `shift(-n)`, normalization statistics computed over the full range, and joins that forward-fill from the future. It also carries a second clause the poisoning cannot reach — every point's `available_at_utc` must equal its `event_time_utc` plus the *declared* lag, because a value that is perfectly trailing and merely claims to have been knowable too early is admitted by the store's filter to a decision that could not have seen it.

**The half that makes the rest mean anything**: `tests/lookahead/test_probe_detects_a_known_leak.py` carries one deliberately broken definition per known leak shape and requires the probe to raise for every one. A leak test that has never been observed to fail is not evidence of anything; it might be asserting `True == True`.

**Also required**: the same test against the *cache*, once one exists. A cached feature keyed on symbol but not on as-of time is a look-ahead bug wearing a performance costume, and it is the single most likely way this invariant gets broken in future (`PERFORMANCE_GUIDE.md` §5).

### 8.2 Safety kernel rejects mainnet — `test_guarded_client_rejects_production_hosts`

**The enemy**: the prime directive failing quietly.

**The test**, and every one of these clauses exists because it is a distinct way through:

```python
PRODUCTION_HOSTS = [
    "api.binance.com", "fapi.binance.com", "dapi.binance.com",
    "stream.binance.com", "fstream.binance.com",
    "api.bybit.com", "stream.bybit.com",
]

@pytest.mark.parametrize("host", PRODUCTION_HOSTS)
def test_rejected_at_construction(host): ...

@pytest.mark.parametrize("host", PRODUCTION_HOSTS)
def test_rejected_when_base_url_overridden_per_call(host):
    client = guarded_client(base_url=TESTNET)          # constructed legitimately
    with pytest.raises(ForbiddenHostError):
        client.get("/api/v3/order", base_url=f"https://{host}")   # overridden per request

def test_rejected_on_redirect_to_production():
    # A 302 to a production host must not be followed.
    ...

def test_rejected_when_env_var_attempts_override():
    monkeypatch.setenv("FKING_ALLOWED_HOSTS", "api.binance.com")
    assert "api.binance.com" not in allowed_hosts()     # the allowlist is compiled in, not read

def test_allowlist_is_frozen_and_unpatchable():
    with pytest.raises((AttributeError, TypeError)):
        fking.platform.safety.ALLOWED_HOSTS.add("api.binance.com")

def test_startup_aborts_when_configured_endpoint_is_not_allowlisted(): ...
```

The per-call override case is the one that actually matters. Validating only at construction is the obvious implementation and it is wrong, because base URLs can be overridden per request — this is why `ARCHITECTURE.md` §8 specifies validation on *every request*.

100% coverage plus the mutation gate (§6.1).

### 8.3 Audit table immutability — `test_audit_tables_reject_update_and_delete`

**The enemy**: an audit log the application can rewrite, which is not an audit log.

**The test**: against the real database, for every table in the audit set, attempt `UPDATE` and `DELETE` as the application role and assert both raise. Then attempt them as the migration role and assert the same.

```python
@pytest.mark.parametrize("table", AUDIT_TABLES)
def test_update_rejected(db, table):
    with pytest.raises(ProgrammingError, match="append-only"):
        db.execute(text(f"UPDATE {table} SET correlation_id = 'x'"))   # noqa: S608 — test fixture

@pytest.mark.parametrize("table", AUDIT_TABLES)
def test_delete_rejected(db, table): ...

def test_audit_table_set_matches_schema(db):
    # Any table whose name matches the audit convention must be in AUDIT_TABLES
    # and must carry the rejecting trigger. A new audit table added without
    # protection fails here rather than silently being mutable.
    assert discovered_audit_tables(db) == set(AUDIT_TABLES)
```

The third test is the important one. Without it, this suite protects the tables that existed when it was written, and a new audit table added six months later is unprotected while the suite stays green.

The enforcement must be a rejecting trigger (or a revoked grant) at the database level, not application logic — `CLAUDE.md` §2 says "enforced by the database" precisely because application-level enforcement is one refactor away from being bypassed.

### 8.4 Duplicate event delivery — `test_pipeline_is_idempotent_under_redelivery`

**The enemy**: Redis Streams' at-least-once delivery producing double-applied fills.

**The test**: run a full signal → order → fill → position sequence through the real bus. Then replay **every** event a second time, in a shuffled order, and assert the terminal state is byte-identical.

```python
def test_full_pipeline_idempotent_under_shuffled_redelivery(bus, db):
    events = run_scenario(SCENARIO_PARTIAL_FILLS_THEN_FLIP)
    state_once = snapshot(db)

    for event in shuffled(events, seed=17):
        bus.deliver(event)                     # every event again, out of order

    assert snapshot(db) == state_once
    assert audit_row_count(db) == len(events)  # redeliveries do NOT append duplicate audit rows
```

Shuffled, not in order. In-order redelivery is the easy case and the one a naive "last seen id" dedupe handles. Out-of-order redelivery after a consumer group rebalance is the real case, and it is what breaks dedupe schemes that assume monotonic ids.

The audit row assertion catches the other half: a consumer that correctly refuses to double-apply a fill but still writes a second audit row has made the audit log lie about what happened.

### 8.5 Testnet wipe recovery — `test_rebuilds_full_state_from_exchange_after_wipe`

**The enemy**: Binance spot testnet wipes roughly every 30 days without notice — keys survive, balances and open orders vanish (`ARCHITECTURE.md` §7). Local state is then fiction, and trading against a phantom book is the outcome.

**The test**: populate local state with positions, open orders and balances. Swap the venue to a recorded post-wipe response set (empty balances, empty open orders, valid credentials). Run `reconcile --full`. Assert:

```python
def test_reconcile_after_wipe(db, wiped_venue):
    seed_local_state(db, positions=3, open_orders=5)

    result = Reconciler(venue=wiped_venue, clock=FixedClock(T0)).reconcile_full()

    assert local_positions(db) == []            # exchange is the source of truth
    assert local_open_orders(db) == []
    assert result.discrepancies == 8            # every divergence is reported, not silently fixed
    assert audit_contains(db, ReconciliationDivergence) == 8
    assert kill_switch_state(db) is KillSwitch.TRIPPED   # a wipe is not a routine reconciliation
```

Two non-obvious assertions:

**The divergences are individually audited.** Reconciliation that silently converges is indistinguishable from reconciliation that silently loses a real position. The audit rows are how you tell, afterwards, whether the divergence was a wipe or a bug.

**The kill switch trips.** A full-book divergence is not a routine correction. It requires a human to confirm the cause before the system resumes placing orders — otherwise a genuine position-tracking bug is auto-healed into invisibility on every cycle.

The inverse direction matters too: a variant of this test gives the exchange *more* positions than local state has, asserting the system adopts them rather than ignoring them. An orphan position on the exchange that we do not know about is unmanaged risk.

---

## 9. Running the suite

```bash
make test                                                    # everything
make test ARGS="tests/unit -x -q"                            # fast loop
make test ARGS="tests/adversarial -v"                        # the five
make test ARGS="--cov=src/fking/risk --cov-branch --cov-report=term-missing"
make test ARGS="--hypothesis-seed=8213371"                   # reproduce a CI counterexample
make test-mutation MODULE=platform/safety
make check                                                   # lint + format + types + imports + tests
```

`make check` must be green before a PR, and you must have run it. `CLAUDE.md` §7.

---

## 10. Writing a new test: the order of questions

1. **What defect would this catch, and would that defect crash?** If it would not crash, this test is more valuable than it looks and belongs higher in the priority list.
2. **Is it position or risk arithmetic?** Then a Hypothesis property is mandatory, and the example test is a supplement, not a substitute.
3. **Does it touch the database?** Real Postgres. No exceptions.
4. **Does it touch the exchange?** Recorded response. Record it if it does not exist; do not write it.
5. **Is anything mocked between two of our own classes?** Then it is an implementation test — remove the mock or move the test.
6. **Is it deterministic?** Clock injected, seed injected, no set-order assertions.
7. **Does it assert behaviour, or that the code ran?** `assert result is not None` is the latter.
