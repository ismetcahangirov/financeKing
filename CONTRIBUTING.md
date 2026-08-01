# Contributing

How to get a working environment, what the development loop is, and what "done" means here.

Read `CLAUDE.md` first — it is the operating manual and it overrides this document where they differ. This one is the practical layer underneath it.

---

## 1. Environment setup

### 1.1 Prerequisites

| Tool | Version | Why this one |
|---|---|---|
| Python | 3.12.x | `TaskGroup`, `slots=True` dataclasses, `datetime.UTC`. Not 3.13 — `ccxt` and the Timescale client stack are not yet clean on it |
| uv | ≥ 0.5 | One resolver, a real lockfile, reproducible CI. Do not use pip or Poetry alongside it |
| Docker + Compose | v2 | Postgres/Timescale, Redis, the OTel stack |
| Node | ≥ 20 | Dashboard only. Not needed for backend work |
| `gh` | any recent | The workflow assumes it for issues, PRs, and labels |

### 1.2 Bootstrap

```bash
git clone <repo> && cd financeKing
uv sync --frozen                       # --frozen: fail rather than silently re-resolve
cp .env.example .env
make up                                # postgres+timescale, redis, otel collector, grafana
make migrate
make check                             # must be green on a clean checkout
```

`uv sync --frozen` rather than `uv sync`. Without `--frozen`, a resolver difference silently updates the lockfile and your first commit contains a dependency bump you did not intend and cannot explain in the PR body.

If `make check` is not green on a clean checkout, stop and report it. Do not start work against a broken baseline — every failure you see afterwards will be ambiguous.

### 1.3 Credentials

`.env` needs Binance **testnet** keys. Nothing else. Get them from the testnet portals:

- Futures testnet: `testnet.binancefuture.com` — API key and secret.
- Spot testnet: `testnet.binance.vision`, GitHub OAuth. Spot user data requires **Ed25519** keys, not HMAC — the `listenKey` mechanism is dead (`ARCHITECTURE.md` §7). Generate the keypair locally and register the public key; the private key never leaves `.env`.

The LLM providers (Gemini, Groq) use free-tier keys. Without them the agent layer degrades to deterministic-only operation, which is a supported mode — you can do most backend work with no LLM keys at all.

**There is no configuration value that points this system at a production trading endpoint.** Not in `.env`, not behind a flag. If you find yourself wanting one, read `CLAUDE.md` §0 and then ask the user.

### 1.4 The one place production hosts legitimately appear

This resolves an apparent contradiction that will otherwise cost you an hour.

`CLAUDE.md` §2 requires cost model parameters to be calibrated from **production** market data. `CLAUDE.md` §11 forbids "let me just check it against mainnet read-only". Both are correct, because they are about different hosts reached by different modules:

- **Historical market data** comes from the public bulk archive (`data.binance.vision`) — flat files, no authentication, no order endpoints. It is reachable **only from `data/`**, and it is how the 0.16bp production spread figure was measured.
- **The trading API allowlist** is testnet-only, applies to `execution/`, and has no exceptions including read-only ones. Read paths become write paths during refactors; that is the whole reason for the blanket rule.

The allowlist in `fking.platform.safety` is partitioned accordingly, and `import-linter` forbids `execution` from importing the archive client. A change that merges the two partitions, or that lets `execution` reach an archive host, is a `safety:critical` change.

### 1.5 Verify the environment is actually working

```bash
make test ARGS="tests/integration/test_container_boot.py -v"   # Postgres+Timescale reachable, migrations applied
make test ARGS="tests/adversarial -v"                          # the five; all must pass on a clean checkout
python -m fking.platform.safety --print-allowlist               # prints the compiled-in host set
```

The last one is worth running once on day one. Seeing the allowlist printed — and noticing that it does not contain `api.binance.com` — is how the demo-only guarantee stops being an abstraction.

---

## 2. The development loop

The commands in `.claude/commands/` implement this; the loop below is what they are doing.

```
/new-task <n>   →  pull main, branch, read the issue, post the plan
/plan           →  contracts, verification, named failure modes
   (test first) →  the failing test, failing for the right reason
/build          →  minimal implementation, instrumented as you go
   make check   →  green, in this transcript
/review         →  your own diff, against the blocking list
/ship <n>       →  commit, push, PR with labels, milestone, evidence
```

### 2.1 Start: pull `main`, then branch

```bash
git status --porcelain                 # must be empty
git checkout main && git pull origin main
git checkout -b feat/<n>-<kebab-slug>
```

Every task, no exceptions. `GIT_WORKFLOW.md` §1 has the two specific failures this prevents — the second one (Alembic revision chains) is the expensive one.

### 2.2 Plan before code

Post the plan as a comment on the issue, so it survives the session. It must state: files and their modules, the contract with units in the names, how it will be verified, the coverage floors that apply, what is out of scope, and any assumption that would waste the work if wrong.

The last item is the one to actually act on. Ask about it **now**, not after building. `CLAUDE.md` §8.

### 2.3 Test first

