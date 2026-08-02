"""Each AST check must catch its violation and must pass clean code.

The second half matters as much as the first. A check that flags everything gets
disabled within a week, and a check that flags nothing is indistinguishable from one
that is not wired up -- which is exactly how `make check` ends up green while the rule
it claims to enforce has been dead for months.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import pytest

from tools.checks import clock_isolation, money_types, naming, no_catch_safety

pytestmark = pytest.mark.unit


class TestMoneyTypes:
    def test_float_annotated_money_parameter_is_rejected(self) -> None:
        source = "def f(notional_usd: float) -> None: ...\n"
        failures = money_types.check_source(source, label="x.py", float_free=False)
        assert len(failures) == 1
        assert "notional_usd" in failures[0]

    def test_float_hidden_inside_an_optional_is_still_rejected(self) -> None:
        """`float | None` is the spelling that gets past a reviewer skimming for `: float`."""
        source = "def f(quote_price: float | None) -> None: ...\n"
        assert money_types.check_source(source, label="x.py", float_free=False)

    def test_decimal_annotated_money_parameter_is_accepted(self) -> None:
        source = "def f(notional_usd: Decimal) -> None: ...\n"
        assert money_types.check_source(source, label="x.py", float_free=False) == []

    def test_float_literal_is_rejected_in_a_float_free_package(self) -> None:
        source = "TAKER = 0.001\n"
        assert money_types.check_source(source, label="x.py", float_free=True)

    def test_float_literal_is_accepted_where_statistics_live(self) -> None:
        """backtest and data compute Sharpe ratios; sampling error dwarfs 2^-53."""
        source = "sharpe = 1.4\n"
        assert money_types.check_source(source, label="x.py", float_free=False) == []

    def test_a_non_money_float_annotation_is_accepted(self) -> None:
        source = "def f(timeout_seconds: float) -> None: ...\n"
        assert money_types.check_source(source, label="x.py", float_free=False) == []

    def test_a_boolean_literal_is_not_mistaken_for_a_float(self) -> None:
        assert money_types.check_source("flag = True\n", label="x.py", float_free=True) == []


class TestClockIsolation:
    @pytest.mark.parametrize(
        "source",
        [
            "as_of = datetime.now(UTC)\n",
            "as_of = datetime.datetime.utcnow()\n",
            "started = time.monotonic()\n",
            "started = time.perf_counter()\n",
            "as_of = date.today()\n",
            # `from time import monotonic` leaves no receiver to inspect.
            "started = monotonic()\n",
        ],
    )
    def test_reading_the_wall_clock_is_rejected(self, source: str) -> None:
        assert clock_isolation.check_source(source, label="x.py")

    @pytest.mark.parametrize(
        "source",
        [
            "as_of = clock.now()\n",
            "as_of = self._clock.now()\n",
            "def evaluate(bars, clock):\n    return clock.now()\n",
        ],
    )
    def test_an_injected_clock_is_accepted(self, source: str) -> None:
        """This is the pattern the rule exists to force; flagging it would kill the check.

        `clock.now()` is an attribute call exactly like `datetime.now()`, which is why
        the check keys on the receiver rather than the attribute name.
        """
        assert clock_isolation.check_source(source, label="x.py") == []


class TestNoCatchSafety:
    @pytest.mark.parametrize(
        "handler",
        ["SafetyViolation", "BaseException", "(ValueError, SafetyViolation)"],
    )
    def test_catching_a_forbidden_exception_is_rejected(self, handler: str) -> None:
        source = f"try:\n    pass\nexcept {handler}:\n    pass\n"
        assert no_catch_safety.check_source(source, label="x.py")

    def test_a_dotted_safety_violation_is_still_caught(self) -> None:
        source = "try:\n    pass\nexcept errors.SafetyViolation:\n    pass\n"
        assert no_catch_safety.check_source(source, label="x.py")

    def test_catching_a_specific_handleable_error_is_accepted(self) -> None:
        source = "try:\n    pass\nexcept TransientExchangeError:\n    pass\n"
        assert no_catch_safety.check_source(source, label="x.py") == []

    def test_pytest_raises_is_not_an_except_clause(self) -> None:
        """The sanctioned way to assert a SafetyViolation, and it must stay usable."""
        source = "with pytest.raises(SafetyViolation):\n    pass\n"
        assert no_catch_safety.check_source(source, label="x.py") == []


class TestNaming:
    @pytest.mark.parametrize("identifier", ["size", "price", "qty", "timeout", "pnl"])
    def test_ambiguous_identifier_is_rejected(self, identifier: str) -> None:
        source = f"def f({identifier}) -> None: ...\n"
        assert naming.check_source(source, label="x.py")

    @pytest.mark.parametrize(
        "identifier", ["base_quantity", "quote_price", "timeout_seconds", "realised_pnl_usd"]
    )
    def test_a_name_carrying_its_unit_is_accepted(self, identifier: str) -> None:
        source = f"def f({identifier}) -> None: ...\n"
        assert naming.check_source(source, label="x.py") == []

    def test_a_name_claiming_both_percent_and_fraction_is_rejected(self) -> None:
        assert naming.check_source("drawdown_fraction_pct = 1\n", label="x.py")

    def test_the_math_escape_suppresses_the_whole_module(self) -> None:
        source = f"{naming.MATH_ESCAPE}\ndef deflated(sr, n, t) -> None: ...\nsize = 1\n"
        assert naming.check_source(source, label="x.py") == []


CHECK_ENTRY_POINTS: Mapping[str, Callable[[Sequence[str]], int]] = {
    "clock_isolation": clock_isolation.main,
    "money_types": money_types.main,
    "naming": naming.main,
    "no_catch_safety": no_catch_safety.main,
}


@pytest.mark.parametrize("check_name", sorted(CHECK_ENTRY_POINTS))
def test_check_succeeds_when_there_is_nothing_to_scan(check_name: str) -> None:
    """An empty argv must exit 0, not raise -- `make checks` runs before most modules exist."""
    assert CHECK_ENTRY_POINTS[check_name]([]) == 0


@pytest.mark.parametrize("check_name", sorted(CHECK_ENTRY_POINTS))
def test_check_passes_over_the_real_source_tree(check_name: str) -> None:
    """The committed tree must be clean, or `make check` is green on a lie."""
    assert CHECK_ENTRY_POINTS[check_name](["src/fking"]) == 0
