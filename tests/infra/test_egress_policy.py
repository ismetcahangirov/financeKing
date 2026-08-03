"""The out-of-process half of the demo-only guarantee, proved from inside the container.

`tests/platform/safety/` proves that `fking.platform.safety` refuses a production
host. This proves the app container could not reach one even if that module did not
exist -- and it proves it the only way that means anything, by opening a socket from
inside the container the stack actually runs.

The probe deliberately uses `socket.create_connection` and nothing else. There is no
`httpx`, no `guarded_client`, no monkeypatch to undo, because the Python safety
kernel is simply not on this code path: a raw socket is exactly what a subprocess
calling curl, a dependency's post-install hook or a native extension would use, and
those are the cases `ARCHITECTURE.md` section 8 names as outside the kernel's reach.
Two controls that fail differently is the point -- a bypass of one must not be a
bypass of both.

The negative and positive halves are both required. Asserting only that
`api.binance.com` is unreachable would pass just as well against a container with no
interpreter, no network stack or a broken image, so the same probe asserts that
`postgres:5432` -- on the internal network -- connects. A run where both halves fail
means the test is broken; a run where both succeed means the network is open.

The stack is brought up under its own Compose project name from the base file alone.
The base file publishes no ports, so this cannot collide with a developer's running
stack or with a host port the OS has reserved -- which on Windows includes 5432 often
enough that the developer override exists to move it.

Skipped when Docker is unreachable. `FKING_REQUIRE_DOCKER=1` -- set by CI -- turns
that skip into a failure, because a container test that silently skipped is a green
result that verified nothing, mirroring `FKING_REQUIRE_DB` in `tests/platform/`.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
BASE_FILE: Final[Path] = REPO_ROOT / "docker-compose.yml"

# Its own project so this never touches `make up`'s containers, volumes or network.
PROJECT: Final[str] = "fking-egress-test"

# A production Binance host and a bare address. Both are needed and they fail
# differently: the name fails at resolution because an internal network has no DNS
# forwarder, and the address fails at routing because it has no default gateway.
# Testing only the name would pass against a container that merely has broken DNS
# while still holding a route.
BLOCKED_NAME: Final[str] = "api.binance.com"
BLOCKED_ADDRESS: Final[str] = "1.1.1.1"

# On the internal network, and therefore the control that distinguishes "the network
# is closed" from "this container is broken".
REACHABLE_SERVICE: Final[str] = "postgres"

# ENETUNREACH as the *container's* kernel numbers it. Spelled out rather than read
# from `errno`, because this assertion runs on the developer's machine and Python
# maps errno names to the host platform: `errno.ENETUNREACH` is 101 on Linux and
# 10051 on Windows, so importing it would make the test pass or fail depending on
# who ran it. 111 (ECONNREFUSED) would be a different and much worse result -- it
# would mean something answered.
LINUX_ENETUNREACH: Final[int] = 101

BUILD_TIMEOUT_SECONDS: Final[float] = 900.0
STACK_TIMEOUT_SECONDS: Final[float] = 300.0
PROBE_TIMEOUT_SECONDS: Final[float] = 180.0

# Runs inside the app container. Emits one JSON object so the assertions read a
# structure rather than parsing prose, and so a failure message carries the errno the
# kernel actually returned instead of "connection failed".
#
# The targets arrive as a JSON argument rather than being interpolated into the
# source, so this stays a plain string that ruff, mypy and a reader can all evaluate
# as Python instead of as a template.
PROBE_SOURCE: Final[str] = """
import json, socket, sys

def probe(host, port):
    try:
        socket.create_connection((host, port), timeout=5).close()
    except OSError as refused:
        return {"reached": False, "error": type(refused).__name__, "errno": refused.errno}
    return {"reached": True, "error": None, "errno": None}

