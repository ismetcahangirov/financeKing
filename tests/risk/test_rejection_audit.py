"""A refusal is an audited decision, and the row has to be complete rather than present.

Two separate claims are checked here, and only the first is about output.

The audit row must carry **every** limit that was evaluated, each with its threshold and
its observed value -- not only the one that bound. The question asked in every
post-incident review is "how close were the others", and a row holding one limit cannot
answer it (`.claude/rules/append-only-audit.md`).

The universe check must run **before** any sizing arithmetic. That is a claim about call
ordering, not about the returned value: a rejection produced after the arithmetic ran
looks identical from the outside, and would still have priced a symbol the venue does not
list. It is asserted by handing the validator a marks mapping that raises if it is read.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest

from fking.domain import (
    Direction,
    DomainError,
    Instrument,
    Portfolio,
    Position,
    Signal,
    Venue,
)
from fking.risk.exposure import (
    ExposureLimits,
    LimitEvaluation,
    PreTradeContext,
    ViolationTally,
    validate_pre_trade,
)
from fking.risk.limits import RiskLimits

pytestmark = pytest.mark.unit

_AS_OF: Final = datetime(2026, 8, 1, tzinfo=UTC)

BTCUSDT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_notional_quote=Decimal("10"),
)
DOGEUSDT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="DOGEUSDT",
    base_asset="DOGE",
    quote_asset="USDT",
    tick_size=Decimal("0.00001"),
    lot_step=Decimal("1"),
    min_notional_quote=Decimal("10"),
)
MARKS_USD: Final = {BTCUSDT: Decimal("64000"), DOGEUSDT: Decimal("0.21")}

# Every ratio limit plus every absolute cap plus the order-level cap. A row missing from
# this set is a limit the reviewer cannot see the headroom of.
# The same refusal replayed twice: at-least-once delivery means a consumer will
# re-present it, and the tally must count intents rather than deliveries.
EXPECTED_REPEAT_VIOLATIONS: Final[int] = 2

EXPECTED_LIMIT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "max_position_equity_ratio",
        "max_position_notional_usd",
        "max_asset_exposure_ratio",
        "max_gross_exposure_ratio",
        "max_portfolio_notional_usd",
        "max_net_exposure_ratio",
        "min_free_margin_ratio",
        "max_single_order_notional_usd",
    }
)


def _clock() -> datetime:
    return _AS_OF


class _ExplodingMarks(Mapping[Instrument, Decimal]):
    """A marks mapping that fails the test if anything reads it.

    The point of the ordering assertion: a validator that sizes first and checks the
    universe afterwards returns exactly the same rejection, so only touching the inputs
    distinguishes the two implementations.
    """

    def __getitem__(self, key: Instrument) -> Decimal:
        raise AssertionError(f"sizing arithmetic priced {key.symbol} before the universe check")

    def __iter__(self) -> Iterator[Instrument]:
        raise AssertionError("sizing arithmetic enumerated marks before the universe check")

    def __len__(self) -> int:
        raise AssertionError("sizing arithmetic measured marks before the universe check")


def _signal(instrument: Instrument, direction: Direction = Direction.LONG) -> Signal:
    return Signal(
        strategy_id="breakout-v3",
        instrument=instrument,
        direction=direction,
        conviction=Decimal("0.7"),
        horizon=timedelta(hours=4),
        invalidation_quote_price=MARKS_USD[instrument] * Decimal("0.97"),
        rationale="audit test",
        decided_at_utc=_AS_OF,
    )


def _book_holding(instrument: Instrument, signed_base_quantity: Decimal) -> Portfolio:
    return Portfolio(
        as_of_utc=_AS_OF,
        positions=(
            Position(
                instrument=instrument,
                signed_base_quantity=signed_base_quantity,
                average_entry_quote_price=MARKS_USD[instrument],
                realised_pnl_quote=Decimal("0"),
                fee_quote_paid=Decimal("0"),
                opened_at_utc=_AS_OF - timedelta(hours=2),
                applied_fill_ids=frozenset(),
            ),
        ),
        cash_balances={},
    )


def test_a_breaching_signal_is_refused_with_every_evaluated_limit_in_the_audit_row() -> None:
    # 0.5 BTC at 64k is 32000 against an equity of 40000: the per-position ratio, the
    # per-asset ratio and the absolute position cap are all already breached.
    assessment = validate_pre_trade(
        signal=_signal(BTCUSDT),
        portfolio=_book_holding(BTCUSDT, Decimal("0.5")),
        marks_usd=MARKS_USD,
        context=PreTradeContext(
            equity_usd=Decimal("40000"),
            exposure_limits=ExposureLimits(),
            absolute_limits=RiskLimits(),
            tradable_symbols=frozenset({"BTCUSDT", "DOGEUSDT"}),
        ),
        clock=_clock,
    )

    assert not assessment.is_approved
    assert assessment.permitted_base_quantity == Decimal("0")
    assert assessment.rejection is not None
    assert assessment.rejection.rejected_at_utc == _AS_OF

    payload = assessment.audit_payload()
    assert payload["verdict"] == "rejected"
    rows = payload["limits_evaluated"]
    assert isinstance(rows, tuple)
    assert {row["limit_name"] for row in rows} == EXPECTED_LIMIT_NAMES
    # Complete, not merely present: threshold, observed value and headroom on every row,
    # including the rows that did not bind.
    for row in rows:
        assert row["threshold_usd"]
        assert row["observed_usd"] is not None
        assert row["headroom_usd"] is not None
    assert assessment.rejection.binding_limit_name in EXPECTED_LIMIT_NAMES
    assert "max_position" in " ".join(assessment.breached_limit_names)


def test_a_symbol_outside_the_universe_is_refused_before_any_sizing_arithmetic() -> None:
    assessment = validate_pre_trade(
        signal=_signal(DOGEUSDT),
        portfolio=_book_holding(BTCUSDT, Decimal("0.1")),
        marks_usd=_ExplodingMarks(),
        context=PreTradeContext(
            equity_usd=Decimal("50000"),
            exposure_limits=ExposureLimits(),
            absolute_limits=RiskLimits(),
            tradable_symbols=frozenset({"BTCUSDT"}),
        ),
        clock=_clock,
    )
    assert not assessment.is_approved
    assert assessment.rejection is not None
    assert assessment.rejection.binding_limit_name == "tradable_universe"
    assert assessment.evaluations == ()


def test_a_refused_signal_counts_against_the_strategy_though_no_exposure_was_taken() -> None:
    assessment = validate_pre_trade(
        signal=_signal(DOGEUSDT),
        portfolio=Portfolio(as_of_utc=_AS_OF, positions=(), cash_balances={}),
        marks_usd=MARKS_USD,
        context=PreTradeContext(
            equity_usd=Decimal("50000"),
            exposure_limits=ExposureLimits(),
            absolute_limits=RiskLimits(),
            tradable_symbols=frozenset({"BTCUSDT"}),
        ),
        clock=_clock,
    )
    tally = ViolationTally().with_assessment(assessment).with_assessment(assessment)
    assert tally.violation_count_for("breakout-v3") == EXPECTED_REPEAT_VIOLATIONS
    assert tally.violation_count_for("someone-else") == 0


def test_closing_an_over_limit_position_is_never_refused() -> None:
    """Refusing to close while over the gross cap is the limit working backwards.

    It would trap the portfolio in exactly the state the limit exists to prevent, which
    is why the reduce-only branch precedes every ceiling.
    """
    assessment = validate_pre_trade(
        signal=Signal(
            strategy_id="breakout-v3",
            instrument=BTCUSDT,
            direction=Direction.FLAT,
            conviction=Decimal("0.9"),
            horizon=timedelta(hours=1),
            invalidation_quote_price=None,
            rationale="flatten",
            decided_at_utc=_AS_OF,
        ),
        portfolio=_book_holding(BTCUSDT, Decimal("2")),
        marks_usd=MARKS_USD,
        context=PreTradeContext(
            equity_usd=Decimal("1000"),
            exposure_limits=ExposureLimits(),
            absolute_limits=RiskLimits(),
            tradable_symbols=frozenset({"BTCUSDT"}),
        ),
        clock=_clock,
    )
    assert assessment.is_approved
    assert assessment.permitted_notional_usd == Decimal("128000")


def test_a_configured_limit_above_its_ceiling_is_refused_and_names_both_numbers() -> None:
    with pytest.raises(ValueError, match="max_gross_exposure_ratio=4") as refusal:
        ExposureLimits(max_gross_exposure_ratio=Decimal("4"))
    # Never clamped: the operator who wrote 4 believed they were running at 4, and a
    # silent substitution leaves them wrong and uninformed.
    assert "3.00" in str(refusal.value)


def test_a_free_margin_floor_below_the_compiled_in_floor_is_refused() -> None:
    with pytest.raises(ValueError, match="min_free_margin_ratio=0"):
        ExposureLimits(min_free_margin_ratio=Decimal("0"))


def test_an_open_position_with_no_mark_refuses_rather_than_pricing_it_at_zero() -> None:
    """A missing mark reports headroom that does not exist, at the worst possible moment."""
    with pytest.raises(DomainError, match="must not be assumed zero"):
        validate_pre_trade(
            signal=_signal(BTCUSDT),
            portfolio=_book_holding(DOGEUSDT, Decimal("100")),
            marks_usd={BTCUSDT: Decimal("64000")},
            context=PreTradeContext(
                equity_usd=Decimal("50000"),
                exposure_limits=ExposureLimits(),
                absolute_limits=RiskLimits(),
                tradable_symbols=frozenset({"BTCUSDT", "DOGEUSDT"}),
            ),
            clock=_clock,
        )


def test_a_mutable_universe_is_refused_at_construction() -> None:
    """A `set` can be widened between two checks; a `frozenset` cannot."""
    with pytest.raises(DomainError, match="must be a frozenset"):
        PreTradeContext(
            equity_usd=Decimal("50000"),
            exposure_limits=ExposureLimits(),
            absolute_limits=RiskLimits(),
            # The suppression below is unavoidable: the runtime guard exists precisely
            # for the untyped callers mypy never sees, so exercising it needs a bad type.
            tradable_symbols={"BTCUSDT"},  # type: ignore[arg-type]
        )


def test_an_unknown_bound_kind_is_refused_rather_than_defaulting_to_a_direction() -> None:
    """A default would silently pick a comparison direction for an unrecognised limit."""
    with pytest.raises(DomainError, match="bound_kind must be"):
        LimitEvaluation(
            limit_name="invented",
            # Same reason as above: the guard is for values arriving without a type.
            bound_kind="sideways",  # type: ignore[arg-type]
            threshold_usd=Decimal("1"),
            observed_usd=Decimal("0"),
        )


def test_a_signal_on_a_symbol_with_no_mark_is_refused_before_sizing() -> None:
    """Sizing needs a price, and refusing is the only honest answer when there is none."""
    assessment = validate_pre_trade(
        signal=_signal(DOGEUSDT),
        portfolio=Portfolio(as_of_utc=_AS_OF, positions=(), cash_balances={}),
        marks_usd={BTCUSDT: Decimal("64000")},
        context=PreTradeContext(
            equity_usd=Decimal("50000"),
            exposure_limits=ExposureLimits(),
            absolute_limits=RiskLimits(),
            tradable_symbols=frozenset({"BTCUSDT", "DOGEUSDT"}),
        ),
        clock=_clock,
    )
    assert not assessment.is_approved
    assert assessment.rejection is not None
    assert assessment.rejection.binding_limit_name == "mark_available"
