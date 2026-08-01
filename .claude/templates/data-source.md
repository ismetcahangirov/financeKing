# Template — Data Source Onboarding

Copy this file to `docs/data-sources/<kebab-slug>.md`, one per source. Example: `docs/data-sources/binance-vision-futures-klines.md`.

A data source is onboarded when this document is complete and its verification commands have been run, not when the first file downloads. The traps section is the reason: every ingestion defect this project has hit produced plausible numbers rather than an error, and plausible numbers propagate into features, backtests, and eventually into a strategy that looks excellent for reasons unrelated to the market. The cost of finding one of these late is a re-run of everything computed since.

Related: `DATA_PIPELINE.md`, `../rules/no-lookahead.md`, `../rules/time-and-timezones.md`, `../contexts/binance-testnet.md`, `research-note.md`.

---

```yaml
---
source_name: <the source as it should be referred to everywhere else>
url: <base url or archive root>
auth_required: <none | api-key | oauth | signed-request>
cost: <free | usd per month | per-request pricing>
licence: <licence name and what it permits — redistribution, derived works, commercial use>
first_verified: <yyyy-mm-dd>
verifier: <human username or agent name who ran the verification commands>
reverify_cadence: <e.g. monthly>
status: <candidate | verified | in-production | deprecated>
---
```

---

## 1. What it provides and at what granularity

*What this source is actually for, and the finest granularity it genuinely offers. Distinguish what the documentation claims from what the files contain — those differ often enough that assuming they match is how a project ends up with a feature it cannot compute. Name the specific fields, not the category.*

| Dataset | Fields | Granularity | Format |
|---|---|---|---|
| `<name>` | `<field, field, field>` | `<1m bars / trade-level / 8h>` | `<csv.zip / parquet / json>` |

```
Documentation claims: <what the docs say>
Files actually contain: <what you observed>
```

> Example: `bookDepth` is documented as order book data. It is not snapshots — it is aggregated depth bands sampled roughly once per minute, which means queue position and resting-liquidity dynamics are not derivable from it at any granularity.

---

## 2. Coverage

*Symbols, earliest date per symbol, and known gaps. Coverage is per-symbol and per-dataset, never global: a source that starts in 2017 for BTCUSDT and 2023 for a mid-cap will silently give any cross-sectional feature a 2023 start date, and the feature store must know that before a strategy inherits it.*

| Symbol | Dataset | Earliest date | Latest date | Known gaps |
|---|---|---|---|---|
| `<symbol>` | `<dataset>` | `<yyyy-mm-dd>` | `<yyyy-mm-dd or rolling>` | `<ranges, with the cause if known>` |

```
Shortest history across the intended universe: <yyyy-mm-dd> — <the symbol that sets it>
Gaps that coincide with market events: <list them; a gap at a known outage is different
                                        from a gap with no explanation>
Command used to enumerate coverage: <command>
```

---

## 3. Cost and rate limits

*What it costs in money and in requests. Rate limits are an architectural constraint on the ingestion schedule, not an operational detail, so state the limit, the window, the penalty for exceeding it, and how the ingester stays under it.*

```
Monetary cost:      <free | amount and billing model>
Rate limit:         <requests per window, and whether weighted>
Penalty on breach:  <429 with backoff | temporary ban of <duration> | account action>
Ingester budget:    <requests per run, and the headroom left>
Backfill duration:  <how long a full historical backfill takes under the limit>
```

---

## 4. Authentication and credentials

*Whether credentials are needed, what scope they need, and where they live. Read-only scope wherever read-only will do. **Credentials require asking the user** — an agent does not create an account, does not sign up for an API key, and does not source a key from elsewhere in the environment. If this source needs one, that is a blocking question, not a task step.*

```
Auth mechanism:     <none | api-key header | HMAC-signed request | Ed25519 | OAuth>
Scope required:     <the minimum — read-only where possible>
Stored in:          <the secret name and store, never the value>
Rotation:           <cadence and procedure>
```

```
If credentials are not already present: stop and ask the user. Record here what was asked
and when it was granted: <request and date, or "no credentials required">
```

---

## 5. Integrity

*How you know a file is the file it claims to be. Checksum-verify every archive before trusting it; a truncated download produces a shorter series rather than an error, and a shorter series is indistinguishable from a market gap once it is in the feature store.*

```
Checksums published:  <yes, at <url pattern> | no>
Algorithm:            <sha256 | md5>
Verification command: <command>
Expected output:      <shape of a passing result>
On mismatch:          <the action — refuse the file, do not repair it, do not proceed>
Coverage of checks:   <every file | sampled, with the sampling rate and why>
```

---

## 6. The traps

*Every property of this source that produces plausible wrong values rather than an error. Fill every row: `none observed` is a finding, an empty cell is an omission. The three verified traps in this project's history are listed as rows because they were each found after the data was already in use.*

