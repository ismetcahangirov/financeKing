"""The process entrypoint: startup ordering, the degraded-mode machine, and the one
sanctioned blind except in this repository.

`docs/rules/error-handling.md` names `fking.platform.supervisor.run` as **the** function
that may write `except Exception`, and `tools/checks/no_catch_safety.py` plus ruff's
BLE001 make that claim enforceable everywhere else. The handler does three things, in
this order, and then exits non-zero: trip the kill switch, flatten the book, write the
fatal audit record. It does not log and continue, it does not restart the loop, it does
not sleep and retry, and it does not swallow the exit code.

**Why exit rather than restart in place.** Restart is the outer supervisor's job -- Docker
Compose (ADR 0010). The distinction is not bureaucratic: a restart from a clean process
start runs the pre-flight checklist and reconciles against the exchange, which
`ARCHITECTURE.md` section 7 makes the source of truth. A restart from inside a process
whose state we have already decided we do not understand skips that reconciliation and
resumes on a local view that may be wrong in exactly the way that caused the crash.

**`SafetyViolation` passes straight through, and that is deliberate.** It inherits
`BaseException`, so this handler never sees it. The process dies *without* flattening,
which looks wrong and is correct: a request addressed to a host outside the allowlist
means the system's model of what it is talking to is wrong, and issuing more orders
through that path -- even closing ones -- is the worst available option.

**Nothing in the handler is itself defended.** If `flatten_all()` raises, that exception
propagates and the process dies with a traceback naming it. Wrapping the remediation in
its own handler would produce a supervisor that reports a tidy exit code while the book
is open, which is the failure this module exists to prevent.

**Why the seams are protocols rather than imports.** `docs/rules/module-boundaries.md`:
`platform` never imports another `fking` module, so it cannot name `fking.risk`'s kill
switch or `fking.execution`'s venue. It names the *shape* of each instead -- `trip`,
`flatten_all`, `record_fatal` -- and the adapters that satisfy them live one layer up
where the vocabulary belongs. This is the one place in `platform` where trading nouns
appear at all, and they appear as the argument list of a function `docs/rules/` already
specified by name. The alternative -- moving the supervisor into `execution` -- would put
the process entrypoint below the layer that owns configuration and logging, and would
make the sanctioned blind except live next to the order path rather than above it.

Startup order is a safety property, not a convenience; see `STARTUP_ORDER`.

FAILSAFE.md sections 2 and 3, ERROR_RECOVERY.md, `docs/rules/error-handling.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, auto
from typing import Final, Protocol, runtime_checkable

from fking.platform.config import EX_CONFIG, ConfigError, Settings, bootstrap
from fking.platform.correlation import BOOT, correlation_scope
from fking.platform.errors import FkingError
from fking.platform.logging import configure_logging, get_logger
from fking.platform.numeric import configure_decimal_context

__all__ = [
    "EXIT_FATAL",
    "EXIT_OK",
    "STARTUP_ORDER",
    "TRIPPING_MODES",
    "BookFlattener",
    "DegradedEntry",
    "DegradedMode",
    "FatalAuditSink",
    "KillSwitchControl",
    "KillSwitchReading",
    "MigrationCheck",
    "Runtime",
    "SignalsHaltedError",
    "StartupStep",
    "SupervisorError",
    "SupervisorGate",
    "SupervisorState",
    "apply_reading",
    "boot_halted_state",
    "enter_degraded",
    "exit_degraded",
    "run",
    "start",
]

_LOG: Final = get_logger(__name__)

# 0 and 1 rather than a sysexits code, because these are the two outcomes Docker Compose
# distinguishes: a clean stop and a crash it should restart. `EX_CONFIG` (78) is the third
# and is re-exported from `fking.platform.config`; a restart policy can decline to loop on
# it, since a process whose configuration is wrong fails identically every few seconds.
EXIT_OK: Final[int] = 0
EXIT_FATAL: Final[int] = 1


class SupervisorError(FkingError):
    """Base for the refusals this module raises."""


class SignalsHaltedError(SupervisorError):
    """Work was offered to a halted process.

    Raised rather than returned so that a caller cannot proceed by forgetting to check a
    boolean, and named for signals rather than for orders because the halt has to bite
    before a signal is ever sized -- a rejection at order construction is a rejection
    after the strategy, the feature reads and the risk decision have already run.
    """

    def __init__(self, halted_reason: str) -> None:
        super().__init__(f"the process is halted and admits no signals: {halted_reason}")
        self.halted_reason = halted_reason


class StartupStep(StrEnum):
    """The startup sequence, as values so the order is data a test can assert.

    A sequence expressed only as statements in a function body is a sequence a later edit
    can reorder with nothing noticing, and every step here is ordered for a reason:

    1. `DECIMAL_CONTEXT` first, because the traps must be live before any money is parsed.
       Configuration itself holds `Decimal` limits, so this precedes even settings.
    2. `LOGGING`, bound to `correlation_id="boot"`, so that every record from here on is
       attributable. Nothing has happened yet from which a real correlation id could be
       derived; `boot` is the one sanctioned literal (`docs/rules/logging-rules.md`).
    3. `ENDPOINT_ALLOWLIST`, which aborts before the event loop accepts work. The
       allowlist has to be established before a single client exists, or the first thing a
       wrong configuration does is talk to somewhere it should not (FAILSAFE.md 4).
    4. `MIGRATIONS`, because the kill-switch journal is a table and reading it against a
       schema mid-migration answers a question about the wrong database.
    5. `KILL_SWITCH_STATE` last, and if the switch was tripped before the last shutdown
       the process starts halted. A kill switch that resets itself on restart is not a
       kill switch, and "turn it off and on again" is an attempt to bypass it -- usually
       not a conscious one.
    """

    DECIMAL_CONTEXT = auto()
    LOGGING = auto()
    ENDPOINT_ALLOWLIST = auto()
    MIGRATIONS = auto()
    KILL_SWITCH_STATE = auto()


STARTUP_ORDER: Final[tuple[StartupStep, ...]] = (
    StartupStep.DECIMAL_CONTEXT,
    StartupStep.LOGGING,
    StartupStep.ENDPOINT_ALLOWLIST,
    StartupStep.MIGRATIONS,
    StartupStep.KILL_SWITCH_STATE,
)


class DegradedMode(StrEnum):
    """The named degraded states, from FAILSAFE.md section 3.

    There is no unnamed degraded state: if the system's behaviour differs from normal, it
    has a name here, an audit event, and a metric. A boolean `is_degraded` would collapse
    five different behaviours into one dashboard tile that tells an operator nothing about
    which of them to act on.
    """

    DATA_STALE = auto()
    EXCHANGE_UNREACHABLE = auto()
    LLM_QUOTA_EXHAUSTED = auto()
    DATABASE_UNAVAILABLE = auto()
    FEATURE_STORE_PARTIAL = auto()


# FAILSAFE.md section 3.4, the least negotiable entry in that table. The audit log is not
# a record of trading, it is a precondition for it: a trade executed while the audit is
# down permanently cannot be reconstructed, and the database is also where kill-switch
# state, high-water marks and the position record live. Buffering audit writes in memory
# and continuing converts a bounded outage into a permanent hole in the record, because
# process restarts are correlated with database problems.
TRIPPING_MODES: Final[frozenset[DegradedMode]] = frozenset({DegradedMode.DATABASE_UNAVAILABLE})


def _require_utc(moment: datetime, *, field: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise SupervisorError(
            f"{field}={moment!r} is naive; every timestamp in this system is timezone-aware "
            f"UTC (docs/rules/time-and-timezones.md)"
        )
    return moment


@dataclass(frozen=True, slots=True)
class DegradedEntry:
    """One active degraded mode, with when it was entered and why.

    `entered_at_utc` is supplied by the caller rather than read here: this module is
    called from the supervisor's own loop and from event consumers alike, and a
    transition whose timestamp depends on when the function ran cannot be replayed from
    the audit log months later.
    """

    mode: DegradedMode
    entered_at_utc: datetime
    detail: str

    def __post_init__(self) -> None:
        _require_utc(self.entered_at_utc, field="entered_at_utc")
        if not self.detail.strip():
            raise SupervisorError(
                f"entering {self.mode} with no detail; the dashboard tile and the audit "
                f"row both carry this text and 'degraded' on its own is not actionable"
            )


@dataclass(frozen=True, slots=True)
class KillSwitchReading:
    """What the kill-switch journal says, reduced to what the supervisor needs.

    Deliberately not `fking.risk.KillSwitchState`: `platform` may not import `risk`, and
    the adapter that maps one onto the other belongs to the layer that owns the journal.
    The reduction is total -- halted or not, and why -- because those are the only two
    facts the process-level gate acts on.
    """

    is_halted: bool
    halted_reason: str | None

    def __post_init__(self) -> None:
        if self.is_halted and not (self.halted_reason or "").strip():
            raise SupervisorError(
                "a halted reading carries no reason; an operator woken at 03:00 reads "
                "this string first"
            )


@dataclass(frozen=True, slots=True)
class SupervisorState:
    """The process's operating state. Immutable; transitions return new objects.

    `signals_admitted` is derived rather than stored, and that is the whole reason this
    type has two fields instead of three. A stored boolean alongside a reason is a pair
    that can disagree -- reading as trading on a dashboard and as halted in a log line,
    with whichever an investigation reaches first being the one it believes. Deriving it
    makes the disagreement unrepresentable rather than merely validated.
    """

    halted_reason: str | None
    degraded: tuple[DegradedEntry, ...]

    def __post_init__(self) -> None:
        if self.halted_reason is not None and not self.halted_reason.strip():
            raise SupervisorError(
                "a halt with an empty reason; use None for 'not halted' so the two "
                "states cannot be spelled the same way"
            )
        modes = [entry.mode for entry in self.degraded]
        if len(set(modes)) != len(modes):
            raise SupervisorError(f"a degraded mode is present twice: {modes}")
        if modes != sorted(modes):
            raise SupervisorError(f"degraded modes are not in canonical order: {modes}")

    @property
    def signals_admitted(self) -> bool:
        """True only when nothing is holding the process back."""
        return self.halted_reason is None

    @property
    def active_modes(self) -> frozenset[DegradedMode]:
        """The degraded modes currently entered."""
        return frozenset(entry.mode for entry in self.degraded)


# The state a process holds before it has proved it may trade. The default reason is the
# honest one: nothing has been read yet, so nothing is known.
_UNPROVEN: Final[str] = "the kill-switch journal has not been read yet"


def boot_halted_state(halted_reason: str = _UNPROVEN) -> SupervisorState:
    """The state a `SupervisorGate` holds at construction.

    Halted, always. A gate constructed before the journal has been read must not admit a
    signal, and a default of "admitted" makes the window between construction and the
    first reading an open one -- which is the boot-halted bug wearing a different hat.
    """
    return SupervisorState(halted_reason=halted_reason, degraded=())


def _tripping_reason(modes: frozenset[DegradedMode]) -> str | None:
    """The halt a set of active degraded modes forces, or None."""
    tripping = sorted(modes & TRIPPING_MODES)
    if not tripping:
        return None
    return (
        f"{', '.join(tripping)} tripped the kill switch; only a human resume clears it "
        f"(FAILSAFE.md 2.6)"
    )


def enter_degraded(
    state: SupervisorState, *, mode: DegradedMode, now_utc: datetime, detail: str
) -> SupervisorState:
    """Enter `mode`. Idempotent: entering a mode already active returns `state` unchanged.

    Idempotence is not politeness. Degraded-mode entries arrive over Redis Streams, whose
    delivery is at-least-once, so the same `EXCHANGE_UNREACHABLE` notice is redelivered on
    every consumer restart -- which is exactly when the exchange is unreachable. A
    non-idempotent entry would stack duplicate rows and, worse, would move
    `entered_at_utc` forward on each redelivery, making a two-hour outage look like it
    started seconds ago.

    Entering a mode in `TRIPPING_MODES` halts immediately rather than waiting for the next
    journal reading, and the halt it writes is sticky: `exit_degraded` removes the mode and
    leaves the reason, because the trip is cleared by a human and by nothing else.
    """
    if mode in state.active_modes:
        return state
    entry = DegradedEntry(mode=mode, entered_at_utc=now_utc, detail=detail)
    degraded = tuple(sorted((*state.degraded, entry), key=lambda item: item.mode))
    halted_reason = state.halted_reason
    if halted_reason is None and mode in TRIPPING_MODES:
        halted_reason = _tripping_reason(frozenset({mode}))
    return SupervisorState(halted_reason=halted_reason, degraded=degraded)


def exit_degraded(state: SupervisorState, *, mode: DegradedMode) -> SupervisorState:
    """Leave `mode`. Idempotent, and it cannot admit signals.

    Leaving `DATABASE_UNAVAILABLE` does not resume trading. The mode's *entry* trips the
    kill switch, and a trip is cleared by a human through `fking.risk.resume` and by
    nothing else -- least of all by the condition going away, since every trigger
    condition is transient by nature and waiting is always sufficient to clear it
    (FAILSAFE.md 2.6). This function therefore only ever removes an entry; the halt
    reason is carried through untouched, so the flapping-database case cannot resume
    itself between two readings.
    """
    degraded = tuple(entry for entry in state.degraded if entry.mode is not mode)
    if len(degraded) == len(state.degraded):
        return state
    return SupervisorState(halted_reason=state.halted_reason, degraded=degraded)


def apply_reading(state: SupervisorState, reading: KillSwitchReading) -> SupervisorState:
    """Fold a fresh kill-switch journal reading into `state`.

    This is the only route from halted to admitted, and it takes a *journal reading*
    rather than a boolean: the journal shows not-halted only after a `RESUME` row, and
    that row can only be written by `fking.risk.resume`, which refuses without a named
    operator, a written root cause, a clean recent reconciliation and a completed recovery
    sequence. So "a human resumed" is a structural property of this signature rather than
    a comment on it.

    A resumed reading still does not admit signals while a mode in `TRIPPING_MODES` is
    active. Those two halts are independent and both must clear.
    """
    if reading.is_halted:
        return SupervisorState(halted_reason=reading.halted_reason, degraded=state.degraded)
    return SupervisorState(
        halted_reason=_tripping_reason(state.active_modes), degraded=state.degraded
    )


class SupervisorGate:
    """The in-process holder the runtime reads before it acts on a signal.

    Mutable, and deliberately so: it is infrastructure, not a domain object, and it is the
    same shape as `fking.risk.KillSwitchGate` for the same reason. The state it holds is
    immutable, so a caller that reads it holds a value nothing can change underneath it --
    including across an await.

    `ensure_signals_admitted()` is synchronous and contains no await, so nothing can
    interleave between the check and the work it authorises.
    """

    __slots__ = ("_state",)

    def __init__(self, state: SupervisorState | None = None) -> None:
        self._state = state if state is not None else boot_halted_state()

    @property
    def state(self) -> SupervisorState:
        """The current state. A frozen value; safe to hold across an await."""
        return self._state

    def ensure_signals_admitted(self) -> None:
        """Raise `SignalsHaltedError` unless the process is admitting work."""
        current = self._state
        if not current.signals_admitted:
            raise SignalsHaltedError(current.halted_reason or _UNPROVEN)

    def adopt(self, reading: KillSwitchReading) -> None:
        """Fold a journal reading in. The only mutator that can admit signals."""
        self._state = apply_reading(self._state, reading)

    def enter_degraded(self, *, mode: DegradedMode, now_utc: datetime, detail: str) -> None:
        """Enter a degraded mode, logging the transition. Idempotent."""
        before = self._state
        self._state = enter_degraded(before, mode=mode, now_utc=now_utc, detail=detail)
        if self._state is not before:
            _LOG.warning(
                "supervisor.degraded_entered",
                mode=str(mode),
                detail=detail,
                entered_at_utc=now_utc.isoformat(),
                signals_admitted=self._state.signals_admitted,
            )

    def exit_degraded(self, *, mode: DegradedMode) -> None:
        """Leave a degraded mode, logging the transition. Never admits signals by itself."""
        before = self._state
        self._state = exit_degraded(before, mode=mode)
        if self._state is not before:
            _LOG.info(
                "supervisor.degraded_exited",
                mode=str(mode),
                signals_admitted=self._state.signals_admitted,
            )


@runtime_checkable
class KillSwitchControl(Protocol):
    """The kill switch, as the supervisor needs it. Implemented one layer up."""

    async def read_boot_state(self) -> KillSwitchReading:
        """Read the journal and report what it implies. Unreadable means halted."""

    async def trip(self, *, reason: str) -> None:
        """Block order entry and append the trip row. Must be safe to call twice."""


@runtime_checkable
class BookFlattener(Protocol):
    """The close-everything path, whose quantities come from the venue (ADR 0014)."""

    async def flatten_all(self) -> None:
        """Cancel resting orders and close every open position, from venue state."""


@runtime_checkable
class FatalAuditSink(Protocol):
    """The append-only record of why this process died."""

    async def record_fatal(self, *, error: BaseException, correlation_id: str) -> None:
        """Append one fatal row. The last thing this process writes."""


@runtime_checkable
class MigrationCheck(Protocol):
    """Proof that the schema in front of us is the schema this code was written against."""

    async def assert_current(self) -> None:
        """Raise unless every migration has been applied."""


@runtime_checkable
class Runtime(Protocol):
    """Everything the supervisor drives. One object, assembled by the composition root.

    A protocol rather than a class because `platform` may not import the modules that
    supply the parts, and because the fake a test builds is then checked structurally by
    `mypy --strict` rather than by a comment claiming it matches.
    """

    @property
    def settings(self) -> Settings:
        """The validated configuration tree, already constructed."""

    @property
    def correlation_id(self) -> str:
        """The id this runtime's work is recorded under."""

    @property
    def kill_switch(self) -> KillSwitchControl:
        """The kill switch."""

    @property
    def execution(self) -> BookFlattener:
        """The order path, narrowed to the one method the supervisor may call."""

    @property
    def audit(self) -> FatalAuditSink:
        """The audit sink."""

    @property
    def migrations(self) -> MigrationCheck:
        """The schema check."""

    async def serve(self, gate: SupervisorGate) -> None:
        """Run until stopped. Consults `gate` before acting on any signal."""