json.dump(
    {name: probe(host, port) for name, (host, port) in json.loads(sys.argv[1]).items()},
    sys.stdout,
)
"""

PROBE_TARGETS: Final[str] = json.dumps(
    {
        "blocked_name": (BLOCKED_NAME, 443),
        "blocked_address": (BLOCKED_ADDRESS, 443),
        "internal_service": (REACHABLE_SERVICE, 5432),
    }
)


def _docker_required() -> bool:
    return os.environ.get("FKING_REQUIRE_DOCKER", "") not in {"", "0", "false"}


def _compose(*arguments: str) -> Sequence[str]:
    return ("docker", "compose", "-p", PROJECT, "-f", str(BASE_FILE), *arguments)


def _run(command: Sequence[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, no interpolated input
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        cwd=REPO_ROOT,
        check=False,
    )


def _unavailable(message: str) -> None:
    """Skip locally, fail under `FKING_REQUIRE_DOCKER`."""
    if _docker_required():
        pytest.fail(f"FKING_REQUIRE_DOCKER is set and {message}")
    pytest.skip(message)


@pytest.fixture(scope="module")
def probe_report() -> Iterator[dict[str, dict[str, object]]]:
    """Build the image, start the control service, run the probe, tear it all down."""
    version = _run(("docker", "compose", "version"), timeout_seconds=60.0)
    if version.returncode != 0:
        _unavailable(f"docker compose is unavailable: {version.stderr.strip() or 'no output'}")

    built = _run(_compose("build", "app"), timeout_seconds=BUILD_TIMEOUT_SECONDS)
    if built.returncode != 0:
        _unavailable(f"the app image would not build: {built.stderr.strip()[-2000:]}")

    try:
        started = _run(
            _compose("up", "-d", "--wait", REACHABLE_SERVICE),
            timeout_seconds=STACK_TIMEOUT_SECONDS,
        )
        if started.returncode != 0:
            _unavailable(f"{REACHABLE_SERVICE} would not start: {started.stderr.strip()[-2000:]}")

        probe = _run(
            _compose(
                "run", "--rm", "--no-deps", "app", "python", "-c", PROBE_SOURCE, PROBE_TARGETS
            ),
            timeout_seconds=PROBE_TIMEOUT_SECONDS,
        )
        assert probe.returncode == 0, f"the probe did not run: {probe.stderr.strip()[-2000:]}"
        report: object = json.loads(probe.stdout.strip().splitlines()[-1])
        assert isinstance(report, dict)
        yield {str(key): value for key, value in report.items()}
    finally:
        # Never `-v`. The volumes are this project's own, but a teardown that removes
        # volumes is a habit, and the habit is what eventually gets typed against the
        # project that holds the audit log. DEPLOYMENT.md 3.
        _run(_compose("down", "--remove-orphans"), timeout_seconds=STACK_TIMEOUT_SECONDS)


def test_a_production_exchange_host_does_not_resolve_inside_the_app_container(
    probe_report: dict[str, dict[str, object]],
) -> None:
    """The safety kernel is not involved here, which is the entire point.

    An `internal: true` network has no DNS forwarder, so a production hostname does
    not become an address at all -- there is nothing for a bypassed allowlist to
    connect to.
    """
    result = probe_report["blocked_name"]
    assert result["reached"] is False, f"{BLOCKED_NAME} was reachable from the app container"
    assert result["error"] == "gaierror", f"expected a resolution failure, got {result}"


def test_a_bare_address_has_no_route_out_of_the_app_container(
    probe_report: dict[str, dict[str, object]],
) -> None:
    """Broken DNS is not a network policy. Skipping the name entirely and dialling an
    address proves the container holds no default route, which is the property that
    survives someone later adding a resolver.

    ENETUNREACH (101) is the kernel saying there is no route, as distinct from
    ECONNREFUSED (111), which would mean something answered.
    """
    result = probe_report["blocked_address"]
    assert result["reached"] is False, f"{BLOCKED_ADDRESS} was reachable from the app container"
    assert result["errno"] == LINUX_ENETUNREACH, f"expected ENETUNREACH, got {result}"


def test_the_internal_service_is_reachable_so_the_negative_results_mean_something(
    probe_report: dict[str, dict[str, object]],
) -> None:
    """Without this the two tests above would pass against a container with no
    interpreter, no network stack, or an image that failed to build.
    """
    result = probe_report["internal_service"]
    assert result["reached"] is True, (
        f"{REACHABLE_SERVICE} was unreachable too, so this container proves nothing "
        f"about egress: {result}"
    )
