"""The eleven checks that run before the demo runtime accepts any work, and the clock-skew
monitor that keeps running afterwards.

Every item produces evidence and every failure is a hard stop. The process aborts before
the event loop takes work, because a runtime that cannot prove where it will send orders,
what its clock says, or which key type it is holding must not be allowed to find out in
the order path -- where the answer costs a session rather than a minute.

Item 1 is the kill-switch journal, and it is first because it is the only item whose
answer does not involve the venue: a process that must not trade should learn that before
it authenticates anywhere. It is also the only item that can leave the runtime *running*
and refusing orders rather than aborting -- coming back halted after a trip is the
designed behaviour, and there has to be a process left for a human to resume.

Two properties are worth stating before the code, because both are easy to undo by
accident:

**`recvWindow` is never widened in response to drift.** A wide window does not fix a
wrong clock; it lets requests signed against a wrong clock through, and every timestamp
then written to the append-only audit log is wrong by the same amount -- permanently,
since audit rows cannot be corrected. `assert_skew_within_budget` therefore reads
`max_clock_drift_ms` and halts; nothing here writes `recv_window_ms`, and
`test_preflight.py` asserts the profile's window is unchanged after a drift failure.

**Skew is measured against `time.monotonic`, never by subtracting wall clocks.** The
sample is `server_time - (local_before + round_trip/2)`, with the round trip taken from a
monotonic counter. Subtracting two wall-clock reads across an NTP step correction yields a
negative interval, and a negative interval makes the correction look like venue latency --
which is the one thing this measurement exists to distinguish.

The two timestamp domains that look alike and fail differently are kept apart by
`classify_timestamp_domain`. Request-signing timestamps are **milliseconds on both spot
and futures**; that is unrelated to the archive fact that spot *data* timestamps moved to
microseconds from 2025-01-01 while futures stayed in milliseconds (ADR-0013). Conflating
them produces `-1021 Timestamp for this request is outside of the recvWindow`, which reads
as clock drift and sends the reader to the wrong file.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final, Literal, Protocol

from fking.execution._errors import PermanentExchangeError
from fking.execution.killswitch_journal import restore_kill_switch
from fking.execution.models import (
    VenueBalance,
    VenueExchangeInfo,
    VenueOrder,
    venue_epoch_to_utc,
)
from fking.execution.symbols import classify_symbol
from fking.execution.venue import ExecutionVenue, UserDataSource
from fking.execution.venue_profile import VenueProfile
from fking.platform.errors import FkingError
from fking.platform.logging import get_logger
from fking.platform.safety import PERMITTED_HOSTS, assert_host_permitted
from fking.risk import JournalReadOutcome, JournalUnreadable, KillSwitchGate

__all__ = [
    "PRODUCTION_PROVENANCE_FORBIDDEN_SUBSTRING",
    "CheckOutcome",
    "ClockDriftError",
    "ClockSkewMonitor",
    "ClockSkewSample",
    "PreflightAbortedError",
    "PreflightError",
    "PreflightItem",
    "PreflightReport",
    "ServerTimeReader",
    "TimestampDomain",
    "assert_skew_within_budget",
    "classify_timestamp_domain",
    "measure_clock_skew",
    "preflight_or_abort",
    "run_preflight",
    "utc_now",
]

_LOG: Final = get_logger(__name__)

# A cost-parameter set may only be loaded when its provenance does not name testnet.
# Futures testnet measured a 7.5bp spread against production's 0.16bp with roughly 10x
# inflated volume, so a model built there looks conservative and is fiction (CLAUDE.md
# section 2). A provenance *string* is checked rather than a boolean flag because the id
# is what an audit row carries months later, and a boolean records nothing.
#
# This is the third site running the same rule, after `CostModel`'s validator and
# `fking.backtest.costs.calibrate`, and the duplication is deliberate for the reason
# `_calibrate.py` states: each site stops the fiction at a different moment. This one
# stops it from being *loaded into the demo runtime*, which is the last moment before it
# starts pricing real fills. It is a copy rather than an import because `fking.execution`
# sits below `fking.backtest` in the layers contract and importing upward would invert
# it; `tests/execution/test_preflight.py` asserts the two agree case by case, so the
# copy cannot drift silently.
PRODUCTION_PROVENANCE_FORBIDDEN_SUBSTRING: Final[str] = "testnet"

# Binance returns this when the signed request's timestamp falls outside recvWindow. It
# belongs to the request-signing domain and is always about milliseconds.
_RECV_WINDOW_VENUE_CODE: Final[int] = -1021
# The signature-class rejections. -1022 is a bad signature (wrong key type or wrong
# canonical string), -1102 a missing one, -2014/-2015 a malformed or unauthorised key.
# Reaching any of them from a signed account call means the credential in hand is not the
# credential this venue expects, which is item 3's entire purpose.
_CREDENTIAL_VENUE_CODES: Final[frozenset[int]] = frozenset({-1022, -1102, -2014, -2015})

_SPOT_ORDER_RATE_PER_10S: Final[int] = 50

# A plain assignment rather than a PEP 695 `type` statement. `type X = ...` binds a
# `TypeAliasType` whose value is lazily evaluated, and static analysers that walk the AST
# for module-level bindings -- CodeQL's py/undefined-export among them -- do not treat the
# statement as a definition, so exporting the name reads as exporting nothing. The
# assignment binds the same alias in a form every reader of the module, human or
# otherwise, can see.
TimestampDomain = Literal["request_signing", "market_data_epoch"]


class PreflightError(FkingError):
    """Base for the startup gate's own failures."""


