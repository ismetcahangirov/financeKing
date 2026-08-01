---
description: Security review — safety kernel allowlist integrity, HTTP client construction, secrets, audit immutability
argument-hint: [path-or-pr-number]
allowed-tools: Read, Grep, Glob, Bash
---

Security review of $ARGUMENTS (default: the working diff against `main`).

The threat model here is not an attacker. It is a config edit, a copied environment variable, an LLM agent generating its own HTTP client, and a library changing a default base URL in a minor version bump. Review for those.

## 1. Safety kernel allowlist is intact — do this first

```bash
git diff main...HEAD -- src/fking/platform/safety/
cat src/fking/platform/safety/allowlist.py 2>/dev/null || ls src/fking/platform/safety/
```

Verify all of the following. Any single failure is a blocking finding:

- The permitted-host set is a **`frozenset` literal compiled into the module**. It must not be read from config, environment, a database, a file, a CLI argument, or assembled at runtime.
- No production Binance or Bybit host has been added. Testnet hosts only.
- No entry is a wildcard, a suffix match, or a regex. Host comparison must be exact-match against the frozenset, not `endswith` and not `in`.
- No `--force`, `ALLOW_LIVE`, `DRY_RUN=false`, `unsafe_`, `bypass`, or environment-conditional branch anywhere in the module.
- No test monkeypatches the allowlist to a wider set. A test fixture that widens it re-creates the hole it is testing for; tests must inject a *narrower* fake host set, never a broader one.
- `guarded_client()` validates the host on **every request**, not only at construction. Base URLs can be overridden per call, so a construction-time-only check is no check at all. Read the request path and confirm.
- Startup endpoint resolution still aborts on a non-allowlisted host, and still logs the allowlist at boot.

```bash
git log --oneline -- src/fking/platform/safety/
```

Any commit touching this path that is not labelled `safety:critical` is a process finding.

## 2. No new direct HTTP/WebSocket client construction

```bash
grep -rn "httpx\.\|aiohttp\.\|websockets\.\|requests\.\|ClientSession\|AsyncClient\|websocket_connect" src/fking/ --include=*.py
```

Every hit outside `src/fking/platform/safety/` is a finding. The execution path must obtain clients from `guarded_client()` only. Then confirm the static contract still exists and still runs:

```bash
grep -rn "httpx\|aiohttp\|websockets\|requests" .importlinter setup.cfg pyproject.toml 2>/dev/null
make check
```

An `import-linter` contract that was weakened, renamed, or moved to `ignore_imports` in this diff is a blocking finding regardless of the stated reason.

Also check that `ccxt` is constructed with the guarded transport injected rather than letting it build its own session — `ccxt` will happily create an `aiohttp` session with its own default base URL.

## 3. Secrets

```bash
git diff main...HEAD | grep -inE "api[_-]?key|secret|token|passwd|password|private[_-]?key|BEGIN .*PRIVATE KEY|[A-Za-z0-9+/]{40,}={0,2}"
grep -rn "getenv\|environ" src/fking/ --include=*.py | head -40
ls -la .env* 2>/dev/null; grep -n "^\.env" .gitignore
```

- No credential literal in source, test, fixture, notebook, or Compose file.
- The Ed25519 private key used for the spot `session.logon` handshake is loaded from a path or secret store, never inlined, never logged, never included in an error message or a span attribute.
- No secret reaches the telemetry pipeline. Confirm log/span redaction covers the key names above.
- Agent prompt/response audit logging is full-fidelity by design — verify it cannot capture credentials, since those rows are append-only and cannot be scrubbed later.

## 4. Untrusted input boundaries

- **Exchange responses are hostile input.** No optimistic indexing into a response dict, no `float()` on a price string, no assumption a field exists. Parse and validate, then trust internally.
- **Agent output is untrusted.** Every LLM response parsed into a schema-validated typed structure; an unparseable response is a failure, never charitably interpreted. No agent output ever reaches `eval`, `exec`, a shell, an f-string SQL query, or a file path join.
- **API layer**: authn on every non-public route; the kill switch endpoint must be authenticated and must itself be audited.

## 5. Audit immutability

```bash
grep -rn "UPDATE\|DELETE" migrations/ src/fking/platform/persistence/ --include=*.py --include=*.sql | head -30
```

Audit tables must be append-only **enforced by the database** — a revoked UPDATE/DELETE grant or a rejecting trigger, not application discipline. Any new migration that grants UPDATE or DELETE on an audit table, or drops such a trigger, is a blocking finding. An audit log the application can rewrite is not an audit log.

## 6. Dependency and supply chain

```bash
uv lock --check
uv pip list --outdated 2>/dev/null | head -20
```

Flag any new dependency added in this diff that performs network I/O, and check whether it was actually needed. Pin `ccxt >= 4.5.70` — earlier versions are wrong on the current Binance endpoint split and user-data model.

## 7. Report

List findings as **Blocking / Should fix / Note**, each with file:line and the concrete failure it enables. If the allowlist and `guarded_client()` are intact and no new direct client construction was introduced, say so explicitly — that sentence is the point of the review.
