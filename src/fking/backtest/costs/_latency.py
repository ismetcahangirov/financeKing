"""Latency, modelled as three stages and charged as adverse drift.

`BACKTEST_ENGINE.md` section 4.4 splits latency into decision-to-send, send-to-ack and
ack-to-fill because the three have different causes and different fixes, and because
`decision_to_send` is the one usually omitted -- this system computes features, may
consult an LLM agent whose latency is seconds against a free-tier quota, applies risk
sizing, and only then sends.

Two things this module is not. It is not the engine's latency mechanism: the loop applies
latency by *scheduling* the ack and the fill at `t + latency`, so the market moves during
the interval exactly as it would live, and that is the only way to reproduce the case
where the price moved through a limit and the order never filled at all. And it is not a
substitute for that. What it produces is the **reported** latency term of the round-trip
cost -- the share of the total that is attributable to the interval between deciding and
being live -- so that a strategy whose costs are dominated by its own decision latency
says so on the tearsheet instead of hiding inside slippage.

The drift coefficient is a production measurement, in basis points of adverse mid
movement per second of exposure to the interval, and it is stored on the model like every
other calibrated parameter.

Durations are `timedelta`, not `_ms` integers. `.claude/rules/naming.md` allows `_ms` for
carrying a venue's own units and warns against computing with them, and the exact
microsecond conversion below keeps the arithmetic in `Decimal` without a float ever
appearing -- `timedelta.total_seconds()` returns a float and would be one.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, model_validator

from fking.backtest.costs._units import NonNegativeBps

_MICROSECOND: Final = timedelta(microseconds=1)
_MICROSECONDS_PER_SECOND: Final = Decimal("1000000")
_NO_TIME: Final = timedelta(0)


class LatencyProfile(BaseModel):
    """The three stages and the drift they expose an order to."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    decision_to_send: timedelta
    send_to_ack: timedelta
    ack_to_fill: timedelta
    adverse_drift_bps_per_second: NonNegativeBps

    @model_validator(mode="after")
    def _stages_do_not_run_backwards(self) -> Self:
        negative = [
            name
            for name, stage in (
                ("decision_to_send", self.decision_to_send),
                ("send_to_ack", self.send_to_ack),
                ("ack_to_fill", self.ack_to_fill),
            )
            if stage < _NO_TIME
        ]
        if negative:
            raise ValueError(f"latency stages must not be negative; {negative} are")
        return self

    @property
    def total_latency(self) -> timedelta:
        """Decision to fill, end to end."""
        return self.decision_to_send + self.send_to_ack + self.ack_to_fill

    @property
    def total_latency_seconds(self) -> Decimal:
        """`total_latency` as an exact `Decimal` count of seconds.

        Via integer microseconds rather than `total_seconds()`, which returns a float and
        would put a binary double into the expression that produces a cost.
        """
        return Decimal(self.total_latency // _MICROSECOND) / _MICROSECONDS_PER_SECOND

    def latency_bps(self) -> Decimal:
        """The cost of being late, in basis points of notional."""
        return self.adverse_drift_bps_per_second * self.total_latency_seconds
