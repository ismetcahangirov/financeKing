"""The replay venue and its recorder, tested on the ways a recording can be wrong.

A replay is only worth having if it refuses. Every test below is a divergence between the
run and the session it claims to reproduce, and the assertion is always that the venue
raises rather than producing a plausible substitute -- because a substitute makes the
divergence invisible and leaves a result that looks like every other result.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from fking.backtest import FillEvent, OrderAckEvent, RejectEvent
from fking.backtest.venue import (
    RecordedFill,
    RecordedRejection,
    RecordedResponse,
    Rejection,
    RejectReason,
    ReplayVenue,
    ResponsePhase,
    VenueRecorder,
    VenueRecording,
    VenueRecordingError,
    VenueSimulationError,
)
from tests.backtest.venue_support import EPOCH, make_bar, make_order, make_venue

pytestmark = pytest.mark.unit

_ACK_AT = EPOCH + timedelta(minutes=1, milliseconds=430)
_FILL_AT = _ACK_AT + timedelta(milliseconds=95)


def _reject_text(reason: RejectReason = RejectReason.MIN_NOTIONAL) -> str:
    return Rejection(reason, "0.64 below the 5.00 floor").as_reject_text()


def _acked_response(*, client_order_id: str = "fk-00000001") -> RecordedResponse:
    return RecordedResponse(
        client_order_id=client_order_id,
        acknowledged_at_utc=_ACK_AT,
        submit_rejection=None,
        ack_rejection=None,
        fills=(
            RecordedFill(
                client_order_id=client_order_id,
                venue_trade_id=f"sim-{client_order_id}-0",
                phase=ResponsePhase.ACK,
                event_time_utc=_FILL_AT,
                quote_price=Decimal("64213.44"),
                base_quantity=Decimal("0.01"),
                fee_quote=Decimal("0.64213440"),
                passive_markout_quote=Decimal("0"),
            ),
        ),
    )


def test_a_recorded_ack_and_fill_are_served_back_against_the_live_order() -> None:
    """The prices come from the recording; the instrument and side come from the order."""
    order = make_order()
    venue = ReplayVenue(recording=VenueRecording(responses=(_acked_response(),)))

    ack = venue.submit(order, decided_at_utc=EPOCH)
    assert isinstance(ack, OrderAckEvent)
    resolved = venue.resolve_ack(ack)

    assert [type(event) for event in resolved] == [FillEvent]
    printed = venue.report.fills[0].fill
    assert printed.quote_price == Decimal("64213.44")
    assert printed.instrument == order.instrument
    assert printed.side == order.side
    assert printed.order_id == order.order_id


def test_an_unknown_client_order_id_raises_rather_than_improvising() -> None:
    """A run that diverged asks for a response the session never produced."""
    venue = ReplayVenue(recording=VenueRecording(responses=(_acked_response(),)))

    with pytest.raises(VenueRecordingError, match="diverged"):
        venue.submit(make_order(ordinal=99), decided_at_utc=EPOCH)


def test_a_recorded_refusal_is_counted_under_its_own_taxonomy_member() -> None:
    """The closed taxonomy survives the round trip through a `RejectEvent`'s free text."""
    response = RecordedResponse(
        client_order_id="fk-00000001",
        acknowledged_at_utc=None,
        submit_rejection=RecordedRejection(occurs_at_utc=_ACK_AT, reject_text=_reject_text()),
        ack_rejection=None,
        fills=(),
    )
    venue = ReplayVenue(recording=VenueRecording(responses=(response,)))

    answer = venue.submit(make_order(), decided_at_utc=EPOCH)

    assert isinstance(answer, RejectEvent)
    assert venue.report.rejection_counts[RejectReason.MIN_NOTIONAL] == 1
    assert venue.report.rejection_total == 1


def test_a_reject_text_with_no_taxonomy_member_is_refused_at_construction() -> None:
    """Finding this out mid-run means throwing the run away."""
    with pytest.raises(VenueRecordingError, match="no taxonomy member"):
        RecordedRejection(occurs_at_utc=_ACK_AT, reject_text="-1013 Filter failure: NOTIONAL")


def test_a_reject_text_naming_an_unknown_member_is_refused() -> None:
    """A catch-all bucket is how a class of refusal stops being visible in the report."""
    with pytest.raises(VenueRecordingError, match="closed rejection taxonomy"):
        RecordedRejection(occurs_at_utc=_ACK_AT, reject_text="-1013 x [margin_call] y")


