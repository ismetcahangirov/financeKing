"""The ordered startup sequence. CONFIGURATION.md section 3.

Every step fails closed. A process that cannot prove what it is configured to do must
not accept work, because the first evidence of a misconfiguration would otherwise be a
filled order.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import structlog

from fking.platform.config import (
    EX_CONFIG,
    ConfigError,
    Settings,
    bootstrap,
    config_hash,
    effective_config,
    load_settings,
    venue_endpoints,
)
from fking.platform.config.__main__ import main as config_main
from fking.platform.safety import SafetyViolation

pytestmark = pytest.mark.unit

# Windows reports 0o666 for an ordinary file regardless of its ACL, so a mode check
# there answers a question the filesystem is not being asked. SECURITY.md section 4.5
# scopes the requirement to POSIX for the same reason.
posix_only = pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX-only")


def test_bootstrap_logs_the_effective_config_with_a_hash_and_the_allowlist() -> None:
    with structlog.testing.capture_logs() as captured:
        bootstrap(load_settings(env_file=None))
    record = next(entry for entry in captured if entry["event"] == "effective_config")
    assert record["config_hash"].startswith("sha256:")
    assert "testnet.binance.vision" in record["allowed_hosts"]
    assert record["config"]["risk"]["max_leverage"] == "2"


def test_the_config_hash_is_stable_across_identical_settings() -> None:
    assert config_hash(load_settings(env_file=None)) == config_hash(load_settings(env_file=None))


def test_the_config_hash_changes_when_configuration_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hash appears on every backtest result and audit row written by the process.
    A hash that did not move would tie a decision to the wrong configuration."""
    before = config_hash(load_settings(env_file=None))
    monkeypatch.setenv("FKING_RISK__MAX_OPEN_POSITIONS", "2")
    assert config_hash(load_settings(env_file=None)) != before


def test_the_effective_config_is_json_serialisable() -> None:
    """It is logged as one structured record. A dump holding a Decimal or a Path would
    fail at the renderer, in the boot path, after validation has already passed."""
    assert json.loads(json.dumps(effective_config(load_settings(env_file=None))))


