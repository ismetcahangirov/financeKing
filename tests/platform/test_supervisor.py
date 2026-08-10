"""The supervisor's startup ordering and its one sanctioned blind except.

Every test here drives the real `run()` against a fake `Runtime`. The fakes are annotated
as the protocols they implement, so `mypy --strict` checks conformance structurally: a
method that drifts from the interface fails the build rather than these assertions.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import FloatOperation, getcontext, localcontext

import pytest

from fking.platform.config import EX_CONFIG, ConfigError, Settings, load_settings
from fking.platform.numeric import DECIMAL_PRECISION
from fking.platform.safety import SafetyViolation
from fking.platform.supervisor import (
    EXIT_FATAL,
    EXIT_OK,
    STARTUP_ORDER,
    BookFlattener,
    DegradedMode,
    FatalAuditSink,
    KillSwitchControl,
    KillSwitchReading,
    MigrationCheck,
    Runtime,
    SignalsHaltedError,
    StartupStep,
    SupervisorError,
    SupervisorGate,
    run,
)

pytestmark = pytest.mark.unit

BOOT_CORRELATION_ID = "6f1d8f8e-6c3b-4a67-9a2f-2c4bd7f1a001"
AN_INSTANT = datetime(2026, 8, 11, 3, 14, tzinfo=UTC)

TRADING = KillSwitchReading(is_halted=False, halted_reason=None)
HALTED = KillSwitchReading(
    is_halted=True, halted_reason="tripped by drawdown-limit before the last shutdown"
)


@pytest.fixture(autouse=True)
def _isolated_decimal_context() -> Iterator[None]:
    """`run()` mutates the process-wide decimal context, which is the point of it.

    `localcontext()` installs a copy for the duration of the test, so the rest of the
    suite runs under the traps it chose. Without it, a randomly ordered run would execute
    every later test under the traps installed here -- a real effect of the code under
    test, leaking into unrelated assertions.
    """
    with localcontext():
        yield


# --------------------------------------------------------------------------- fakes


class RecordingKillSwitch:
    """A kill switch that appends every call it receives to a shared ordered list."""

    def __init__(self, calls: list[str], *, reading: KillSwitchReading) -> None:
        self._calls = calls
        self._reading = reading
        self.trip_reasons: list[str] = []

    async def read_boot_state(self) -> KillSwitchReading:
        self._calls.append("read_boot_state")
        return self._reading

    async def trip(self, *, reason: str) -> None:
        self._calls.append("trip")
        self.trip_reasons.append(reason)


class RecordingExecution:
    def __init__(self, calls: list[str], *, failure: Exception | None = None) -> None:
        self._calls = calls
        self._failure = failure

    async def flatten_all(self) -> None:
        self._calls.append("flatten_all")
        if self._failure is not None:
            raise self._failure


class RecordingAudit:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.errors: list[BaseException] = []
        self.correlation_ids: list[str] = []

    async def record_fatal(self, *, error: BaseException, correlation_id: str) -> None:
        self._calls.append("record_fatal")
        self.errors.append(error)
        self.correlation_ids.append(correlation_id)


class RecordingMigrations:
    """Records the check and, when asked, observes the decimal context while it runs."""

    def __init__(
        self, calls: list[str], *, failure: Exception | None = None, traps: list[bool] | None = None
    ) -> None:
        self._calls = calls
        self._failure = failure
        self._traps = traps

    async def assert_current(self) -> None:
        self._calls.append("assert_current")
        if self._traps is not None:
            self._traps.append(getcontext().traps[FloatOperation])
        if self._failure is not None:
            raise self._failure


class FakeRuntime:
    """A runtime whose parts record what the supervisor asked of them, in order."""

    def __init__(  # noqa: PLR0913 - one keyword per failure the supervisor must answer
        # for. Collapsing them into a settings object would let a call site omit one by
        # omitting a field, which is how a test stops exercising the path it names.
        self,
        *,
        settings: Settings,
        reading: KillSwitchReading = TRADING,
        serve_raises: BaseException | None = None,
        migrations_raises: Exception | None = None,
        flatten_raises: Exception | None = None,
        observed_traps: list[bool] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._settings = settings
        self._kill_switch = RecordingKillSwitch(self.calls, reading=reading)
        self._execution = RecordingExecution(self.calls, failure=flatten_raises)
        self._audit = RecordingAudit(self.calls)
        self._migrations = RecordingMigrations(
            self.calls, failure=migrations_raises, traps=observed_traps
        )
        self._serve_raises = serve_raises
        self.gates: list[SupervisorGate] = []

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def correlation_id(self) -> str:
        return BOOT_CORRELATION_ID

    @property
    def kill_switch(self) -> RecordingKillSwitch:
        return self._kill_switch

    @property
    def execution(self) -> RecordingExecution:
        return self._execution

    @property
    def audit(self) -> RecordingAudit:
        return self._audit

    @property
    def migrations(self) -> RecordingMigrations:
        return self._migrations

    async def serve(self, gate: SupervisorGate) -> None:
        self.calls.append("serve")
        self.gates.append(gate)
        if self._serve_raises is not None:
            raise self._serve_raises


class FailingConfigRuntime(FakeRuntime):
    """A runtime whose settings cannot be read. Exercises the EX_CONFIG path."""

    @property
    def settings(self) -> Settings:
        raise ConfigError("invalid configuration: FKING_RISK__MAX_LEVERAGE is not a number")


def _runtime(
    *,
    reading: KillSwitchReading = TRADING,
    serve_raises: BaseException | None = None,
    migrations_raises: Exception | None = None,
    flatten_raises: Exception | None = None,
    observed_traps: list[bool] | None = None,
) -> FakeRuntime:
    return FakeRuntime(
        settings=load_settings(env_file=None),
        reading=reading,
        serve_raises=serve_raises,
        migrations_raises=migrations_raises,
        flatten_raises=flatten_raises,
        observed_traps=observed_traps,
    )


# --------------------------------------------------------------------------- protocols


def test_the_fakes_satisfy_the_protocols_the_supervisor_declares() -> None:
    """Structural conformance, asserted where a reader can see it.

    The annotations are the stronger half -- mypy --strict checks them at build time --
    but these calls fail loudly if a protocol gains a member and a fake does not.
    """
    runtime = _runtime()
    as_runtime: Runtime = runtime
    kill_switch: KillSwitchControl = runtime.kill_switch
    flattener: BookFlattener = runtime.execution
    audit: FatalAuditSink = runtime.audit
    migrations: MigrationCheck = runtime.migrations
    assert isinstance(as_runtime, Runtime)
    assert isinstance(kill_switch, KillSwitchControl)
    assert isinstance(flattener, BookFlattener)
    assert isinstance(audit, FatalAuditSink)
    assert isinstance(migrations, MigrationCheck)


# --------------------------------------------------------------------------- the handler


@pytest.mark.asyncio
async def test_an_unhandled_exception_trips_flattens_audits_and_exits_one() -> None:
    """The acceptance criterion, asserted by call ordering rather than by call counts.

    Order is the property: block order entry first so nothing new is admitted, close the
    book second, record third. A handler that audited before flattening would leave a
    window in which the record says the book was closed and it was not.
    """
    boom = RuntimeError("the feature store returned a column that does not exist")
    runtime = _runtime(serve_raises=boom)

    assert await run(runtime) == EXIT_FATAL

    assert runtime.calls[-4:] == ["serve", "trip", "flatten_all", "record_fatal"]
    assert runtime.audit.errors == [boom]
    assert runtime.audit.correlation_ids == [BOOT_CORRELATION_ID]
    assert "RuntimeError" in runtime.kill_switch.trip_reasons[0]
    assert "column that does not exist" in runtime.kill_switch.trip_reasons[0]


@pytest.mark.asyncio
async def test_a_safety_violation_propagates_untouched_and_nothing_is_flattened() -> None:
    """`SafetyViolation` is a BaseException, so the handler never sees it.

    The process dies without flattening, which looks wrong and is correct: a request
    addressed to a host outside the allowlist means our model of what we are talking to
    is wrong, and issuing more orders through that path -- even closing ones -- is the
    worst available option.
    """
    runtime = _runtime(serve_raises=SafetyViolation("host api.binance.com is not permitted"))

    with pytest.raises(SafetyViolation, match=r"api\.binance\.com"):
        await run(runtime)

    assert runtime.calls == ["assert_current", "read_boot_state", "serve"]


@pytest.mark.asyncio
async def test_a_failure_inside_the_remediation_is_not_defended() -> None:
    """Nothing in the handler is wrapped, so a flatten that fails kills the process.

    The alternative -- a try/except around the remediation -- produces a supervisor that
    returns a tidy exit code while the book is still open, which is the exact failure this
    module exists to prevent.
    """
    runtime = _runtime(
        serve_raises=RuntimeError("first failure"),
        flatten_raises=TimeoutError("the venue did not answer the cancel-all"),
    )

    with pytest.raises(TimeoutError, match="cancel-all"):
        await run(runtime)

    assert "record_fatal" not in runtime.calls


@pytest.mark.asyncio
async def test_a_clean_serve_exits_zero_and_remediates_nothing() -> None:
    runtime = _runtime()

    assert await run(runtime) == EXIT_OK
    assert runtime.calls == ["assert_current", "read_boot_state", "serve"]


# --------------------------------------------------------------------------- startup


@pytest.mark.asyncio
async def test_startup_aborts_before_serving_when_an_endpoint_is_off_the_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 3 runs before the runtime accepts any work, and it aborts rather than returns.

    Asserting that nothing was called is the whole test: an allowlist check that runs
    after the scheduler has started is a check on a process that has already made
    requests.
    """
    monkeypatch.setenv("FKING_EXCHANGE__BINANCE__SPOT_REST_URL", "https://api.binance.com")
    runtime = _runtime()

    with pytest.raises(SafetyViolation, match=r"api\.binance\.com"):
        await run(runtime)

    assert runtime.calls == []


