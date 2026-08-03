"""`python -m fking.platform.persistence` -- the seed command an operator runs.

In process and by subprocess, for the reason `tests/platform/config/test_boot.py` gives:
the subprocess test proves the exit code an operator actually sees, and the in-process
one proves what the command printed, which a coverage run can measure and a subprocess
cannot.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

from fking.platform.config import EX_CONFIG
from fking.platform.config.settings import DatabaseSettings
from fking.platform.persistence.__main__ import main as seed_main
from fking.platform.persistence.engine import build_engine
from fking.platform.persistence.seed import INSTRUMENTS, VENUES


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