async def start(runtime: Runtime) -> SupervisorGate:
    """Run `STARTUP_ORDER` and hand back the gate the runtime must consult.

    Raises rather than returning a partial success. Partial recovery is not recovery: a
    system that established connectivity but not its kill-switch state is a system trading
    with an unverified halt, which looks operational on every dashboard (FAILSAFE.md 4).
    """
    configure_decimal_context()
    settings = runtime.settings
    configure_logging(settings.telemetry)

    with correlation_scope(BOOT):
        _LOG.info(
            "supervisor.startup_step",
            step=str(StartupStep.DECIMAL_CONTEXT),
            steps=[str(step) for step in STARTUP_ORDER],
        )
        _LOG.info("supervisor.startup_step", step=str(StartupStep.LOGGING))
        # Steps 2 through 7 of CONFIGURATION.md 3, of which endpoint verification is the
        # one that can abort: it raises SafetyViolation, a BaseException, which nothing
        # here or anywhere catches. The process dies before the event loop accepts work.
        bootstrap(settings)
        _LOG.info("supervisor.startup_step", step=str(StartupStep.ENDPOINT_ALLOWLIST))

        await runtime.migrations.assert_current()
        _LOG.info("supervisor.startup_step", step=str(StartupStep.MIGRATIONS))

        gate = SupervisorGate()
        gate.adopt(await runtime.kill_switch.read_boot_state())
        _LOG.info("supervisor.startup_step", step=str(StartupStep.KILL_SWITCH_STATE))

        if not gate.state.signals_admitted:
            _LOG.critical("supervisor.boot_halted", halted_reason=gate.state.halted_reason)
        else:
            _LOG.info("supervisor.boot_trading")
    return gate


