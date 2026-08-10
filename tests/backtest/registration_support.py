"""The trial-ledger registration every `EventLoop` needs before it will dispatch.

Two constants rather than a fixture, because most of the engine suite constructs its loop
inline in a one-line expression and threading a fixture through those would change what
each test is about.

`REGISTERED` stands in for a ledger row: a plausible charge against a `spec_hash` that a
real run would have obtained from `TrialLedger.registration_for`. `UNREGISTERED` is the
same specification with the count the ledger returns for a hash it has never seen, which
is the refusal the engine exists to make -- the value, not the absence of one.

Constructing these by hand is safe here and only here: the engine's contract is "a run
without a charge does not execute", and proving that needs both sides of the boundary.
The property that a charge cannot be *obtained* without the ledger is asserted against
real Postgres in `tests/evolution/test_trial_ledger.py`.
"""

from __future__ import annotations

from typing import Final

from fking.backtest import ExecutionReport, SpecRegistration

# sha256(b"tests.backtest.registration_support"). A digest rather than 64 repeated
# characters so that a test which accidentally compares two spec hashes fails on a
# meaningful difference instead of on a placeholder both sides happen to share.
_SPEC_HASH: Final[str] = "6b2a3e4b8bd93de0dbd4e5c1a5b0eb7fcb9e4c60d9a2f3c14d6c0a9b7e5f2413"

REGISTERED: Final[SpecRegistration] = SpecRegistration(spec_hash=_SPEC_HASH, trials_charged=200)
UNREGISTERED: Final[SpecRegistration] = SpecRegistration(spec_hash=_SPEC_HASH, trials_charged=0)

#: The path label the engine suite runs under. One value shared across those tests because
#: none of them is about the label; the tests that are about it name their own.
PATH_LABEL: Final[str] = "unit-suite"


class RecordingReporter:
    """An in-process `ExecutionReporter` that keeps what the loop told it.

    Fine for asserting that a report was made and what it said, and useless for asserting
    that the charge is *durable* -- a list can be truncated and this one has no ledger
    behind it. That property is asserted against real Postgres in
    `tests/backtest/test_ledger_enforcement.py`, and the two claims are not the same
    claim.

    One instance per loop rather than a module-level singleton: a shared recorder makes
    every test's assertions depend on which tests ran before it.
    """

    def __init__(self) -> None:
        self.reports: list[ExecutionReport] = []

    def report_execution(self, execution: ExecutionReport) -> None:
        self.reports.append(execution)
