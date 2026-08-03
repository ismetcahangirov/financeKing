"""The container and network posture, asserted mechanically. #107.

`fking.platform.safety` validates hosts inside the Python process. It is the right
control and it is thorough, but it protects only code that goes through it -- a
subprocess calling curl, a dependency's post-install hook and a native extension
opening its own socket are all outside its reach (`ARCHITECTURE.md` section 8). The
Compose topology is the control that does not depend on Python, and these tests are
what stop it from decaying into a comment.

Parsed from YAML rather than grepped, for the same reason as
`test_compose_contract.py`: a reformat or a line wrap must not be able to make a
violation invisible.

Each invariant here corresponds to a failure that is silent when introduced:

- a service with no `user:` inherits whatever the next image rebuild decides, and a
  root process with a bind mount is one `docker exec` away from the Postgres data
  directory -- which is where the append-only guarantee's grants and triggers live;
- a service on a second, non-internal network has egress that nothing announces, and
  the safety kernel becomes the only control again;
- a writable root filesystem lets an agent-authored dependency persist across a
  restart, which is the difference between a bad afternoon and a reinstall;
- `no-new-privileges` missing means a setuid binary inside the image can still
  escalate, which turns "runs as uid 1001" into a statement about the first
  instruction rather than about the process.

`docs/adr/0016` records why the network is closed rather than filtered.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

BASE_FILE: Final[Path] = REPO_ROOT / "docker-compose.yml"
OVERRIDE_FILE: Final[Path] = REPO_ROOT / "docker-compose.override.yml"
DEMO_FILE: Final[Path] = REPO_ROOT / "docker-compose.demo.yml"

COMPOSE_FILES: Final[tuple[Path, ...]] = (BASE_FILE, OVERRIDE_FILE, DEMO_FILE)

# The single network every service joins. It is `internal: true`, so Docker installs
# no default route and no external DNS forwarder on it.
INTERNAL_NETWORK: Final[str] = "fking_internal"

# Spellings of root. `user:` accepts a name, a uid, or a uid:gid pair, so a check for
# the string "root" alone misses "0" and "0:0" -- which is the spelling someone
# reaches for precisely because it does not look like the word.
ROOT_USERS: Final[frozenset[str]] = frozenset({"root", "0", "root:root", "0:0"})

# grafana runs as 472:0. Group 0 is the image's own choice -- the directories it
# writes are group-owned by root and group-writable -- and it is not a privilege:
# with `cap_drop: ALL` and `no-new-privileges` there is nothing attached to the gid.
# Recorded here so the exception is a decision rather than a gap in the check.
ROOT_GROUP_PERMITTED: Final[Mapping[str, str]] = {
    "grafana": (
        "uid 472 with gid 0. The image group-owns /var/lib/grafana to root and makes "
        "it group-writable; 472:472 makes Grafana fail to write its own database. "
        "cap_drop ALL plus no-new-privileges means the gid carries no privilege."
    ),
}

NO_NEW_PRIVILEGES: Final[str] = "no-new-privileges:true"


def _load(path: Path) -> Mapping[str, object]:
    parsed: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{path.name} did not parse as a YAML mapping"
    return {str(key): value for key, value in parsed.items()}


def _services(path: Path) -> Mapping[str, Mapping[str, object]]:
    block: object = _load(path).get("services")
    assert isinstance(block, dict), f"{path.name} has no services mapping"
    services: dict[str, Mapping[str, object]] = {}
    for name, definition in block.items():
        assert isinstance(definition, dict), f"{path.name}: service {name!r} is not a mapping"
        services[str(name)] = {str(key): value for key, value in definition.items()}
    return services


def _base_service_names() -> Sequence[str]:
    return sorted(_services(BASE_FILE))


def _string_list(definition: Mapping[str, object], key: str) -> Sequence[str]:
    value: object = definition.get(key)
    if value is None:
        return ()
    assert isinstance(value, list), f"{key} must be a list, got {type(value).__name__}"
    return tuple(str(entry) for entry in value)


# ---------------------------------------------------------------------------
# The network is closed
# ---------------------------------------------------------------------------


def test_the_only_declared_network_is_internal() -> None:
    """`internal: true` is the whole out-of-process control.

    Without it Docker attaches a default gateway and forwards DNS, and every
    container can reach the internet regardless of what any Python code believes.
    """
    declared: object = _load(BASE_FILE)["networks"]
    assert isinstance(declared, dict)
    assert set(declared) == {INTERNAL_NETWORK}, "a second network is a second egress path"
    spec: object = declared[INTERNAL_NETWORK]
    assert isinstance(spec, dict)
    assert spec.get("internal") is True, f"{INTERNAL_NETWORK} must be internal: true"


@pytest.mark.parametrize("service", _base_service_names())
def test_every_service_joins_the_internal_network_and_nothing_else(service: str) -> None:
    """A service that omits `networks:` lands on Compose's implicit default bridge,
    which is not internal. The omission looks like nothing and grants egress.
    """
    networks = _string_list(_services(BASE_FILE)[service], "networks")
    assert networks == (INTERNAL_NETWORK,), f"{service} declares networks {networks}"


def test_no_override_adds_a_network() -> None:
    """The override files exist to publish ports and change log levels. A network
    added there would open egress in a file nobody reads for that reason.
    """
    offending = [
        f"{path.name}: {name} -> {_string_list(definition, 'networks')}"
        for path in (OVERRIDE_FILE, DEMO_FILE)
        for name, definition in _services(path).items()
        if "networks" in definition
    ]
    assert offending == []
    for path in (OVERRIDE_FILE, DEMO_FILE):
        assert "networks" not in _load(path), f"{path.name} declares a top-level network"


def test_postgres_and_redis_are_reachable_only_on_the_internal_network() -> None:
    """The two stores that hold every audit row and every unacknowledged event.

    The base and demo files publish nothing at all; the developer override binds
    them to loopback, which `test_compose_contract.py` asserts separately. What this
    test adds is that neither can be reached from off-host by any route, because
    neither sits on a network that has one.
    """
    for service in ("postgres", "redis"):
        assert _string_list(_services(BASE_FILE)[service], "networks") == (INTERNAL_NETWORK,)
        for path in (BASE_FILE, DEMO_FILE):
            assert "ports" not in _services(path)[service], f"{path.name}: {service} publishes"


# ---------------------------------------------------------------------------
# The processes are unprivileged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service", _base_service_names())
def test_every_service_declares_a_non_root_user(service: str) -> None:
    """Declared here even where the image already sets USER.

    That is not redundancy: an image rebuild that dropped its USER would otherwise
    be invisible, and `redis` is the one image in this stack that has no USER at all
    -- its entrypoint gosu-drops, which means PID 1 and every later `docker exec`
    are root.
    """
    definition = _services(BASE_FILE)[service]
    user: object = definition.get("user")
    assert user is not None, f"{service} does not declare user:"
    spelled = str(user).strip()
    assert spelled.lower() not in ROOT_USERS, f"{service} runs as root: user: {spelled!r}"
    assert not spelled.startswith("0:"), f"{service} runs as uid 0: user: {spelled!r}"
    if spelled.endswith(":0"):
        assert service in ROOT_GROUP_PERMITTED, (
            f"{service} uses gid 0; if that is deliberate it needs its reason recorded "
            f"in ROOT_GROUP_PERMITTED"
        )


@pytest.mark.parametrize("service", _base_service_names())
def test_every_service_drops_every_capability(service: str) -> None:
    """Nothing in this stack binds a privileged port or manipulates the network.

    Dropping the lot rather than enumerating what to drop means a capability added
    to Docker's default set in a future release is not silently inherited.
    """
    assert _string_list(_services(BASE_FILE)[service], "cap_drop") == ("ALL",)


@pytest.mark.parametrize("service", _base_service_names())
def test_every_service_forbids_privilege_escalation(service: str) -> None:
    """Without this, a setuid binary inside the image can still escalate, and
    `user: 1001` describes only the first instruction the container executes.
    """
    security_opt = _string_list(_services(BASE_FILE)[service], "security_opt")
    assert NO_NEW_PRIVILEGES in security_opt, f"{service} security_opt is {security_opt}"


@pytest.mark.parametrize("service", _base_service_names())
def test_every_service_has_a_read_only_root_filesystem(service: str) -> None:
    """Every writable path is either a named volume or a declared tmpfs.

    The property this buys is that nothing an agent-authored dependency writes at
    runtime survives a restart, and that the set of paths the stack can persist to
    is enumerable from this file rather than discoverable only by inspecting a
    running container.
    """
    assert _services(BASE_FILE)[service].get("read_only") is True, (
        f"{service} has a writable root filesystem"
    )


@pytest.mark.parametrize("service", _base_service_names())
def test_no_service_is_privileged_or_widens_capabilities(service: str) -> None:
    definition = _services(BASE_FILE)[service]
    assert definition.get("privileged") is not True, f"{service} is privileged"
    assert "cap_add" not in definition, f"{service} adds capabilities"


# ---------------------------------------------------------------------------
# The resource budget is explicit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service", _base_service_names())
def test_every_service_declares_both_memory_and_cpu_limits(service: str) -> None:
    """`test_compose_contract.py` covers the memory budget's arithmetic. This covers
    the half that is usually forgotten: an unbounded CPU share lets a backtest
    starve Postgres, which times out the OMS, which presents as an application bug.

    Both live under `deploy.resources.limits` rather than the older `mem_limit`/
    `cpus` keys. Compose v2 enforces `deploy` outside swarm, and declaring both
    forms makes Compose warn about a duplicate declaration -- a warning nobody
    reads is worse than one spelling.
    """
    deploy: object = _services(BASE_FILE)[service].get("deploy")
    assert isinstance(deploy, dict), f"{service} declares no deploy block"
    resources: object = deploy.get("resources")
    assert isinstance(resources, dict), f"{service} declares no deploy.resources"
    limits: object = resources.get("limits")
    assert isinstance(limits, dict), f"{service} declares no deploy.resources.limits"
    assert limits.get("memory") is not None, f"{service} declares no memory limit"
    assert limits.get("cpus") is not None, f"{service} declares no cpu limit"
