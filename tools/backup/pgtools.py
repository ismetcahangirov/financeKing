"""Locating a `pg_dump`/`pg_restore` that matches the server, and running it.

The failure this module exists to prevent is the one the issue names: a dump that has
run nightly for eight months and cannot be restored. Two of the ways that happens are
version skew -- `pg_dump` from a client older than the server refuses outright, and a
`pg_restore` from a *different* major silently omits objects it does not understand --
and TimescaleDB, whose extension and hypertable chunk metadata do not restore like
plain Postgres.

So the client is not "whatever is on PATH". It is, in order:

1. `FKING_PG_BIN`, for an operator who knows exactly which installation they mean.
2. A `pg_dump` on PATH whose major version matches the server's.
3. The pinned TimescaleDB image itself, run through `docker run --rm`.

Option 3 is the one that makes this work identically on a developer machine with no
PostgreSQL client installed and on a CI runner, and it is the only option that is
*guaranteed* version-matched, because it is the same image digest the server runs from.
It is deliberately not last-resort-only in spirit: preferring the image is the correct
default and PATH is the optimisation.

Nothing here constructs a network client. `pg_dump` speaks the PostgreSQL wire protocol
to a database DSN, which is not an HTTP or WebSocket transport and is not the
`fking.platform.safety` allowlist's subject; and this module lives in `tools/`, outside
`src/fking`, so it is not on any execution path.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import sqlalchemy as sa

__all__ = [
    "PgRunner",
    "PgToolsUnavailableError",
    "libpq_dsn",
    "resolve_runner",
]

# The digest docker-compose.yml and tests/conftest.py pin. Restated rather than imported
# because tools/ must not depend on the test package, and a drift between the three is
# caught by tests/infra/test_backup_manifest.py.
TIMESCALE_IMAGE: Final[str] = (
    "timescale/timescaledb-ha@sha256:"
    "15c65f4176e5f19742d44fde1cb12c3db27b55033dbc8f11f0143635223add85"
)

_LOCALHOST_NAMES: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})
_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"(\d+)\.")


class PgToolsUnavailableError(RuntimeError):
    """No version-matched pg_dump/pg_restore could be found or started."""


def libpq_dsn(dsn: str) -> str:
    """Strip the SQLAlchemy driver from a DSN so libpq will accept it.

    `postgresql+asyncpg://...` is a SQLAlchemy URL, not a connection string; handing it
    to `pg_dump` produces `invalid URI scheme`, which reads like a malformed password.
    """
    url = sa.engine.make_url(dsn).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


@dataclass(frozen=True, slots=True)
class PgRunner:
    """How to invoke a PostgreSQL client binary, and where it sees the filesystem.

    `workdir_in_container` is the path a file at `workdir` is visible at from inside the
    invocation. For a local binary the two are the same string; under Docker the host
    directory is bind-mounted, so a caller must translate paths through
    `container_path` rather than passing a host path to a containerised `pg_restore`.
    """

    kind: str  # "local" or "docker"
    workdir: Path
    _mount_point: str | None = None

    def container_path(self, path: Path) -> str:
        """`path` as the binary will see it."""
        if self._mount_point is None:
            return str(path)
        return f"{self._mount_point}/{path.name}"

    def command(self, program: str, arguments: list[str]) -> list[str]:
        if self._mount_point is None:
            return [program, *arguments]
        return [
            "docker",
            "run",
            "--rm",
            # Docker Desktop resolves this by itself; on Linux the flag is what makes a
            # DSN pointing at the host's published port reachable from the container.
            "--add-host=host.docker.internal:host-gateway",
            "-e",
            "PGPASSWORD",
            "-v",
            f"{self.workdir.resolve()}:{self._mount_point}",
            TIMESCALE_IMAGE,
            program,
            *arguments,
        ]

    def dsn_for(self, dsn: str) -> str:
        """`dsn` as reachable from wherever the binary runs."""
        plain = libpq_dsn(dsn)
        if self._mount_point is None:
            return plain
        url = sa.engine.make_url(plain)
        if (url.host or "") in _LOCALHOST_NAMES:
            url = url.set(host="host.docker.internal")
        return url.render_as_string(hide_password=False)

    def run(self, program: str, arguments: list[str], *, password: str | None) -> str:
        """Run the binary, returning stdout. Raises with stderr on a non-zero exit."""
        environment = dict(os.environ)
        if password is not None:
            environment["PGPASSWORD"] = password
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            self.command(program, arguments),
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        if completed.returncode != 0:
            raise PgToolsUnavailableError(
                f"{program} exited {completed.returncode} "
                f"({self.kind}): {completed.stderr.strip() or completed.stdout.strip()}"
            )
        return completed.stdout


def _major_of(version_output: str) -> int | None:
    match = _VERSION_PATTERN.search(version_output)
    return None if match is None else int(match.group(1))


def resolve_runner(workdir: Path, *, server_major: int) -> PgRunner:
    """Pick a client whose major version matches the server's, or raise saying why."""
    explicit = os.environ.get("FKING_PG_BIN")
    if explicit:
        candidate = Path(explicit)
        program = str(candidate / "pg_dump") if candidate.is_dir() else explicit
        return PgRunner(kind=f"local:{program}", workdir=workdir)

    on_path = shutil.which("pg_dump")
    if on_path is not None:
        probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [on_path, "--version"], capture_output=True, text=True, check=False
        )
        if probe.returncode == 0 and _major_of(probe.stdout) == server_major:
            return PgRunner(kind="local", workdir=workdir)

    if shutil.which("docker") is None:
        raise PgToolsUnavailableError(
            f"no pg_dump matching server major {server_major} on PATH and no docker to "
            f"run {TIMESCALE_IMAGE} with. Install the matching client, or set "
            f"FKING_PG_BIN."
        )
    return PgRunner(kind="docker", workdir=workdir, _mount_point="/backup")
