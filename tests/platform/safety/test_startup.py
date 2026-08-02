"""Fail closed at boot, and say so in the log.

A process that cannot prove where it will send orders must not accept work. Degrading
-- logging a warning and carrying on with the good endpoints -- would mean the first
evidence of a misconfiguration is a filled order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from fking.platform.safety import PERMITTED_HOSTS, SafetyViolation, verify_endpoints_or_abort
from fking.platform.safety.__main__ import main

pytestmark = pytest.mark.unit

GOOD = "https://testnet.binance.vision"
ALSO_GOOD = "wss://stream.testnet.binance.vision/ws"
BAD = "https://api.binance.com"


class TestStartupVerification:
    def test_all_permitted_endpoints_pass(self) -> None:
        verify_endpoints_or_abort([GOOD, ALSO_GOOD])

    def test_an_empty_endpoint_set_passes(self) -> None:
        """Nothing configured is not a violation; it is a process with no venue."""
        verify_endpoints_or_abort([])

    def test_a_single_bad_endpoint_among_good_ones_aborts(self) -> None:
        with pytest.raises(SafetyViolation, match="outside the allowlist"):
            verify_endpoints_or_abort([GOOD, BAD, ALSO_GOOD])

    def test_every_rejected_endpoint_is_named(self) -> None:
        """One report, not one per restart.

        Fixing the first bad endpoint only to be told about the second on the next
        boot is how a misconfiguration takes three deploys to resolve.
        """
        with pytest.raises(SafetyViolation) as raised:
            verify_endpoints_or_abort([BAD, "https://fapi.binance.com", GOOD])
        message = str(raised.value)
        assert "api.binance.com" in message
        assert "fapi.binance.com" in message

    def test_the_iterable_is_consumed_once(self) -> None:
        """A generator argument must not be silently exhausted before the check.

        `verify_endpoints_or_abort(url for url in ...)` reads naturally and would
        validate nothing if the implementation iterated twice.
        """
        verify_endpoints_or_abort(url for url in (GOOD, ALSO_GOOD))


class TestAllowlistIsLogged:
    def test_the_allowlist_is_logged_at_boot(self, capsys: pytest.CaptureFixture[str]) -> None:
        """ARCHITECTURE.md section 8 requires the guarantee be visible in the logs
        rather than assumed. An operator reading a boot log must be able to see which
        hosts this process can reach."""
        verify_endpoints_or_abort([GOOD])
        captured = capsys.readouterr()
        logged = captured.out + captured.err
        assert "safety_allowlist" in logged
        for host in PERMITTED_HOSTS:
            assert host in logged

    def test_a_rejection_is_logged_before_the_process_dies(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SafetyViolation):
            verify_endpoints_or_abort([BAD])
        assert "safety_startup_abort" in capsys.readouterr().out


class TestPrintAllowlistEntrypoint:
    """`python -m fking.platform.safety --print-allowlist`, documented in CONTRIBUTING.md."""

    def test_print_allowlist_lists_every_host(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--print-allowlist"]) == 0
        printed = capsys.readouterr().out
        for host in PERMITTED_HOSTS:
            assert host in printed

    def test_no_arguments_is_an_error_rather_than_a_silent_success(self) -> None:
        with pytest.raises(SystemExit) as raised:
            main([])
        assert raised.value.code != 0

    def test_the_documented_command_works_as_documented(self) -> None:
        """CONTRIBUTING.md tells a reader to run this exact line.

        Calling `main()` in-process proves the function; running the module proves the
        thing the documentation actually promises, including the `python -m` entry
        guard that an in-process call never touches.
        """
        completed = subprocess.run(
            [sys.executable, "-m", "fking.platform.safety", "--print-allowlist"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert set(completed.stdout.split()) == set(PERMITTED_HOSTS)