def test_a_response_answering_a_submission_twice_is_refused() -> None:
    with pytest.raises(VenueRecordingError, match="exactly one answer"):
        RecordedResponse(
            client_order_id="fk-00000001",
            acknowledged_at_utc=_ACK_AT,
            submit_rejection=RecordedRejection(occurs_at_utc=_ACK_AT, reject_text=_reject_text()),
            ack_rejection=None,
            fills=(),
        )


def test_a_refused_submission_carrying_later_outcomes_is_refused() -> None:
    """An order the venue never acknowledged cannot have filled."""
    with pytest.raises(VenueRecordingError, match="after the ack it never received"):
        RecordedResponse(
            client_order_id="fk-00000001",
            acknowledged_at_utc=None,
            submit_rejection=RecordedRejection(occurs_at_utc=_ACK_AT, reject_text=_reject_text()),
            ack_rejection=None,
            fills=_acked_response().fills,
        )


def test_a_fill_filed_under_the_wrong_order_is_refused() -> None:
    with pytest.raises(VenueRecordingError, match="is filed under"):
        RecordedResponse(
            client_order_id="fk-00000002",
            acknowledged_at_utc=_ACK_AT,
            submit_rejection=None,
            ack_rejection=None,
            fills=_acked_response().fills,
        )


def test_an_aggressive_print_carrying_a_passive_markout_is_refused() -> None:
    """Charging adverse selection to a taker double-counts the spread."""
    with pytest.raises(VenueRecordingError, match="aggressive print"):
        RecordedFill(
            client_order_id="fk-00000001",
            venue_trade_id="sim-fk-00000001-0",
            phase=ResponsePhase.ACK,
            event_time_utc=_FILL_AT,
            quote_price=Decimal("64213.44"),
            base_quantity=Decimal("0.01"),
            fee_quote=Decimal("0.64"),
            passive_markout_quote=Decimal("0.02"),
        )


def test_a_print_of_nothing_is_refused() -> None:
    with pytest.raises(VenueRecordingError, match="not a print"):
        RecordedFill(
            client_order_id="fk-00000001",
            venue_trade_id="sim-fk-00000001-0",
            phase=ResponsePhase.ACK,
            event_time_utc=_FILL_AT,
            quote_price=Decimal("64213.44"),
            base_quantity=Decimal("0"),
            fee_quote=Decimal("0"),
            passive_markout_quote=Decimal("0"),
        )


def test_one_client_order_id_cannot_name_two_submissions() -> None:
    """It is the venue-side idempotency key, in a recording as much as on the wire."""
    with pytest.raises(VenueRecordingError, match="appears twice"):
        VenueRecording(responses=(_acked_response(), _acked_response()))


def test_resolving_an_ack_twice_is_refused() -> None:
    venue = ReplayVenue(recording=VenueRecording(responses=(_acked_response(),)))
    ack = venue.submit(make_order(), decided_at_utc=EPOCH)
    assert isinstance(ack, OrderAckEvent)
    venue.resolve_ack(ack)

    with pytest.raises(VenueSimulationError, match="resolved twice"):
        venue.resolve_ack(ack)


def test_resolving_an_ack_for_an_unsubmitted_order_is_refused() -> None:
    venue = ReplayVenue(recording=VenueRecording(responses=(_acked_response(),)))

    with pytest.raises(VenueSimulationError, match="not holding"):
        venue.resolve_ack(OrderAckEvent(order=make_order(), occurs_at_utc=_ACK_AT))


def test_a_decision_before_the_order_existed_is_refused() -> None:
    venue = ReplayVenue(recording=VenueRecording(responses=(_acked_response(),)))

    with pytest.raises(VenueSimulationError, match="before it was created"):
        venue.submit(make_order(), decided_at_utc=EPOCH - timedelta(seconds=1))


def test_a_recording_from_a_later_session_is_refused() -> None:
    """An ack that predates the decision belongs to a different run."""
    venue = ReplayVenue(recording=VenueRecording(responses=(_acked_response(),)))

    with pytest.raises(VenueRecordingError, match="different session"):
        venue.submit(
            make_order(created_at_utc=EPOCH), decided_at_utc=_ACK_AT + timedelta(seconds=1)
        )


