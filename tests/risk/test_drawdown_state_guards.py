"""Every way persisted drawdown state can arrive malformed, and its refusal.

These are the branches that decide what happens when the row on disk is not what the
code expects — a schema change, a partial write, a hand-edited row during an incident,
a JSON decoder that turned an equity string into a float on the way past. Every one of
them is a path where the tempting behaviour is to fill in a plausible value and carry
on, and every plausible fill-in widens a limit.

The guards are therefore tested one at a time rather than left to whatever the property
tests happen to reach: an untested `raise` is a `raise` that can be turned into a
`return` by a later edit with nothing going red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Final

import pytest

from fking.risk.drawdown import (
    DrawdownBudgets,
    DrawdownState,
    DrawdownStateError,
    EquityMark,
    evaluate,
    from_row,
    open_first_time,
    restore,
    to_row,
    with_equity,
)

pytestmark = pytest.mark.unit

_MIDDAY: Final = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)
_DAY_START: Final = datetime(2026, 3, 14, 0, 0, tzinfo=UTC)


def _restore_spoiled(**overrides: object) -> DrawdownState:
    """Call `restore` with a valid argument set and exactly one field spoiled.

    The `type: ignore` is unavoidable, and it is the whole point of these tests: the
    spoiled values are statically invalid, and a caller that reads them out of a database
    row carries no static type at all. What is being asserted is that the *runtime* guard
    refuses them, which is the only guard that row has.
    """
    arguments: dict[str, object] = {
        "scope": "strategy",
        "subject_id": "s-1",
        "peak_equity_usd": Decimal("100000"),
        "current_equity_usd": Decimal("98000"),
        "day_start_utc": _DAY_START,
        "day_open_equity_usd": Decimal("100000"),
        "rolling_marks": (EquityMark(_MIDDAY, Decimal("98000")),),
        "observed_at_utc": _MIDDAY,
        "latched_breach": None,
    } | overrides
    return restore(**arguments)  # type: ignore[arg-type]


def _valid_row() -> dict[str, object]:
    return dict(
        to_row(
            open_first_time(
                scope="strategy",
                subject_id="s-1",
                opening_equity_usd=Decimal("100000"),
                as_of_utc=_MIDDAY,
            )
        )
    )


# --- construction guards ----------------------------------------------------------


def test_a_non_datetime_observation_moment_is_refused() -> None:
    with pytest.raises(DrawdownStateError, match="must be a datetime"):
        _restore_spoiled(observed_at_utc="2026-03-14T12:00:00+00:00")


def test_an_aware_but_non_utc_moment_is_refused_rather_than_converted() -> None:
    """`astimezone(UTC)` would launder a wrong guess made upstream into a confident value.

    The offset that matters here is the one that moves the 00:00 boundary: a +04:00
    anchor resets the daily budget at 20:00 UTC, every day, silently.
    """
    baku = timezone(timedelta(hours=4))
    with pytest.raises(DrawdownStateError, match="must be UTC"):
        _restore_spoiled(observed_at_utc=datetime(2026, 3, 14, 16, 0, tzinfo=baku))


def test_a_float_equity_is_refused_by_name() -> None:
    """The error names `float` specifically because its rounding predates this call."""
    with pytest.raises(DrawdownStateError, match="not a float"):
        _restore_spoiled(peak_equity_usd=100000.0)


def test_a_non_decimal_equity_is_refused() -> None:
    with pytest.raises(DrawdownStateError, match="must be a Decimal"):
        _restore_spoiled(current_equity_usd="98000")


def test_a_non_finite_equity_is_refused() -> None:
    """`Decimal('NaN')` compares unequal to itself and would make every ratio silent."""
    with pytest.raises(DrawdownStateError, match="must be finite"):
        _restore_spoiled(day_open_equity_usd=Decimal("NaN"))


def test_zero_equity_is_refused_because_every_ratio_divides_by_it() -> None:
    with pytest.raises(DrawdownStateError, match="must be positive"):
        _restore_spoiled(day_open_equity_usd=Decimal("0"))


def test_a_day_anchor_off_the_utc_boundary_is_refused() -> None:
    """An anchor at an arbitrary instant makes the reset unrepeatable across a replay."""
    with pytest.raises(DrawdownStateError, match="not a 00:00 UTC boundary"):
        _restore_spoiled(day_start_utc=datetime(2026, 3, 14, 0, 30, tzinfo=UTC))


def test_unordered_rolling_marks_are_refused() -> None:
    """Out-of-order marks would make window pruning drop the wrong end."""
    with pytest.raises(DrawdownStateError, match="ordered by observed_at_utc"):
        _restore_spoiled(
            rolling_marks=(
                EquityMark(_MIDDAY, Decimal("98000")),
                EquityMark(_DAY_START, Decimal("100000")),
            )
        )


def test_a_naive_equity_mark_moment_is_refused() -> None:
    with pytest.raises(DrawdownStateError, match="timezone-aware"):
        EquityMark(datetime(2026, 3, 14, 12, 0), Decimal("1"))  # noqa: DTZ001 - the point


def test_a_naive_observation_time_on_a_transition_is_refused() -> None:
    state = open_first_time(
        scope="strategy",
        subject_id="s-1",
        opening_equity_usd=Decimal("100000"),
        as_of_utc=_MIDDAY,
    )
    with pytest.raises(DrawdownStateError, match="timezone-aware"):
        with_equity(
            state,
            equity_usd=Decimal("99000"),
            as_of_utc=datetime(2026, 3, 14, 13, 0),  # noqa: DTZ001 - the point of the test
        )


# --- decoder guards ---------------------------------------------------------------


def test_a_row_whose_scope_is_unknown_is_refused() -> None:
    with pytest.raises(DrawdownStateError, match="strategy or portfolio"):
        from_row(_valid_row() | {"scope": "book"})


def test_a_row_with_a_blank_subject_is_refused() -> None:
    """A blank subject id produces state that belongs to nothing and reconciles to nothing."""
    with pytest.raises(DrawdownStateError, match="non-empty text"):
        from_row(_valid_row() | {"subject_id": "   "})


def test_a_row_whose_equity_arrives_as_a_number_is_refused() -> None:
    """A JSON number has already been through a float by the time it is read."""
    with pytest.raises(DrawdownStateError, match="already been through a float"):
        from_row(_valid_row() | {"current_equity_usd": 98000})


def test_a_row_whose_moment_is_not_a_string_is_refused() -> None:
    with pytest.raises(DrawdownStateError, match="ISO-8601 string"):
        from_row(_valid_row() | {"observed_at_utc": 1773489600})


def test_a_row_with_an_empty_rolling_window_is_refused() -> None:
    with pytest.raises(DrawdownStateError, match="non-empty sequence"):
        from_row(_valid_row() | {"rolling_marks": ()})


def test_a_row_whose_rolling_window_is_a_string_is_refused() -> None:
    """A `str` is a `Sequence`, so the type check has to exclude it explicitly."""
    with pytest.raises(DrawdownStateError, match="non-empty sequence"):
        from_row(_valid_row() | {"rolling_marks": "not-marks"})


def test_a_row_whose_rolling_mark_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(DrawdownStateError, match="rolling mark must be a mapping"):
        from_row(_valid_row() | {"rolling_marks": ("2026-03-14T12:00:00+00:00",)})


def test_a_row_whose_latched_breach_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(DrawdownStateError, match="mapping or null"):
        from_row(_valid_row() | {"latched_breach": "drawdown"})


def test_a_row_whose_breach_names_an_unknown_limit_is_refused() -> None:
    """An unknown limit name means the schema moved; guessing which limit it was is worse."""
    breach = {
        "limit_name": "weekly_loss",
        "observed_ratio": "0.2",
        "budget_ratio": "0.15",
        "breached_at_utc": _MIDDAY.isoformat(),
        "response": "suspend_strategy",
    }
    with pytest.raises(DrawdownStateError, match="limit_name is unknown"):
        from_row(_valid_row() | {"latched_breach": breach})


def test_a_row_whose_breach_ratio_is_not_a_string_is_refused() -> None:
    breach = {
        "limit_name": "drawdown",
        "observed_ratio": 0.2,
        "budget_ratio": "0.15",
        "breached_at_utc": _MIDDAY.isoformat(),
        "response": "suspend_strategy",
    }
    with pytest.raises(DrawdownStateError, match="observed_ratio must be a decimal string"):
        from_row(_valid_row() | {"latched_breach": breach})


def test_a_row_whose_breach_ratio_is_negative_is_refused() -> None:
    """A negative consumed fraction reads as headroom that the next loss does not have."""
    breach = {
        "limit_name": "drawdown",
        "observed_ratio": "-0.2",
        "budget_ratio": "0.15",
        "breached_at_utc": _MIDDAY.isoformat(),
        "response": "suspend_strategy",
    }
    with pytest.raises(DrawdownStateError, match="finite fraction"):
        from_row(_valid_row() | {"latched_breach": breach})


def test_a_latched_breach_survives_the_persisted_round_trip() -> None:
    """The halt is only durable if the breach itself is written and read back."""
    state = open_first_time(
        scope="portfolio",
        subject_id="p-1",
        opening_equity_usd=Decimal("100000"),
        as_of_utc=_MIDDAY,
    )
    breached = with_equity(
        state, equity_usd=Decimal("85000"), as_of_utc=_MIDDAY + timedelta(days=2)
    )
    verdict = evaluate(
        breached,
        DrawdownBudgets(
            scope="portfolio",
            drawdown_ratio=Decimal("0.10"),
            daily_loss_ratio=Decimal("0.03"),
        ),
    )
    assert verdict.breach is not None

    revived = from_row(to_row(verdict.state))
    assert revived == verdict.state
    assert revived.latched_breach is not None
    assert revived.latched_breach.response == "request_kill_switch_trip"
