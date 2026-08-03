---
number: 0013
title: Archive format is resolved per (market, dataset, date), and an undeclared combination raises
date: 2026-08-03
status: accepted
deciders: [ismetcahangirov, architect, data-engineer]
supersedes: null
superseded_by: null
related_issues: ["#21", "#22", "#23", "#25"]
related_adrs: [ADR-0011]
---

## Context

`data.binance.vision` is the only source of history this project has, and it is free, unauthenticated and unversioned. Its files change shape over time, and the changes are not announced anywhere a program can read.

```
Forces:
- VF-015: Binance spot archives (klines, trades, aggTrades) emit MICROSECOND
  epochs from 2025-01-01. USD-M futures archives emit milliseconds, then and
  now. No field, header or filename carries the unit.
- VF-016: futures kline CSVs open with a header row; spot kline CSVs open with
  data.
- F-005: spot trade archives serialise booleans Python-style, True/False, on
  is_buyer_maker and is_best_match.
- None of the three raises. Each is a format assumption a competent engineer
  makes correctly for one (market, dataset, date) and incorrectly for another.
- The damage is asymmetric per trap. A wrong unit is loud at the endpoints
  (1970 or the year 56,000) and silent in the middle of a resampled series. A
  wrong header assumption drops exactly one row per file, always 00:00 UTC. A
  wrong boolean encoding leaves row counts, prices and volumes all correct and
  inverts only the trade side, uniformly.
- P1 is about to write six loaders (#22-#28) and a feature store (#29). Every
  one of them needs these three answers on its first line of parsing.

The constraint that forces a decision now:
#21 is the first task in P1 and every subsequent parser reads its format
decisions from whatever this issue builds. If a module-level parsing constant
is the path of least resistance when #23 is written, it will be taken -- and a
corpus ingested under a wrong constant is not repaired by fixing the constant,
because the wrong values are already in Parquet and already backtested.
```

## Decision

**Archive format is a property of `(market, dataset, date)` and is answered by one resolver, `fking.data.format_resolver`, which raises `DataIntegrityError` on any combination this project has not declared.** The declaration table carries the epoch unit, whether the file has a header row, the boolean encoding and the columns it applies to, each as a half-open date segment. There is no default, no fallback and no inference from a neighbouring dataset or the other market. A date range crossing a format boundary is two ingestion specs, not one with a conditional inside the row loop. Normalisation additionally asserts that every produced instant falls in `[2010-01-01, now + 1 day)`, where `now` is a parameter fixed for the whole run rather than a clock read per row.

## Alternatives considered

### Alternative 1 — sniff the format from the file itself (strongest rejected)

**What it would have given us.** All three traps are, on their face, detectable from the bytes in front of you. A header row is a first line whose first token is `open_time` rather than an integer. A boolean encoding is whichever of `True`/`true`/`1` appears in the column. An epoch unit is a magnitude: a 13-digit integer is milliseconds, a 16-digit one is microseconds, and the two are three orders of magnitude apart, which no amount of price movement can confuse. Sniffing needs no table, cannot go stale, and — crucially — **keeps working when Binance changes something nobody has told us about.** A declaration table is a claim about the world that decays; the file is the world.

**Why it lost.** Sniffing is right about detection and wrong about what to do with it. The magnitude heuristic is genuinely sound for a *single* value, and that is exactly the case that does not hurt us: a whole file read in the wrong unit lands in 1970 or the year 56,000 and is obvious. The failure that costs money is a series assembled from a range that *crosses* the cutover, where each half sniffs correctly, each half is individually plausible, and the resampled result has correct-looking endpoints with corrupted spacing in the middle. A sniffer produces that outcome silently and considers itself successful, because every file it looked at was fine.

The deeper objection is what sniffing does to the failure it cannot handle. When the archive changes in a way the heuristic did not anticipate — a new dataset, a second cutover, a 14-digit epoch during some transitional week — a sniffer picks the closest match and continues. A declaration raises and names the combination. The whole point of #21 being the first task in P1 is that **an unknown format must be an escalation, not an inference** (`DATA_PIPELINE.md` §11), because inference is the mechanism of all three traps rather than an unfortunate side effect of one.

**What survives the rejection, and is adopted.** Sniffing's best argument — that the file is ground truth and the table is a decaying claim — is correct, and it is adopted in two places. The plausibility window `[2010-01-01, now + 1 day)` is a magnitude check on the *output*, so a table that has silently become wrong is caught on the first row rather than at the end of the quarter; that half ships with #21. The other half ships with the parsers in #23: `has_header_row` is verified against the file's first token before any row is parsed, with a mismatch rejecting the whole file (`DATA_PIPELINE.md` §4, step 2). The declaration decides; the file gets a veto. What the file never gets is the casting vote.

