# Memory System

Three tiers of agent memory, what belongs in each, and the rules that keep what the system remembers true.

This expands `ARCHITECTURE.md` §9. The one-line summary: **memory is append-only, and an agent cannot rewrite its own history to look better.**

---

## 1. Three tiers, and the standard failure

| Tier | Lifetime | Store | Contains |
|---|---|---|---|
| **Working** | One agent invocation | Process memory | The scratch state of the call in flight |
| **Episodic** | Forever | Postgres, append-only | Things that happened, with provenance |
| **Semantic** | Until superseded or expired | Postgres + `pgvector` | Generalisations across many things that happened |

### The standard failure is conflating them

`ARCHITECTURE.md` §9 names it directly. Each conflation produces a system that is confidently wrong in a different way:

| Conflation | What it looks like | What breaks |
|---|---|---|
| **Working used as a source of truth** | "The agent said earlier in this session that BTCUSDT spreads widen at funding" is written down as a fact | The claim has no provenance. It may have been a hallucination three turns ago. It is now indistinguishable from a measured finding |
| **Episodic treated as a cache** | Old rows deleted or overwritten "to keep the table small" | Post-mortems become impossible. The record of what the system believed *at the time of an incident* is gone, which is exactly the record an incident needs |
| **Semantic filled with unpromoted opinions** | An agent writes "momentum works on 5m bars" straight into semantic memory | Every future retrieval returns it as a lesson with the same standing as a finding backed by 40 trials. The store degrades into a plausible-sounding opinion pile |
| **Semantic used as episodic** | Every observation stored as a "lesson" | Retrieval returns 400 near-duplicates; k=5 becomes meaningless; the agent skims |

The tiers are not a performance optimisation with three levels of cache. They are **three different epistemic categories**: scratch, evidence, and belief. Code that moves data between them without a promotion decision has collapsed the distinction.

### The load-bearing distinction

- **Episodic answers "what happened".** It is never wrong, because it records events, not claims. An episodic row saying an agent asserted X is true even if X is false.
- **Semantic answers "what we believe".** It can be wrong, which is why every lesson carries a falsifier, an evidence count, and a review date.

An episodic row is a fact about the system. A semantic row is a claim about the world. They are stored differently because they can fail differently.

---

## 2. The tiers in detail

### 2.1 Working memory

Ephemeral, per-invocation, in-process. The retrieval result an agent is reasoning over, the candidate duplicate set during a write, the partially-built output structure.

Rules:

- **Discarded at the end of the call.** No serialisation, no cross-invocation reuse.
- **Never cited as provenance for anything persisted.** If a fact is worth writing down, it must be traceable to an episodic row or an external primary source. "The agent said so earlier in the session" is not provenance.
- Size-capped. An agent whose working set exceeds its token budget has been handed too much context, which is a retrieval bug (§3), not a memory-capacity problem.

### 2.2 Episodic memory

Append-only, in Postgres, forever.

```python
class EpisodicRow(BaseModel):
    row_id: UUID
    agent: str
    kind: Literal["decision", "observation", "postmortem", "trial",
                  "incident", "correction"]
    correlation_id: UUID | None
    payload: dict[str, Any]           # schema-validated per kind
    supersedes: UUID | None
    created_at: datetime              # DB-assigned, tz-aware UTC
    # no update path exists
```

What belongs here:

- Every agent invocation: inputs, output, prompt hash, provider, latency, token cost.
- Every decision the system made, with the inputs that produced it — and every decision it **declined** to make.
- Every backtest run, including voided ones. Voided runs are the record of what the system nearly believed.
- Every validation plan and result, including abandoned reconfigurations. Those are what meta-overfitting looks like from the outside.
- Every ingestion run with its `NormalizationResult`.
- Every memory write **rejected**, and why.
- Every incident and post-mortem.

What does not belong here: generalisations, opinions, summaries. Those are semantic candidates, and they arrive only through promotion (§5).

`created_at` is assigned by the database (`DEFAULT now()` on `timestamptz`), never by the client. A client clock must not be able to reorder history.

Payloads are schema-validated **per `kind`** at write time. Validate at the boundary, then trust internally (`CLAUDE.md` §4).

### 2.3 Semantic memory

Distilled lessons with embeddings, retrievable by similarity.

```python
class SemanticLesson(BaseModel):
    lesson_id: UUID
    claim: str                        # immutable once written
    falsifier: str                    # what observation would make this false
    scope: Literal["strategy", "lineage", "regime", "venue",
                   "mechanical", "global"]
    evidence_row_ids: list[UUID]      # must resolve, always
    evidence_count: int
    embedding_model: str
    review_after: date
    supersedes: UUID | None
    status: Literal["active", "superseded", "expired"]
```

