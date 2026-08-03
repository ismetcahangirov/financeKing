# Security

Threat model, the safety kernel, secrets, audit integrity, dependencies, disclosure.

This expands `ARCHITECTURE.md` §8 and `CLAUDE.md` §0. If you read one section, read §2 — the threat model is not the one people expect, and every control below is shaped by it.

---

## 1. What this system is, and what that means for its security posture

**financeKing is a demo-only research system.** It trades exclusively against Binance testnet. It holds no customer funds, no customer data, and no real money. It runs on one developer machine, bound to localhost, behind a residential NAT.

That changes the shape of the security work substantially, and being honest about it is more useful than pretending otherwise:

**What is genuinely lower risk here**

- There is no attacker with a financial motive, because there is nothing to steal. Testnet balances are free and infinite on request.
- There is no multi-tenancy, no untrusted user input from the internet, no public attack surface. Every port binds to `127.0.0.1`.
- A total compromise of the exchange credentials costs the price of regenerating them from a web form.

**What is genuinely higher risk than the above suggests**

- **The demo-only guarantee is the entire value proposition.** A system that might trade real money is a system that cannot be run unattended, and running unattended is the point. Breaching that guarantee once destroys the property permanently, regardless of financial loss.
- **The system runs autonomously, writes its own code paths, and is edited mostly by AI agents across sessions with no shared memory.** The usual assumption that a human reviewed the change does not reliably hold.
- **It processes attacker-influenceable text.** News headlines, exchange error messages, and prior agent outputs all flow into a system that runs LLM agents unattended. Prompt injection is a live vector even without a motivated attacker, because a malformed headline can do it by accident.

So: the *asset* being protected is not money. It is **the guarantee that money is not at risk**, plus the integrity of the audit record that proves it.

This is not a claim of production-grade security, and no part of this document should be read as authorising real-money operation. `CLAUDE.md` §0 is the governing statement and this document is subordinate to it.

---

## 2. Threat model: carelessness, not malice

> **The threat is not an attacker. It is a config edit, a copied environment variable, an agent generating its own HTTP client, or a library changing a default base URL in a minor version bump.**

A guardrail that lives in configuration defends against none of those, **because configuration is precisely what changes.**

### The four canonical vectors

| # | Vector | Concrete sequence | Why config-based controls fail |
|---|---|---|---|
| 1 | **Config edit** | Someone sets `BINANCE_BASE_URL` to production to "check something", forgets, commits | A validated config field moves the failure from "wrong host" to "wrong config" — the same failure with an extra step |
| 2 | **Copied env var** | A `.env` is copied from another project, or an exchange key from a different account is pasted in | Nothing in the environment can distinguish a testnet key from a production one; only the *host* can |
| 3 | **Agent-generated HTTP client** | An LLM agent writes a data-enrichment module and reaches for `httpx.AsyncClient()` because that is the idiom in its training data | The agent has not read `CLAUDE.md`. It will do this every time unless the import itself fails |
| 4 | **Library default change** | `ccxt` minor bump changes a default base URL, or adds a new endpoint whose host differs | Nothing in this repository changed. A diff review sees nothing |

Vector 4 deserves emphasis: it is the one where **every human process succeeds and the system still ends up pointed at production.** No bad commit, no careless edit, no review failure. That is why the control must be a runtime check on every request rather than any form of review.

### Two secondary vectors

| # | Vector | Sequence |
|---|---|---|
| 5 | **Prompt injection** | A news headline or an exchange error message contains instruction-shaped text, is interpolated into a system prompt, and changes an agent's behaviour |
| 6 | **Audit mutation** | A migration adds `UPDATE` grants "for a data fix", or a cleanup script deletes rows, and the append-only property is gone before anyone notices |

### The threat model record

Every control is recorded with an honest structural/advisory classification:

```python
class ThreatModelEntry(BaseModel):
    actor: str                        # "a future refactor", "a minor version bump"
    asset: str
    vector: str
    current_control: str
    control_is_structural: bool       # config-based controls are False
    residual_risk: str
```

**Advisory controls in the safety path are findings**, not acceptable states. They carry either an explicit accepted-risk note signed off by the user, or a remediation ticket.

---

## 3. The safety kernel

