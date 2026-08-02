"""The Compose topology's invariants, asserted mechanically.

These are parsed from YAML rather than grepped, so a reformat, a line wrap or a
comment cannot make a violation invisible to the check. The one exception is the
digest-comment rule, which is a statement about the raw text by definition.

Each invariant here corresponds to a failure that is silent at the moment it is
introduced and expensive later:

- a port bound to the wildcard address puts a Grafana holding a view of exchange
  credentials on the local network, and nothing announces it;
- an anonymous volume comes back empty on recreate, and nothing announces it;
- a floating image tag makes the build a function of the day it ran, and the
  symptom is an intermittent CI failure that reads as a flaky test;
- a bare `depends_on` waits for the container to start rather than to be ready,
  and the app's first query fails against a Postgres that is still loading its
  extension.

DEPLOYMENT.md is the specification these assert.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Final

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

BASE_FILE: Final[Path] = REPO_ROOT / "docker-compose.yml"
OVERRIDE_FILE: Final[Path] = REPO_ROOT / "docker-compose.override.yml"
DEMO_FILE: Final[Path] = REPO_ROOT / "docker-compose.demo.yml"
DOCKERFILE: Final[Path] = REPO_ROOT / "Dockerfile"
MAKEFILE: Final[Path] = REPO_ROOT / "Makefile"

COMPOSE_FILES: Final[tuple[Path, ...]] = (BASE_FILE, OVERRIDE_FILE, DEMO_FILE)

# A published port must name the loopback interface explicitly. The short form
# "9090:9090" binds every interface, which is the same exposure as the wildcard
# address with none of the visibility. DEPLOYMENT.md 2.
#
# The host port may be a variable so a developer with something already on 5432
# can move it; the host interface may NOT, because the same convenience would
# otherwise let an environment variable widen the binding to every interface.
# That asymmetry is the rule this pattern encodes: literal prefix, free port.
LOOPBACK_PORT: Final[re.Pattern[str]] = re.compile(
    r"^127\.0\.0\.1:(?:\d{1,5}|\$\{[A-Z0-9_]+:-\d{1,5}\}):\d{1,5}(?:/(?:tcp|udp))?$"
)

# The string this file exists to hunt for. S104 flags the literal as "possible
# binding to all interfaces", which is exactly backwards here -- searching for it
# is the opposite of binding to it, and it is the one place in the repository the
# literal legitimately appears.
WILDCARD_ADDRESS: Final[str] = "0.0.0.0"  # noqa: S104

# "-- pinned 2026-08-03", next to a human-readable tag. A bare digest is
# unreadable and gets tidied up by someone who cannot tell what it was.
PIN_COMMENT: Final[re.Pattern[str]] = re.compile(r"pinned\s+\d{4}-\d{2}-\d{2}")

# Services with no container-level health check, each for a reason that is
# recorded here rather than inferred from the absence. An unexplained gap in
# this map is a service someone forgot, which is exactly what the test is for.
HEALTHCHECK_EXEMPT: Final[Mapping[str, str]] = {
    "otel-collector": (
        "distroless image: no shell, no wget, no curl, so a container-level check "
        "cannot be expressed. Its health_check extension is on :13133 and Prometheus "
        "scrapes its self-telemetry. Nothing gates on it -- dependents use "
        "service_started, because telemetry must never gate the trading system."
    ),
    "app": (
        "one-shot boot validator at this milestone, not a long-running service. "
        "The /health/ready endpoint it would check does not exist until #102; the "
        "health check and condition: service_healthy land in that pull request."
    ),
}

# DEPLOYMENT.md 4, sized for a 16 GB host. The ceiling is the documented total
# including the dashboard service that #103 adds, so this stack must sit under it
# with room for that service to arrive without a re-budget.
HOST_MEMORY_MIB: Final[int] = 16 * 1024
BUDGET_CEILING_MIB: Final[int] = 13_824  # 13.5 GiB, the DEPLOYMENT.md 4 total
MINIMUM_HEADROOM_MIB: Final[int] = 2 * 1024 + 205  # 2.2 GiB; must exceed capped DuckDB peak

_MEMORY_UNITS: Final[Mapping[str, int]] = {"k": 1, "m": 1, "g": 1024}

# `COPY --from=builder` names a build stage, not an image, and has no digest to
# pin. A registry reference always carries a path separator; a stage name never
# does. Matching on that rather than on a hardcoded stage list means a new stage
# does not need this file edited.
_STAGE_REFERENCE: Final[re.Pattern[str]] = re.compile(r"--from=(?![^\s]*/)[^\s]+")


def _load_compose(path: Path) -> Mapping[str, object]:
    parsed: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{path.name} did not parse as a YAML mapping"
    return {str(key): value for key, value in parsed.items()}


def _services(path: Path) -> Mapping[str, Mapping[str, object]]:
    block: object = _load_compose(path).get("services")
    assert isinstance(block, dict), f"{path.name} has no services mapping"
    services: dict[str, Mapping[str, object]] = {}
    for name, definition in block.items():
        assert isinstance(definition, dict), f"{path.name}: service {name!r} is not a mapping"
        services[str(name)] = {str(key): value for key, value in definition.items()}
    return services


def _memory_mib(limit: str) -> int:
    """Parse a Compose memory string into MiB.

    Compose accepts b/k/m/g suffixes, case-insensitively, and a bare integer as
    bytes. Anything else is a typo that Compose would reject at runtime, so it is
    an assertion failure here rather than a silently-zero limit.
    """
    # Compose writes "512M"; Redis writes "384mb". Both are the same unit, and a
    # parser that understands only one of them silently mis-compares the two
    # numbers this file exists to keep ordered.
    match = re.fullmatch(r"(\d+)\s*([kmgKMG]?)[bB]?", limit.strip())
    assert match is not None, f"unparseable memory limit {limit!r}"
    amount = int(match.group(1))
    suffix = match.group(2).lower()
    if suffix == "":
        return amount // (1024 * 1024)
    if suffix == "k":
        return amount // 1024
    return amount * _MEMORY_UNITS[suffix]


def _declared_memory_mib(definition: Mapping[str, object]) -> int | None:
    deploy: object = definition.get("deploy")
    if not isinstance(deploy, dict):
        return None
    resources: object = deploy.get("resources")
    if not isinstance(resources, dict):
        return None
    limits: object = resources.get("limits")
    if not isinstance(limits, dict):
        return None
    memory: object = limits.get("memory")
    if memory is None:
        return None
    return _memory_mib(str(memory))


def _published_ports(definition: Mapping[str, object]) -> Sequence[str]:
    ports: object = definition.get("ports")
    if ports is None:
        return ()
    assert isinstance(ports, list), "ports must be a list"
    published: list[str] = []
    for entry in ports:
        # The long form is a mapping; it must still name the interface.
        if isinstance(entry, dict):
            host_ip = entry.get("host_ip")
            published.append(f"{host_ip!s}:{entry.get('published')!s}:{entry.get('target')!s}")
        else:
            published.append(str(entry))
    return tuple(published)


def _volume_mounts(definition: Mapping[str, object]) -> Sequence[str]:
    volumes: object = definition.get("volumes")
    if volumes is None:
        return ()
    assert isinstance(volumes, list), "volumes must be a list"
    return tuple(str(entry) for entry in volumes if not isinstance(entry, dict))


def _image_lines(path: Path) -> Iterator[tuple[int, str]]:
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        references_image = (
            stripped.startswith("image:")
            or stripped.startswith("FROM ")
            or ("--from=" in stripped and not _STAGE_REFERENCE.search(stripped))
        )
        if references_image:
            yield number, stripped


# ---------------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", list(COMPOSE_FILES), ids=lambda p: p.name)
def test_no_compose_file_mentions_the_wildcard_address(path: Path) -> None:
    """DEPLOYMENT.md 7 runs exactly this grep before any strategy is enabled.

    Scoped to the compose files on purpose. `0.0.0.0` inside ops/ configuration is
    correct and necessary -- a server bound to 127.0.0.1 inside its own network
    namespace is unreachable from any other container. What must never be wide is
    the *host* publishing, which only these files control.
    """
    offending = [
        f"{path.name}:{number}: {line}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if WILDCARD_ADDRESS in line
    ]
    assert offending == []


def test_the_base_file_publishes_no_ports() -> None:
    """The default is closed. A service is reachable only if an override said so."""
    offending = {
        name: _published_ports(definition)
        for name, definition in _services(BASE_FILE).items()
        if _published_ports(definition)
    }
    assert offending == {}


def test_the_demo_runtime_publishes_no_ports() -> None:
    """The mode that places orders exposes nothing to the host."""
    offending = {
        name: _published_ports(definition)
        for name, definition in _services(DEMO_FILE).items()
        if _published_ports(definition)
    }
    assert offending == {}


def test_every_published_port_binds_the_loopback_interface_explicitly() -> None:
    offending = [
        f"{name}: {port}"
        for name, definition in _services(OVERRIDE_FILE).items()
        for port in _published_ports(definition)
        if not LOOPBACK_PORT.fullmatch(port)
    ]
    assert offending == []


def test_credentials_are_mounted_read_only_and_into_the_app_alone() -> None:
    """Grafana, Prometheus, Loki and Tempo have no reason to see exchange keys."""
    for path in COMPOSE_FILES:
        for name, definition in _services(path).items():
            for mount in _volume_mounts(definition):
                if "secrets" not in mount:
                    continue
                assert name == "app", f"{path.name}: {name} mounts secrets: {mount}"
                assert mount.endswith(":ro"), f"{path.name}: {name} mounts secrets writable"


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [BASE_FILE, DOCKERFILE], ids=lambda p: p.name)
def test_every_image_is_pinned_by_digest(path: Path) -> None:
    """A floating tag makes the build a function of when it ran.

    The symptom is not a clean failure: it is an intermittent CI failure that
    reads as a flaky test, and a database image that minor-bumps under a
    TimescaleDB extension is a genuinely bad afternoon.
    """
    offending = [
        f"{path.name}:{number}: {line}"
        for number, line in _image_lines(path)
        if "@sha256:" not in line
    ]
    assert offending == []


@pytest.mark.parametrize("path", [BASE_FILE, DOCKERFILE], ids=lambda p: p.name)
def test_every_digest_carries_a_comment_naming_its_tag_and_pin_date(path: Path) -> None:
    """A bare digest is unreadable, so it gets "tidied up" by someone who cannot
    tell what it was. The comment is the only thing that makes an upgrade
    reviewable. DEPLOYMENT.md 1.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    # Keyed by digest rather than by line: a multi-stage Dockerfile names the
    # same base image twice, and demanding the pin comment on both would be
    # duplicated prose that drifts apart the first time one copy is updated.
    annotated: set[str] = set()
    occurrences: list[tuple[int, str, str]] = []
    for number, line in _image_lines(path):
        digest_match = re.search(r"@sha256:[0-9a-f]{64}", line)
        assert digest_match is not None, f"{path.name}:{number}: no digest to annotate"
        digest = digest_match.group(0)
        occurrences.append((number, line, digest))
        # Walk back over the contiguous comment block immediately above.
        preceding = "\n".join(
            candidate
            for candidate in reversed(lines[max(0, number - 12) : number - 1])
            if candidate.strip().startswith("#")
        )
        if PIN_COMMENT.search(preceding):
            annotated.add(digest)
    offending = [
        f"{path.name}:{number}: {line}"
        for number, line, digest in occurrences
        if digest not in annotated
    ]
    assert offending == []