Two required fields carry most of the weight:

**`falsifier`.** A lesson without a stated falsifier is an opinion. It cannot expire, cannot be contradicted, and cannot be checked. Requiring it at write time is what keeps the store from filling with unfalsifiable folk wisdom.

**`evidence_row_ids`.** Every lesson points at the episodic rows that produced it, and every one must resolve. A lesson whose evidence no longer exists is referential corruption in an append-only store, which should be impossible and escalates if observed.

### What a good lesson looks like

> "Passive limits inside 1bp on BTCUSDT filled 71% of the time, but realised 5-minute post-fill markout was −3.2bp against a captured spread of 0.8bp: the fills are adversely selected. Passive is only economic at offsets beyond 3bp, where fill rate drops to 12%."
> *falsifier:* "A month of passive fills inside 1bp with markout better than −1bp."

Specific, numeric, scoped, checkable.

### What a bad lesson looks like

> "Passive orders have adverse selection."

True, useless, and unfalsifiable. It applies everywhere, predicts nothing, and will be retrieved for every execution question forever, crowding out the version with numbers in it.

### Embeddings and the index

- `pgvector` with an HNSW index (`m=16`, `ef_construction=64` at current corpus size).
- **`embedding_model` is recorded on every row.** Cosine similarity is not comparable across models, so a corpus with two models in it silently returns nonsense for cross-model comparisons.
- Changing the embedding model is an **ADR-level decision** requiring a re-embed of the entire corpus with the supersession chain intact.
- A re-embed produces a new row with **identical claim text** and a new vector. If the text changes, the lesson changed, and that requires promotion (§5). Silently rewording a lesson during a model migration is how a memory store drifts away from its own evidence.

---

## 3. Retrieval

### Filter before ranking

> **Scope filters cut the candidate set far more usefully than similarity does.**

A strategy-scoped lesson about `L-03` is noise when reasoning about `L-11`, however similar the wording — and the wording will be very similar, because strategy lessons are written in the same vocabulary. Similarity search over an unfiltered corpus returns the nearest *phrasings*, not the most relevant lessons.

Order of operations: scope filter → status filter → recency weight → similarity rank → cap.

### Cap the payload

Default **k=5**, similarity threshold 0.75, ranked by `similarity * evidence_weight * recency_weight`.

Retrieval that returns 40 lessons is retrieval that will be skimmed. Prefer k=5 with high evidence counts over k=40 sorted by cosine — and the token budget makes this concrete: a free-tier agent with a 25k-token budget spending 12k on retrieved memory has 13k left for the actual problem.

### Provenance on every item

> **No memory is ever returned to an agent without provenance.**

```python
class RetrievedItem(BaseModel):
    lesson_id: UUID
    claim: str
    similarity: Decimal
    evidence_count: int
    age_days: int
    status: Literal["active", "expired"]   # expired items returned, flagged


class RetrievalResult(BaseModel):
    items: list[RetrievedItem]
    query_embedding_model: str
    total_candidates: int                  # so the caller knows if they saw a slice
    filtered_by: dict[str, str]
```

An agent reasoning over anonymous "context" cannot weigh it. A lesson with `evidence_count=1` from 90 days ago and a lesson with `evidence_count=40` from last week must not arrive looking identical, because the agent's job is to weigh them and it can only do that if it can see the difference.

`total_candidates` tells the caller whether they saw a slice or the set. A k=5 response from 200 candidates and a k=5 response from 5 candidates mean completely different things.

### Expired lessons are returned, flagged

Never silently dropped. A lesson past its `review_after` date is still evidence about what the system used to believe, and hiding it makes the agent think the topic is unexplored — so it re-derives the same conclusion, writes it again, and the store grows a duplicate that has lost its history.

### Empty results are a finding

An empty retrieval is reported explicitly as "no prior experience here", not as "no relevant lessons". The second phrasing sounds like a judgement — as though the system looked and found nothing worth returning — and an agent will treat it as licence rather than as absence of information.

---

## 4. The append-only rule

> **Memory is append-only. An agent cannot rewrite its own history to look better.**

Not through the ORM. Not through a migration. Not through a "cleanup script".

### Three enforcement layers

1. **`REVOKE UPDATE, DELETE`** from the application role on every memory table.
2. **A rule or trigger** that raises on `UPDATE` and `DELETE`.
3. **A CI test asserting the mutation attempt raises** — not that the row is unchanged.

Belt, braces, and a third thing, because one of them will be forgotten in a future migration. The CI test is the layer that survives a migration dropping a trigger, and it asserts a *raise* specifically because a test checking that the row is unchanged passes when the mutation silently no-ops.

