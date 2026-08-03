# Deployment

Docker Compose topology, first-time setup, backup and restore, and the pre-flight checklist.

Target: **one developer machine, zero budget, single node.** `ARCHITECTURE.md` §2 rejects microservices and §12 rejects Kubernetes on exactly this basis. That constraint is not a limitation to work around — it means the entire system must come up with `make up` and be identical to what CI runs.

The failure this document exists to prevent: *"works on my machine"* becoming a meaningful sentence.

---

## 1. What runs

| Service | Image | Purpose |
|---|---|---|
| `postgres` | `timescale/timescaledb-ha` (pg16) | Operational state, hypertables, audit tables, `pgvector` |
| `redis` | `redis` 7 | Event bus (Redis Streams) |
| `app` | built from `Dockerfile` | The modular monolith: ingestion, strategy, risk, execution, agents, API |
| `dashboard` | built from `dashboard/Dockerfile` | Next.js 15 UI |
| `otel-collector` | `otel/opentelemetry-collector-contrib` | Telemetry fan-out |
| `prometheus` | `prom/prometheus` | Metrics, 15d |
| `loki` | `grafana/loki` | Logs, 30d |
| `tempo` | `grafana/tempo` | Traces, 7d |
| `grafana` | `grafana/grafana` | Dashboards, provisioned as code |

Nine services. Adding a tenth requires justifying it against §4's memory budget, and **adding a service to reduce load on another service is forbidden** — that is microservices arriving by the back door, and `ARCHITECTURE.md` §2 already priced it.