# ---------------------------------------------------------------------------
# Readiness and ordering
# ---------------------------------------------------------------------------


def test_every_service_has_a_health_check_or_a_recorded_reason() -> None:
    missing = {
        name for name, definition in _services(BASE_FILE).items() if "healthcheck" not in definition
    }
    assert missing == set(HEALTHCHECK_EXEMPT), (
        "a service without a health check needs its reason recorded in "
        "HEALTHCHECK_EXEMPT, so the gap is a decision rather than an oversight"
    )


def test_every_dependency_waits_on_a_condition_rather_than_on_start() -> None:
    """Bare `depends_on` waits for the container to exist, which for Postgres
    means the process is up and not yet accepting connections. Whatever the app
    does next -- crash loop, backoff, swallowed exception -- is a worse version
    of what a health check does correctly.
    """
    offending: list[str] = []
    for path in COMPOSE_FILES:
        for name, definition in _services(path).items():
            depends: object = definition.get("depends_on")
            if depends is None:
                continue
            if not isinstance(depends, dict):
                offending.append(f"{path.name}: {name} uses the bare list form")
                continue
            for upstream, clause in depends.items():
                if not isinstance(clause, dict) or "condition" not in clause:
                    offending.append(f"{path.name}: {name} -> {upstream} has no condition")
    assert offending == []


