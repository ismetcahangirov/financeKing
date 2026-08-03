"""`python -m fking.platform.persistence` -- the seed command an operator runs.

In process and by subprocess, for the reason `tests/platform/config/test_boot.py` gives:
the subprocess test proves the exit code an operator actually sees, and the in-process
one proves what the command printed, which a coverage run can measure and a subprocess
cannot.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from fking.platform.config import EX_CONFIG
from fking.platform.config.settings import DatabaseSettings
from fking.platform.persistence.__main__ import main as seed_main
from fking.platform.persistence.engine import build_engine
from fking.platform.persistence.roles import UnknownLoginRoleError, passwords_from_settings
from fking.platform.persistence.seed import INSTRUMENTS, VENUES


async def _current_user(dsn: str) -> str:
    """Who the database thinks is connected, asked of the database.

    The assertion that matters is not that the command printed a role name -- it is
    that the credential it applied opens a connection, which only the server can say.
    """
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as connection:
            return str(await connection.scalar(sa.text("SELECT current_user")))
    finally:
        await engine.dispose()


def _run_module(
    environment: dict[str, str], working_directory: Path
) -> subprocess.CompletedProcess[str]:
    """Run the entrypoint in a directory with no `.env`.

    `cwd` is a temporary directory rather than the repository root: `.env` is gitignored,
    so a developer machine may well have one, and a subprocess that picks it up makes
    this test pass or fail according to a file CI has never seen.
    """
    child_environment = {
        key: entry for key, entry in os.environ.items() if not key.startswith("FKING_")
    }
    child_environment.update(environment)
    return subprocess.run(
        [sys.executable, "-m", "fking.platform.persistence"],
        capture_output=True,
        text=True,
        check=False,
        cwd=working_directory,
        env=child_environment,
    )


@pytest.mark.integration
@pytest.mark.slow
def test_the_entrypoint_seeds_and_reports_what_it_inserted(
    migrated_dsn: str, tmp_path: Path
) -> None:
    completed = _run_module({"FKING_DATABASE__DSN": migrated_dsn}, tmp_path)
    assert completed.returncode == 0, completed.stderr

    printed = json.loads(completed.stdout)
    assert printed["event"] == "seed_completed"
    assert printed["inserted_venue_count"] == len(VENUES)
    assert printed["inserted_instrument_count"] == len(INSTRUMENTS)
    assert printed["venue_count"] == len(VENUES)
    assert printed["instrument_count"] == len(INSTRUMENTS)


@pytest.mark.integration
@pytest.mark.slow
def test_running_the_entrypoint_twice_inserts_nothing_the_second_time(
    migrated_dsn: str, tmp_path: Path
) -> None:
    """Zero on the second run is the signal that the first one worked, which is why the
    row counts are printed beside the insert counts rather than instead of them."""
    _run_module({"FKING_DATABASE__DSN": migrated_dsn}, tmp_path)
    completed = _run_module({"FKING_DATABASE__DSN": migrated_dsn}, tmp_path)

    printed = json.loads(completed.stdout)
    assert (printed["inserted_venue_count"], printed["inserted_instrument_count"]) == (0, 0)


@pytest.mark.integration
@pytest.mark.slow
def test_the_entrypoint_prints_what_it_inserted(
    migrated_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """In process as well as by subprocess.

    The subprocess test proves the exit code an operator sees; this one proves what the
    command actually printed, which a coverage run can measure and a subprocess cannot.
    """
    monkeypatch.chdir(tmp_path)  # no .env in reach; see _run_module
    monkeypatch.setenv("FKING_DATABASE__DSN", migrated_dsn)

    assert seed_main() == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["inserted_instrument_count"] == len(INSTRUMENTS)


@pytest.mark.unit
def test_the_entrypoint_exits_78_on_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """EX_CONFIG, matching `python -m fking.platform.config`, so a deploy script can
    tell a misconfiguration from a database that is simply not up yet."""
    monkeypatch.chdir(tmp_path)  # no .env in reach
    monkeypatch.setenv("FKING_RISK__MAX_LEVERAGE", "99")
    assert seed_main() == EX_CONFIG
    assert "max_leverage" in capsys.readouterr().err


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
async def test_the_engine_pins_utc_and_applies_the_statement_timeout(migrated_dsn: str) -> None:
    """Asked of the server, not of the engine's constructor arguments.

    Both are server-side settings rather than client-side intentions: a client that
    gives up on a statement does not stop the server executing it, and a session
    timezone that follows the server's locale makes a TIMESTAMPTZ render differently on
    two machines holding the same row. Reading them back with `SHOW` is what
    distinguishes "we passed the option" from "the option took effect".
    """
    settings = DatabaseSettings.model_validate(
        {"dsn": migrated_dsn, "statement_timeout_seconds": 7}
    )
    engine = build_engine(settings)
    try:
        async with engine.connect() as connection:
            timeout = await connection.scalar(sa.text("SHOW statement_timeout"))
            timezone_name = await connection.scalar(sa.text("SHOW timezone"))
    finally:
        await engine.dispose()

    assert timeout == "7s"
    assert timezone_name == "UTC"


# ---------------------------------------------------------------------------
# provision-roles (#106)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_provision_roles_refuses_to_run_without_an_administrative_connection(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No default, and no silent fallback to the application DSN.

    Falling back would be the worst available behaviour: `ALTER ROLE ... PASSWORD` from
    the application connection fails with a permission error that reads like a broken
    database rather than like a missing argument.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FKING_ADMIN_DSN", raising=False)
    assert seed_main(["provision-roles"]) == EX_CONFIG
    assert "FKING_ADMIN_DSN" in capsys.readouterr().err


@pytest.mark.unit
def test_passwords_are_read_from_the_dsns_that_already_carry_them() -> None:
    """One source for the credential, not two.

    A separate password setting beside the DSN is one edit away from a database that
    disagrees with the connection string, and that disagreement surfaces as an
    authentication failure in whichever process happens to restart first.
    """
    database = DatabaseSettings.model_validate(
        {
            "dsn": "postgresql+asyncpg://fking_app_login:app-secret@127.0.0.1:5432/fking",
            "ingest_dsn": "postgresql+asyncpg://fking_ingest_login@127.0.0.1:5432/fking",
            "migrator_dsn": "postgresql+asyncpg://fking_migrator_login:mig@127.0.0.1:5432/fking",
        }
    )
    found = passwords_from_settings(database)

    assert set(found) == {"fking_app_login", "fking_migrator_login"}
    assert found["fking_app_login"].get_secret_value() == "app-secret"
    # The passwordless DSN is absent rather than present with an empty string: a role
    # that authenticates against nothing is strictly worse than one that cannot
    # authenticate at all.
    assert "fking_ingest_login" not in found


@pytest.mark.unit
def test_a_dsn_naming_a_role_outside_the_matrix_is_refused() -> None:
    database = DatabaseSettings.model_validate(
        {"dsn": "postgresql+asyncpg://postgres:secret@127.0.0.1:5432/fking"}
    )
    with pytest.raises(UnknownLoginRoleError, match="postgres"):
        passwords_from_settings(database)


@pytest.mark.integration
@pytest.mark.slow
def test_provision_roles_sets_a_password_the_role_can_then_connect_with(
    migrated_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """End to end: the command runs, and the credential it applied actually works.

    Asserting on the printed role list alone would prove the command reported success,
    which is the claim least worth making about a provisioning step.
    """
    monkeypatch.chdir(tmp_path)
    password = secrets.token_urlsafe(24)
    database = make_url(migrated_dsn).render_as_string(hide_password=False)
    app_dsn = make_url(migrated_dsn).set(username="fking_app_login", password=password)
    monkeypatch.setenv("FKING_DATABASE__DSN", app_dsn.render_as_string(hide_password=False))

    assert seed_main(["provision-roles", "--admin-dsn", database]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["provisioned_roles"] == ["fking_app_login"]

    assert asyncio.run(_current_user(app_dsn.render_as_string(hide_password=False))) == (
        "fking_app_login"
    )