class PreflightAbortedError(PreflightError):
    """One or more pre-flight items failed. The runtime does not start."""


class ClockDriftError(PreflightError):
    """Local time differs from the venue's by more than the profile permits.

    Not a transient condition and not something a wider `recvWindow` addresses: the
    process halts and pages. Raised at startup by item 2 and, afterwards, by
    `ClockSkewMonitor` on every sample.
    """


class PreflightItem(StrEnum):
    """The checklist, in the order it runs.

    The value is what the CRITICAL record's `item` field carries and what the abort
    message names, so an operator reading either one can find the item without a lookup.
    """

    # First, and it touches no venue at all. A process that must not trade should learn
    # that before it authenticates anywhere, and the journal read is the only item whose
    # answer is already true of this deployment before the network is consulted.
    KILL_SWITCH_JOURNAL = "preflight.kill_switch_journal"
    ALLOWLISTED_ENDPOINTS = "preflight.endpoints_allowlisted"
    CLOCK_SKEW = "preflight.clock_skew"
    CREDENTIAL_KEY_TYPE = "preflight.credential_key_type"
    SYMBOL_UNIVERSE = "preflight.symbol_universe"
    SYMBOL_ROUND_TRIP = "preflight.symbol_round_trip"
    SYMBOL_FILTERS = "preflight.symbol_filters"
    EXCHANGE_STATE = "preflight.exchange_state"
    ORDER_RATE_BUDGET = "preflight.order_rate_budget"
    USER_DATA_HEARTBEAT = "preflight.user_data_heartbeat"
    COST_PARAMETER_PROVENANCE = "preflight.cost_parameter_provenance"


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """One item's verdict plus the evidence it was reached from.

    `routed` is a third state rather than a pass: item 7's zero-everything case is not a
    discrepancy to report, it is a wipe to hand to the wipe handler, and collapsing it
    into pass or fail loses the one distinction that decides what happens next.
    """

    item: PreflightItem
    verdict: Literal["passed", "failed", "routed"]
    detail: str
    evidence: Mapping[str, str]

    @property
    def is_blocking(self) -> bool:
        return self.verdict == "failed"


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """Every item's outcome, in checklist order."""

    venue_id: str
    outcomes: tuple[CheckOutcome, ...]

    @property
    def failures(self) -> tuple[CheckOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.is_blocking)

    @property
    def is_ready(self) -> bool:
        return not self.failures and len(self.outcomes) == len(PreflightItem)

    @property
    def wipe_suspected(self) -> bool:
        """True when item 7 found the venue empty where local state expected otherwise.

        The wipe handler (#67) owns what happens next. Spot testnet is wiped roughly
        every 30 days without notice -- keys survive, balances and open orders do not --
        so this is a routine condition with a specific response, not an anomaly.
        """
        return any(
            outcome.item is PreflightItem.EXCHANGE_STATE and outcome.verdict == "routed"
            for outcome in self.outcomes
        )

    @property
    def kill_switch_halted(self) -> bool:
        """True when the journal named an open incident and the runtime starts halted.

        Reported rather than blocking. The gate is what refuses orders -- this is so the
        boot record says which of "ready and trading" and "ready and halted" happened,
        and a report that cannot distinguish them is a report an operator has to guess at.
        """
        return any(
            outcome.item is PreflightItem.KILL_SWITCH_JOURNAL and outcome.verdict == "routed"
            for outcome in self.outcomes
        )


@dataclass(frozen=True, slots=True)
class ClockSkewSample:
    """One skew measurement, with the round trip it was corrected for."""

    venue_id: str
    server_time_utc: datetime
    local_midpoint_utc: datetime
    round_trip_seconds: float
    skew_ms: int

    @property
    def absolute_skew_ms(self) -> int:
        return abs(self.skew_ms)


class ServerTimeReader(Protocol):
    """Reads the venue's server time, in milliseconds, over a guarded transport.

    A Protocol rather than a concrete adapter because the monitor is constructed once and
    then sampled on a timer: the thing it holds must be the venue object the rest of the
    runtime already owns, not a second client it opened for itself.
    """

    async def server_time_ms(self) -> int:
        """`GET /api/v3/time`, or the venue's equivalent, as an integer epoch."""


