# Workflow — Deployment

One target: the local Docker Compose stack against Binance **testnet**. There is no production deployment and there will not be one.

---

## 1. Pre-flight

```bash
git status --porcelain      # clean
git log --oneline -1        # record the commit being deployed
make check                  # green, run now
```

Then read the safety kernel before deploying a build you have not read:

```bash
grep -rn "frozenset" src/fking/platform/safety/
git log --oneline -5 -- src/fking/platform/safety/
```

- Compiled-in `frozenset`, testnet hosts only
- No wildcard, no suffix match, no override flag
- Any recent commit to this path that is not labelled `safety:critical` stops the deployment

A bad deployment can be rolled back. Orders that were sent were sent.

---

## 2. Infrastructure up

```bash
make up
docker compose ps
make migrate
```

All services healthy: Postgres+TimescaleDB, Redis, OTel Collector, Prometheus, Loki, Tempo, Grafana.

**Telemetry comes up before trading, not after.** A run started with the collector down produces a gap in exactly the history the next investigation needs, and instrumentation gaps cannot be backfilled.

Migrations are forward-only. One that grants UPDATE or DELETE on an audit table does not deploy.

---

## 3. Confirm the allowlist at boot

```bash
make logs | grep -i "allowlist\|permitted host"
```

The allowlist is logged at every boot by design. Read it, do not assume it. If it is absent from the log, the safety kernel did not initialize — stop the process.

---

## 4. Credentials

- Futures user data: `listenKey`, still works.
- Spot user data: `listenKey` is **dead** — 410 Gone. Spot requires the WebSocket `session.logon` handshake with **Ed25519 keys**. Confirm the key file is mounted and readable, and that it never appears in a log line or span attribute.
- `ccxt >= 4.5.70`. Earlier versions are wrong about the endpoint split and the post-`listenKey` model.

---

## 5. Reconcile before enabling anything

```bash
python -m fking.execution.reconcile --full
```

**Binance spot testnet wipes roughly every 30 days without notice**: keys survive, balances and open orders vanish. Starting from stale local state after a wipe means believing you hold positions that do not exist and trading against a phantom book.

Exchange state is the source of truth; local converges to it. Confirm agreement on balances, open orders, and positions before continuing.

If the reconciliation shows a large unexplained drop, check for the wipe signature — keys authenticating, balances near zero, open orders gone, no corresponding fills in the audit log — before treating it as a loss.

---

## 6. Start armed, enable gradually

1. Start with the kill switch **armed** and all strategies disabled.
2. Watch one full data cycle: features compute, timestamps sane, no error-level logs.
3. Enable strategies one at a time. For each, confirm a signal is emitted and the risk engine's decision appears in the audit log with a correlation ID.
4. Release the kill switch last, deliberately, and record who released it and when.

---

## 7. Post-deploy verification

```bash
make logs | tail -100
curl -s localhost:8000/health
```

- No error-level logs in the first five minutes
- Metrics in Prometheus, traces in Tempo
- Kill switch endpoint responds and its invocation is audited
- Agent quota consumption within free-tier limits — and confirm the degraded deterministic-only path is configured, because quota exhaustion is a matter of when

---

## 8. Watch the first cycle

Specifically watch realized slippage against modelled slippage. A divergence appearing at deployment means the cost model and the venue have drifted apart, and every downstream result is suspect until that is explained. Remember the cost model is calibrated on **production** data — testnet's ~7.5bp spread against production's ~0.16bp is why testnet-calibrated numbers are fiction.

---

## 9. Rollback

```bash
make down
git checkout <previous commit>
make up && make migrate
python -m fking.execution.reconcile --full
```

Reconcile after every rollback, without exception.