### Why this rule and not a softer one

The obvious objection is that immutability makes corrections awkward. It does. That cost is paid deliberately, for three reasons:

**A post-mortem needs the old belief, not the current one.** An incident from July must be explicable in terms of what the system believed in July. If the lesson that drove the decision has been updated to say something better, the incident becomes inexplicable — the record now says the system should have done the right thing.

**An agent that can edit its history will.** Not maliciously. An agent asked to reconcile a contradiction will take the shortest path, and updating the old row is shorter than reasoning about supersession. The database has to make the short path unavailable.

**Mutable memory is unauditable in principle.** `OBSERVABILITY.md` §1 requires that any trade be reconstructable months later, including which agent reasoning contributed. If that reasoning's inputs could have changed since, the reconstruction is of a decision that was never made.

### Corrections are new rows

A correction writes a new row with `supersedes` pointing at the old one. The old row remains queryable forever with `status="superseded"`. See §5.

### Escalation

**Any successful mutation of an episodic or semantic row escalates to the user immediately.** It means database-level enforcement was bypassed, and the audit property of the whole system is in question — not just memory's.

An *attempted* mutation is also a signal: it is reported to `security` and `database`, because it means some code thinks memory is editable, and that code is a template someone will copy.

### What never goes into any tier

Secrets, API keys, Ed25519 material, raw exchange credentials — including inside an archived prompt or response payload. Memory rows are forever, which makes them the worst possible place for a credential (`SECURITY.md` §4.6).

---

## 5. Promotion and supersession

### Promotion criteria: episodic → semantic

Promotion is a **deliberate decision by the `learning` agent, gated on evidence.** The memory layer stores what is promoted; it does not decide what deserves storing. An agent may not promote its own observation.

A candidate must satisfy **all** of:

| # | Criterion | Rationale |
|---|---|---|
| 1 | **≥ 3 independent episodic observations**, or 1 if `scope == "mechanical"` | See below |
| 2 | Observations are **independent** — different runs, different windows, different strategies | Three observations of the same backtest are one observation |
| 3 | A **falsifier** is stated and is checkable from data the system has | An unfalsifiable claim cannot be retired |
| 4 | A **scope** is assigned, and it is the narrowest that fits the evidence | Over-broad scope is how one strategy's quirk becomes a global "lesson" |
| 5 | The claim is **specific and numeric** where the evidence is numeric | "Spreads widen at funding" vs "p99 spread on BTCUSDT roughly triples in the 5 minutes around funding settlement" |
| 6 | No active lesson **contradicts** it without the contradiction being addressed | Two lessons that disagree require a third that scopes both |
| 7 | A **`review_after`** date is set | §6 |

### The mechanical exception

> **Mechanical lessons are promotable on a single observation.**

A mechanical lesson is a fact about how a system behaves, not a statistical claim about markets:

> "`ccxt` accepts a per-call `urls` override that skips the session base URL, so guarding at session construction is insufficient."

> "Binance spot archives switched to microsecond epochs on 2025-01-01; futures did not."

> "TimescaleDB's extension becomes available a few seconds after Postgres accepts connections, so `pg_isready` alone is an insufficient health check."

These are observed once and are then simply true. Requiring three observations of a format trap means being burned three times by the same trap, which is exactly the outcome the memory system exists to prevent. The three ingestion traps in `DATA_PIPELINE.md` §3 live in semantic memory permanently on this basis.

The distinction that keeps this from being a loophole: a mechanical lesson is **verifiable by re-running one command**. A statistical lesson is not, and needs a sample.

### Deduplication on write

Cosine ≥ **0.95** against an active lesson of the same scope means **merge**, not insert:

- New row, `evidence_count` incremented (inherited + new).
- `supersedes` pointing at the old row.
- **Identical claim text** unless `learning` re-authored it — the memory layer never rewords.
- Old row marked `superseded`.
- The merge decision itself is recorded as an episodic row pointing at both lesson ids, so it can be second-guessed later.

Target duplicate rate below 2% at cosine 0.95.

### Contradictions

Two lessons that disagree are **information, not a problem to clean up.**

- **Never delete a contradictory lesson.**
- Write a **third** lesson that scopes both and supersedes them, keeping the chain intact.
- Flag the contradiction to `learning` rather than storing both silently.

A common near-contradiction worth naming: an *observation* and a *policy* that read as if both were measurements. "The OMS position view may lag by up to 94 seconds after a futures reconnect" (observed) and "size nothing for 120 seconds after a reconnect" (a safety margin) are not in conflict, but a lesson that states both without distinguishing them is ambiguous and gets treated as a measurement error later.

### The supersession chain