Write the failing test. Run it. Watch it fail **for the reason you expect** — a test that fails with an `ImportError` has not told you anything.

For anything in `risk/`, `domain/`, or any position arithmetic, a Hypothesis property test is mandatory (`TESTING.md` §3).

### 2.4 Implement, minimally, instrumented

The non-negotiables while writing are in `CLAUDE.md` §2 and expanded with examples in `CODING_STANDARDS.md`.

Instrument as you go: emit the event, propagate the correlation ID that originated at the top of the flow, write the audit row. Deferred instrumentation never gets added properly and is missing from exactly the history the next investigation needs.

### 2.5 Fast inner loop

```bash
make test ARGS="tests/unit/<path> -x -q"      # sub-second
make types                                    # mypy --strict, ~10s
```

Run the full `make check` before the self-review, not on every save.

### 2.6 Self-review, then ship

```bash
git diff main...HEAD
git diff main...HEAD | grep '^-' | grep -vE '^---'    # read the deletions
make check
```

Then run `/review` on your own diff against the blocking list in `CODE_REVIEW.md` §1. Self-review with the checklist finds roughly half of what a reviewer would, at a fraction of the round-trip cost.

---

## 3. Which changes require an ADR

An ADR is required when a decision **constrains future changes** — when someone later will need to know not just what we do but what we considered and rejected.

**Required:**

| Change | Example |
|---|---|
| Adopting, replacing, or rejecting a significant dependency | Choosing `ccxt` over `python-binance`; rejecting `NautilusTrader` |
| A change to the module dependency graph or an `import-linter` contract | Allowing a new inward dependency |
| A storage or schema decision with migration cost | Timescale hypertable vs plain table; Parquet partitioning scheme |
| A change to the survival score's inputs or weighting | Adding a capacity penalty |
| A change to the cost model's structure | Modelling maker/taker split per fill |
| A change to the validation methodology | Fold count, purge/embargo length, holdout policy |
| A new agent, or a change to an existing agent's forbidden-decision list | Adding a Critic |
| Anything touching the safety kernel's design | Partitioning the allowlist |
| Anything touching backtest/live parity | A venue-specific fill behaviour |
| A decision to **not** do something, where the option will resurface | "We are not adding an L2 book feed, because free full-depth history does not exist" |

**Not required:** adding a strategy that uses existing primitives; adding a feature to the existing registry; bug fixes; refactors that preserve behaviour and contracts; performance work that does not change semantics; documentation.

**Write it with `/adr`.** The load-bearing section is **Alternatives considered** — specifically, the strongest rejected alternative, given its best case. An ADR whose alternatives are all obviously bad is an ADR that did not consider any. Include "do nothing" explicitly; it is frequently right and nobody writes it down.

ADRs are **immutable once accepted**. Changing a decision means writing a new ADR that supersedes the old one, adding exactly one line (`> Superseded by ADR-00XX`) to the old one and changing nothing else in it. The record of rejected paths is the valuable part; editing it destroys the reason the record exists.

An ADR cannot decide to weaken demo-only execution, backtest/live parity, risk's exclusive authority to construct orders, point-in-time features, or append-only audit. If it touches one of those, it needs the user's explicit agreement before its status becomes Accepted.

---

## 4. Which changes require `safety:critical` review

**Any diff touching `src/fking/platform/safety/`.** No judgement call, no "this one is trivial".

Concretely, including all of these:

- Adding or removing a host from either allowlist partition
- Reformatting the allowlist literal
- Changing a type annotation on the allowlist
- Editing `guarded_client()` or anything it calls
- Editing the startup endpoint resolution check
- Changing the `import-linter` contracts that forbid raw HTTP clients in `execution`
- Editing the safety module's own tests
- Changing how the allowlist is logged at boot

Reformatting is on the list deliberately. A `frozenset` re-wrapped across multiple lines shows the entire block as changed, which is precisely the diff in which one entry changes and nobody sees it.

**What the label triggers** (`GIT_WORKFLOW.md` §5):

- Review from the repository owner. No self-approval, no automation approval.
- The `safety-kernel-diff` CI check, which fails by design on any change to that path and requires a written human override.
- A mutation-testing gate at ≥ 90% on that module — 100% coverage there is achievable without ever testing a rejection (`TESTING.md` §6.1).
- Individual listing, with its diff, in the release notes.

Adjacent changes that are **not** `safety:critical` but do need a second look and should say so in the PR body: adding a new venue adapter, changing how credentials are loaded, adding a new outbound network call anywhere, and changing the reconciliation logic.

---

## 5. Definition of done

A change is done when **all** of these are true. Not most.

**Correctness**
- [ ] The behaviour the issue asked for exists and works.
- [ ] No `TODO`, no `NotImplementedError`, no stub that looks finished, no documentation describing something that does not exist.
- [ ] Scope is what the issue asked for. Anything left out is stated explicitly in the PR body with a reason.