@pytest.mark.asyncio
async def test_a_migration_that_is_not_current_stops_the_start_before_serving() -> None:
    """The journal is a table; reading it against a half-migrated schema answers a
    question about a different database."""
    runtime = _runtime(migrations_raises=RuntimeError("head is 0021, database is at 0019"))

    with pytest.raises(RuntimeError, match="0019"):
        await run(runtime)

    assert runtime.calls == ["assert_current"]
    # Nothing has traded, so nothing is flattened: the startup sequence runs outside the
    # sanctioned handler on purpose.
    assert "flatten_all" not in runtime.calls


@pytest.mark.asyncio
async def test_invalid_configuration_exits_seventy_eight_without_serving() -> None:
    """A restart policy can decline to loop on EX_CONFIG. A crash exit code would make it
    restart every few seconds until a person edits a file."""
    runtime = FailingConfigRuntime(settings=load_settings(env_file=None))

    assert await run(runtime) == EX_CONFIG
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_a_persisted_trip_starts_the_process_halted_and_admits_no_signals() -> None:
    """A restart is not a reset. The gate handed to `serve` refuses work.

    The refusal is the assertion that matters, not the flag: the runtime consults the gate
    before acting on a signal, and a gate that merely reported a boolean would be a gate a
    caller could forget to read.
    """
    runtime = _runtime(reading=HALTED)

    assert await run(runtime) == EXIT_OK

    (gate,) = runtime.gates
    assert gate.state.signals_admitted is False
    with pytest.raises(SignalsHaltedError, match="drawdown-limit"):
        gate.ensure_signals_admitted()