def measure_clock_skew(
    *,
    venue_id: str,
    server_epoch_ms: int,
    local_before_utc: datetime,
    monotonic_before_seconds: float,
    monotonic_after_seconds: float,
) -> ClockSkewSample:
    """Skew of the local clock against the venue's, corrected for half the round trip.

    `monotonic_*` come from `time.monotonic()` and are passed in rather than read here, so
    a test can state a round trip instead of hoping for one. A regression in the monotonic
    counter is a programming error, not a measurement, and is refused rather than clamped:
    clamping it would report a plausible skew derived from an impossible interval.
    """
    round_trip_seconds = monotonic_after_seconds - monotonic_before_seconds
    if round_trip_seconds < 0:
        raise ClockDriftError(
            f"{venue_id} skew sample spans a monotonic regression of "
            f"{round_trip_seconds}s; time.monotonic cannot go backwards, so the readings "
            f"did not come from one counter"
        )
    if local_before_utc.tzinfo is None or local_before_utc.utcoffset() != timedelta(0):
        raise ClockDriftError(
            f"{venue_id} skew sample carries a local reading that is not aware UTC: "
            f"{local_before_utc!r}"
        )
    local_midpoint_utc = local_before_utc + timedelta(seconds=round_trip_seconds / 2)
    server_time_utc = venue_epoch_to_utc(server_epoch_ms)
    skew = server_time_utc - local_midpoint_utc
    return ClockSkewSample(
        venue_id=venue_id,
        server_time_utc=server_time_utc,
        local_midpoint_utc=local_midpoint_utc,
        round_trip_seconds=round_trip_seconds,
        # Rounded to whole milliseconds because that is the unit the budget is stated in
        # and the unit the venue signs in. Sub-millisecond precision here would be
        # precision the measurement does not have.
        skew_ms=round(skew / timedelta(milliseconds=1)),
    )


def assert_skew_within_budget(sample: ClockSkewSample, *, profile: VenueProfile) -> None:
    """Halt when the sample exceeds the profile's budget.

    Deliberately reads `max_clock_drift_ms` and never `recv_window_ms`. The budget sits
    well below the window so that drift is caught here rather than surfacing as a -1021
    in the order path, and the correct response to exceeding it is to fix the host clock.
    """
    if sample.absolute_skew_ms > profile.max_clock_drift_ms:
        raise ClockDriftError(
            f"{sample.venue_id} local clock is {sample.skew_ms}ms from venue time, past "
            f"the {profile.max_clock_drift_ms}ms budget (round trip "
            f"{sample.round_trip_seconds}s). Fix the host clock; widening recvWindow "
            f"(currently {profile.recv_window_ms}ms) would sign requests against a wrong "
            f"clock and write that error into every audit row"
        )


def classify_timestamp_domain(*, venue_code: int | None, epoch_ms: int | None) -> TimestampDomain:
    """Say which of the two timestamp domains a failure belongs to.

    They look alike and fail differently. `-1021` is the venue refusing a *signed
    request* whose timestamp is outside `recvWindow`: milliseconds, both markets, a clock
    problem. An epoch that normalises outside the plausible range is *market data* read
    with the wrong unit -- the spot archive moved to microseconds in 2025 and futures did
    not -- which is a parsing problem and no amount of clock correction touches it.
    """
    if venue_code == _RECV_WINDOW_VENUE_CODE:
        return "request_signing"
    if epoch_ms is not None:
        try:
            venue_epoch_to_utc(epoch_ms)
        except PermanentExchangeError:
            return "market_data_epoch"
    return "request_signing"


class ClockSkewMonitor:
    """Samples skew on the profile's reconcile cadence and halts on a breach.

    The monitor holds the same venue object the runtime uses, so it cannot drift onto a
    different endpoint than the one orders are signed against.
    """

    def __init__(
        self,
        reader: ServerTimeReader,
        profile: VenueProfile,
        *,
        now_utc: Callable[[], datetime],
        monotonic_seconds: Callable[[], float],
    ) -> None:
        self._reader = reader
        self._profile = profile
        self._now_utc = now_utc
        self._monotonic_seconds = monotonic_seconds

    @property
    def profile(self) -> VenueProfile:
        return self._profile

    async def sample(self) -> ClockSkewSample:
        """One measurement. Does not raise on drift -- `assert_skew_within_budget` does."""
        monotonic_before_seconds = self._monotonic_seconds()
        local_before_utc = self._now_utc()
        server_epoch_ms = await self._reader.server_time_ms()
        return measure_clock_skew(
            venue_id=str(self._profile.venue_id),
            server_epoch_ms=server_epoch_ms,
            local_before_utc=local_before_utc,
            monotonic_before_seconds=monotonic_before_seconds,
            monotonic_after_seconds=self._monotonic_seconds(),
        )

    async def sample_or_halt(self) -> ClockSkewSample:
        """One measurement, checked. Raises `ClockDriftError` past the budget."""
        sample = await self.sample()
        if sample.absolute_skew_ms > self._profile.max_clock_drift_ms:
            _LOG.critical(
                "clock.drift_exceeded",
                venue_id=str(self._profile.venue_id),
                skew_ms=sample.skew_ms,
                max_clock_drift_ms=self._profile.max_clock_drift_ms,
                recv_window_ms=self._profile.recv_window_ms,
                round_trip_seconds=round(sample.round_trip_seconds, 3),
            )
            assert_skew_within_budget(sample, profile=self._profile)
        else:
            _LOG.info(
                "clock.skew_measured",
                venue_id=str(self._profile.venue_id),
                skew_ms=sample.skew_ms,
                round_trip_seconds=round(sample.round_trip_seconds, 3),
            )
        return sample


def _outcome(
    item: PreflightItem,
    *,
    verdict: Literal["passed", "failed", "routed"],
    detail: str,
    **evidence: str,
) -> CheckOutcome:
    return CheckOutcome(item=item, verdict=verdict, detail=detail, evidence=evidence)


