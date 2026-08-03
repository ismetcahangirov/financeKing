---
number: 0016
title: Close the container network entirely rather than filter egress, and open it per service when a caller exists
date: 2026-08-03
status: accepted
deciders: [ismetcahangirov, architect, security, devops]
supersedes: null
superseded_by: null
related_issues: ["#107", "#112", "#102"]
related_adrs: [ADR-0006, ADR-0010]
---

## Context

ADR-0006 makes the permitted-host set a compiled-in `frozenset` in `fking.platform.safety`, validated on every request. That control is correct and it is thorough, and it has one structural limit stated in its own threat model (`ARCHITECTURE.md` §8): **it protects only code that goes through it.** A `subprocess.run(["curl", ...])`, a dependency's post-install hook, and a native extension opening its own socket are all outside its reach, and the threat model here is carelessness rather than malice — this system generates its own strategy and adapter code, and generated code reaches for whatever the training data reaches for.

So #107 asks for a second control at the container layer, one that does not depend on Python being involved at all. The question this record answers is not *whether* to have one; it is *which* one, because the obvious answer collides with a property of the safety kernel.

```
Forces:
- Two controls are only worth having if they fail differently. A second control
  that a bypass of the first also bypasses is ceremony.
- Docker Compose has no per-host egress filter. Host-level filtering means a
  forward proxy, and a proxy means the application must be told to use it.
- guarded_client() is constructed with trust_env=False, deliberately: with
  trust_env on, an HTTPS_PROXY variable silently reroutes every request while
  the URL still reads testnet.binance.vision. Honouring a proxy env var is the
  exact hole the kernel closes.
- Therefore a filtering proxy requires a compiled-in proxy URL inside
  fking.platform.safety -- a safety:critical change, forced through the CI
  forced-stop gate (GIT_WORKFLOW.md §7).
- At this milestone the application makes no outbound request at all. The
  entrypoint validates configuration and resolves endpoints against the
  allowlist in memory; the venue adapters (#112), the LLM gateway and the
  archive loader do not exist.
- CLAUDE.md §9 forbids leaving an implementation that looks finished and is
  not, and §3 requires two concrete callers before an abstraction exists.

The constraint that forces a decision now:
#107 sets the posture every later service inherits. A stack that ships with a
default bridge network and gains services one at a time never acquires a closed
default -- each new service is added the way the previous one was.
```

## Decision

Every Compose service joins exactly one network, `fking_internal`, declared `internal: true`. Docker installs no default route and no external DNS forwarder on such a network, so **no container in this stack can reach anything off this host**, by name or by address, regardless of what any Python code believes.

No egress path is built now. When a service needs one, that service — and only that service — gains a second, non-internal network in the pull request that gives it a reason to, with the in-process allowlist still applying to every request it makes. The two controls then overlap on the hosts the kernel permits and the network exposes, which is the intended steady state.

`tests/infra/test_egress_policy.py` proves the property from inside the container using a raw `socket.create_connection`, with no `httpx` and no monkeypatch, because a raw socket is precisely what the bypasses in the threat model would use.

## Alternatives considered

### Alternative 1 — an allowlisted forward proxy on a separate network (strongest rejected)

**What it would have given us.** A `squid` or `tinyproxy` container on both `fking_internal` and a non-internal network, holding a host allowlist generated from `PERMITTED_HOSTS` plus the archive and provider hosts. The app keeps no route of its own; every outbound request is mediated and logged by something that is not Python and cannot be bypassed by generated code, because there is no other way out. It is the textbook answer, it produces an egress audit trail we would otherwise not have, and — crucially — it is the answer we will eventually need anyway, since the app cannot stay hermetic forever. Building it now would mean the venue-adapter pull request inherits a working, reviewed egress path instead of having to invent one under deadline.

**Why it lost.** The app cannot be told to use it without changing the safety kernel. `guarded_client()` sets `trust_env=False` on purpose, so `HTTPS_PROXY` is ignored — that is not an oversight to work around, it is the control that stops an environment variable from silently rerouting a validated host. Routing through a proxy therefore requires a compiled-in proxy URL inside `fking.platform.safety`, which is a `safety:critical` change requiring a forced-stop override and a human decision. Making that change **speculatively**, for a caller that does not exist, inverts the friction ADR-0006 exists to create: the one edit that is supposed to be expensive and deliberate would be made cheaply, in a chore pull request, to serve a hypothetical. And the proxy would be unexercised — its allowlist would be a list of hosts nothing dials, so the first time it is genuinely load-bearing is the first time anyone finds out whether it works. A closed network has no such gap: it is exercised by every container, every start, and a test that opens a socket.