| Trap | What to check | Observed on this source | Handling |
|---|---|---|---|
| Timestamp unit | Whether it is s, ms or us, **and whether it changes by date or by market** | `<observed — state it per (market, date) if it varies>` | `<normalization keyed on (market, date), never a global constant>` |
| Header rows | Whether the CSV has one, per dataset | `<observed — this project has one dataset with and one without>` | `<handling>` |
| Boolean serialization | `true`/`false` versus Python-style `True`/`False` versus `0`/`1` | `<observed>` | `<handling>` |
| Decimal separators | `.` versus `,`, and thousands separators | `<observed>` | `<handling>` |
| Symbol naming | Venue symbol versus canonical, delistings, renames, perp versus spot collisions | `<observed>` | `<mapping table location>` |
| Unicode safety | Non-ASCII in symbol names or metadata fields, encoding declared versus actual | `<observed>` | `<handling>` |
| Numeric type | Whether prices arrive as strings or floats in the raw file | `<observed>` | `<parsed to `Decimal` from `str`, never through `float`>` |
| Row ordering | Whether rows are guaranteed time-ordered within a file | `<observed>` | `<handling>` |
| Duplicate rows | Exact duplicates, and same-timestamp different-value rows | `<observed>` | `<handling>` |

```
Raw bytes inspected: <paste the first two and last two raw lines of one file per dataset —
                      not the parsed output, the bytes>
```

> Example row, from this project's history: spot archives switched to microsecond timestamps on 2025-01-01 while futures stayed in milliseconds. A global unit constant parses one market into 1970 and raises nothing.

---

## 7. Point-in-time semantics

*The question that decides whether this source can back a feature at all: is `available_at` recoverable, or only `event_time`? A source that gives you only the time an event happened, with no way to know when you could first have observed it, cannot support a point-in-time feature without an assumption — and that assumption is a look-ahead defect that does not fail, it makes bad strategies look excellent.*

```
event_time available:      <yes — the field is <name>>
available_at recoverable:  <yes, from <field or publication schedule> | no>
If no, the assumption required: <the lag you would have to assume, and its justification>
Revisions:                 <does this source revise past values? if yes, are revisions
                            timestamped, and is the original value retrievable?>
Publication lag:           <measured, not documented — how you measured it>
```

```
Consequence for the feature store: <what this source may back — a point-in-time feature, a
                                    lagged feature with a declared lag, or research-only use
                                    that never reaches a live strategy>
```

---

## 8. Normalization plan

*Keyed on `(market, date)`, never on a global constant, because that is exactly the assumption the timestamp trap breaks. Name the module, the table, and what happens to a pair that is not in the table — failing closed on an unknown pair is the property that makes this table trustworthy.*

```python
# src/fking/data/<module>.py
UNIT_TABLE: dict[tuple[Market, DateRange], TimestampUnit] = {
    (<market>, <date range>): <unit>,
    (<market>, <date range>): <unit>,
}
```

```
Unknown (market, date) pair: <raises <ExceptionName> — never defaults, never guesses>
Canonical symbol mapping:    <where the mapping lives>
Output schema:               <the normalized type this produces>
Storage target:              <TimescaleDB hypertable / partitioned Parquet path>
Idempotency:                 <the key that makes a re-ingest of the same file a no-op>
```

---

## 9. Availability contract entry

*What the feature store will declare, so that a strategy cannot silently assume data we do not have. The contract refuses unavailable requests deliberately; this is the entry that makes the refusal correct.*

```python
declare_feature(
    feature_id="<id>",
    source="<source_name>",
    granularity=<granularity>,
    earliest_clean={<symbol>: date(<yyyy>, <m>, <d>), ...},
    point_in_time=<True | False>,
    assumed_lag=<timedelta or None>,
    known_gaps=[<ranges>],
)
```

```
Explicitly not declared, and why: <the adjacent thing people will assume this source
                                   provides and it does not>
```

---

## 10. Verification commands

*Real commands with the shape of their expected output, runnable by someone who has never touched this source. Each one checks a claim made above; a claim with no command behind it is unverified and should say so.*

```console
$ <coverage check — earliest and latest date per symbol>
Expected: <output shape>
```

```console
$ <checksum verification>
Expected: <output shape>
```

```console
$ <timestamp sanity — raw integers plus rendered UTC for first and last rows, per market>
Expected: <output shape, showing the dates render inside a sane range>
```

```console
$ <normalization round-trip — ingest a known file and assert the parsed output>
Expected: <output shape>
```

```console
$ <the check that an unknown (market, date) pair fails closed>
Expected: <the exception, not a default>
```

---

## 11. Re-verification

*What invalidates this record, and how often it is re-checked anyway. Sources change without announcing it — a unit change, a new header row, a renamed field — and every one of those has shipped as a minor change somewhere.*

```
Cadence:        <from the frontmatter>
Last verified:  <yyyy-mm-dd> by <name>
Next due:       <yyyy-mm-dd>

Invalidated immediately by:
- <a schema change in any dataset above>
- <a change in the checksum publication>
- <a new symbol entering the universe, which needs its own coverage row>
- <a licence change>
- <any ingestion defect traced to this source, which also requires a post-mortem entry>
```

---

## Definition of done

- [ ] Every claim in section 1 was checked against actual file contents, not documentation
- [ ] Coverage is stated per symbol with an earliest date, and the shortest history is named
- [ ] Rate limits state the window, the penalty, and the ingester's headroom
- [ ] Credential requirements are stated, and any needed credential was requested from the user rather than sourced
- [ ] Checksum verification has been run and its command is recorded
- [ ] Every trap row is filled, including with `none observed`
- [ ] Raw bytes from a real file are pasted, not parsed output
- [ ] The `available_at` question is answered explicitly, and any assumed lag is justified
- [ ] Normalization is keyed on `(market, date)` and an unknown pair raises rather than defaults
- [ ] The availability contract entry names what this source explicitly does not provide
- [ ] Every verification command has been executed and its real output shape recorded
- [ ] `first_verified`, `verifier`, and `status` in the frontmatter reflect reality