async def _check_kill_switch_journal(
    read_journal: Callable[[], Awaitable[JournalReadOutcome]], gate: KillSwitchGate
) -> CheckOutcome:
    """Item 1. Rehydrate the kill switch from its journal before anything else runs.

    Three outcomes, and the middle one is the reason `CheckOutcome` has a third verdict:

    - **Unreadable** is a failed item. The gate is left halted first and the process then
      aborts, so both senses of "halted" hold: no order can be constructed by anything
      holding this gate, and there is no runtime left to construct one. A process that
      cannot read the journal also cannot append a trip row to it, so it could not stop
      itself later either.
    - **Halted** is `routed`, not failed. The system is meant to come back up halted after
      a trip and wait for a person; aborting instead would leave nothing running for that
      person to resume, and #53's resume procedure is the owner of what happens next.
    - **Trading** is the only outcome that opens the gate, and this is the only call that
      can open it.
    """
    outcome = await restore_kill_switch(read_journal, gate=gate)
    state = gate.state
    if isinstance(outcome, JournalUnreadable):
        return _outcome(
            PreflightItem.KILL_SWITCH_JOURNAL,
            verdict="failed",
            detail=state.halted_reason or "the kill-switch journal could not be read",
            halted="true",
        )
    if state.is_halted:
        return _outcome(
            PreflightItem.KILL_SWITCH_JOURNAL,
            verdict="routed",
            detail=(
                f"incident {state.incident_id} is open in the journal; the runtime starts "
                f"halted and only #53's resume procedure reopens it"
            ),
            halted="true",
            incident_id=str(state.incident_id),
            tripped_at_utc=state.tripped_at_utc.isoformat() if state.tripped_at_utc else "",
        )
    return _outcome(
        PreflightItem.KILL_SWITCH_JOURNAL,
        verdict="passed",
        detail="the journal was read and holds no open incident",
        halted="false",
    )


def _check_endpoints_allowlisted(profile: VenueProfile) -> CheckOutcome:
    """Item 1. Resolve every URL this profile would connect to and log the allowlist.

    The allowlist is logged on every boot (ARCHITECTURE.md section 8) so that the set the
    process is actually holding is in the record, rather than the set someone believes is
    compiled in.
    """
    _LOG.info(
        "safety.allowlist",
        venue_id=str(profile.venue_id),
        permitted_hosts=sorted(PERMITTED_HOSTS),
        endpoints=list(profile.endpoint_urls),
    )
    # A SafetyViolation is a BaseException with no handler anywhere; it propagates
    # through this frame and kills the process, which is the correct outcome for a
    # configured endpoint outside the allowlist and is stricter than a failed item.
    #
    # `finally` rather than `except`, and that distinction is the whole point: an
    # `except SafetyViolation` here would be a handler for the exception the safety
    # kernel guarantees has none (tools/checks/no_catch_safety.py rejects one outright).
    # The finally block only names the item in the record an operator will read; the
    # violation continues past it untouched and takes the process with it.
    permitted = False
    try:
        hosts = tuple(assert_host_permitted(url) for url in profile.endpoint_urls)
        permitted = True
    finally:
        if not permitted:
            _LOG.critical(
                "preflight.item_failed",
                item=PreflightItem.ALLOWLISTED_ENDPOINTS.value,
                venue_id=str(profile.venue_id),
                detail=(
                    "a configured endpoint is outside the compiled-in allowlist; the "
                    "process aborts on the SafetyViolation rather than on this item"
                ),
                endpoints=list(profile.endpoint_urls),
            )
    return _outcome(
        PreflightItem.ALLOWLISTED_ENDPOINTS,
        verdict="passed",
        detail=f"{len(hosts)} endpoint(s) resolved inside the compiled-in allowlist",
        hosts=",".join(hosts),
    )


async def _check_clock_skew(monitor: ClockSkewMonitor) -> CheckOutcome:
    """Item 2. Skew inside the profile's budget, which sits below `recvWindow`."""
    profile = monitor.profile
    sample = await monitor.sample()
    evidence = {
        "skew_ms": str(sample.skew_ms),
        "max_clock_drift_ms": str(profile.max_clock_drift_ms),
        "recv_window_ms": str(profile.recv_window_ms),
        "round_trip_seconds": str(round(sample.round_trip_seconds, 3)),
        "server_time_utc": sample.server_time_utc.isoformat(),
    }
    if sample.absolute_skew_ms > profile.max_clock_drift_ms:
        return CheckOutcome(
            item=PreflightItem.CLOCK_SKEW,
            verdict="failed",
            detail=(
                f"local clock is {sample.skew_ms}ms from venue time, past the "
                f"{profile.max_clock_drift_ms}ms budget; fix the clock, never the window"
            ),
            evidence=evidence,
        )
    return CheckOutcome(
        item=PreflightItem.CLOCK_SKEW,
        verdict="passed",
        detail=f"skew {sample.skew_ms}ms inside the {profile.max_clock_drift_ms}ms budget",
        evidence=evidence,
    )


def _required_credential_kind(profile: VenueProfile) -> Literal["ed25519", "hmac"]:
    """The key type the venue's user-data mechanism forces.

    Spot's `session.logon` is an Ed25519 handshake -- `POST /api/v3/userDataStream` is
    410 Gone -- so an HMAC key on spot cannot open the stream at all. Futures signs its
    `listenKey` calls with HMAC. The mechanism decides the key type, so deriving it here
    beats carrying a second field that can disagree with the first.
    """
    return "ed25519" if profile.user_data_mechanism == "session_logon_ed25519" else "hmac"