`fking.platform.safety`. The demo-only guarantee, implemented structurally.

### 3.1 The compiled-in allowlist

```python
# src/fking/platform/safety/_allowlist.py
# The set of TRADING hosts this system may ever contact.
# Compiled in deliberately: not config, not env, not database, not file.
# Widening this requires a source edit and a PR labelled safety:critical.
# See CLAUDE.md §0 and ARCHITECTURE.md §8.
PERMITTED_HOSTS: Final[frozenset[str]] = frozenset({
    "testnet.binance.vision",           # spot testnet REST
    "stream.testnet.binance.vision",    # spot testnet market-data WS
    "ws-api.testnet.binance.vision",    # spot testnet WS API: session.logon
    "testnet.binancefuture.com",        # USDⓈ-M futures testnet REST
    "stream.binancefuture.com",         # USDⓈ-M futures testnet WS
    "api-testnet.bybit.com",            # fallback venue
    "stream-testnet.bybit.com",
})
```

Properties, each deliberate:

- **`frozenset`, not `set`.** A `set` can be mutated at runtime by any code holding a reference. A `frozenset` cannot, so the only way to widen it is to edit the source.
- **`Final`.** `mypy --strict` rejects rebinding.
- **A module-level constant, not a function return.** A function can read a file. A constant cannot.
- **No production hosts appear anywhere in the repository** — not in comments, not in `.env.example`, not commented out. A commented-out mainnet URL is one uncomment away from being live, and it will be uncommented by someone in a hurry.

**`data.binance.vision` is deliberately absent, and earlier drafts of this document had it here.** The public archive is the system's primary research input and there is no testnet equivalent, so it must be reachable — but not from the client that can place orders. `PERMITTED_HOSTS` is not a permission list; it is a proof about which hosts a process holding order-placement code can reach at all, and that proof is worth having only while the set is short and entirely venue endpoints. Once it also holds a data host, reviewing an addition means deciding which category the new entry falls into, and that judgement belongs to whoever wants the entry added.

So the archive host has its own literal and its own client:

```python
# src/fking/platform/safety/_archive_allowlist.py
ARCHIVE_HOSTS: Final[frozenset[str]] = frozenset({
    "data.binance.vision",              # public bulk archive, no auth (VF-013, VF-014)
})
```

`guarded_archive_client()` in `fking.platform.safety.archive` validates against this set, attaches no credential and cannot import one — `import-linter` forbids the archive modules from importing `fking.platform.config`, where every `SecretStr` lives, and forbids `fking.execution` from importing the archive modules at all. The two sets are disjoint, and each client refuses every host in the other's. Both files are covered by the `safety-kernel-diff` CI job, so widening either still requires `safety:critical`. ADR 0017 carries the argument, including what the rejected single-list design got right — which is why the *host checking* is shared (`_hostcheck.py`) even though the *allowlists* are not.

### 3.2 Per-request validation

```python
def guarded_client(*, purpose: str) -> GuardedClient: ...
```

> **The host is validated on every request, not at client construction.**

Construction-time validation checks the wrong moment. Base URLs can be overridden per call, and `ccxt` exposes exactly that: `client.urls['api'] = ...` before a call redirects a correctly-constructed client to an arbitrary host. A guard that ran at construction would see nothing.

The guard therefore hooks the request path itself — the transport layer, below any URL the caller supplies — and raises `SafetyViolation` on a non-allowlisted host. This applies identically to HTTP and WebSocket connections, including redirects: **a redirect to a non-allowlisted host is a rejection, not a follow.**

`SafetyViolation` is **unrecoverable by construction.** It must propagate. Catching it and continuing is the worst possible handling of the loudest signal the system has, and `CLAUDE.md` §4 forbids it independently:

```python
# Forbidden. This converts the system's single loudest safety signal into
# a silent None, and a later refactor will then trade on absent data.
except SafetyViolation:
    logger.warning("host not allowed, skipping")
    return None
```

Every rejection increments `fking_platform_allowlist_rejections_total` and pages (`OBSERVABILITY.md` §8).

### 3.3 Fail-closed startup

At boot, before any consumer starts:

1. Resolve every configured endpoint — exchange REST, exchange WS, archive host, LLM provider hosts.
2. Validate each against `PERMITTED_HOSTS`.
3. **Abort the process on any failure.** Not degrade, not warn — exit non-zero.
4. Log the full allowlist and every resolved endpoint at `info`.

Logging the allowlist at every boot is not ceremony. It is the artefact that lets someone answer "what could this process have contacted, on the day of that incident" from the log alone, months later, without access to the binary that ran.

The process refusing to start on invalid safety configuration is the same principle `CONFIGURATION.md` §3 applies to configuration generally: a trading system that continues after an unexpected state is more dangerous than one that stops.

### 3.4 Import-level enforcement

`import-linter` contracts, green on every commit to `main`:

- `execution` may not import `httpx`, `aiohttp`, `websockets`, `requests`, or `urllib3`.
- `data`, `agents` and `api` may not import them either, outside `platform.safety`.
- `strategy` may not import `execution` (this is a design contract, but it is also a safety one: a strategy with no path to order construction has no path to a network client either).
- `domain` imports nothing but stdlib.

`CLAUDE.md` §0 is explicit that you should not need the linter to stop you. The linter exists because vector 3 — an agent writing `httpx.AsyncClient()` because that is the idiom — is not deterred by documentation the agent did not read.

### 3.5 The deliberate absence of an override

**There is no override. No flag, no environment variable, no `--force`, no `if TESTING:` branch, no fixture that patches the guard.**

The friction is the feature. Enabling real trading requires editing `PERMITTED_HOSTS` in source and merging a PR labelled `safety:critical` with a reviewer who is not the author.

This includes tests. **Tests exercise the real guard against a fake host**, never a fake guard against a real host. A monkeypatched `guarded_client` in a test fixture is a template someone will copy into non-test code, and it removes the only coverage the guard has.

It also includes the read-only case, which is the one that comes up:

> *"Let me just check it against mainnet read-only."*

**No.** Read paths become write paths during refactors. A function that fetches a price today is a function that someone extends to place an order in eight weeks, and the host it points at will not be revisited. There is no read-only exception, no price-sanity-check exception, and no status-page exception.

### 3.6 Coverage

`platform/safety` has a **100% coverage floor** (`CLAUDE.md` §5), and the tests assert that the bad thing **raises**. A test that a permitted host is accepted proves almost nothing. The tests that matter:

- An unlisted host raises on every request, not only the first.
- A per-call URL override to an unlisted host raises.
- A redirect to an unlisted host raises rather than being followed.
- A subdomain of an allowed host (`evil.testnet.binance.vision`) raises — matching is exact, never suffix-based.
- A host that differs only by unicode homoglyph or trailing dot raises.
- Startup aborts when a configured endpoint is not allowlisted.
- `PERMITTED_HOSTS` is a `frozenset` and cannot be mutated at runtime.

---

## 4. Secrets

### 4.1 What secrets exist

| Secret | Purpose | Rotation |
|---|---|---|
| Binance spot testnet Ed25519 private key | WebSocket `session.logon` for spot user data | On suspicion; free to regenerate |
| Binance spot testnet API key | Paired with the Ed25519 key | On suspicion |
| Binance futures testnet API key + secret | HMAC-signed futures REST and `listenKey` | On suspicion |
| Bybit testnet key + secret | Fallback venue | On suspicion |
| Gemini API key | LLM provider | On suspicion |
| Groq API key | LLM fallback | On suspicion |
| Postgres password | Local database | On stack rebuild |
| Grafana admin password | Local dashboards | On stack rebuild |

All of these are free to regenerate, which makes rotation cheap and therefore the correct first response to any suspicion. **Rotate first, scrub second.** Rotation is the fix; scrubbing git history is hygiene.

### 4.2 `SecretStr` everywhere

Every secret is typed `pydantic.SecretStr` on the settings model. Never `str`.

```python
class BinanceSettings(BaseSettings):
    futures_api_key: SecretStr
    futures_api_secret: SecretStr
    spot_ed25519_key_path: Path            # a path, not the key material
```

What this buys concretely:

- `repr()` and `str()` render `SecretStr('**********')`. An accidental f-string, a traceback with locals, a `print` during debugging, and a Pydantic validation error message all leak nothing.
- `model_dump()` excludes the value unless `.get_secret_value()` is called explicitly. The full effective config can therefore be logged at boot (`CONFIGURATION.md` §4) without a redaction pass that someone must remember to maintain.
- Retrieving the raw value requires writing `.get_secret_value()`, which is greppable. Every call site is auditable with one command, and there should be very few.

**No secret is ever placed on a Pydantic model that gets serialised to the API**, even as a `SecretStr` — FastAPI response models are a different serialisation path and the safe assumption is that anything on a response model reaches a browser.

### 4.3 `.env` hygiene

- `.env` is gitignored. `.env.example` is committed, is generated from the settings tree, and lists **every** variable the application reads — verified in both directions by `tests/platform/config/test_env_example.py`, so it cannot fall behind the model or accumulate keys nothing reads. Values are **blank**, not plausible placeholders: a placeholder shaped like a credential is a credential somebody pastes over with a real one and then commits, which is vector 2 in §2. Descriptions are per section, and the field docstrings in `fking.platform.config.settings` are the single source of truth for the rest — a second copy of 150 descriptions is a second copy that diverges.
- `.env.example` contains **no real hosts, no real keys, and no production URLs**, not even commented out.
- `.env` is loaded only by `pydantic-settings` at startup. Nothing reads `os.environ` at call time.
- File mode `0600` where the OS supports it. Checked at startup and warned on.
- **Docker Compose reads secrets from `.env` via `env_file`. Never inline in `docker-compose.yml`**, because Compose files are committed and `.env` is not.
- `.env` is never mounted into the observability containers. Grafana, Prometheus, Loki and Tempo have no reason to see exchange credentials, and a compromised or misconfigured Grafana plugin is a realistic way for an environment to leak.

### 4.4 gitleaks

`gitleaks` runs in three places, because each catches a different failure:

| Where | Catches |
|---|---|
| Pre-commit hook | The commit that would have introduced it — the only place where the fix is free |
| CI on every PR | A commit made with hooks bypassed or on a machine without them |
| Scheduled full-history scan (weekly) | A secret introduced before the rule existed, or by a merge |

Config lives in `.gitleaks.toml` with rules for Binance API key shapes (64-char alphanumeric), Ed25519 PEM headers, and generic high-entropy strings. **The allowlist for false positives is per-rule and per-path, never global.** A global allowlist entry disables the scanner for everything that resembles the exempted pattern.

A gitleaks hit in CI **blocks the merge**. A gitleaks hit on `main` is an incident: rotate the credential, then scrub.

### 4.5 Ed25519 keys

Binance spot user data requires a WebSocket `session.logon` handshake with Ed25519 keys, because `POST /api/v3/userDataStream` returns **410 Gone** everywhere. Futures still uses `listenKey`. These are genuinely different mechanisms and are modelled as such (`ARCHITECTURE.md` §7).

Handling rules:

- **The private key is read from a file, never from an environment variable.** Environment variables are inherited by child processes, appear in `docker inspect`, appear in `/proc/<pid>/environ`, and are trivially dumped by any code running in the process. A file has permissions.
- **Permissions are checked at load time.** Mode wider than `0600` on a POSIX filesystem → **refuse to start.** Not a warning. A key readable by other local accounts is a key that must be rotated, and starting anyway means the check exists to produce a log line nobody reads.
- The key material is loaded into a `cryptography` private-key object and the raw bytes are dropped. It is never held as a `str`, never on a Pydantic model, never in `SecretStr` (which stores a `str` and can be coerced to one).
- **Signing happens in one function.** The key object does not leave the module that loaded it.
- Keys live in `secrets/` — gitignored, mounted read-only into the app container, and never into any other container.
- Public key registration with Binance is a manual, one-time, human step (`DEPLOYMENT.md` §5). It is deliberately not automated; automating credential provisioning creates a code path that provisions credentials.

### 4.6 Where a secret must stop

Follow every secret from environment → config object → client → request → log → span → metric → prompt → audit row, and find where it stops. It must stop before the logger.