def test_a_resting_print_is_replayed_only_at_the_bar_it_was_stamped_on() -> None:
    """Keyed on the close instant, so a recording whose bars do not line up is a failure."""
    client_order_id = "fk-00000001"
    bar = make_bar(open_time_utc=EPOCH + timedelta(minutes=5))
    response = RecordedResponse(
        client_order_id=client_order_id,
        acknowledged_at_utc=_ACK_AT,
        submit_rejection=None,
        ack_rejection=None,
        fills=(
            RecordedFill(
                client_order_id=client_order_id,
                venue_trade_id=f"sim-{client_order_id}-1",
                phase=ResponsePhase.BAR,
                event_time_utc=bar.close_time_utc,
                quote_price=Decimal("64000.00"),
                base_quantity=Decimal("0.01"),
                fee_quote=Decimal("0.12800000"),
                passive_markout_quote=Decimal("0.05"),
            ),
        ),
    )
    venue = ReplayVenue(recording=VenueRecording(responses=(response,)))
    ack = venue.submit(make_order(), decided_at_utc=EPOCH)
    assert isinstance(ack, OrderAckEvent)
    venue.resolve_ack(ack)

    assert venue.observe(make_bar()) == ()
    replayed = venue.observe(bar)

    assert len(replayed) == 1
    assert venue.report.passive_fill_count == 1
    assert venue.report.passive_markout_quote == Decimal("0.05")


def test_a_recorder_captures_a_backtest_session_a_replay_can_serve() -> None:
    """The round trip: record a real simulated session, replay it, get the same prints."""
    bar = make_bar()
    backtest = make_venue()
    recorder = VenueRecorder()
    backtest.observe(bar)
    order = make_order(limit_quote_price="64300.00")
    answer = backtest.submit(order, decided_at_utc=bar.close_time_utc)
    recorder.record_submission(answer)
    assert isinstance(answer, OrderAckEvent)
    recorder.record_ack_outcome(backtest.resolve_ack(answer))

    replay = ReplayVenue(recording=recorder.build(backtest.report))
    replayed_ack = replay.submit(order, decided_at_utc=bar.close_time_utc)
    assert isinstance(replayed_ack, OrderAckEvent)
    replay.resolve_ack(replayed_ack)

    assert [record.fill for record in replay.report.fills] == [
        record.fill for record in backtest.report.fills
    ]


def test_a_recorder_refuses_a_client_order_id_submitted_twice() -> None:
    recorder = VenueRecorder()
    recorder.record_submission(OrderAckEvent(order=make_order(), occurs_at_utc=_ACK_AT))

    with pytest.raises(VenueRecordingError, match="submitted twice"):
        recorder.record_submission(OrderAckEvent(order=make_order(), occurs_at_utc=_ACK_AT))


def test_a_recorder_refuses_a_fill_for_a_submission_it_never_saw() -> None:
    """A recorder wired in after the session started produces a recording with holes."""
    bar = make_bar()
    backtest = make_venue()
    backtest.observe(bar)
    answer = backtest.submit(
        make_order(limit_quote_price="64300.00"), decided_at_utc=bar.close_time_utc
    )
    assert isinstance(answer, OrderAckEvent)
    resolved = backtest.resolve_ack(answer)

    with pytest.raises(VenueRecordingError, match="never recorded"):
        VenueRecorder().record_ack_outcome(resolved)


def test_a_recorder_captures_a_refusal_that_arrives_when_the_ack_resolves() -> None:
    """An unfillable IOC is refused at the ack, and the recording has to carry that."""
    recorder = VenueRecorder()
    order = make_order()
    recorder.record_submission(OrderAckEvent(order=order, occurs_at_utc=_ACK_AT))
    recorder.record_ack_outcome(
        (
            RejectEvent(
                order=order,
                occurs_at_utc=_FILL_AT,
                reason=_reject_text(RejectReason.UNFILLABLE_DEPTH),
            ),
        )
    )

    recording = recorder.build(make_venue().report)
    venue = ReplayVenue(recording=recording)
    ack = venue.submit(order, decided_at_utc=EPOCH)
    assert isinstance(ack, OrderAckEvent)

    assert [type(event) for event in venue.resolve_ack(ack)] == [RejectEvent]
    assert venue.report.rejection_counts[RejectReason.UNFILLABLE_DEPTH] == 1


def test_a_recorder_captures_a_refused_submission() -> None:
    recorder = VenueRecorder()
    order = make_order()
    recorder.record_submission(
        RejectEvent(order=order, occurs_at_utc=_ACK_AT, reason=_reject_text())
    )

    recording = recorder.build(make_venue().report)

    assert recording.response_for(order.client_order_id).submit_rejection is not None