The rejection is narrow and it expires. When #112 lands a venue adapter, the proxy is the likely answer and the compiled-in proxy URL is then a `safety:critical` change with a real caller, argued in a superseding ADR rather than assumed here.

### Alternative 2 — keep the default bridge and rely on the safety kernel alone

**What it would have given us.** Nothing to build, nothing to explain, and no risk of a future service failing to start because someone forgot a network declaration. The kernel already refuses every production host at the point of the request, its coverage floor is 100%, and its tests enumerate the production endpoints explicitly. Adding a network control could be argued as defence in depth against a threat the primary control already handles.

**Why it lost.** It handles it only for code that calls it, and the two most likely bypasses in this project's threat model do not: an agent-generated adapter that constructs its own client, and a transitive dependency that opens a socket during import. `import-linter` catches the first *if the import is direct and inside `src/fking`*, and catches neither a `subprocess` invocation nor anything inside a third-party package. The whole reason for a second control is that the first one's blast radius is bounded by an import graph, and a socket is not.

### Alternative 3 — do nothing

```
Cost of the status quo: every container holds a default route, so the demo-only
guarantee rests entirely on an in-process check whose own documentation names
three ways past it. #107 cannot close, and #112 and #102 both add services to a
stack whose default is open.
Why that is no longer payable: the posture a stack ships with is the posture it
keeps. Closing a network that eight services already depend on being open is a
migration; closing it while the app makes no outbound request at all is a
one-line declaration verified in an afternoon.
```

## Consequences

**What becomes easier**
- The demo-only guarantee stops depending on a single mechanism. `api.binance.com` is unreachable from the app container by name and by address, provably, and the proof runs in CI.
- The set of paths any container can persist to is enumerable from `docker-compose.yml`: a named volume or a declared `tmpfs`, with `read_only: true` everywhere else.
- A new service that forgets `networks:` lands on Compose's implicit bridge, which is not internal — and `test_every_service_joins_the_internal_network_and_nothing_else` fails on it, so the omission is loud rather than invisible.

**What becomes harder**
- Anything inside a container that expected to reach the internet fails, including a diagnostic `pip install` or `curl` run during an incident. That is the intent, and `docker run` outside the project network remains available for genuinely ad-hoc work.
- Grafana cannot fetch a plugin or check for updates. Both are already disabled by environment variables; the network now enforces what the configuration asked for.
- Every service must declare `user:`, `read_only:`, `cap_drop:` and `security_opt:`, and adding a service means finding the paths it writes. `tmpfs` on `/var/run/postgresql` was needed because Postgres exits with `could not create lock file` otherwise — an error that reads like an image bug.

**What we now cannot do**
- Reach a production exchange from any container in this stack, including read-only and including `exchangeInfo`, which is public and unauthenticated. That is the same refusal ADR-0006 makes, now enforced twice.
- Add egress in a configuration file. Opening it means editing `docker-compose.yml` to add a second network to one named service, in a reviewed diff, and the test above fails until the change is deliberate.

## What would make us revisit this

```
Trigger:   the first pull request that needs an outbound request from a
           container -- the Binance testnet adapter (#112), the LLM gateway, or
           the archive loader. Any of them makes "no egress" untenable rather
           than merely strict.
Observed:  a pull request adding a second network to any service, or adding a
           proxy URL under src/fking/platform/safety/.
Then:      Open a superseding ADR choosing between a per-service non-internal
           network and an allowlisted forward proxy with a compiled-in proxy URL.
           Do not widen fking_internal, and do not move a service to the default
           bridge.
```

## Verification

```
Confirmed if:  tests/infra/test_egress_policy.py passes on every CI run for six
               months, and zero pull requests in that window add a network to a
               service without a superseding ADR
Refuted if:    a service is moved off fking_internal to make something work, or
               the egress test is skipped, xfailed or deleted rather than
               updated alongside a superseding ADR
Checked by:    the security agent, via
               `make test ARGS="tests/infra"` with FKING_REQUIRE_DOCKER=1, and
               the CI "Container gate" job
Review date:   2027-02-03
```