def test_telemetry_never_gates_the_application() -> None:
    """`service_started`, not `service_healthy`.

    If the collector is unhealthy the SDK buffers and then drops, and the app
    keeps working. The inverse -- the app refusing to start because a telemetry
    container is unhappy -- is an availability failure introduced by an
    observability tool.
    """
    depends: object = _services(BASE_FILE)["app"]["depends_on"]
    assert isinstance(depends, dict)
    collector: object = depends["otel-collector"]
    assert isinstance(collector, dict)
    assert collector["condition"] == "service_started"


def test_the_postgres_health_check_tests_the_extension_not_just_the_port() -> None:
    """TimescaleDB's extension loads a few seconds AFTER the server accepts
    connections. A migration in that window fails with a misleading "type does
    not exist" that points nowhere near the cause, so pg_isready alone is a
    health check that reports ready before the thing the app needs exists.
    """
    healthcheck: object = _services(BASE_FILE)["postgres"]["healthcheck"]
    assert isinstance(healthcheck, dict)
    probe = " ".join(str(part) for part in healthcheck["test"] if isinstance(part, str))
    assert "pg_isready" in probe
    assert "pg_extension" in probe
    assert "timescaledb" in probe


# ---------------------------------------------------------------------------
# Durability
# ---------------------------------------------------------------------------