@pytest.mark.asyncio
async def test_a_halted_process_admits_signals_only_after_a_resumed_journal_reading() -> None:
    """The only route out of halted is a fresh reading, and a not-halted reading exists
    only after `fking.risk.resume` wrote a RESUME row -- which refuses without a named
    operator, a written root cause and a clean recent reconciliation."""
    runtime = _runtime(reading=HALTED)
    await run(runtime)
    (gate,) = runtime.gates

    gate.adopt(HALTED)
    with pytest.raises(SignalsHaltedError):
        gate.ensure_signals_admitted()

    gate.adopt(TRADING)
    gate.ensure_signals_admitted()


@pytest.mark.asyncio
async def test_the_decimal_context_is_live_before_any_other_startup_step() -> None:
    """`Decimal(0.1)` must already raise by the time configuration is parsed: settings
    hold Decimal risk limits, and a value parsed under the default context is rounded
    under a precision nobody chose."""
    observed: list[bool] = []
    runtime = _runtime(observed_traps=observed)

    await run(runtime)

    assert observed == [True]
    assert getcontext().prec == DECIMAL_PRECISION


def test_the_startup_order_is_the_documented_one() -> None:
    """The order is a safety property. Asserted as data so a reordering fails here rather
    than in an incident."""
    assert STARTUP_ORDER == (
        StartupStep.DECIMAL_CONTEXT,
        StartupStep.LOGGING,
        StartupStep.ENDPOINT_ALLOWLIST,
        StartupStep.MIGRATIONS,
        StartupStep.KILL_SWITCH_STATE,
    )


