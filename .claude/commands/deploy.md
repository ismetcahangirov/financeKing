---
description: Bring up or update the demo runtime with pre-flight safety checks and post-deploy reconciliation
argument-hint: [up|update|down|status]
allowed-tools: Read, Grep, Glob, Bash
---

Deploy action: $ARGUMENTS (default: `status`).

There is one deployment target: the local Docker Compose stack running against Binance **testnet**. There is no production deployment and there will not be one.

## 1. Pre-flight — refuse to start if any of these fail

```bash
git status --porcelain            # must be clean
git log --oneline -1              # note the deployed commit
make check                        # must be green
```

Then the safety assertions:

```bash
grep -rn "frozenset" src/fking/platform/safety/
```

- The host allowlist is a compiled-in `frozenset` containing testnet hosts only.
- No production host, no wildcard, no suffix match.
- Startup endpoint resolution aborts on a non-allowlisted host.

Deploying a build whose safety kernel you have not read is the one thing in this project that cannot be undone by a rollback, because orders sent are sent.

## 2. Bring up infrastructure

```bash
make up
docker compose ps
make migrate
```

Confirm all services healthy: Postgres+TimescaleDB, Redis, OTel Collector, Prometheus, Loki, Tempo, Grafana. A run started with telemetry down produces a gap in exactly the history the next investigation needs.

Migrations must be forward-only. A migration that grants UPDATE or DELETE on an audit table does not deploy.

## 3. Confirm the allowlist at boot

```bash
make logs | grep -i "allowlist\|permitted host"
```

The allowlist is logged at every boot by design. Read it. If it is not in the log, the safety kernel did not initialize and the process must be stopped.

## 4. Credentials and the two user-data paths

- Futures uses `listenKey` — still works.
- Spot `listenKey` is **dead**: `POST /api/v3/userDataStream` returns 410 Gone everywhere. Spot requires a WebSocket `session.logon` handshake with **Ed25519 keys**. Confirm the Ed25519 key path is mounted and readable, and that the key never appears in a log line or span attribute.
- `ccxt >= 4.5.70`. Earlier versions are wrong about both the endpoint split and the post-`listenKey` model.

## 5. Reconcile before trading, every time

```bash
python -m fking.execution.reconcile --full
```

Binance spot testnet **wipes roughly every 30 days without notice**: API keys survive, balances and open orders vanish. If the system starts from stale local state after a wipe, it will believe it holds positions that do not exist and will trade against a phantom book.

Exchange state is the source of truth; local state converges to it. Confirm the reconciliation report shows exchange-vs-local agreement on balances, open orders, and positions before enabling any strategy. A wipe looks exactly like a catastrophic loss in the equity curve — check reconciliation before reacting to a drawdown alert.

## 6. Enable strategies deliberately

Start with the kill switch armed and strategies disabled, then enable them one at a time, confirming each emits a signal and that the risk engine's decision appears in the audit log with a correlation ID.

## 7. Post-deploy verification

```bash
make logs | tail -100
curl -s localhost:8000/health
```

- No error-level logs in the first five minutes.
- Metrics arriving in Prometheus; traces in Tempo.
- The kill switch endpoint responds and its invocation is audited.

## 8. Rollback

```bash
make down
git checkout <previous commit>
make up && make migrate
python -m fking.execution.reconcile --full
```

Always reconcile after a rollback. Rolling back code does not roll back orders that were already placed.

## 9. Report

Deployed commit, service health, allowlist as logged, reconciliation result, strategies enabled, kill-switch state.