def test_every_volume_is_named() -> None:
    """An anonymous volume loses data silently on recreate: the container comes
    back, the volume is new and empty, and nothing announces it. A named volume
    that fails to mount produces a visible error.
    """
    declared: object = _load_compose(BASE_FILE)["volumes"]
    assert isinstance(declared, dict)
    unnamed = [
        key for key, spec in declared.items() if not isinstance(spec, dict) or "name" not in spec
    ]
    assert unnamed == []


def test_no_service_mounts_an_anonymous_volume() -> None:
    """A single-token entry like `- /var/lib/data` is anonymous."""
    offending = [
        f"{path.name}: {name}: {mount}"
        for path in COMPOSE_FILES
        for name, definition in _services(path).items()
        for mount in _volume_mounts(definition)
        if ":" not in mount
    ]
    assert offending == []


def test_the_postgres_volume_is_mounted_at_this_image_s_pgdata() -> None:
    """timescaledb-ha keeps PGDATA at /home/postgres/pgdata/data, not at the
    /var/lib/postgresql/data of the upstream postgres image. Mounting the
    upstream path succeeds, mounts nothing useful, and loses the database on
    every recreate without announcing it -- which is the same class of silent
    loss as an anonymous volume, wearing a named volume as a disguise.
    """
    mounts = _volume_mounts(_services(BASE_FILE)["postgres"])
    assert any(mount.startswith("fking_pgdata:/home/postgres/pgdata") for mount in mounts), mounts


def test_no_make_target_removes_volumes() -> None:
    """Dropping the volumes destroys every audit row and hours of
    checksum-verified archives. That is an act performed by typing the full
    command with the volume name on purpose, never by a target someone reaches
    for while tired. DEPLOYMENT.md 3.
    """
    makefile = MAKEFILE.read_text(encoding="utf-8")
    offending = [
        line
        for line in makefile.splitlines()
        if "compose" in line and "down" in line and re.search(r"(^|\s)(-v\b|--volumes\b)", line)
    ]
    assert offending == []


# ---------------------------------------------------------------------------
# Resource budget
# ---------------------------------------------------------------------------


def test_every_service_declares_a_memory_limit() -> None:
    """One unbounded container is enough to make the whole stack unreliable in a
    way that looks like something else entirely: Loki and Prometheus will each
    take everything available, and they will do it during a backtest, which
    starves Postgres, which times out the OMS, which presents as an application
    bug.
    """
    missing = [
        name
        for name, definition in _services(BASE_FILE).items()
        if _declared_memory_mib(definition) is None
    ]
    assert missing == []


def test_the_memory_budget_leaves_headroom_for_the_capped_duckdb_peak() -> None:
    """Headroom is not slack. DuckDB is capped at 3 GB (CONFIGURATION.md 7)
    precisely so that the shortfall is decided by an explicit limit rather than
    by the kernel's OOM score -- which frequently picks Postgres, the worst
    possible choice, and presents as a hung dashboard and Redis consumer
    timeouts that point at Redis rather than at memory.
    """
    allocated = sum(
        limit
        for definition in _services(BASE_FILE).values()
        if (limit := _declared_memory_mib(definition)) is not None
    )
    headroom = HOST_MEMORY_MIB - allocated
    assert allocated <= BUDGET_CEILING_MIB, f"allocated {allocated} MiB over budget"
    assert headroom >= MINIMUM_HEADROOM_MIB, f"headroom {headroom} MiB is too thin"


def test_redis_maxmemory_sits_below_its_container_limit() -> None:
    """If Redis reaches the cgroup limit the kernel kills it; if it reaches
    maxmemory first it applies its own policy and stays alive. The ordering is
    the whole point, so the two numbers cannot be tuned independently.
    """
    definition = _services(BASE_FILE)["redis"]
    command: object = definition["command"]
    assert isinstance(command, list)
    tokens = [str(token) for token in command]
    maxmemory = _memory_mib(tokens[tokens.index("--maxmemory") + 1])
    limit = _declared_memory_mib(definition)
    assert limit is not None
    assert maxmemory < limit, f"maxmemory {maxmemory} MiB is not below the {limit} MiB limit"