async def _check_credential_key_type(
    venue: ExecutionVenue,
    profile: VenueProfile,
    *,
    credential_kind: Literal["ed25519", "hmac"],
) -> CheckOutcome:
    """Item 3. The cheap check that is expensive to skip.

    A key-type mismatch found here costs a minute. Found in the order path it costs a
    session, and it arrives disguised as `-1022 Signature for this request is not valid`,
    which reads as a signing bug rather than as the wrong key in the wrong slot.
    """
    required = _required_credential_kind(profile)
    if credential_kind != required:
        return _outcome(
            PreflightItem.CREDENTIAL_KEY_TYPE,
            verdict="failed",
            detail=(
                f"{profile.venue_id} authenticates with {profile.user_data_mechanism} and "
                f"requires a {required} key; a {credential_kind} key was presented"
            ),
            required_key_type=required,
            presented_key_type=credential_kind,
        )
    try:
        balances = await venue.fetch_balances()
    except PermanentExchangeError as rejected:
        classification = (
            "credential"
            if rejected.venue_code in _CREDENTIAL_VENUE_CODES
            else "unclassified venue rejection"
        )
        return _outcome(
            PreflightItem.CREDENTIAL_KEY_TYPE,
            verdict="failed",
            detail=(
                f"the venue refused a signed account call with {classification} error "
                f"{rejected.venue_code}: {rejected}"
            ),
            required_key_type=required,
            venue_code=str(rejected.venue_code),
        )
    return _outcome(
        PreflightItem.CREDENTIAL_KEY_TYPE,
        verdict="passed",
        detail=f"{required} key signed an account call returning {len(balances)} balance rows",
        required_key_type=required,
        asset_row_count=str(len(balances)),
    )


def _check_symbol_universe(
    *,
    venue_symbols: Sequence[str],
    required_symbols: frozenset[str],
) -> CheckOutcome:
    """Item 4. Every requested symbol is in the venue's tradable set.

    The archive half of the intersection is #59's: this asserts the venue side and that
    the requested set is fully covered. A requested symbol the venue does not list
    produces zero fills, and zero fills score as "no edge" -- retiring a good strategy for
    an infrastructure reason.
    """
    tradable = frozenset(
        classification.symbol
        for classification in map(classify_symbol, venue_symbols)
        if classification.is_tradable
    )
    missing = required_symbols - tradable
    if missing:
        return _outcome(
            PreflightItem.SYMBOL_UNIVERSE,
            verdict="failed",
            detail=(
                f"{sorted(missing)} requested but not tradable on the venue; "
                f"{len(tradable)} of {len(venue_symbols)} listed symbols are tradable"
            ),
            missing_symbols=",".join(sorted(missing)),
            tradable_symbol_count=str(len(tradable)),
        )
    if not required_symbols:
        return _outcome(
            PreflightItem.SYMBOL_UNIVERSE,
            verdict="failed",
            detail="no symbols were requested; a runtime with an empty universe trades nothing",
            tradable_symbol_count=str(len(tradable)),
        )
    return _outcome(
        PreflightItem.SYMBOL_UNIVERSE,
        verdict="passed",
        detail=f"{len(required_symbols)} requested symbol(s) inside {len(tradable)} tradable",
        tradable_symbol_count=str(len(tradable)),
        listed_symbol_count=str(len(venue_symbols)),
    )


def _survives_the_log_sink(raw: str) -> bool:
    """True when `raw` reaches a log sink and comes back with its code points intact.

    Three separate ways a symbol fails to, in the order they occur:

    - `classify_symbol` altered it. It is contractually forbidden to (an NFKC normalise
      would change the bytes we must send back), and this asserts that rather than
      trusting it, because the quarantine and the wire share one string.
    - It is not encodable at all. A lone surrogate -- what a mis-decoded response
      produces -- raises `UnicodeEncodeError` on `encode`, and every sink in the process
      encodes eventually. `errors="strict"` is the default and is stated here so nobody
      "fixes" this by passing `errors="replace"`, which would silently mangle the symbol
      instead of refusing it.
    - The render is not reversible. `ensure_ascii=True` mirrors the JSON renderer in
      `fking.platform.logging`: it escapes the code points rather than emitting them,
      which is what makes the record encodable on a cp1252 console. The round trip is
      what proves the escape carries the original back.
    """
    if classify_symbol(raw).symbol != raw:
        return False
    try:
        raw.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    rendered = json.dumps({"symbol": raw}, ensure_ascii=True)
    parsed = json.loads(rendered)
    return bool(parsed["symbol"] == raw)


def _check_symbol_round_trip(venue_symbols: Sequence[str]) -> CheckOutcome:
    """Item 5. Every symbol survives the log sink with its code points intact.

    Testnet `exchangeInfo` carries deliberate non-ASCII symbols. The failure they are
    there to provoke is not a wrong universe -- `classify_symbol` already quarantines
    them -- it is a `UnicodeEncodeError` inside the *logging* path on a console whose
    default codepage is cp1252, which takes the process down at startup while every
    parsing test passes.
    """
    damaged = tuple(raw for raw in venue_symbols if not _survives_the_log_sink(raw))
    non_ascii = tuple(raw for raw in venue_symbols if not raw.isascii())
    if damaged:
        return _outcome(
            PreflightItem.SYMBOL_ROUND_TRIP,
            verdict="failed",
            detail=f"{len(damaged)} symbol(s) did not round-trip through the log sink",
            damaged_symbol_count=str(len(damaged)),
        )
    return _outcome(
        PreflightItem.SYMBOL_ROUND_TRIP,
        verdict="passed",
        detail=(
            f"{len(venue_symbols)} symbol(s) round-tripped, including {len(non_ascii)} non-ascii"
        ),
        non_ascii_symbol_count=str(len(non_ascii)),
    )


