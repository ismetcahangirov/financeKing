"""A strategy whose entry rule reads its own signal bar's `high` is caught by the
look-ahead check rather than producing good numbers.

The scenario is this package's own worked example: a breakout strategy enters when a
bar's `high` clears a threshold, and a leaky implementation fills at that same bar's
`high` -- a price it could not have known until the bar had already closed. The corrected
implementation defers the fill to the next bar's `open`, and Sharpe drops when it does.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from decimal import Decimal

import pytest

from fking.backtest.results import (
    AuditCheck,
    AuditStatus,
    Bar,
    Entry,
    check_entry_fills_are_achievable,
)

pytestmark = pytest.mark.unit

# A breakout level each bar's high either clears or does not. Deterministic, no seed
# needed: the series is written out so every entry and its outcome are visible in the
# diff, the same posture `tests/lookahead/leaky.py` takes for its own fixed closes. Every
# bar that clears the level is followed by one that gives some of the move back, which is
# what makes a fill at the level itself look better than a fill at the next bar's open --
# the same inflation `docs/rules/no-lookahead.md` describes for a label measured from the
# decision bar's own close, one step removed to the fill price instead of the label.
_BREAKOUT_LEVEL: Decimal = Decimal("100")

_BARS: tuple[Bar, ...] = tuple(
    Bar(open=Decimal(o), high=Decimal(h), low=Decimal(low), close=Decimal(c))
    for o, h, low, c in (
        ("98", "101", "97", "99"),  # clears the level
        ("99", "100", "96", "98"),  # does not clear
        ("98", "106", "95", "103"),  # clears
        ("104", "103", "99", "101"),  # does not clear
        ("101", "115", "100", "110"),  # clears
        ("109", "108", "103", "106"),  # does not clear
        ("106", "120", "104", "116"),  # clears
        ("113", "111", "105", "108"),  # does not clear (last bar; not a signal bar)
    )
)


def _breakout_entries(bars: Sequence[Bar], *, leaky: bool) -> tuple[Entry, ...]:
    """One entry per bar that clears `_BREAKOUT_LEVEL`.

    `leaky=True` fills at the breakout level itself, on the strength of having seen the
    signal bar's own `high` clear it -- "the entry rule uses `high` of the signal bar to
    confirm breakout, and the fill is simulated at the breakout level on that same bar."
    That is a price only knowable once the bar's range is known, i.e. once the bar has
    closed. `leaky=False` defers the fill to the next bar's open, the only price a
    decision taken at bar close could actually have achieved.
    """
    entries: list[Entry] = []
    for index, bar in enumerate(bars[:-1]):
        if bar.high <= _BREAKOUT_LEVEL:
            continue
        next_open = bars[index + 1].open
        entries.append(
            Entry(
                signal_bar=bar,
                fill_price=_BREAKOUT_LEVEL if leaky else next_open,
                next_bar_open=next_open,
            )
        )
    return tuple(entries)


def _sharpe(returns: Sequence[Decimal]) -> float:
    """A minimal, un-annualised Sharpe: mean over sample stdev. Enough to show direction."""
    series = [float(r) for r in returns]
    stdev = statistics.pstdev(series)
    if stdev == 0.0:
        return 0.0
    return statistics.fmean(series) / stdev


def _one_bar_forward_returns(entries: Sequence[Entry], bars: Sequence[Bar]) -> tuple[Decimal, ...]:
    """Return from each entry's fill price to the close of the bar after the signal bar."""
    by_signal_bar = {id(entry.signal_bar): entry for entry in entries}
    returns: list[Decimal] = []
    for index, bar in enumerate(bars[:-1]):
        entry = by_signal_bar.get(id(bar))
        if entry is None:
            continue
        exit_close = bars[index + 1].close
        returns.append((exit_close - entry.fill_price) / entry.fill_price)
    return tuple(returns)


def test_the_leaky_entry_rule_is_caught_by_the_look_ahead_check() -> None:
    leaky_entries = _breakout_entries(_BARS, leaky=True)
    assert leaky_entries, "the fixture must produce at least one breakout"

    result = check_entry_fills_are_achievable(leaky_entries)

    assert result.check is AuditCheck.LOOK_AHEAD
    assert result.status is AuditStatus.FAIL
    assert "own high" in result.evidence or "signal bar" in result.evidence


def test_the_corrected_entry_rule_passes_the_look_ahead_check() -> None:
    corrected_entries = _breakout_entries(_BARS, leaky=False)

    result = check_entry_fills_are_achievable(corrected_entries)

    assert result.status is AuditStatus.PASS


def test_deferring_the_fill_to_the_next_bar_open_degrades_the_measured_edge() -> None:
    """The leak does not merely fail a check -- it is why the leaky version looks good.

    Filling at the signal bar's own high buys at a price already partway through the
    move that triggered the entry, so every return realised from that price captures
    less of the continuation than a fill at the next bar's open would. The corrected
    Sharpe on this fixture is markedly worse, exactly as the mission's own worked
    example describes ("Sharpe drops to 0.9 when deferred to next open").
    """
    leaky_entries = _breakout_entries(_BARS, leaky=True)
    corrected_entries = _breakout_entries(_BARS, leaky=False)

    leaky_sharpe = _sharpe(_one_bar_forward_returns(leaky_entries, _BARS))
    corrected_sharpe = _sharpe(_one_bar_forward_returns(corrected_entries, _BARS))

    assert corrected_sharpe < leaky_sharpe, (
        f"corrected Sharpe {corrected_sharpe} should be lower than the leaky Sharpe "
        f"{leaky_sharpe}; a look-ahead leak produces good numbers, not a small change"
    )


def test_zero_entries_is_inconclusive_rather_than_a_silent_pass() -> None:
    result = check_entry_fills_are_achievable(())
    assert result.status is AuditStatus.INCONCLUSIVE
