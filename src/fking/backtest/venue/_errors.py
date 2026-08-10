"""The simulator's own failures.

A rejection is not one of these. A rejection is a modelled market outcome that the
venue reports as an event and counts in its report; an error here means the simulator
was handed something it cannot model at all -- an `exchangeInfo` payload missing the
filter that decides whether an order is legal, a book quote for the wrong instrument, a
fill asked for on an order the venue never acknowledged.

Keeping the two apart is the point. If a malformed filter payload arrived as
`RejectReason.LOT_SIZE`, a parse bug would appear on the tearsheet as a market
observation about capacity, and the run would still produce a number.
"""

from __future__ import annotations

from fking.backtest._errors import BacktestError


class VenueSimulationError(BacktestError):
    """The simulated venue was asked for something it cannot model.

    Terminal, like every other member of `BacktestError`. There is no continue: an
    order resolved against a book the venue could not build is an order filled at an
    invented price, and an invented price is indistinguishable from a real one once it
    is in the equity curve.
    """


class VenueRecordingError(VenueSimulationError):
    """A replay was asked for a response the recording does not hold, or holds twice.

    Separate from the base so a divergence between a run and its recording is legible in
    a traceback without reading the message: it is the failure that says the run stopped
    following the session it claims to reproduce, which is a finding about the run and
    never a reason to substitute a plausible response.
    """