### Alternative 2 — a per-market constant, or a global one with a date branch inside the loader

**What it would have given us.** Much less machinery. Two constants — `SPOT_UNIT` and `FUTURES_UNIT` — plus an `if archive_date >= CUTOVER` inside the spot branch covers every fact known today, in about six lines, with no table, no enums and no resolver to test. Every parser in P1 imports two names and moves on.

**Why it lost.** A per-market constant is wrong for spot before 2025, which is most of the corpus. Adding the date branch fixes that and relocates the problem: the branch now lives inside the row loop of whichever loader was written first, and the second loader either imports it (coupling two parsers through a private conditional) or reimplements it (and one of the two copies is eventually edited alone). More importantly it answers only trap 1. Traps 2 and 3 get their own constants in their own modules under the same reasoning, and then the thing that actually matters — that these are three instances of *one* rule — is nowhere in the code, so the fourth instance, whatever it turns out to be, is discovered the way the first three were.

The version of this that nearly won is a table without segments: one `ArchiveFormat` per `(market, dataset)`, with the cutover handled by callers splitting their date range. It is simpler and it is what the loaders will do anyway. It was rejected because it moves the one piece of knowledge that must not be duplicated — *where* the boundary is — out of the table and into every caller that splits a range.

### Alternative 3 — do nothing (let #23 decide when it writes the first parser)

```
Cost of the status quo: #22 through #28 are six loaders, and the first one
written establishes the pattern the other five copy. The pattern available at
that moment, with a deadline and no resolver, is a module-level constant --
which is trap 1 exactly, reintroduced by the task that was supposed to defend
against it.
Why that is no longer payable: a wrong constant is not repaired by fixing the
constant. The corpus is already in Parquet, strategies are already validated
against it, and the trial ledger has already charged the trials. Re-ingesting
is cheap; un-charging a trial and un-believing a result are not.
```

## Consequences

**What becomes easier**
- Adding a symbol, a dataset or a market is a table entry with a citation, reviewed as data rather than as control flow.
- A format change upstream surfaces as a named refusal at ingest — `no archive format is declared for market='spot' dataset='aggTrades'` — instead of as a backtest anomaly six weeks later.
- The three traps become one testable rule. `tests/data/test_format_resolver.py` parametrizes over the enum product, so a new `Dataset` member cannot be added without either being declared or being asserted to raise.
- Trap 1 is now caught twice by independent mechanisms: the declaration decides the unit, and the plausibility window checks the result. Either alone would be enough for the loud direction; both are needed because they fail differently.

**What becomes harder**
- Ingesting anything undeclared is blocked until an archive has actually been read. `(spot, aggTrades)` is the live example: its epoch unit is verified by VF-015, its boolean encoding is not, and it is almost certainly `python` — but "almost certainly" is precisely trap 3's mechanism, so it raises until #22 has observed one. That is a real ordering constraint on #22 and #23.
- Every parser must thread a resolved `ArchiveFormat` rather than reading a constant, which is a parameter on call paths that would otherwise not need one.
- `epoch_to_utc` takes `now_utc`, so callers must fix a reference instant per run. This is deliberate — a bound re-read per row drifts mid-file, and then one raw integer is accepted at the top of a file and rejected at the bottom — but it is friction at every call site.

**What we now cannot do**
- Ingest a date range that crosses a format boundary as a single spec. The range must be split, and the split is visible in the ingestion record.
- Infer a format from a neighbouring dataset, the other market, or the adjacent date segment. Reopening that is reopening the entire decision: the traps are not "we occasionally guess wrong", they are "guessing is the failure".

## What would make us revisit this

```
Trigger:   The declaration table exceeds roughly 40 segments, or a single
           upstream format change requires editing more than three of them.
Observed:  The length of DECLARED_FORMATS in
           src/fking/data/format_resolver.py, reviewed whenever #22-#32 add an
           entry.
Then:      Open a superseding ADR for a format *derivation* -- rules over
           (market, dataset) that generate segments -- keeping the refusal on
           an underived combination. Not a return to sniffing, and not a
           default.
```

## Verification

```
Confirmed if:  zero ingested series are found to carry a wrong epoch unit,
               a dropped first row, or an inverted boolean column, across the
               whole corpus, measured by 2027-02-01; and every format refusal
               in the log names a combination that was genuinely undeclared
               rather than one the table should already have covered
Refuted if:    any module under src/fking/data acquires a format constant
               outside format_resolver, or resolve_archive_format acquires a
               branch that returns a value on an undeclared combination
Checked by:    data-engineer agent, via `make check`, the enum-product
               parametrization and the structural no-default test in
               tests/data/test_format_resolver.py, and
               `rg -n '/ *1000|/ *1_000_000|\* *1000' src/fking/data`
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
- [x] Linked from #21 and from `.claude/knowledge/decisions-log.md`