def test_a_non_allowlisted_endpoint_aborts_the_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Step 3 delegates to the safety kernel, so this terminates on a SafetyViolation
    rather than a clean exit 78. The kernel outranks the exit-code convention."""
    monkeypatch.setenv("FKING_EXCHANGE__BINANCE__SPOT_REST_URL", "https://api.binance.com")
    with pytest.raises(SafetyViolation, match=r"api\.binance\.com"):
        bootstrap(load_settings(env_file=None))


def test_a_missing_key_file_aborts_the_boot(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {"exchange": {"binance": {"spot_ed25519_key_path": str(tmp_path / "absent.pem")}}}
    )
    with pytest.raises(ConfigError, match=r"absent\.pem"):
        bootstrap(settings)


@posix_only
def test_a_world_readable_key_file_aborts_the_boot(tmp_path: Path) -> None:
    """Not a warning. A key readable by other local accounts is a key that must be
    rotated, and starting anyway means the check exists to produce a log line nobody
    reads. SECURITY.md section 4.5."""
    key_path = tmp_path / "ed25519_spot.pem"
    key_path.write_text("not-a-real-key\n", encoding="utf-8")
    key_path.chmod(0o644)
    settings = Settings.model_validate(
        {"exchange": {"binance": {"spot_ed25519_key_path": str(key_path)}}}
    )
    with pytest.raises(ConfigError, match="0600"):
        bootstrap(settings)


def test_a_correctly_permissioned_key_file_is_accepted(tmp_path: Path) -> None:
    key_path = tmp_path / "ed25519_spot.pem"
    key_path.write_text("not-a-real-key\n", encoding="utf-8")
    key_path.chmod(0o600)
    settings = Settings.model_validate(
        {"exchange": {"binance": {"spot_ed25519_key_path": str(key_path)}}}
    )
    assert bootstrap(settings) is settings


def test_a_directory_in_place_of_a_key_file_aborts_the_boot(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {"exchange": {"binance": {"spot_ed25519_key_path": str(tmp_path)}}}
    )
    with pytest.raises(ConfigError):
        bootstrap(settings)


def _run_module(
    environment: dict[str, str], working_directory: Path
) -> subprocess.CompletedProcess[str]:
    """Run the entrypoint in a directory with no `.env`.

    `cwd` is a temporary directory rather than the repository root on purpose: `.env` is
    gitignored, so a developer machine may well have one, and a subprocess that picks it
    up makes this test pass or fail according to a file CI has never seen. The package
    is installed in the environment, so cwd has no bearing on the import.
    """
    child_environment = {
        key: entry for key, entry in os.environ.items() if not key.startswith("FKING_")
    }
    child_environment.update(environment)
    return subprocess.run(
        [sys.executable, "-m", "fking.platform.config"],
        capture_output=True,
        text=True,
        check=False,
        cwd=working_directory,
        env=child_environment,
    )


def test_the_entrypoint_exits_78_on_invalid_configuration(tmp_path: Path) -> None:
    """EX_CONFIG, so a supervisor can tell a configuration error from a crash and
    decline to restart-loop on it."""
    completed = _run_module({"FKING_RISK__MAX_LEVERAGE": "99"}, tmp_path)
    assert completed.returncode == EX_CONFIG
    assert "max_leverage" in completed.stderr


def test_the_entrypoint_exits_zero_on_valid_configuration(tmp_path: Path) -> None:
    completed = _run_module({}, tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert "effective_config" in completed.stdout


def test_the_entrypoint_prints_the_redacted_config_to_stdout(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """In process as well as by subprocess: the subprocess test proves the exit code an
    operator sees, and this one proves what the command actually printed -- which a
    coverage run can measure and a subprocess cannot."""
    monkeypatch.chdir(tmp_path)  # no .env in reach; see _run_module
    # capture_logs keeps the boot record out of stdout, so what is left is exactly what
    # the command itself wrote. Where the log record goes is the logging pipeline's
    # business (#18); the printed dump is this entrypoint's contract.
    with structlog.testing.capture_logs():
        assert config_main() == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["config_hash"].startswith("sha256:")
    assert printed["config"]["api"]["host"] == "127.0.0.1"


def test_the_entrypoint_returns_ex_config_without_raising(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FKING_RISK__MAX_LEVERAGE", "99")
    assert config_main() == EX_CONFIG
    assert "max_leverage" in capsys.readouterr().err


def test_a_configured_fallback_venue_is_validated_too() -> None:
    """The bybit block is optional, and an optional block is the one that gets left out
    of the endpoint sweep."""
    settings = Settings.model_validate(
        {"exchange": {"bybit": {"rest_url": "https://api-testnet.bybit.com"}}}
    )
    # `.count(...) == 1` rather than `in`: `in` against a URL reads as a substring
    # check whether or not the operand is a tuple, and a substring check on an
    # unparsed URL is bypassable -- which is why CodeQL rejects the shape on sight and
    # why the safety kernel parses hosts rather than matching strings. This also
    # asserts the endpoint is swept exactly once rather than merely present.
    endpoints = venue_endpoints(settings)
    assert endpoints.count("https://api-testnet.bybit.com/") == 1
    assert endpoints.count("wss://stream-testnet.bybit.com/v5/public/linear") == 1
    assert bootstrap(settings) is settings


def test_a_non_allowlisted_fallback_venue_aborts_the_boot() -> None:
    settings = Settings.model_validate(
        {"exchange": {"bybit": {"rest_url": "https://api.bybit.com"}}}
    )
    with pytest.raises(SafetyViolation, match=r"api\.bybit\.com"):
        bootstrap(settings)