**Tests**
- [ ] A test existed before the implementation and failed for the right reason.
- [ ] Property tests for any position or risk arithmetic, asserting the properties in `TESTING.md` §3.2.
- [ ] Tests assert behaviour, not implementation. They fail if you deliberately break the code.
- [ ] Real Postgres. Recorded exchange responses. No mocked database, no hand-written fixtures.
- [ ] Deterministic: clock injected, seed injected, no assertions on set ordering.
- [ ] Coverage at or above the module's floor, measured with `--cov-branch` for `domain`, `risk`, `execution`, `platform/safety`.

**Standards**
- [ ] `make check` green, run now, output in the transcript.
- [ ] `Decimal` from `str` for all money. No float ever touched the value.
- [ ] Timezone-aware UTC. No `utcnow()`. No clock read in `strategy/` or `risk/`.
- [ ] Domain objects frozen, with immutable field types.
- [ ] `mypy --strict` clean; every `# type: ignore` narrowed to an error code and justified inline.
- [ ] Names carry units. Constants in `risk/` carry provenance.
- [ ] No network client constructed outside `guarded_client()`.

**Observability**
- [ ] Correlation ID propagates across every new module hop.
- [ ] An audit row exists for every new decision point.
- [ ] Metrics and structured log fields added at the same time as the code, not after.

**Integration**
- [ ] `import-linter` green with no contract relaxed.
- [ ] Migration is forward-only and preserves append-only enforcement on audit tables.
- [ ] If a feature definition, the cost model, the scoring engine, or the backtest engine changed: a `Results-Invalidating:` git trailer is present (`GIT_WORKFLOW.md` §3).
- [ ] If `src/fking/platform/safety/` changed: `safety:critical` label, owner review, stated in the first line of the PR body.

**Documentation**
- [ ] An ADR if §3 requires one.
- [ ] Docs updated where behaviour changed; documentation that is now false is **deleted**, not annotated (`DOCUMENTATION_GUIDE.md` §4).
- [ ] Every command example in anything you wrote was actually executed.

**The PR**
- [ ] Labels, milestone, assignee `ismetcahangirov`, `Closes #<n>`.
- [ ] Verification section contains real command output, not a claim.
- [ ] Under ~400 substantive lines, or a stated reason it is atomic.
- [ ] You read your own diff, including the deletions.

---

## 6. Getting unblocked

**Decide yourself** when there is a defensible default, the outcome is reversible, or the answer is discoverable from the codebase. Pick, state the choice in one line, continue. Do not present a menu of options you are not going to pursue.

**Ask the user** when the answer changes the architecture and both readings are plausible; when it needs a credential, an account, or an external signup; when it involves money, legal exposure, or the safety kernel; or when proceeding under a wrong assumption would waste substantial work.

Ask concisely, one topic, with a recommendation attached. "I recommend A because X; say so if you would rather have B" is a better question than "which of these do you want?".

**When blocked mid-task**, do everything that does not depend on the answer first, then ask. Do not stop with nothing delivered — unless proceeding would be unsafe, or would make the delivered work useless if the assumption turns out wrong.

**If something is broken and you cannot fix it, say so plainly and describe what you tried.** That is a useful contribution. A false completion claim is not.

---

## 7. Command reference

```bash
make check       # lint, format check, mypy strict, import-linter, tests. Before every PR.
make test        # tests only
make lint        # ruff check + format check
make types       # mypy --strict
make up          # docker compose up
make down        # docker compose down
make logs        # tail service logs
make migrate     # apply Alembic migrations
make backtest    # run a backtest from a config file
```

Useful arguments:

```bash
make test ARGS="tests/unit -x -q"
make test ARGS="--cov=src/fking/risk --cov-branch --cov-report=term-missing"
make test ARGS="--hypothesis-seed=8213371"     # reproduce a CI counterexample
make test ARGS="-p randomly --count=5"         # hunt inter-test state leakage
make backtest CONFIG=configs/backtest/<pinned>.toml
```

Slash commands in `.claude/commands/`: `/new-task`, `/plan`, `/build`, `/test`, `/review`, `/ship`, `/refactor`, `/performance`, `/adr`, `/release`, `/backtest`, `/risk`, `/security`, `/debug`, `/research`.

---

## 8. Where to read next

| You are about to | Read |
|---|---|
| Write any code | `CODING_STANDARDS.md` |
| Write any test | `TESTING.md` |
| Review a PR | `CODE_REVIEW.md` |
| Branch, commit, or open a PR | `GIT_WORKFLOW.md` |
| Touch sizing, limits, or the kill switch | `RISK_PHILOSOPHY.md` |
| Add or change a strategy | `SURVIVAL_PROTOCOL.md`, `EVOLUTION_ENGINE.md` |
| Touch features or ingestion | `DATA_PIPELINE.md` |
| Optimise anything | `PERFORMANCE_GUIDE.md` |
| Write documentation | `DOCUMENTATION_GUIDE.md` |
| Cut a release | `RELEASE_PROCESS.md` |
| Understand why any of this is shaped this way | `ARCHITECTURE.md` |
