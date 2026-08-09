"""The forward return a decision could actually have achieved.

A label is not a feature and its point-in-time rule is the mirror image of one. A feature
stamped at `t` may use only what existed at `t`; a label stamped at `t` uses only what
happened *after* `t`, and the one thing it may never use is the bar that carried the
decision.

**Entry is the open of the bar after the decision bar.** The decision was taken on the
close of bar *i*, and that close is not knowable until bar *i* is over -- so the earliest
price the decision could have transacted at is the open of bar *i+1*. Measuring from
`close[i]` instead inflates the measured edge by exactly the move the feature was computed
from, and for any momentum or reversal feature built on that same close, that move *is*
the signal. It is the leak that most reliably produces a strategy which looks profitable
and is not (`docs/rules/no-lookahead.md`, `DATA_PIPELINE.md` section 7).

The consequence is testable rather than argued: perturb the close of bar *i* alone and a
correctly aligned label at *i* does not move. `tests/lookahead/` runs exactly that, over
this function and over a deliberately misaligned one, and requires the first to hold and
the second to fail.

**Exit is the close of the last bar at or before `decision + horizon`.** Not an
interpolation onto the horizon instant, which would be a price nobody could have traded
at. A decision with no bar inside its horizon yields no label at all, rather than a label
measured over whatever window happened to be loaded.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from fking.data.features.spec import FeatureObservation

__all__ = ["LabelPoint", "forward_return_label"]


@dataclass(frozen=True, slots=True)
class LabelPoint:
    """One realised outcome, carrying the three instants that make it checkable.

    `entry_time_utc` is stored rather than derived on demand because it is the field the
    alignment argument turns on: a reviewer can see that it is never equal to
    `decision_time_utc`, and a test can assert it.
    """

    decision_time_utc: datetime
    entry_time_utc: datetime
    exit_time_utc: datetime
    return_fraction: Decimal


def forward_return_label(
    observations: Sequence[FeatureObservation], *, horizon: timedelta
) -> tuple[LabelPoint, ...]:
    """Return from the next bar's open to the last close inside `horizon`.

    Exact throughout: a return is a ratio of two prices, both `Decimal`, and there is no
    estimate here whose sampling error would dwarf the arithmetic's.
    """
    points: list[LabelPoint] = []
    for index in range(len(observations) - 1):
        decision = observations[index]
        entry = observations[index + 1]
        deadline = decision.event_time_utc + horizon
        if entry.event_time_utc > deadline:
            # The next bar closes after the horizon is up, so the position could not have
            # been opened and closed inside it. No label, rather than one measured over a
            # window that is not the declared one.
            continue
        exit_index = index + 1
        for position in range(index + 2, len(observations)):
            if observations[position].event_time_utc > deadline:
                break
            exit_index = position
        points.append(
            LabelPoint(
                decision_time_utc=decision.event_time_utc,
                entry_time_utc=entry.event_time_utc,
                exit_time_utc=observations[exit_index].event_time_utc,
                return_fraction=(
                    observations[exit_index].close_quote_price / entry.open_quote_price
                    - Decimal("1")
                ),
            )
        )
    return tuple(points)