| Sink | Rule |
|---|---|
| Log line | Never. Allowlist-based redaction in the pipeline (`OBSERVABILITY.md` §7) |
| Span attribute | Never. Tempo has no meaningful access control and 7-day retention |
| Metric label | Never — and it would be unbounded cardinality besides |
| LLM prompt | **Never.** A secret sent to a provider has left the trust boundary permanently |
| Audit row | Never. Audit rows are forever, which makes them the worst place to leak |
| Exception message | Never — `SecretStr` handles this, which is why the type is mandatory rather than a convention |
| HTTP header on an outbound request | Yes, and **headers are never serialised into any log record**, allowlisted or otherwise |

---

## 5. Input validation

Everything crossing a boundary is hostile until parsed and validated.

**Exchange responses.** Parse and validate; never index optimistically. `data["result"][0]["price"]` on an error response raises a `KeyError` or `IndexError` at a place that gives no clue what the exchange actually said. Every response goes through a Pydantic model, and the **raw body is recorded in the audit row before parsing** — the raw response is the only artefact that remains useful six months later when the same code path breaks differently.

**Agent outputs.** Schema-validated into typed structures. An unparseable response is a failure, not something to interpret charitably (`CLAUDE.md` §10). There is no free-text fallback, ever — see `PROMPT_LIBRARY.md` §3.

**Prompt injection.** Untrusted text is never interpolated into a system prompt. A strategy's `rationale`, a previous agent's output, an exchange error message, a news headline — all attacker-influenced in a system that runs unattended. They go into clearly delimited user-role content with an explicit instruction that content inside the delimiters is data to analyse, never instructions to follow. **Injection probes are part of the golden set and must be resisted 100%** (`PROMPT_LIBRARY.md` §4). A probe that succeeds stops everything.

**API input.** The FastAPI application binds to `127.0.0.1` only and validates every request body through Pydantic. It exposes no endpoint that can place an order, widen a limit, or alter the allowlist — the API is an observation surface, and its write surface is limited to lifecycle actions that are themselves gated by the deterministic engines.

---

## 6. The audit trail

`CLAUDE.md` §2: **audit tables are append-only, enforced by the database.** An audit log the application can rewrite is not an audit log.

### Three controls, because one will be lost

1. **`REVOKE UPDATE, DELETE`** from the application role on every audit and memory table.
2. **A rule or trigger** that raises on `UPDATE` and `DELETE`.
3. **A CI test** asserting the mutation attempt **raises** — not that the row is unchanged.

Belt, braces, and a third thing, because a future migration will forget one of them. The CI test is the control that survives a migration that drops a trigger, and it is the reason the test asserts a raise rather than an absence of change: a test that checks the row is unchanged passes when the mutation silently no-ops.

### Additional properties

- `created_at` is assigned by the database (`DEFAULT now()` on `timestamptz`), never by the client. A client clock must not be able to reorder history.
- Audit writes participate in the **same transaction** as the state change they describe.
- Corrections are **new rows with `supersedes: UUID`**, never edits. `MEMORY_SYSTEM.md` §5 covers the supersession chain for memory; the same rule governs audit corrections.
- No retention policy, no TimescaleDB compression on any audit hypertable (`OBSERVABILITY.md` §2).
- **Any successful mutation of an audit or memory row escalates to the user immediately.** It means database-level enforcement was bypassed, and the audit property of the whole system is in question.
- An `UPDATE` or `DELETE` grant appearing in a migration blocks the merge and escalates.

---

## 7. Dependency security

| Control | Detail |
|---|---|
| Single resolver | `uv`. No `pip install` outside it, no hand-edited `uv.lock` |
| Full lock | `uv.lock` is committed and is the single source of dependency truth |
| Image pinning | Every container image pinned **by digest**, with a comment naming the tag and the date pinned. No `latest`, ever |
| Action pinning | GitHub Actions pinned by commit SHA on anything that touches secrets |
| Advisory scanning | `pip-audit --strict` against the set exported from `uv.lock`, in the pull-request gate **and** nightly (`make audit` runs the same command locally) |
| Scheduled scanning | `.github/workflows/nightly-security.yml` at 03:17 UTC. A finding opens or comments on a single `status:needs-human` issue rather than filing one per night |
| `ccxt` floor | `>= 4.5.70`. Currently the only client correct on both the endpoint split and the post-`listenKey` user-data model |
| Major-version policy | A `ccxt` major bump **escalates**. Its correctness on current Binance reality is why it was chosen and cannot be assumed across majors |

