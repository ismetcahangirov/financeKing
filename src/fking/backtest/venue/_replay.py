"""`ReplayVenue`: a session's recorded venue responses, served back in order.

A replay exists so that a run can be re-driven against responses that were *observed*
rather than modelled. That is what makes it the second half of the parity test: a
`BacktestVenue` computes its answers and a `ReplayVenue` reads them off a recording, so if
the two runs still emit the same `Signal` sequence, the signals cannot be a function of
how the venue answered. Fills legitimately differ between them; signals must not
(`BACKTEST_ENGINE.md` section 3).

Two decisions are worth stating because the cheaper alternatives both hide a divergence.

**A recording holds only what the venue said.** Prices, quantities, fees, trade ids and
instants. The order's identity -- instrument, side, `order_id` -- comes from the order the
run submits, so a fill can only be built by pairing a recorded response with a live
submission. A recording that carried the whole `Fill` could be replayed against a run that
submitted something else entirely and would report fills for orders that run never sent.

**An unknown `client_order_id` raises.** The tempting alternative -- fall through to a
modelled response -- turns "this run diverged from the session it claims to reproduce"
into a silently different but plausible-looking result. A replay that improvises is not a
replay.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid5

from fking.backtest._events import FillEvent, OrderAckEvent, RejectEvent
from fking.backtest._guards import require_finite_decimal, require_text, require_utc
from fking.backtest.venue._errors import VenueRecordingError, VenueSimulationError
from fking.backtest.venue._rejections import RejectReason
from fking.backtest.venue._simulation import (
    FILL_NAMESPACE,
    VenueFillRecord,
    VenueReport,
)
from fking.domain import Bar, Fill, Order

__all__ = [
    "RecordedFill",
    "RecordedRejection",
    "RecordedResponse",
    "ReplayVenue",
    "ResponsePhase",
    "VenueRecorder",
    "VenueRecording",
]

_ZERO: Final = Decimal("0")

# `Rejection.as_reject_text()` writes the taxonomy member between brackets precisely so
# that the closed enum survives the round trip through a `RejectEvent`'s free text. A
# recording whose reject text has no member is refused rather than counted under a
# catch-all, because a catch-all bucket is how a whole class of refusal stops being
# visible in the report it was added to.
_REASON_PATTERN: Final = re.compile(r"\[([a-z_]+)\]")


def _reason_from(reject_text: str) -> RejectReason:
    """The taxonomy member a recorded reject text names, or a refusal to guess."""
    found = _REASON_PATTERN.search(reject_text)
    if found is None:
        raise VenueRecordingError(
            f"recorded reject text {reject_text!r} carries no taxonomy member; it was not "
            f"produced by Rejection.as_reject_text()"
        )
    try:
        return RejectReason(found.group(1))
    except ValueError as unknown:
        raise VenueRecordingError(
            f"recorded reject text names {found.group(1)!r}, which is not a member of the "
            f"closed rejection taxonomy"
        ) from unknown


class ResponsePhase(StrEnum):
    """When in the order's life the venue produced a response.

    The two phases are not cosmetic: they decide which call replays the response, and
    they carry the aggressor flag. The simulator prints an aggressive fill only when an
    ack resolves and a passive fill only when a bar closes, so the phase *is* the
    liquidity flag, and recording them separately would let the two disagree.
    """

    ACK = "ack"
    BAR = "bar"


@dataclass(frozen=True, slots=True)
class RecordedFill:
    """One print the venue made, as the venue described it.

    No instrument and no side. Those come from the order this response is paired with at
    replay time -- see the module docstring.
    """

    client_order_id: str
    venue_trade_id: str
    phase: ResponsePhase
    event_time_utc: datetime
    quote_price: Decimal
    base_quantity: Decimal
    fee_quote: Decimal
    passive_markout_quote: Decimal

    def __post_init__(self) -> None:
        require_text(self.client_order_id, "client_order_id")
        require_text(self.venue_trade_id, "venue_trade_id")
        require_utc(self.event_time_utc, "event_time_utc")
        require_finite_decimal(self.quote_price, "quote_price")
        require_finite_decimal(self.base_quantity, "base_quantity")
        require_finite_decimal(self.fee_quote, "fee_quote")
        require_finite_decimal(self.passive_markout_quote, "passive_markout_quote")
        if self.base_quantity <= _ZERO:
            raise VenueRecordingError(
                f"{self.venue_trade_id} records a base_quantity of {self.base_quantity}; "
                f"a print of nothing is not a print"
            )
        if self.phase is ResponsePhase.ACK and self.passive_markout_quote != _ZERO:
            raise VenueRecordingError(
                f"{self.venue_trade_id} is an aggressive print carrying a passive markout "
                f"of {self.passive_markout_quote}; adverse selection is what a resting "
                f"order pays, and charging it to a taker double-counts the spread"
            )

    @property
    def is_passive(self) -> bool:
        """Whether the print was earned by resting rather than by crossing."""
        return self.phase is ResponsePhase.BAR


@dataclass(frozen=True, slots=True)
class RecordedRejection:
    """One refusal the venue made, with the text it carried into the trace."""

    occurs_at_utc: datetime
    reject_text: str

    def __post_init__(self) -> None:
        require_utc(self.occurs_at_utc, "occurs_at_utc")
        require_text(self.reject_text, "reject_text")
        # Parsed at construction rather than at replay: a recording that cannot be mapped
        # back onto the closed taxonomy is unusable, and finding that out when the run is
        # already half way through means throwing the run away.
        _reason_from(self.reject_text)

    @property
    def reason(self) -> RejectReason:
        """The taxonomy member this refusal belongs to."""
        return _reason_from(self.reject_text)


@dataclass(frozen=True, slots=True)
class RecordedResponse:
    """Everything one venue said about one order.

    A submission is answered exactly once -- an ack or a refusal, never both and never
    neither -- and everything after that hangs off the ack.
    """

    client_order_id: str
    acknowledged_at_utc: datetime | None
    submit_rejection: RecordedRejection | None
    ack_rejection: RecordedRejection | None
    fills: tuple[RecordedFill, ...]

    def __post_init__(self) -> None:
        require_text(self.client_order_id, "client_order_id")
        acknowledged = self.acknowledged_at_utc is not None
        if acknowledged == (self.submit_rejection is not None):
            raise VenueRecordingError(
                f"{self.client_order_id} must record exactly one answer to its "
                f"submission: an ack instant or a refusal, never both and never neither"
            )
        if self.acknowledged_at_utc is not None:
            require_utc(self.acknowledged_at_utc, "acknowledged_at_utc")
        if not acknowledged and (self.fills or self.ack_rejection is not None):
            raise VenueRecordingError(
                f"{self.client_order_id} was refused at submission yet records outcomes "
                f"after the ack it never received"
            )
        for recorded in self.fills:
            if recorded.client_order_id != self.client_order_id:
                raise VenueRecordingError(
                    f"{recorded.venue_trade_id} is filed under {self.client_order_id} but "
                    f"names {recorded.client_order_id}"
                )

    def fills_in_phase(self, phase: ResponsePhase) -> tuple[RecordedFill, ...]:
        """The prints this order earned in one phase, oldest first."""
        return tuple(
            sorted(
                (recorded for recorded in self.fills if recorded.phase is phase),
                key=lambda recorded: (recorded.event_time_utc, recorded.venue_trade_id),
            )
        )


@dataclass(frozen=True, slots=True)
class VenueRecording:
    """A whole session's responses, addressable by the id the run will submit under."""

    responses: tuple[RecordedResponse, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for response in self.responses:
            if response.client_order_id in seen:
                raise VenueRecordingError(
                    f"{response.client_order_id} appears twice in the recording; a client "
                    f"order id is the venue-side idempotency key and cannot name two "
                    f"submissions"
                )
            seen.add(response.client_order_id)

    def response_for(self, client_order_id: str) -> RecordedResponse:
        """The recorded answer for `client_order_id`, or a refusal to guess."""
        for response in self.responses:
            if response.client_order_id == client_order_id:
                return response
        raise VenueRecordingError(
            f"the recording holds no response for {client_order_id!r}; the run has "
            f"diverged from the session it is replaying, and a modelled substitute would "
            f"hide that"
        )


class ReplayVenue:
    """Serves a `VenueRecording` back through the `SimulatedVenue` interface.

    Holds no cost model, no book and no filters. Everything it answers was observed, so
    there is nothing here to calibrate and nothing here that can drift from the simulator
    -- which is exactly why it is the venue the parity test compares against.
    """

    __slots__ = ("_acknowledged", "_fills", "_recording", "_rejections", "_replayed")

    def __init__(self, *, recording: VenueRecording) -> None:
        self._recording = recording
        self._acknowledged: dict[str, Order] = {}
        self._replayed: set[str] = set()
        self._fills: list[VenueFillRecord] = []
        self._rejections: dict[RejectReason, int] = {}

    def observe(self, bar: Bar) -> tuple[FillEvent, ...]:
        """Replay every recorded resting print stamped at this bar's close.

        Keyed on the bar's close instant rather than on a range, because that is the
        instant the simulator stamps a passive print with. A range would silently absorb a
        recording whose bars do not line up with the run's, which is a divergence.
        """
        events: list[FillEvent] = []
        for client_order_id in sorted(self._acknowledged):
            order = self._acknowledged[client_order_id]
            if order.instrument.symbol != bar.instrument.symbol:
                continue
            response = self._recording.response_for(client_order_id)
            for recorded in response.fills_in_phase(ResponsePhase.BAR):
                if recorded.event_time_utc == bar.close_time_utc:
                    events.append(self._print(order, recorded))
        return tuple(events)

    def submit(self, order: Order, *, decided_at_utc: datetime) -> OrderAckEvent | RejectEvent:
        """Answer the submission the way the recorded session answered it."""
        require_utc(decided_at_utc, "decided_at_utc")
        if decided_at_utc < order.created_at_utc:
            raise VenueSimulationError(
                f"{order.client_order_id} was decided at {decided_at_utc.isoformat()}, "
                f"before it was created at {order.created_at_utc.isoformat()}"
            )
        response = self._recording.response_for(order.client_order_id)
        if response.submit_rejection is not None:
            return self._reject(order, response.submit_rejection)
        acknowledged_at_utc = response.acknowledged_at_utc
        if acknowledged_at_utc is None:  # pragma: no cover - RecordedResponse forbids it
            raise VenueRecordingError(f"{order.client_order_id} records neither ack nor refusal")
        if acknowledged_at_utc < decided_at_utc:
            raise VenueRecordingError(
                f"{order.client_order_id} was acknowledged at "
                f"{acknowledged_at_utc.isoformat()} in the recording but decided at "
                f"{decided_at_utc.isoformat()} in this run; the recording belongs to a "
                f"different session"
            )
        self._acknowledged[order.client_order_id] = order
        return OrderAckEvent(order=order, occurs_at_utc=acknowledged_at_utc)

    def resolve_ack(self, ack: OrderAckEvent) -> tuple[FillEvent | RejectEvent, ...]:
        """Replay whatever the recorded session produced when this ack resolved."""
        client_order_id = ack.order.client_order_id
        if client_order_id not in self._acknowledged:
            raise VenueSimulationError(
                f"ack for {client_order_id} names an order this replay is not holding; "
                f"either it was resolved twice or it was never submitted"
            )
        if client_order_id in self._replayed:
            raise VenueSimulationError(f"ack for {client_order_id} was resolved twice")
        self._replayed.add(client_order_id)
        response = self._recording.response_for(client_order_id)
        events: list[FillEvent | RejectEvent] = [
            self._print(ack.order, recorded)
            for recorded in response.fills_in_phase(ResponsePhase.ACK)
        ]
        if response.ack_rejection is not None:
            events.append(self._reject(ack.order, response.ack_rejection))
        return tuple(events)

    @property
    def report(self) -> VenueReport:
        """What the recorded session did, as far as this replay has served it."""
        counts = {reason: self._rejections.get(reason, 0) for reason in RejectReason}
        return VenueReport(rejection_counts=counts, fills=tuple(self._fills))

    # -- internals ------------------------------------------------------------------

    def _print(self, order: Order, recorded: RecordedFill) -> FillEvent:
        fill = Fill(
            fill_id=uuid5(FILL_NAMESPACE, recorded.venue_trade_id),
            order_id=order.order_id,
            venue_trade_id=recorded.venue_trade_id,
            instrument=order.instrument,
            side=order.side,
            event_time_utc=recorded.event_time_utc,
            quote_price=recorded.quote_price,
            base_quantity=recorded.base_quantity,
            fee_quote=recorded.fee_quote,
        )
        self._fills.append(
            VenueFillRecord(
                fill=fill,
                is_passive=recorded.is_passive,
                passive_markout_quote=recorded.passive_markout_quote,
            )
        )
        return FillEvent(fill=fill)

    def _reject(self, order: Order, rejection: RecordedRejection) -> RejectEvent:
        reason = rejection.reason
        self._rejections[reason] = self._rejections.get(reason, 0) + 1
        return RejectEvent(
            order=order,
            occurs_at_utc=rejection.occurs_at_utc,
            reason=rejection.reject_text,
        )


class VenueRecorder:
    """Captures what a venue answered, in the shape a `ReplayVenue` can serve back.

    Driven by the session rather than by wrapping a venue. A recording wrapper would be a
    fifth venue implementation that has to satisfy the same Protocol and would be the one
    place a venue-conditional branch could hide; a recorder the session calls alongside
    the venue cannot change what the venue did.

    `build` takes the venue's own report because adverse selection lives there and not on
    the `FillEvent` -- deliberately, so a strategy can read a fee and a markout apart.
    Joining on `venue_trade_id` is exact: the simulator derives that id from the client
    order id and a per-order sequence number.
    """

    __slots__ = ("_client_order_ids", "_fills", "_responses")

    def __init__(self) -> None:
        self._responses: dict[str, RecordedResponse] = {}
        self._client_order_ids: dict[UUID, str] = {}
        self._fills: dict[str, list[RecordedFill]] = {}

    def record_submission(self, answer: OrderAckEvent | RejectEvent) -> None:
        """Record the venue's answer to one submission."""
        order = answer.order
        client_order_id = order.client_order_id
        if client_order_id in self._responses:
            raise VenueRecordingError(
                f"{client_order_id} was submitted twice in one session; a client order id "
                f"is the venue-side idempotency key"
            )
        self._client_order_ids[order.order_id] = client_order_id
        self._fills[client_order_id] = []
        if isinstance(answer, OrderAckEvent):
            self._responses[client_order_id] = RecordedResponse(
                client_order_id=client_order_id,
                acknowledged_at_utc=answer.occurs_at_utc,
                submit_rejection=None,
                ack_rejection=None,
                fills=(),
            )
            return
        self._responses[client_order_id] = RecordedResponse(
            client_order_id=client_order_id,
            acknowledged_at_utc=None,
            submit_rejection=RecordedRejection(
                occurs_at_utc=answer.occurs_at_utc, reject_text=answer.reason
            ),
            ack_rejection=None,
            fills=(),
        )

    def record_ack_outcome(self, events: Iterable[FillEvent | RejectEvent]) -> None:
        """Record what resolving an ack produced: aggressive prints, or a late refusal."""
        for event in events:
            if isinstance(event, RejectEvent):
                self._record_late_rejection(event)
                continue
            self._record_fill(event, phase=ResponsePhase.ACK)

    def record_observed(self, events: Iterable[FillEvent]) -> None:
        """Record the passive prints a closing bar paid out.

        Fills only, by type. A refusal cannot arrive here -- the simulator refuses at
        submission or when an ack resolves, never in the middle of paying the resting
        queue -- and a parameter that admitted one would be a branch no session can reach.
        """
        for event in events:
            self._record_fill(event, phase=ResponsePhase.BAR)

    def build(self, report: VenueReport) -> VenueRecording:
        """The recording, with adverse selection joined on from `report`."""
        markouts = {
            record.fill.venue_trade_id: record.passive_markout_quote for record in report.fills
        }
        responses: list[RecordedResponse] = []
        for client_order_id in sorted(self._responses):
            response = self._responses[client_order_id]
            fills = tuple(
                RecordedFill(
                    client_order_id=recorded.client_order_id,
                    venue_trade_id=recorded.venue_trade_id,
                    phase=recorded.phase,
                    event_time_utc=recorded.event_time_utc,
                    quote_price=recorded.quote_price,
                    base_quantity=recorded.base_quantity,
                    fee_quote=recorded.fee_quote,
                    passive_markout_quote=markouts.get(recorded.venue_trade_id, _ZERO),
                )
                for recorded in self._fills[client_order_id]
            )
            responses.append(
                RecordedResponse(
                    client_order_id=response.client_order_id,
                    acknowledged_at_utc=response.acknowledged_at_utc,
                    submit_rejection=response.submit_rejection,
                    ack_rejection=response.ack_rejection,
                    fills=fills,
                )
            )
        return VenueRecording(responses=tuple(responses))

    def _record_fill(self, event: FillEvent, *, phase: ResponsePhase) -> None:
        client_order_id = self._client_order_id_for(event.fill.order_id)
        self._fills[client_order_id].append(
            RecordedFill(
                client_order_id=client_order_id,
                venue_trade_id=event.fill.venue_trade_id,
                phase=phase,
                event_time_utc=event.fill.event_time_utc,
                quote_price=event.fill.quote_price,
                base_quantity=event.fill.base_quantity,
                fee_quote=event.fill.fee_quote,
                # Replaced from the report in `build`; a fill event does not carry it.
                passive_markout_quote=_ZERO,
            )
        )

    def _record_late_rejection(self, event: RejectEvent) -> None:
        client_order_id = event.order.client_order_id
        existing = self._responses[client_order_id]
        self._responses[client_order_id] = RecordedResponse(
            client_order_id=existing.client_order_id,
            acknowledged_at_utc=existing.acknowledged_at_utc,
            submit_rejection=existing.submit_rejection,
            ack_rejection=RecordedRejection(
                occurs_at_utc=event.occurs_at_utc, reject_text=event.reason
            ),
            fills=existing.fills,
        )

    def _client_order_id_for(self, order_id: UUID) -> str:
        client_order_id = self._client_order_ids.get(order_id)
        if client_order_id is None:
            raise VenueRecordingError(
                f"a fill arrived for order {order_id} whose submission was never recorded; "
                f"the recorder was wired in after the session started"
            )
        return client_order_id