# --------------------------------------------------------------------------- the gate


def test_the_gate_starts_halted_when_constructed_with_no_state() -> None:
    """The window between construction and the first reading admits nothing."""
    with pytest.raises(SignalsHaltedError, match="has not been read yet"):
        SupervisorGate().ensure_signals_admitted()


def test_a_halted_reading_without_a_reason_is_refused() -> None:
    """An operator woken at 03:00 reads this string first."""
    with pytest.raises(SupervisorError, match="no reason"):
        KillSwitchReading(is_halted=True, halted_reason="  ")


def test_entering_the_database_unavailable_mode_halts_the_gate_immediately() -> None:
    """FAILSAFE.md 3.4: the audit log is a precondition for trading, not a record of it.
    The halt does not wait for the next journal reading."""
    gate = SupervisorGate()
    gate.adopt(TRADING)
    gate.ensure_signals_admitted()

    gate.enter_degraded(
        mode=DegradedMode.DATABASE_UNAVAILABLE,
        now_utc=AN_INSTANT,
        detail="asyncpg: connection refused on the audit write",
    )

    with pytest.raises(SignalsHaltedError, match="human resume"):
        gate.ensure_signals_admitted()


def test_leaving_the_database_unavailable_mode_does_not_resume_trading() -> None:
    """Every trigger condition is transient by nature, so waiting is always sufficient to
    clear it. A system that unhalts itself has a kill switch in name only."""
    gate = SupervisorGate()
    gate.adopt(TRADING)
    gate.enter_degraded(
        mode=DegradedMode.DATABASE_UNAVAILABLE, now_utc=AN_INSTANT, detail="connection refused"
    )

    gate.exit_degraded(mode=DegradedMode.DATABASE_UNAVAILABLE)

    assert gate.state.active_modes == frozenset()
    with pytest.raises(SignalsHaltedError):
        gate.ensure_signals_admitted()


def test_a_non_tripping_degraded_mode_leaves_signals_admitted() -> None:
    """`LLM_QUOTA_EXHAUSTED` is a non-event for trading, and that is a design assertion:
    if quota exhaustion ever stopped order flow, an LLM would be in the order path."""
    gate = SupervisorGate()
    gate.adopt(TRADING)

    gate.enter_degraded(
        mode=DegradedMode.LLM_QUOTA_EXHAUSTED, now_utc=AN_INSTANT, detail="gemini free tier"
    )

    gate.ensure_signals_admitted()
    assert gate.state.active_modes == frozenset({DegradedMode.LLM_QUOTA_EXHAUSTED})


def test_entering_a_degraded_mode_with_no_detail_is_refused() -> None:
    """The dashboard tile and the audit row both carry this text."""
    with pytest.raises(SupervisorError, match="no detail"):
        SupervisorGate().enter_degraded(
            mode=DegradedMode.DATA_STALE, now_utc=AN_INSTANT, detail="   "
        )


def test_a_naive_entry_timestamp_is_refused() -> None:
    """Crypto trades 24/7 with no session boundary to make the error obvious."""
    with pytest.raises(SupervisorError, match="naive"):
        SupervisorGate().enter_degraded(
            mode=DegradedMode.DATA_STALE,
            now_utc=datetime(2026, 8, 11, 3, 14),  # noqa: DTZ001 - the value under test
            detail="BTCUSDT last tick is 40 minutes old",
        )