The supply-chain concern here is not a targeted attack. It is vector 4 again: a transitive dependency changing a default that quietly alters where a request goes. That is also why the per-request host guard is the real control and dependency pinning is only a way to reduce how often it has to fire.

**The nightly run is the half that matters.** A pull-request gate catches what a diff introduces; it cannot catch an advisory published tomorrow against a dependency that is already locked, which is the more common shape — a `ccxt` advisory lands against a floor of `>= 4.5.70` without anybody touching a file in this repository. The nightly result is an issue rather than a red check because nobody is watching a scheduled workflow at 03:17, and because the decision it needs — upgrade, pin, or accept with a written reason — is a human one.

The `binance-*` official SDKs shipped 11 and 16 major versions in roughly twelve months (`ARCHITECTURE.md` §7). That release cadence is itself a supply-chain risk for unattended operation, and it is a stated reason they were rejected.

---

## 8. Access control

- **Every port binds to `127.0.0.1` explicitly.** A Grafana or dashboard on `0.0.0.0` is exposed to the local network, and this stack holds exchange credentials. `DEPLOYMENT.md` §3.
- **Three database roles, three connection strings, and configuration refuses a shared one.** See §8.1.
- Grafana is not anonymous; the admin password comes from `.env`.
- No service in the Compose stack is published to the host except through localhost bindings.
- **Every container's root filesystem is read-only, runs as a declared non-root user, drops every capability, and sets `no-new-privileges`.** Every writable path is a named volume or an enumerated `tmpfs`. `secrets/` is mounted read-only into the app container alone.

### 8.1 Database roles

Privileges are held by three `NOLOGIN` **group** roles; processes connect as `LOGIN` roles that are members of them. The split exists because a credential and a privilege matrix change at different rates: a password is rotated, revoked or duplicated per service, while the matrix changes only when the schema does.

| Group role | Login role | Holds |
|---|---|---|
| `fking_migrator` | `fking_migrator_login` | **Owns every object.** No grants — an owner's rights are not expressible as one. |
| `fking_app` | `fking_app_login` | `SELECT, INSERT` on the twelve append-only tables; full DML on mutable operational state; `SELECT` only on market data; nothing at all on the feature store. |
| `fking_ingest` | `fking_ingest_login` | Writes market data. Cannot read the audit log. |

Four properties are worth stating because none of them is visible from the grants alone:

**Ownership is separate from privilege, and that is the point.** A table's owner may `ALTER TABLE ... DISABLE TRIGGER` regardless of what has been revoked from it. If `fking_app` owned `audit_log` it could switch off the append-only guard in one statement while every grant in the catalogue still read as correct. Ownership is the privilege people forget to check, so it is asserted over the whole catalogue rather than over the two tables that obviously matter.

**`TRUNCATE` is why the grant is the primary control and the trigger is the backstop**, not the other way round: `TRUNCATE` fires no row trigger and can only be stopped by a revoked grant (`.claude/rules/append-only-audit.md`).

**The login roles are created without a password**, so a database that has been migrated but not provisioned refuses connections rather than accepting them on an empty credential. `python -m fking.platform.persistence provision-roles --admin-dsn ...` applies the credentials the configured DSNs already carry. A password in a migration would be a secret in version control, identical on every deployment and unrotatable without a new migration.

**Every table carries a declared privilege class**, and a table with none fails the test suite. That is the mechanism, rather than per-table grants written one migration at a time: a missing grant fails loudly at the first query, but an *extra* one never fails at all, and neither shows up in review because review sees the grants that exist and not the ones that do not. `src/fking/platform/persistence/privileges.py` is the specification; migration `0008` applies it; `tests/platform/persistence/test_privileges.py` asserts the live catalogue still equals it.

The one thing this cannot defend against is a single connection string copied into all three environment variables during a deploy — nothing fails, because the over-privileged connection serves every query correctly. Configuration validation refuses it at boot, which is the only moment it is observable.

### 8.2 The container network is the second control