```
lesson_A (2026-05-14, evidence 3, superseded)
    ▲
    │ supersedes
lesson_B (2026-06-20, evidence 4, superseded)
    ▲
    │ supersedes
lesson_C (2026-07-31, evidence 6, active)
```

Every row in the chain remains queryable forever. Retrieval returns only `active` rows by default; an investigation can walk the chain backwards to reconstruct what the system believed on any date.

**Write conflicts** — two corrections to the same row — keep both, chained in `created_at` order, and flag to `learning`. Never resolve by preference.

---

## 6. Decay and expiry

Some lessons stop being true. A market regime ends, an exchange changes an API, a strategy is retired. A memory store with no expiry accumulates confident claims about a world that no longer exists, and those claims are retrieved with the same authority as current ones.

### `review_after`

Every semantic lesson carries a review date, set at promotion from its scope:

| Scope | Default review interval | Why |
|---|---|---|
| `mechanical` | **never** (`review_after = 9999-12-31`) | A format trap does not expire. If the exchange changes it back, that is a new lesson superseding this one |
| `venue` | 180 days | Exchange behaviour changes on a slow clock |
| `regime` | 90 days | Regimes end |
| `lineage` | 90 days | Tied to a strategy family's life |
| `strategy` | Expires when the strategy is retired | A lesson about a dead strategy is history, not belief |
| `global` | 365 days, and promoted to `global` only rarely | A claim about everything is usually a claim about one thing, over-scoped |

### On expiry

A lesson past `review_after` becomes `status="expired"`. It is **not deleted** and is **still returned by retrieval, flagged** (§3).

`learning` reviews expired lessons and does exactly one of:

1. **Re-affirm** — new row, identical claim, fresh `review_after`, evidence count including new observations, superseding the expired one.
2. **Revise** — new row with amended claim and its own evidence, superseding.
3. **Retire** — leave it expired. The claim stops being retrieved as belief and remains as history.

There is no fourth option, and specifically **there is no "extend the date"** — that would be an in-place update.

### Recency weighting is not decay

Retrieval weights recent lessons slightly higher. That is ranking, not expiry, and it must not be mistaken for it: recency weighting makes a stale lesson rank lower; only expiry marks it as no longer believed. A store that relies on recency weighting alone keeps returning a three-year-old claim whenever nothing newer exists on the topic, which is precisely when it is most dangerous.

### Growth as a signal

**Semantic memory growing faster than expected means the promotion bar is being applied too loosely.** That degrades every future retrieval — more candidates, lower average quality, k=5 filled with near-misses — and it escalates to `learning` rather than being absorbed as normal growth.

---

## 7. Failure handling

| Failure | Response |
|---|---|
| `pgvector` index missing or corrupt | Fall back to exact search, **report degraded latency**. Never silently fall back to keyword matching — the caller's reasoning depends on knowing what it saw |
| Embedding call fails (provider quota) | Queue the write as episodic with `pending_embedding`. Never drop it, and **never store a zero vector** — a zero vector is similar to everything |
| Write conflict on the supersession chain | Keep both, chain in `created_at` order, flag to `learning` |
| Retrieval returns nothing | Say so explicitly as a finding, not as a judgement |
| A lesson's evidence rows do not resolve | Escalate. Referential corruption in an append-only store should be impossible |
| Attempted mutation of a memory row | Reject, record as episodic, report to `security` and `database` |
| **Successful** mutation of a memory row | Escalate to the user immediately. The audit property of the system is in question |
| Embedding model change proposed | Escalate. ADR-level; invalidates cross-model similarity and requires a full re-embed |

---

## 8. Success metrics

- **Zero mutations** of episodic or semantic rows, verified by constraint tests in CI.
- **Retrieval precision:** agents cite retrieved lessons in their reasoning more than **50%** of the time. Below that, retrieval is returning plausible-looking irrelevance, which costs tokens and adds noise.
- Duplicate rate in semantic memory **below 2%** at cosine 0.95.
- Every active lesson has at least one evidence row id that resolves.
- Median retrieval latency **under 100 ms** at current corpus size.
- Every promoted lesson has a stated falsifier and a review date.

---

## 9. Cross-references

| For | See |
|---|---|
| Why memory is append-only, and the three tiers | `ARCHITECTURE.md` §9, `CLAUDE.md` §10 |
| Audit-table immutability (the same three controls) | `SECURITY.md` §6 |
| Why prompt hashes must resolve for replay | `PROMPT_LIBRARY.md` §2 |
| Reconstruction requirement and correlation IDs | `OBSERVABILITY.md` §1, §3 |
| Memory read/write tool contracts and permissions | `TOOLS.md` §5 |
| What agents may and may not decide | `AI_MANIFEST.md` |