> **Current state (2026-08-03).** Eight of the nine are defined and come up healthy. `dashboard` is absent because the Next.js application does not exist yet (#103), and shipping a service definition for an application that is not there would be a stub. `app` is present and builds, but is a **one-shot boot validator** rather than a long-running service: it runs the boot sequence that exists today — validate configuration, resolve every venue endpoint against the compiled-in allowlist, log the allowlist — and exits 0, or exits 78 and takes the stack down with it. The long-running process arrives with the runtime and event bus (#18) and the FastAPI surface (#102); the `/health/ready` health check described in §3 and its `condition: service_healthy` land in the pull request that lands the endpoint. The schema and its migrations landed with #17, but they are applied by `make migrate` rather than from the entrypoint — see §3's note on the entrypoint.
>
> The seven infrastructure services all carry health checks and reach healthy, so `make up` is unattended and all-green today. This table describes the intended end state and is the specification the compose files are written against; this note records how far along it is, so that a reader can tell a design from a claim.

### Pinning

> **Every image is pinned by digest. No `latest`, no floating minor tags, anywhere.**

```yaml
# timescale/timescaledb-ha:pg16.4-ts2.17.2 — pinned 2026-05-14
image: timescale/timescaledb-ha@sha256:9f2c4a1e8b...
```

A floating tag makes the build a function of *when* you ran it. A database image that silently minor-bumps under a TimescaleDB extension is a genuinely bad afternoon, and worse, it produces intermittent CI failures that look like flaky tests. Every digest carries a comment naming the human-readable tag and the date pinned, because a bare digest is unreadable and gets "tidied up".

The same applies to GitHub Actions: pinned by commit SHA on anything that touches secrets.

**CI and Compose run the same digests.** Tests run against real Postgres in a container (`CLAUDE.md` §5); if that container is a different build from the one `make up` starts, the tests are validating a database that does not exist.

---

## 2. The base/override split

```
docker-compose.yml                  base: every service, no ports, no dev mounts
docker-compose.override.yml         local dev: localhost ports, source bind mounts,
                                    debug log level. Loaded automatically.
docker-compose.demo.yml             demo runtime: no source mounts, restart policies,
                                    tighter limits. Explicit: -f base -f demo
docker-compose.ci.yml               CI: no observability stack, ephemeral volumes
```

Compose loads `docker-compose.yml` + `docker-compose.override.yml` automatically, so a bare `docker compose up` is the developer experience. The demo runtime requires naming files explicitly, which is deliberate friction — the mode that places orders should not be the mode you get by accident.

The base file publishes **no ports at all**. Every port binding lives in an override. That way a service is unreachable from the host unless an override deliberately exposed it, and the default is closed.

```yaml
# override files only
ports:
  - "127.0.0.1:8000:8000"
```

> **Every published port binds to `127.0.0.1` explicitly.** Never `0.0.0.0`, never a bare `"8000:8000"` (which binds all interfaces). This stack holds exchange credentials and a Grafana reachable from the local network is an exposure with no upside.

---

## 3. Topology and service definitions

### Dependency ordering by health check, not start order

> **`depends_on` uses `condition: service_healthy`, always. Never bare `depends_on`, never a sleep, never a retry loop in the entrypoint.**

Bare `depends_on` waits for the container to *start*, which for Postgres means the process exists and is not yet accepting connections. The app then fails its first query, and whatever happens next — a crash loop, a retry with backoff, a swallowed exception — is a worse version of what a health check does correctly.

```yaml
services:
  postgres:
    image: timescale/timescaledb-ha@sha256:9f2c4a1e8b...   # pg16.4-ts2.17.2, pinned 2026-05-14
    healthcheck:
      # pg_isready alone is insufficient: TimescaleDB's extension becomes
      # available a few seconds AFTER the server accepts connections, and a
      # migration run in that window fails with a misleading
      # "type does not exist". Test the thing the app actually needs.
      test: ["CMD-SHELL",
             "pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB && \
              psql -U $$POSTGRES_USER -d $$POSTGRES_DB -tAc \
              \"SELECT 1 FROM pg_extension WHERE extname='timescaledb'\" | grep -q 1"]
      interval: 5s
      timeout: 5s
      retries: 20
      start_period: 30s
    volumes:
      # PGDATA in timescaledb-ha is /home/postgres/pgdata/data, NOT the
      # /var/lib/postgresql/data of the upstream postgres image. Mounting the
      # upstream path succeeds, mounts nothing useful, and loses the database on
      # every recreate without announcing it.
      - fking_pgdata:/home/postgres/pgdata
    deploy:
      resources:
        limits: {memory: 4G, cpus: "2.0"}

  app:
    build: .
    depends_on:
      postgres: {condition: service_healthy}
      redis:    {condition: service_healthy}
      otel-collector: {condition: service_started}
    env_file: [.env]
    volumes:
      - fking_parquet:/data/parquet
      - ./secrets:/run/secrets:ro
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8000/health/ready"]
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 40s
    deploy:
      resources:
        limits: {memory: 5G, cpus: "3.0"}
```

That TimescaleDB health check is the non-obvious one and it has bitten this project: `pg_isready` returns success while the extension is still loading, migrations run, and they fail with `type "timestamptz" does not exist`-shaped errors that point nowhere near the real cause. **A health check must test the thing the app needs, not merely that a port is open.**

The `app` health endpoint is `/health/ready`, which returns 200 only after configuration validation, allowlist validation and migrations have all succeeded. A liveness endpoint that returns 200 as soon as the web server binds would let the dashboard start against an app that is about to exit 78.

`otel-collector` uses `service_started`, not `service_healthy`, deliberately: telemetry is not allowed to gate the trading system. If the Collector is unhealthy, the SDK buffers and then drops (`OBSERVABILITY.md` §2) and the app keeps working. The inverse — the app refusing to start because Grafana is unhappy — is an availability failure introduced by an observability tool.

### Named volumes

> **Every volume is named. No anonymous volumes, no bind mount for anything that must survive.**

```yaml
volumes:
  fking_pgdata:      {name: fking_pgdata}
  fking_parquet:     {name: fking_parquet}
  fking_redis:       {name: fking_redis}
  fking_prometheus:  {name: fking_prometheus}
  fking_loki:        {name: fking_loki}
  fking_tempo:       {name: fking_tempo}
  fking_grafana:     {name: fking_grafana}
```

The property this buys is the one that matters most in daily use:

> **`docker compose down` never destroys ingested history.**

Named volumes survive `down`, `down --rmi all`, container recreation, and image upgrades. They are removed only by `docker compose down -v` or an explicit `docker volume rm`.

**`make down` never passes `-v`.** There is no Makefile target that removes volumes. Destroying years of checksum-verified archives and every audit row is a deliberate act performed by typing the full command, with the volume name, on purpose.

Anonymous volumes are forbidden because they lose data silently on recreate — the container comes back, the volume is new and empty, and nothing announces it. A named volume that fails to mount produces a visible error.

`fking_parquet` deserves particular care: re-downloading years of archives is hours of work, some archives may no longer be available upstream, and every one must be checksum-verified again. It is the most expensive volume to lose and the least obviously so.

### Entrypoint

The app entrypoint does exactly two things before starting: run Alembic migrations, then start. Migrations run as the `fking_migrator` role; the application connects as `fking_app`, which has no `UPDATE`/`DELETE` on audit or memory tables (`SECURITY.md` §6).

Migrations run in the entrypoint rather than as a separate service because a separate migration service that must complete before the app starts is a health-check dependency with extra steps, and it can be skipped.

> **Current state (2026-08-03).** The migrations exist (#17) and `make migrate` applies them, but the entrypoint does not run them yet, because there is no long-running process for them to precede — the container is still a one-shot boot validator that exits 0. Wiring `alembic upgrade head` into a start that then exits would apply a schema change from a service whose logs nobody watches. The two-step entrypoint above lands with the server in #18/#102. The `fking_migrator`/`fking_app` split is created by #17's first migration as NOLOGIN group roles with grants on the audit tables only; the full privilege matrix and login roles are #106.

---

## 4. Resource limits

> **Every service has a memory limit. No exceptions.**

Loki and Prometheus will each take everything available if permitted, and they will do it during a backtest — which starves Postgres, which times out the OMS, which presents as an application bug. One unbounded container is enough to make the whole stack unreliable in a way that looks like something else entirely.

### The budget (16 GB host)

| Service | Limit | Observed p95 | Note |
|---|---|---|---|
| `postgres` | 4.0 G | 2.4 G | Source of operational truth; never squeezed for the app |
| `app` | 5.0 G | 4.6 G | Includes the DuckDB backtest scan |
| `dashboard` | 512 M | 180 M | |
| `redis` | 512 M | 90 M | `maxmemory` set below the container limit |
| `prometheus` | 1.0 G | 620 M | |
| `loki` | 1.0 G | 340 M | |
| `tempo` | 800 M | 410 M | |
| `grafana` | 512 M | 230 M | |
| `otel-collector` | 512 M | 150 M | |
| **Total allocated** | **13.8 G** | | |
| **Headroom** | **2.2 G** | | Must exceed the capped DuckDB peak |

Every limit in the Compose file carries a comment stating **the observed p95 it was derived from and the date measured.** An undocumented limit is a number someone will "tidy up" later, and the tidying will be a guess.

### The DuckDB rule

> **DuckDB's `memory_limit` and `threads` are set explicitly, and the limit must fit inside the *headroom*, not inside the host.**

`CONFIGURATION.md` §7 makes them mandatory settings. Defaults are `memory_limit=3GB`, `threads=4`.

DuckDB uses most of available memory by default. A full-universe 18-month scan over 1m Parquet peaks around **4.2 GB uncapped**. Left uncapped against 1.5 GB of real headroom, the kernel resolves the shortfall by OOM-killing the process with the highest OOM score — which is frequently **Postgres**, the worst possible choice. The symptom is a hung dashboard and Redis consumer timeouts, and both are misleading: the consumers were blocked on database writes, not on Redis.

Capping DuckDB makes the scan slower and bounded. Slower is the correct trade: **explicit limits are how you decide the outcome instead of the kernel deciding it by score.**

The corresponding rule: **never solve contention by giving the app more memory at the database's expense.** Raising the app limit in the scenario above makes it fail *sooner*, because DuckDB then has more room to consume before anything stops it.

### Retention, enforced not merely configured

| Store | Retention | Enforcement |
|---|---|---|
| Prometheus | 15d | `--storage.tsdb.retention.time=15d` |
| Loki | 30d | Compactor must actually be running; verify, do not assume |
| Tempo | 7d | Block retention in `tempo.yaml` |
| Postgres market-data hypertables | 90d hot, compressed beyond | TimescaleDB policies |
| **Postgres audit tables** | **forever** | **No policy. No compression.** |
| Parquet archives | forever | Checksum-verified; cold-archived if disk-bound |

Retention is verified by **measurement** — check the oldest record actually present, not the configured window. A configured-but-unenforced retention shows up as a full disk three months later.

**No retention policy and no TimescaleDB compression on any audit hypertable.** Compression rewrites chunks, which is mutation of append-only data under a different name, and a compressed chunk that fails to decompress is silent data loss on exactly the rows that must never be lost.

Disk stays under 70%. Order of action when it does not: expire traces, expire logs, compress non-audit hypertables, cold-archive Parquet with verified checksums, **then escalate**. Audit data is never deleted.

---

## 5. First-time setup

From a clean clone to a healthy stack. Target: **under 15 minutes, unattended after the credential steps.**

### 5.1 Prerequisites

Docker Engine 24+ with Compose v2, `make`, `git`, `uv`, and `openssl`. Nothing else. 16 GB RAM and 200 GB free disk for a useful history depth.

### 5.2 Binance spot testnet keys — GitHub OAuth

Spot testnet authentication is not an API-key form. It is a GitHub OAuth login, and the keys it produces are Ed25519-based.

1. Go to **https://testnet.binance.vision/**.
2. Click **Log in with GitHub** and authorise. There is no email signup; a GitHub account is required.
3. Generate the Ed25519 key pair **locally, first** (§5.4). Binance needs the *public* key; the private key must never leave your machine.
4. In the testnet UI, create an API key of type **Ed25519** and paste the public key in PEM form.
5. Record the **API key** string it returns. Put it in `.env` as `FKING_EXCHANGE__BINANCE__SPOT_API_KEY`.

> **Spot testnet wipes roughly every 30 days without notice. Keys survive; balances and open orders vanish.**

This is why reconciliation is a first-class feature rather than a nicety (`ARCHITECTURE.md` §7). Plan for it: the system must be able to rebuild its entire view of the world from the exchange at any moment, and a wipe is a routine event, not an incident. If balances are zero one morning, that is what happened.

Note also that `POST /api/v3/userDataStream` returns **410 Gone** everywhere — the spot `listenKey` mechanism is dead. Spot user data requires a WebSocket `session.logon` handshake with the Ed25519 key. Futures `listenKey` still works. Two genuinely different mechanisms behind one interface.

### 5.3 Binance USDⓈ-M futures testnet keys

1. Go to **https://testnet.binancefuture.com/**.
2. Log in with GitHub (same account is fine).
3. Under **API Key**, generate a key. This one is HMAC-style: you get an **API key** and an **API secret**.
4. **The secret is shown once.** Copy it immediately into `.env` as `FKING_EXCHANGE__BINANCE__FUTURES_API_SECRET`.
5. Fund the testnet account from the faucet in the UI if the balance is zero.

Futures testnet is more stable than spot and does not wipe on the same cadence, but it is still testnet: **its market data is statistically worthless** — 7.5bp spread against production's 0.16bp, ~10x inflated volume. Never calibrate anything from it (`BACKTEST_ENGINE.md` §4).

### 5.4 Generating Ed25519 keys

```bash
mkdir -p secrets && chmod 700 secrets

# Private key — never leaves this machine, never enters an environment variable.
openssl genpkey -algorithm ed25519 -out secrets/ed25519_spot.pem
chmod 600 secrets/ed25519_spot.pem

# Public key — this is what you paste into the Binance testnet UI.
openssl pkey -in secrets/ed25519_spot.pem -pubout -out secrets/ed25519_spot.pub
cat secrets/ed25519_spot.pub
```

Then in `.env`:

```
FKING_EXCHANGE__BINANCE__SPOT_ED25519_KEY_PATH=/run/secrets/ed25519_spot.pem
```

**A path, not the key material.** Environment variables are inherited by child processes, appear in `docker inspect`, and appear in `/proc/<pid>/environ`. A file has permissions.

The app **refuses to start** if the key file's mode is wider than `0600` on a POSIX filesystem (`SECURITY.md` §4.5). Not a warning — a key readable by other local accounts is a key that must be rotated, and starting anyway means the check exists only to produce a log line nobody reads.

`secrets/` is gitignored and mounted **read-only into the `app` container only.** It is never mounted into Grafana, Prometheus, Loki, Tempo or the dashboard, none of which have any reason to see exchange credentials.

### 5.5 Environment

```bash
cp .env.example .env
```

`.env.example` lists every variable with a description and a safe placeholder. It contains **no real hosts and no production URLs, not even commented out** — a commented-out mainnet URL is one uncomment away from being live.

Fill in: the four Binance testnet values, `FKING_DATABASE__DSN`, `FKING_BUS__REDIS_URL`, the Grafana admin password, and (optionally) `FKING_AGENTS__PRIMARY__API_KEY`. Agents default to disabled, so a clone with no LLM key runs.

Every application setting has a default, so **`.env` is optional** — the base file declares it `required: false` and a clean clone comes up unattended. `.env` supplies credentials and overrides, not the ability to start.

### 5.5.1 Compose-only variables

A small set of variables is read by Compose itself rather than by the application, so they are **not** in `.env.example` — that file is generated from and tested against the `Settings` model in both directions, and an entry the application does not read would fail its own test. They go in `.env` alongside the rest.

| Variable | Default | Why you would set it |
|---|---|---|
| `FKING_POSTGRES_USER` / `_PASSWORD` / `_DB` | `postgres` / `fking_local_only` / `fking` | The bootstrap superuser. Least-privilege roles are a separate concern (`SECURITY.md` §6) |
| `FKING_GRAFANA_ADMIN_USER` / `_PASSWORD` | `admin` / `admin` | Required, with no default, in the demo runtime — Compose refuses to start without it |
| `FKING_POSTGRES_PORT`, `FKING_REDIS_PORT`, `FKING_PROMETHEUS_PORT`, `FKING_LOKI_PORT`, `FKING_TEMPO_PORT`, `FKING_GRAFANA_PORT`, `FKING_OTLP_GRPC_PORT`, `FKING_OTLP_HTTP_PORT`, `FKING_OTEL_HEALTH_PORT` | 5432, 6379, 9090, 3100, 3200, 3001, 4317, 4318, 13133 | A host port already in use. Developer override only — the base file publishes nothing |

These carry the `FKING_` prefix and land in the app container through `env_file`, which looks like it should collide with `env_prefix="FKING_"` and `extra="forbid"` on the settings model. It does not: pydantic-settings only reads variables matching a declared field, and ignores the rest. Verified 2026-08-03 rather than assumed, because the failure mode — the app exiting 78 because someone moved a port — would be an unpleasant thing to discover during a first-time setup.

**Only the host port is a variable; the host interface never is.** Parameterising `127.0.0.1` would let an environment variable widen every binding to all interfaces, which is precisely what §2 forbids.

### 5.6 Bring it up

```bash
make up          # docker compose up -d
make logs        # watch until every service is healthy
make migrate     # Alembic; moves into the app entrypoint with #18
make seed        # venues and instruments for local development; idempotent
make check       # lint, format, mypy --strict, import-linter, tests
```

`make check` green from a clean clone in under 15 minutes is the reproducibility bar. If it is not green, the environment is wrong, not the tests.

### 5.7 First ingestion

```bash
make ingest SYMBOL=BTCUSDT MARKET=futures_um FROM=2024-01-01 TO=2025-01-01
```

Expect the run to report rows in / out / **rejected with reasons**, and any gaps found. A run that reports only success has hidden its rejections. Every archive is checksum-verified before it is read (`DATA_PIPELINE.md` §2); a checksum failure that survives a re-download is a data-integrity event, not a retry.

### 5.8 Verify before believing

```bash
make verify-allowlist     # boot log shows the compiled-in host set
make verify-reconstruct   # reconstruction test against a stored fill
make backtest CONFIG=configs/smoke.yaml
```

`CLAUDE.md` §7: never claim something works without having run it. That applies to a deployment as much as to a code change.

---

## 6. Backup and restore

### What is worth backing up, in order

| # | Asset | Volume | If lost |
|---|---|---|---|
| 1 | **Audit tables** | `fking_pgdata` | The system's central guarantee is gone. Unrecoverable |
| 2 | **Agent memory** (episodic + semantic) | `fking_pgdata` | Every lesson the system learned. Unrecoverable |
| 3 | **Parquet archives** | `fking_parquet` | Hours of re-download, and some archives may no longer exist upstream |
| 4 | Operational state (positions, orders) | `fking_pgdata` | Recoverable by reconciliation from the exchange |
| 5 | Metrics / logs / traces | various | Convenience data. Accept the loss |
| 6 | Grafana | `fking_grafana` | Nothing — dashboards are provisioned from code |

Rows 1–3 are backed up. Rows 4–6 are not, and being explicit about that is what keeps the backup small enough to actually run.

### Backup

```bash
make backup    # writes backups/<UTC-timestamp>/
```

1. `pg_dump --format=custom --compress=9` of the whole database. Custom format because it supports selective restore, which matters when you want audit tables back but not stale operational state.
2. `tar` of `fking_parquet` — **or, preferably, nothing**, because Parquet files carry their own verified checksums and a manifest of `(path, sha256, source_url)` is enough to reconstruct from upstream. The manifest is a few hundred kilobytes; the archive is hundreds of gigabytes.
3. SHA-256 of every artefact, written alongside.
4. The `config_hash` and git SHA of the running system, so a restore can be matched to the code that wrote it.

Backups go to a **different physical device**. A backup on the same disk as the database protects against exactly one failure mode — accidental deletion — and not the one that actually happens.

### Restore

```bash
make restore FROM=backups/2026-08-01T03-00-00Z
```

1. Stop the stack (`make down` — **never `-v`**).
2. Verify the backup's checksums before touching anything. A restore from a corrupt backup destroys a working system to install a broken one.
3. Restore Postgres into a **fresh volume**, not over the existing one, so a failed restore is a no-op rather than a loss.
4. Restore or re-download Parquet, verifying every checksum.
5. Start the stack.
6. **Reconcile against the exchange immediately.** Restored operational state is a snapshot; exchange state is the truth, and local state converges to it (`ARCHITECTURE.md` §7).
7. Run the reconstruction test against a fill from before the backup point. That is the check that the restore preserved the property the backup existed for.

### Restore is tested, not assumed

A backup that has never been restored is a hypothesis. **Quarterly: restore into a scratch stack and run the reconstruction test.** A restore procedure discovered to be broken during an incident is a restore procedure that does not exist.

### Audit data is never deleted

If disk becomes the constraint, audit rows are **archived to cold Parquet with a verified checksum** and the archive location recorded. Not deleted, not compressed in place, not truncated.

---

## 7. Pre-flight checklist before enabling any strategy

Run before the first strategy is enabled on `DemoVenue`, and again after any change to the safety kernel, the risk engine, the venue adapters, or the cost model.

**Every line is verified by running something and reading the output.** A checklist completed from memory is not a checklist.

### Safety

- [ ] Boot log shows the compiled-in allowlist, and it contains **no production trading host**.
- [ ] Every resolved endpoint in the boot log is on the allowlist.
- [ ] `lint-imports` green — `execution` imports no raw HTTP or WebSocket library.
- [ ] `platform/safety` coverage is **100%**, and the rejection-path tests are present and passing.
- [ ] `grep -rn "0.0.0.0" docker-compose*.yml` returns nothing.
- [ ] No mainnet URL anywhere in the repo, including comments and `.env.example`.
- [ ] `gitleaks` clean on full history.

### Configuration

- [ ] `make check` green on the exact commit being deployed.
- [ ] Effective config logged at boot; every risk limit **at or below** its compiled-in ceiling.
- [ ] `kill_switch_enabled` is `true` and the daily-loss threshold is what you intended.
- [ ] `require_invalidation_level` is `true`.
- [ ] Ed25519 key file mode is `0600`; the app started, which proves the check passed.
- [ ] Fee settings are VIP-0 unless a better tier is actually held.

### Data

- [ ] Coverage report shows the required window for every configured symbol, with gaps listed.
- [ ] Zero synthesised rows: the "no interpolated bars" query returns 0.
- [ ] The adversarial look-ahead test passes **and has been observed to fail** when the guard is broken on purpose.
- [ ] Live stream staleness under threshold for every symbol in the universe.

### Strategy

- [ ] Backtest `credibility == "credible"` with all seven audit checks non-inconclusive.
- [ ] `cost_model_calibration_source` does **not** contain `testnet`.
- [ ] `edge_to_cost_ratio >= 2.0`.
- [ ] Trade count ≥ 200; ≥ 30 per CPCV fold.
- [ ] Walk-forward / CPCV passed, with **purge and embargo lengths present in the report** and the embargo ≥ `max_feature_lookback + max_holding_horizon`.
- [ ] `pbo <= 0.30`.
- [ ] Deflated Sharpe reported with its trial count.
- [ ] `risk_limit_breaches == 0`.
- [ ] Held-out period status is `intact` (or the user explicitly burned it).

### Execution

- [ ] Reconciliation ran at startup and converged.
- [ ] A test order round-trips on testnet through `DemoVenue` — submit, ack, fill or cancel — with an audit row for each. **This is the only permitted "test" order and it is placed by the deployment procedure, never by a strategy or an agent.**
- [ ] User-data stream connected: spot via `session.logon`, futures via `listenKey`. **Both checked** — a failure in one tells you nothing about the other.
- [ ] Client order ID prefix set, so orders are attributable if the account is shared.

### Observability

- [ ] All nine services healthy; `docker compose ps` shows no restarts.
- [ ] Reconstruction test passes on a stored fill.
- [ ] Correlation ID present on 100% of order-path spans in a sample trace.
- [ ] Prometheus active series under budget; no unbounded label.
- [ ] All seven paging alerts loaded and evaluating; **at least one has been fired deliberately** and observed to arrive.
- [ ] Dashboards provisioned from code and rendering.
- [ ] No secret appears in a log line, span attribute, or metric label in a grep of the last hour.

That last alert item is the one people skip. An alerting pipeline that has never delivered an alert is an alerting pipeline that is presumed working. Fire one on purpose.

---

## 8. Day-2 operations

### Upgrading a service

1. Resolve the new digest: `docker buildx imagetools inspect <image>:<tag>`.
2. Update the digest **and its comment** in `docker-compose.yml`.
3. Update CI to the same digest in the same commit. They must move together.
4. `make down && make up`, watch health checks.
5. Run the reconstruction test. A Postgres upgrade that damaged an audit table must be found now, not in six months.

### Rollback

Revert the commit, `make down && make up`. Named volumes survive, so a rollback is an image change, not a data migration — **provided the migration was backward-compatible.** Migrations that drop or rename columns are avoided for exactly this reason; the additive path (add, backfill, switch reads, later remove) keeps rollback a one-command operation.

### Restarting the app safely

`make down` stops containers and leaves volumes. On restart the app re-runs migrations, revalidates config and the allowlist, reconciles against the exchange, and resumes bus consumption from the last acknowledged stream id. Every consumer is idempotent because Redis Streams delivery is at-least-once (`CLAUDE.md` §2), so replaying a few messages across a restart is correct behaviour rather than something to guard against.

**Check for resting orders after any restart.** Every working style has an abandon condition, but a resting order placed before an ungraceful stop survives on the exchange and is not owned by anything until reconciliation claims it.

### Common failures

| Symptom | Actual cause |
|---|---|
| Migration fails with a missing-type error | TimescaleDB extension not yet loaded — the health check is wrong, not the migration |
| Dashboard hangs, Redis consumers time out during a backtest | Postgres was OOM-killed. DuckDB uncapped. §4 |
| Service fails health check on startup | Read the logs before raising the timeout. Usually a migration or a volume permission problem, and it will recur |
| CI green, local red | Assume the environment differs before assuming the test is flaky. Diff versions first — nine times in ten it is a version, a locale, or a timezone setting |
| Balances are zero, keys still work | Spot testnet wiped. Expected roughly monthly. Reconcile |
| Lockfile conflict on merge | Regenerate with `uv lock`. Never hand-merge — a hand-merged lockfile is a resolution nobody computed |

---

## 9. Cross-references

| For | See |
|---|---|
| Why single-node, and when to revisit it | `ARCHITECTURE.md` §2, §13 |
| Reconciliation as a first-class feature | `ARCHITECTURE.md` §7 |
| Every setting referenced here | `CONFIGURATION.md` |
| Secret handling and key permissions | `SECURITY.md` §4 |
| Retention rationale and the audit exemption | `OBSERVABILITY.md` §2 |
| Ingestion commands, checksums, coverage | `DATA_PIPELINE.md` §2, §10 |
| Kill switch, degraded modes, recovery | `FAILSAFE.md`, `ERROR_RECOVERY.md` |