Every service joins one `internal: true` network and no other, so no container can reach anything off this host — by name or by address. The safety kernel (§3) is an in-process control and protects only code that goes through it; its own threat model names a `subprocess` calling `curl`, a post-install hook and a native extension opening a socket as outside its reach. This is the control that does not depend on Python, and the value is that the two fail differently: a bypass of one is not a bypass of both.

There is no egress path today and adding one is a per-service change reviewed on its own merits. `docs/adr/0016` records the decision and argues the rejected alternative — an allowlisted forward proxy — at full strength, including why it becomes the right answer the moment a caller exists.

`tests/infra/test_egress_policy.py` proves the property by opening a raw socket from inside the app container: no `httpx`, no `guarded_client`, no monkeypatch, because a raw socket is exactly what the bypasses in the threat model would use. It asserts an internal service *is* reachable in the same run, so a green result cannot come from a broken container.

---

## 9. Review triggers

A change gets a security review when it:

- touches `fking/platform/safety` in any way — **`safety:critical` label required, reviewer must not be the author**
- constructs, wraps or configures a network client
- reads an environment variable or adds a settings field
- handles exchange credentials or key material
- adds or alters DDL on an audit or memory table
- adds an LLM prompt, or changes what text reaches one
- adds a dependency, or changes a pin
- weakens or removes an `import-linter` contract
- attaches a service to a second network, or relaxes `read_only`, `user`, `cap_drop` or `security_opt` on any container

Findings state the **exploit path**, not the category. "Secrets in logs" is not actionable. "`OrderRequest.model_dump()` at `execution/oms.py:212` includes `api_key`, is passed to `logger.info` at line 219, and Loki retains it for 30 days" is.

### Immediate escalation, review incomplete

- Any change that would widen the allowlist, add an override, or make host validation conditional.
- A secret found in git history, a log, a span, a prompt, or an audit row.
- `import-linter` contracts weakened in the same PR that adds a network call.
- A service moved off `fking_internal`, or `fking_internal` itself losing `internal: true`, without a superseding ADR.
- An `UPDATE`/`DELETE` grant on an audit or memory table in a migration.
- An injection probe succeeding.
- A dependency in the execution path with a known-exploited advisory and no patched version.

**When you cannot determine whether a path is guarded, treat it as unguarded.** Uncertainty about the safety kernel resolves toward blocking.

---

## 10. Responsible disclosure

This is a personal research project with no production deployment, no users, and no data of consequence. It is nonetheless open source, and a real report deserves a real process.

**Report to:** the repository's GitHub Security Advisories ("Report a vulnerability"), which is private by default. Do not open a public issue for a vulnerability.

**Include:** the exploit path as a concrete sequence, the affected commit or version, and what an attacker gains. A category name without a path cannot be triaged.

**Expect:** acknowledgement within 7 days, an assessment within 14, and a fix or an explicit "won't fix, here is why" within 30 for anything rated high or critical. There is no bounty; there is no budget for one.

**Severity here is measured against the guarantee, not against money.** A path by which the system could contact a non-allowlisted host is **critical** even though it moves no funds, because it breaks the property the project exists to demonstrate. A leaked testnet API key is **low** — it is free to rotate and grants access to fake money — though it is still fixed and rotated promptly, because a habit of tolerating leaked credentials does not stay confined to the harmless ones.

**Out of scope:** anything requiring local access to the developer machine (the system is single-user and localhost-bound by design), testnet funds, denial of service against a self-hosted single-node stack, and missing hardening on services that are not network-reachable.

---

## 11. Cross-references

| For | See |
|---|---|
| The prime directive and its corollary | `CLAUDE.md` §0 |
| Why the safety kernel is structural | `ARCHITECTURE.md` §8 |
| Redaction pipeline and log field allowlist | `OBSERVABILITY.md` §7 |
| Config layering, startup validation, hard ceilings | `CONFIGURATION.md` §3, §8 |
| Testnet key acquisition and Ed25519 generation | `DEPLOYMENT.md` §5 |
| Untrusted text, delimiters, injection probes | `PROMPT_LIBRARY.md` §4 |
| Tool permissions and the irreversibility rule | `TOOLS.md` §2 |
| What the AI layer may never be permitted to be | `AI_MANIFEST.md` |