def _check_symbol_filters(
    exchange_info: VenueExchangeInfo, *, required_symbols: frozenset[str]
) -> CheckOutcome:
    """Item 6. Tick and step for every requested symbol, as `Decimal`, positive.

    A missing filter is fatal rather than defaulted: a default tick size is a number
    nobody chose being used to round a price the venue will reject, and the rejection
    then arrives in the order path instead of here. The load happens *inside* the item,
    so an unparseable filter array is that item's failure rather than an exception
    escaping the gate with no item named.
    """
    filters_by_symbol: dict[str, tuple[Decimal, Decimal]] = {}
    for symbol in sorted(required_symbols):
        try:
            filters = exchange_info.symbol(symbol).order_filters()
        except PermanentExchangeError as unusable:
            return _outcome(
                PreflightItem.SYMBOL_FILTERS,
                verdict="failed",
                detail=f"{symbol} filters could not be loaded: {unusable}",
                symbol=symbol,
            )
        filters_by_symbol[symbol] = (filters.tick_size, filters.step_size)

    # That the values are `Decimal` is guaranteed one layer down rather than asserted
    # here: `VenueDecimal` refuses anything that is not a string, so a filter that had
    # been through a float parser fails validation before it reaches this function. What
    # is left for this item is presence -- handled above -- and sign.
    for symbol in sorted(required_symbols):
        tick_size, step_size = filters_by_symbol[symbol]
        if tick_size <= 0 or step_size <= 0:
            return _outcome(
                PreflightItem.SYMBOL_FILTERS,
                verdict="failed",
                detail=f"{symbol} declares tick {tick_size} and step {step_size}",
                symbol=symbol,
            )
    return _outcome(
        PreflightItem.SYMBOL_FILTERS,
        verdict="passed",
        detail=f"tick and step loaded as Decimal for {len(required_symbols)} symbol(s)",
        symbol_with_filters_count=str(len(required_symbols)),
        filters=" ".join(
            f"{symbol}:tick={tick}:step={step}"
            for symbol, (tick, step) in sorted(filters_by_symbol.items())
        ),
    )


async def _check_exchange_state(
    venue: ExecutionVenue, *, local_state_expects_holdings: bool
) -> CheckOutcome:
    """Item 7. Fetch the venue's view; local state is overwritten by it, never merged.

    Zero balances *and* no open orders, where local state expected otherwise, is the
    signature of a testnet wipe -- keys survive, balances and open orders do not -- and it
    is routed to the wipe handler rather than reported as a discrepancy. The same order
    missing while balances are intact is a different and much worse condition: a rejection
    that was never recorded.

    Both reads happen *inside* the item, for the reason `_check_symbol_filters` gives: a
    venue that refuses `openOrders` would otherwise escape the gate as a bare exception
    with no item named, and "the runtime would not start" is a much less useful record
    than "the runtime would not start because it could not read the venue's open orders".
    """
    try:
        balances: Sequence[VenueBalance] = await venue.fetch_balances()
        open_orders: Sequence[VenueOrder] = await venue.fetch_open_orders()
    except PermanentExchangeError as refused:
        return _outcome(
            PreflightItem.EXCHANGE_STATE,
            verdict="failed",
            detail=(
                f"the venue refused the state read, so local state cannot be rebuilt "
                f"from it: {refused}"
            ),
            venue_code=str(refused.venue_code),
        )
    held = tuple(
        entry
        for entry in balances
        if entry.free_amount != 0 or (entry.locked_amount or Decimal(0)) != 0
    )
    venue_is_empty = not held and not open_orders
    if venue_is_empty and local_state_expects_holdings:
        return _outcome(
            PreflightItem.EXCHANGE_STATE,
            verdict="routed",
            detail=(
                "venue reports zero balances and no open orders where local state "
                "expected holdings; routed to the wipe handler, not reported as a "
                "discrepancy"
            ),
            asset_row_count=str(len(balances)),
            open_order_count=str(len(open_orders)),
        )
    return _outcome(
        PreflightItem.EXCHANGE_STATE,
        verdict="passed",
        detail=(
            f"local state rebuilt from {len(held)} funded asset(s) and "
            f"{len(open_orders)} open order(s)"
        ),
        funded_asset_count=str(len(held)),
        open_order_count=str(len(open_orders)),
    )