async def run(runtime: Runtime) -> int:
    """Start the process, serve until it stops, and report the exit code.

    The startup sequence runs outside the handler below on purpose. An exception during
    startup is not a crash with an open book -- nothing has traded yet -- and calling
    `flatten_all()` against a venue the process has not finished proving it may talk to
    would be the supervisor's own version of the mistake it exists to prevent.
    """
    try:
        gate = await start(runtime)
    except ConfigError as invalid:
        # Specific, and the only exception this function handles other than the one below.
        # Exit 78 rather than 1 so a restart policy can decline to loop on a failure that
        # will reproduce identically every few seconds until a person edits a file.
        _LOG.critical("supervisor.configuration_rejected", correlation_id=BOOT, reason=str(invalid))
        return EX_CONFIG

    try:
        await runtime.serve(gate)
    except Exception as err:  # noqa: BLE001 - the one sanctioned blind except; see the
        # module docstring and docs/rules/error-handling.md. We do not know what this is,
        # which is precisely why we must not continue: unknown state plus open positions
        # is the condition FAILSAFE.md exists to prevent. Nothing below is defended -- a
        # failure inside the remediation must kill the process loudly rather than let it
        # report a tidy exit code with the book still open.
        await runtime.kill_switch.trip(reason=f"unhandled: {type(err).__name__}: {err}")
        await runtime.execution.flatten_all()
        await runtime.audit.record_fatal(error=err, correlation_id=runtime.correlation_id)
        _LOG.exception(
            "supervisor.unhandled_exception",
            correlation_id=runtime.correlation_id,
            error_type=type(err).__name__,
        )
        return EXIT_FATAL
    return EXIT_OK
