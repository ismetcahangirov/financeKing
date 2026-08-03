"""`python -m fking.platform.persistence` -- the database commands an operator runs.

Two subcommands, and `seed` is the default so that the bare module invocation keeps
doing what it did before this file grew a second one.

`seed` writes reference data. Idempotent: run it as often as you like. The second run
reports zero insertions, which is the signal that the first one worked rather than that
nothing happened -- the row counts are printed alongside so the difference is visible.

`provision-roles` sets the login roles' passwords to whatever the configured DSNs
already carry. `0008_least_privilege.py` creates those roles without a password on
purpose, so a freshly migrated database has three roles that cannot authenticate until
this runs. That ordering is deliberate: a missing provisioning step fails as an
authentication error at startup, which is loud and immediate, whereas a password baked
into a migration is a shared credential that is identical on every deployment and cannot
be rotated without a new migration.

Both exit `EX_CONFIG` (78) on invalid configuration, matching
`python -m fking.platform.config`, so a deploy script can tell a misconfiguration from a
database that is simply not up yet.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from pydantic import PostgresDsn

from fking.platform.config import EX_CONFIG, ConfigError, load_settings
from fking.platform.persistence.engine import build_engine
from fking.platform.persistence.roles import (
    UnknownLoginRoleError,
    passwords_from_settings,
    provision_login_passwords,
)
from fking.platform.persistence.seed import count_reference_rows, seed_reference_data


async def _seed() -> dict[str, object]:
    settings = load_settings()
    engine = build_engine(settings.database)
    try:
        async with engine.begin() as connection:
            report = await seed_reference_data(connection)
            venue_count, instrument_count = await count_reference_rows(connection)
    finally:
        await engine.dispose()
    return {
        "inserted_venue_count": report.inserted_venue_count,
        "inserted_instrument_count": report.inserted_instrument_count,
        "venue_count": venue_count,
        "instrument_count": instrument_count,
    }


async def _provision_roles(admin_dsn: str) -> dict[str, object]:
    """Apply each configured DSN's credential to the login role it connects as.

    The admin DSN is an argument rather than a setting, and that is the load-bearing
    detail. `ALTER ROLE ... PASSWORD` needs `CREATEROLE` or superuser, and the login
    roles have neither -- so the very first provisioning cannot be done by any of the
    three configured connections, which is a chicken-and-egg the settings tree cannot
    resolve. Putting an admin credential in that tree would also make it a permanent
    part of the configuration surface every running process loads, when it is needed
    exactly once per database, by an operator, at bootstrap.

    Same shape as `CREATE EXTENSION` in migration 0001: a bootstrap step that needs more
    privilege than steady-state operation, and is therefore kept out of steady state.
    """
    settings = load_settings()
    passwords = passwords_from_settings(settings.database)
    admin = settings.database.model_copy(update={"dsn": PostgresDsn(admin_dsn)})
    engine = build_engine(admin)
    try:
        async with engine.begin() as connection:
            provisioned = await provision_login_passwords(connection, passwords)
    finally:
        await engine.dispose()
    return {
        "provisioned_roles": list(provisioned),
        # Named rather than counted. "2 of 3" leaves the reader to work out which role
        # is still unable to authenticate, and that is the entire content of the message.
        "roles_without_a_password": sorted(set(passwords) - set(provisioned)),
    }


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m fking.platform.persistence")
    parser.add_argument(
        "command",
        nargs="?",
        default="seed",
        choices=("seed", "provision-roles"),
        help="seed reference data (default), or set the login roles' passwords",
    )
    parser.add_argument(
        "--admin-dsn",
        default=os.environ.get("FKING_ADMIN_DSN"),
        help=(
            "administrative connection string used by provision-roles; may also be "
            "supplied as FKING_ADMIN_DSN. Deliberately not part of the settings tree."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # `argv or []` rather than letting argparse fall back to sys.argv: an in-process
    # caller that passes nothing means "no arguments", and inheriting the host process's
    # command line instead makes the function's behaviour depend on who imported it --
    # which under pytest means parsing the test paths as a subcommand.
    arguments = _parse(argv or [])
    try:
        if arguments.command == "seed":
            payload: dict[str, object] = {"event": "seed_completed", **asyncio.run(_seed())}
        elif arguments.admin_dsn is None:
            print(
                "provision-roles needs an administrative connection: pass --admin-dsn "
                "or set FKING_ADMIN_DSN",
                file=sys.stderr,
            )
            return EX_CONFIG
        else:
            payload = {
                "event": "roles_provisioned",
                **asyncio.run(_provision_roles(arguments.admin_dsn)),
            }
    except ConfigError as invalid:
        print(f"configuration error: {invalid}", file=sys.stderr)
        return EX_CONFIG
    except UnknownLoginRoleError as unknown:
        # Same exit code as a validation failure, because that is what it is: the DSN
        # names a role the grant matrix says nothing about.
        print(f"configuration error: {unknown}", file=sys.stderr)
        return EX_CONFIG

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