def _check_order_rate_budget(
    profile: VenueProfile, *, configured_order_rate_per_10s: int
) -> CheckOutcome:
    """Item 8. The limiter's budget comes from the profile, and spot is 50 per 10s.

    Production allows 100 per 10s. A limiter built from that number produces `-1015
    TOO_MANY_ORDERS` here, and the fix is to reject the strategy rather than to sleep in
    the client -- a limiter that sleeps turns a capacity problem into a latency problem,
    and latency in the order path is how you get fills at prices the decision never saw.
    """
    if configured_order_rate_per_10s != profile.order_rate_per_10s:
        return _outcome(
            PreflightItem.ORDER_RATE_BUDGET,
            verdict="failed",
            detail=(
                f"order-rate budget is {configured_order_rate_per_10s}/10s but "
                f"{profile.venue_id} declares {profile.order_rate_per_10s}/10s"
            ),
            configured_order_rate_per_10s=str(configured_order_rate_per_10s),
            profile_order_rate_per_10s=str(profile.order_rate_per_10s),
        )
    if profile.market == "spot" and profile.order_rate_per_10s != _SPOT_ORDER_RATE_PER_10S:
        return _outcome(
            PreflightItem.ORDER_RATE_BUDGET,
            verdict="failed",
            detail=(
                f"spot budget is {profile.order_rate_per_10s}/10s; testnet was measured "
                f"at {_SPOT_ORDER_RATE_PER_10S}/10s on 2026-08-01 and is the binding "
                f"constraint"
            ),
            profile_order_rate_per_10s=str(profile.order_rate_per_10s),
        )
    return _outcome(
        PreflightItem.ORDER_RATE_BUDGET,
        verdict="passed",
        detail=f"limiter budget {profile.order_rate_per_10s}/10s taken from the profile",
        profile_order_rate_per_10s=str(profile.order_rate_per_10s),
    )


async def _check_user_data_heartbeat(
    source: UserDataSource | None, *, timeout_seconds: float
) -> CheckOutcome:
    """Item 9. At least one event or heartbeat before the runtime declares itself ready.

    A connected socket is not a working stream. Spot binds its session to the socket and
    futures' `listenKey` expires on a missed keepalive, so both fail *silently* -- fills
    simply stop arriving. Requiring one observed event turns that into a startup failure.
    """
    if source is None:
        return _outcome(
            PreflightItem.USER_DATA_HEARTBEAT,
            verdict="failed",
            detail="no user-data source was supplied; fills would arrive nowhere",
        )
    await source.connect()
    events = source.events()
    try:
        first = await asyncio.wait_for(anext(events), timeout=timeout_seconds)
    except TimeoutError:
        return _outcome(
            PreflightItem.USER_DATA_HEARTBEAT,
            verdict="failed",
            detail=(
                f"no user-data event within {timeout_seconds}s of connecting; the socket "
                f"is open and the stream is not"
            ),
            timeout_seconds=str(timeout_seconds),
        )
    except StopAsyncIteration:
        return _outcome(
            PreflightItem.USER_DATA_HEARTBEAT,
            verdict="failed",
            detail="the user-data stream closed before delivering an event",
        )
    return _outcome(
        PreflightItem.USER_DATA_HEARTBEAT,
        verdict="passed",
        detail=f"first user-data event observed with {len(first)} field(s)",
        event_field_count=str(len(first)),
    )


def _condensed_provenance(candidate: str) -> str:
    """Casefold and drop every separator, so `Test-Net` and `TESTNET` compare equal.

    Byte-for-byte the rule `fking.backtest.costs` applies. Condensing cannot create a
    false positive against a provenance string this project would legitimately write:
    `binance_um_production_2026-03..2026-05` condenses to `binanceumproduction202603202605`.
    """
    return "".join(character for character in candidate.casefold() if character.isalnum())


def _check_cost_parameter_provenance(provenance_id: str) -> CheckOutcome:
    """Item 10. The loaded cost parameters name a production calibration.

    Testnet is not a smaller production: futures testnet showed a 7.5bp spread against
    production's 0.16bp with roughly 10x inflated volume. A cost model built from that
    looks conservative and is fiction, so a testnet provenance refuses to start the demo
    runtime rather than warning.

    A blank provenance is refused for a different reason and is not a lesser one: it
    records nothing an investigator could check months later, so it is indistinguishable
    from a testnet calibration whose id was cleared.
    """
    if not provenance_id.strip():
        return _outcome(
            PreflightItem.COST_PARAMETER_PROVENANCE,
            verdict="failed",
            detail=(
                "cost parameters carry a blank provenance; an id that records nothing "
                "cannot be distinguished later from a testnet calibration"
            ),
            provenance_id=provenance_id,
        )
    if PRODUCTION_PROVENANCE_FORBIDDEN_SUBSTRING in _condensed_provenance(provenance_id):
        return _outcome(
            PreflightItem.COST_PARAMETER_PROVENANCE,
            verdict="failed",
            detail=(
                f"cost parameters carry provenance {provenance_id!r}, which names testnet; "
                f"testnet spreads are roughly 47x production's and produce a model that "
                f"looks conservative and is fiction"
            ),
            provenance_id=provenance_id,
        )
    return _outcome(
        PreflightItem.COST_PARAMETER_PROVENANCE,
        verdict="passed",
        detail=f"cost parameters calibrated from {provenance_id}",
        provenance_id=provenance_id,
    )


