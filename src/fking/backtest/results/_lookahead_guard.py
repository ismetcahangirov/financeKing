"""The look-ahead check: catches an entry rule that reads its own signal bar's range.

`docs/rules/no-lookahead.md` clause 3: "you may not trade on the close of the bar whose
close you used to decide." A decision derived from bar `[t0, t1)` has `decision_time >=
t1`, and the earliest price it could have transacted at is the *next* bar's open --
`open[i+1]`, the same alignment `docs/rules/no-lookahead.md`'s `forward_return` uses for
labels. A fill priced at the signal bar's own `high`, `low` or `close` needed to know that
bar's full range before it closed, which is exactly the leak the worked example in this
package's own mission describes: "the entry rule uses `high` of the signal bar to confirm
breakout, and the fill is simulated at the breakout level on that same bar."

This module checks the *fill*, not the strategy's source code. That is deliberate: a
strategy is free to read its own signal bar's high to decide *whether* to enter -- the
leak is entering *at* a price only knowable once the bar closed, not deciding on one. So
the check is a single, cheap, exhaustive predicate -- `fill_price == next_bar_open` --
rather than a static-analysis pass over strategy code, which could always be defeated by
a rewrite the checker had not seen.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from fking.backtest.results._finding import AuditCheck, AuditFinding, AuditStatus


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLC bar. Local to this guard: nothing else in the check needs more."""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True, slots=True)
class Entry:
    """One entry decision, as reported by the run: the bar it was decided on, and the
    price it actually filled at.
    """

    signal_bar: Bar
    fill_price: Decimal
    next_bar_open: Decimal


def check_entry_fills_are_achievable(entries: Sequence[Entry]) -> AuditFinding:
    """FAIL any run where an entry filled at a price its signal bar could not have offered
    before it closed.

    The only achievable fill for a decision taken on bar *i*'s close is `open[i+1]`.
    Anything else -- and in particular a fill equal to the signal bar's own `high` --
    means the fill was simulated at a price the strategy could not have transacted at,
    which is the mechanism, not merely the symptom, of a look-ahead leak.
    """
    if not entries:
        return AuditFinding(
            check=AuditCheck.LOOK_AHEAD,
            status=AuditStatus.INCONCLUSIVE,
            evidence="0 entries were supplied to the look-ahead guard; there is nothing to check",
        )

    leaking = tuple(entry for entry in entries if entry.fill_price != entry.next_bar_open)
    if leaking:
        sample = leaking[0]
        leaked_on_own_high = sample.fill_price == sample.signal_bar.high
        return AuditFinding(
            check=AuditCheck.LOOK_AHEAD,
            status=AuditStatus.FAIL,
            evidence=(
                f"{len(leaking)}/{len(entries)} entries filled at a price other than "
                f"their signal bar's next open; e.g. fill_price={sample.fill_price} "
                f"next_bar_open={sample.next_bar_open} "
                f"signal_bar.high={sample.signal_bar.high} "
                f"signal_bar.low={sample.signal_bar.low} "
                f"(filled at the signal bar's own high: {leaked_on_own_high})"
            ),
        )
    return AuditFinding(
        check=AuditCheck.LOOK_AHEAD,
        status=AuditStatus.PASS,
        evidence=(
            f"{len(entries)}/{len(entries)} entries filled at their signal bar's next "
            f"open; no entry priced itself off its own bar's range"
        ),
    )
