"""Each AST check must catch its violation and must pass clean code.

The second half matters as much as the first. A check that flags everything gets
disabled within a week, and a check that flags nothing is indistinguishable from one
that is not wired up -- which is exactly how `make check` ends up green while the rule
it claims to enforce has been dead for months.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from tools.checks import (
    clock_isolation,
    feature_registry,
    metric_cardinality,
    money_types,
    naming,
    no_catch_safety,
)

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

    def test_a_banned_word_that_names_a_package_is_accepted(self) -> None:
        """`data` is banned as an identifier and is also the name of src/fking/data.
        The settings section that configures that package mirrors its name."""
        source = "data: DataSettings = Field(default_factory=DataSettings)\n"
        assert naming.check_source(source, label="x.py", exempt=frozenset({"data"})) == []

    def test_the_package_exemption_does_not_leak_to_other_banned_words(self) -> None:
        """Exempting one name must not disarm the check for the rest of the list."""
        source = "size = 1\n"
        assert naming.check_source(source, label="x.py", exempt=frozenset({"data"}))

    def test_package_names_are_read_from_the_tree(self, tmp_path: Path) -> None:
        """Derived, not hardcoded: the exemption can only be widened by creating a
        package, which the layering contract already reviews."""
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "__init__.py").touch()
        (tmp_path / "notapackage").mkdir()
        assert naming.package_names(tmp_path) == frozenset({"data"})


class TestFeatureRegistry:
    """The check that closes the "compute it outside the registry" route (#31).

    Its failure mode is silence: a computation nobody registered is a computation the
    adversarial probe never runs, and nothing else in the repository would say so.
    """

    def _package(self, tmp_path: Path, *, registry: str, features: Mapping[str, str]) -> Path:
        package = tmp_path / "fking"
        directory = package / "data" / "features"
        directory.mkdir(parents=True)
        (package / "data" / "__init__.py").touch()
        (directory / "__init__.py").touch()
        (directory / "registry.py").write_text(registry, encoding="utf-8")
        for name, source in features.items():
            (directory / name).write_text(source, encoding="utf-8")
        return package

    def test_an_unregistered_computation_is_rejected(self, tmp_path: Path) -> None:
        package = self._package(
            tmp_path,
            registry="FEATURES = {}\n",
            features={
                "compute.py": (
                    "def sneaky(observations: Sequence[FeatureObservation], "
                    "window: FeatureWindow) -> tuple[FeaturePoint, ...]:\n    return ()\n"
                )
            },
        )
        failures = feature_registry.check_package(package)
        assert len(failures) == 1
        assert "'sneaky'" in failures[0]
        assert "look-ahead probe never runs" in failures[0]

    def test_a_registered_computation_is_accepted(self, tmp_path: Path) -> None:
        package = self._package(
            tmp_path,
            registry="FEATURES = {'x': FeatureSpec(name='x', compute=declared)}\n",
            features={
                "compute.py": (
                    "def declared(observations: Sequence[FeatureObservation], "
                    "window: FeatureWindow) -> tuple[FeaturePoint, ...]:\n    return ()\n"
                )
            },
        )
        assert feature_registry.check_package(package) == []

    def test_a_helper_that_is_not_a_computation_is_not_demanded(self, tmp_path: Path) -> None:
        """Keyed on the `FeatureWindow` parameter, so a private helper is not swept in.

        A check that flags every function in the package gets disabled within a week.
        """
        package = self._package(
            tmp_path,
            registry="FEATURES = {}\n",
            features={"compute.py": "def _mean(values: Sequence[Decimal]) -> Decimal:\n    ...\n"},
        )
        assert feature_registry.check_package(package) == []

    def test_a_lambda_registration_leaves_the_function_unregistered(self, tmp_path: Path) -> None:
        """`compute=lambda ...` collects no name, so the real function is still reported.

        Which is correct rather than incidental: `definition_digest` refuses a lambda too,
        because a definition has to be diffable against the version it is registered under.
        """
        package = self._package(
            tmp_path,
            registry="FEATURES = {'x': FeatureSpec(compute=lambda o, w: declared(o, w))}\n",
            features={
                "compute.py": (
                    "def declared(observations: Sequence[FeatureObservation], "
                    "window: FeatureWindow) -> tuple[FeaturePoint, ...]:\n    return ()\n"
                )
            },
        )
        assert feature_registry.check_package(package)

    def test_a_strategy_importing_a_compute_module_is_rejected(self, tmp_path: Path) -> None:
        package = self._package(tmp_path, registry="FEATURES = {}\n", features={})
        strategy = package / "strategy"
        strategy.mkdir()
        (strategy / "__init__.py").touch()
        (strategy / "breakout.py").write_text(
            "from fking.data.features.compute import trailing_return_fraction\n", encoding="utf-8"
        )
        failures = feature_registry.check_package(package)
        assert len(failures) == 1
        assert "point-in-time mechanism is bypassed" in failures[0]

    def test_a_strategy_importing_the_store_or_the_spec_is_accepted(self, tmp_path: Path) -> None:
        """The sanctioned route. Refusing it would leave no way to read a feature at all,
        and a rule with no permitted path is a rule that gets deleted."""
        package = self._package(tmp_path, registry="FEATURES = {}\n", features={})
        strategy = package / "strategy"
        strategy.mkdir()
        (strategy / "__init__.py").touch()
        (strategy / "breakout.py").write_text(
            "from fking.data.features.store import FeatureStore\n"
            "from fking.data.features.spec import FeatureRef\n",
            encoding="utf-8",
        )
        assert feature_registry.check_package(package) == []

    def test_a_missing_registry_is_a_failure_rather_than_a_pass(self, tmp_path: Path) -> None:
        """A check that reports success when its subject is absent is worse than no check."""
        package = tmp_path / "fking"
        package.mkdir()
        assert feature_registry.check_package(package)


class TestMetricCardinality:
    def test_a_label_filled_from_a_correlation_id_is_rejected(self) -> None:
        """The label name is bounded and the value is not, which the registry cannot see."""
        source = "handle.increment(1, venue=event.correlation_id)\n"
        failures = metric_cardinality.check_source(source, label="x.py")
        assert len(failures) == 1
        assert "correlation_id" in failures[0]

    def test_a_label_filled_from_an_order_id_is_rejected(self) -> None:
        source = "handle.increment(1, reason=str(order_id))\n"
        assert metric_cardinality.check_source(source, label="x.py")

    def test_a_symbol_indexed_out_of_an_exchange_response_is_rejected(self) -> None:
        """A symbol from a venue payload is hostile input and is not the resolved universe."""
        source = 'handle.increment(1, symbol=payload["symbol"], venue="binance")\n'
        failures = metric_cardinality.check_source(source, label="x.py")
        assert len(failures) == 1
        assert "resolved universe" in failures[0]

    def test_a_client_order_id_indexed_out_of_a_response_is_rejected(self) -> None:
        source = 'handle.increment(1, venue=ack["clientOrderId"])\n'
        assert metric_cardinality.check_source(source, label="x.py")

    def test_a_validated_symbol_passed_as_a_label_is_accepted(self) -> None:
        """Guards the cases above: a check that flagged every label would pass them all."""
        source = 'handle.increment(1, symbol=instrument.symbol, venue="binance")\n'
        assert metric_cardinality.check_source(source, label="x.py") == []

    def test_an_identifier_passed_somewhere_that_is_not_a_label_is_ignored(self) -> None:
        """order_id belongs on a log line and a span attribute, and this is one."""
        source = 'log.info("order.accepted", order_id=order_id)\n'
        assert metric_cardinality.check_source(source, label="x.py") == []

    def test_the_declared_set_is_inside_its_budget_and_matches_the_grammar(self) -> None:
        failures, total = metric_cardinality.report_declared_cardinality()
        assert failures == []
        assert total > 0


CHECK_ENTRY_POINTS: Mapping[str, Callable[[Sequence[str]], int]] = {
    "clock_isolation": clock_isolation.main,
    "feature_registry": feature_registry.main,
    "metric_cardinality": metric_cardinality.main,
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