# One keyword-only parameter per checklist input, rather than a settings object. A
# settings object would hide which item each value feeds and would let an unset input
# read as a default rather than as an omission -- and an omitted input to a gate is the
# shape of a check that silently did not run.
async def run_preflight(  # noqa: PLR0913 - see the note above
    venue: ExecutionVenue,
    *,
    kill_switch: KillSwitchGate,
    read_kill_switch_journal: Callable[[], Awaitable[JournalReadOutcome]],
    clock_monitor: ClockSkewMonitor,
    credential_kind: Literal["ed25519", "hmac"],
    required_symbols: frozenset[str],
    configured_order_rate_per_10s: int,
    cost_parameter_provenance_id: str,
    user_data: UserDataSource | None,
    local_state_expects_holdings: bool,
    user_data_timeout_seconds: float = 30,
) -> PreflightReport:
    """Run the checklist in order and stop at the first blocking failure.

    Stopping rather than collecting: every item after a failure would be measuring a
    system whose premises are already known to be wrong, and an item-4 failure means the
    later authenticated calls are guaranteed to fail for a reason that is not their own.
    The failure is logged at CRITICAL naming the item, and the caller aborts.
    """
    profile = venue.profile
    outcomes: list[CheckOutcome] = [
        await _check_kill_switch_journal(read_kill_switch_journal, kill_switch)
    ]

    if not outcomes[-1].is_blocking:
        outcomes.append(_check_endpoints_allowlisted(profile))
    if not outcomes[-1].is_blocking:
        outcomes.append(await _check_clock_skew(clock_monitor))
    if not outcomes[-1].is_blocking:
        outcomes.append(
            await _check_credential_key_type(venue, profile, credential_kind=credential_kind)
        )
    if not outcomes[-1].is_blocking:
        exchange_info = await venue.exchange_info()
        listed = tuple(entry.symbol for entry in exchange_info.symbols if entry.is_trading)
        outcomes.append(
            _check_symbol_universe(venue_symbols=listed, required_symbols=required_symbols)
        )
        if not outcomes[-1].is_blocking:
            outcomes.append(_check_symbol_round_trip(listed))
        if not outcomes[-1].is_blocking:
            outcomes.append(_check_symbol_filters(exchange_info, required_symbols=required_symbols))
    if not outcomes[-1].is_blocking:
        outcomes.append(
            await _check_exchange_state(
                venue, local_state_expects_holdings=local_state_expects_holdings
            )
        )
    if not outcomes[-1].is_blocking:
        outcomes.append(
            _check_order_rate_budget(
                profile, configured_order_rate_per_10s=configured_order_rate_per_10s
            )
        )
    if not outcomes[-1].is_blocking:
        outcomes.append(
            await _check_user_data_heartbeat(user_data, timeout_seconds=user_data_timeout_seconds)
        )
    if not outcomes[-1].is_blocking:
        outcomes.append(_check_cost_parameter_provenance(cost_parameter_provenance_id))

    report = PreflightReport(venue_id=str(profile.venue_id), outcomes=tuple(outcomes))
    for failure in report.failures:
        # One stable event name with the item as a *field*, not the item as the event
        # name. An interpolated event name cannot be aggregated -- "how often does the
        # credential item fail" becomes a regex against our own log format -- and it is
        # rejected by tests/platform/logging/test_static_event_names.py.
        _LOG.critical(
            "preflight.item_failed",
            item=failure.item.value,
            venue_id=report.venue_id,
            detail=failure.detail,
            **failure.evidence,
        )
    return report


async def preflight_or_abort(  # noqa: PLR0913 - mirrors run_preflight exactly
    venue: ExecutionVenue,
    *,
    kill_switch: KillSwitchGate,
    read_kill_switch_journal: Callable[[], Awaitable[JournalReadOutcome]],
    clock_monitor: ClockSkewMonitor,
    credential_kind: Literal["ed25519", "hmac"],
    required_symbols: frozenset[str],
    configured_order_rate_per_10s: int,
    cost_parameter_provenance_id: str,
    user_data: UserDataSource | None,
    local_state_expects_holdings: bool,
    user_data_timeout_seconds: float = 30,
) -> PreflightReport:
    """Run the checklist and raise unless every item cleared.

    The gate the runtime entrypoint calls. It raises rather than returning a status,
    because a status can be ignored by forgetting to check it and a process that starts
    with an unproven venue is the condition this whole module exists to prevent.
    """
    report = await run_preflight(
        venue,
        kill_switch=kill_switch,
        read_kill_switch_journal=read_kill_switch_journal,
        clock_monitor=clock_monitor,
        credential_kind=credential_kind,
        required_symbols=required_symbols,
        configured_order_rate_per_10s=configured_order_rate_per_10s,
        cost_parameter_provenance_id=cost_parameter_provenance_id,
        user_data=user_data,
        local_state_expects_holdings=local_state_expects_holdings,
        user_data_timeout_seconds=user_data_timeout_seconds,
    )
    if report.failures:
        raise PreflightAbortedError(
            f"{report.venue_id} pre-flight failed: "
            + "; ".join(f"{failure.item.value}: {failure.detail}" for failure in report.failures)
        )
    if not report.is_ready:
        raise PreflightAbortedError(
            f"{report.venue_id} pre-flight ran {len(report.outcomes)} of "
            f"{len(PreflightItem)} items; an unrun item is not a passed item"
        )
    return report


def utc_now() -> datetime:
    """The wall clock, aware and UTC. Injected into `ClockSkewMonitor`, never read inside it."""
    return datetime.now(UTC)
