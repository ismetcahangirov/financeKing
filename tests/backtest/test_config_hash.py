"""`config_hash` is a run's identity, so it must be a function of the values alone.

Two properties are asserted, and the second is the one that is easy to lose. It must be
**stable** -- across two constructions, across a mapping written in a different order,
and across processes with different hash salts -- and it must be **sensitive**, changing
when any field that can change the run's output changes.

A hash that is stable but insensitive is worse than none: it makes the determinism check
pass vacuously, because two genuinely different runs compare as the same run and nobody
ever compares their digests.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from fking.backtest import RunConfig, RunConfigError, config_hash, derive_seed
from fking.domain import decode, encode
from tests.support.run_config import DEFAULT_START, config_for

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
BAKU = timezone(timedelta(hours=4))

# The child builds this config from literals rather than importing the shared helper, so
# what the subprocess proves is that the *digest* travels -- not that two processes
# running one helper agree with each other.
_CHILD_PROGRAM = """
from datetime import UTC, datetime
from decimal import Decimal

from fking.backtest import RunConfig, config_hash

print(
    config_hash(
        RunConfig(
            strategy_id="breakout-4h",
            strategy_version="1.3.0",
            symbols=("BTCUSDT",),
            parameters={
                "lookback_bars": Decimal("20"),
                "entry_threshold_bps": Decimal("12.5"),
            },
            start_utc=datetime(2026, 8, 1, 0, 0, tzinfo=UTC),
            end_utc=datetime(2026, 8, 2, 0, 0, tzinfo=UTC),
            run_seed=20260801,
            event_budget=10_000,
        )
    )
)
"""


def _hash_in_a_child(hash_seed: str) -> str:
    completed = subprocess.run(  # noqa: S603 - a fixed argv, no shell, nothing a caller supplied
        [sys.executable, "-c", _CHILD_PROGRAM],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_the_same_configuration_hashes_the_same_twice() -> None:
    assert config_hash(config_for()) == config_hash(config_for())


@pytest.mark.slow
def test_the_hash_survives_a_different_process_and_a_different_hash_salt() -> None:
    """`PYTHONHASHSEED` salts `hash()` per process; the digest must not depend on it.

    This is what rules out `hash()` and a mapping's `repr` as the implementation. Both
    pass every in-process assertion in this file and disagree between two machines --
    which is exactly where the determinism check is meant to be run.
    """
    salted = _hash_in_a_child("0")
    differently_salted = _hash_in_a_child("524287")

    assert salted == differently_salted
    assert salted == config_hash(config_for())


def test_the_parameter_mapping_order_does_not_change_the_hash() -> None:
    forwards = config_for(parameters={"alpha": Decimal("1"), "beta": Decimal("2")})
    backwards = config_for(parameters={"beta": Decimal("2"), "alpha": Decimal("1")})
    assert config_hash(forwards) == config_hash(backwards)


CHANGES: tuple[tuple[str, RunConfig], ...] = (
    ("strategy_id", config_for(strategy_id="breakout-8h")),
    ("strategy_version", config_for(strategy_version="1.3.1")),
    ("symbols", config_for(symbols=("BTCUSDT", "ETHUSDT"))),
    ("a parameter's value", config_for(parameters={"lookback_bars": Decimal("21")})),
    # A trailing zero is a different Decimal and must encode differently: `Decimal("20.0")`
    # and `Decimal("20")` are the same number and not the same input, and a hash that
    # conflates them cannot tell two points of a parameter sweep apart.
    ("a parameter's precision", config_for(parameters={"lookback_bars": Decimal("20.0")})),
    ("start_utc", config_for(start_utc=DEFAULT_START + timedelta(minutes=1))),
    ("window", config_for(window=timedelta(days=2))),
    ("run_seed", config_for(run_seed=20260802)),
    ("event_budget", config_for(event_budget=10_001)),
)


@pytest.mark.parametrize(("field_name", "changed"), CHANGES, ids=[change[0] for change in CHANGES])
def test_changing_any_field_changes_the_hash(field_name: str, changed: RunConfig) -> None:
    assert config_hash(changed) != config_hash(config_for()), (
        f"changing {field_name} left the config hash unchanged, so two different runs "
        f"share one identity"
    )


def test_a_config_round_trips_through_its_own_encoding() -> None:
    """The hash is taken over `encode`, so a value that encoding drops is a value it ignores."""
    original = config_for()
    rebuilt = decode(RunConfig, encode(original))
    assert rebuilt == original
    assert config_hash(rebuilt) == config_hash(original)


def test_parameters_cannot_be_mutated_through_the_reference_the_caller_kept() -> None:
    """`frozen=True` protects the binding; the mapping proxy protects what it is bound to."""
    parameters = {"lookback_bars": Decimal("20")}
    config = config_for(parameters=parameters)
    parameters["lookback_bars"] = Decimal("999")

    assert config.parameters["lookback_bars"] == Decimal("20")
    with pytest.raises(TypeError):
        config.parameters["lookback_bars"] = Decimal("999")  # type: ignore[index]  # the point


@pytest.mark.parametrize(
    ("kwargs", "expected_message"),
    [
        ({"strategy_id": "  "}, "must not be blank"),
        ({"symbols": ()}, "at least one instrument"),
        ({"symbols": ("BTCUSDT", "BTCUSDT")}, "duplicate"),
        ({"window": timedelta(0)}, "must follow"),
        ({"window": timedelta(days=-1)}, "must follow"),
        ({"run_seed": -1}, "must not be negative"),
        ({"run_seed": True}, "must be an int"),
        ({"event_budget": 0}, "must be positive"),
        # `True` satisfies `isinstance(x, int)`, and a budget of `True` is a budget of one.
        ({"event_budget": True}, "must be an int"),
    ],
)
def test_a_malformed_configuration_is_refused_at_construction(
    kwargs: Mapping[str, object], expected_message: str
) -> None:
    with pytest.raises(RunConfigError, match=expected_message):
        config_for(**kwargs)  # type: ignore[arg-type]  # deliberately wrong, one field at a time


def test_a_float_parameter_is_refused_rather_than_converted() -> None:
    """The rounding happened in the parser, before this call. Converting would hide it."""
    with pytest.raises(RunConfigError, match="not a float"):
        config_for(parameters={"entry_threshold_bps": 12.5})  # type: ignore[dict-item]  # the point


def test_a_naive_window_boundary_is_refused() -> None:
    with pytest.raises(RunConfigError, match="timezone-aware"):
        config_for(start_utc=datetime(2026, 8, 1, 0, 0))  # noqa: DTZ001 - the value under test


def test_a_non_utc_boundary_is_refused_even_when_it_names_the_right_instant() -> None:
    """04:00+04:00 *is* midnight UTC, and is still refused.

    Converting would accept a datetime whose offset was guessed wrong three modules
    upstream and record the guess as fact.
    """
    same_instant = datetime(2026, 8, 1, 4, 0, tzinfo=BAKU)
    assert same_instant == DEFAULT_START
    with pytest.raises(RunConfigError, match="must be UTC"):
        config_for(start_utc=same_instant)


def test_seeds_are_derived_per_label_and_reproducible() -> None:
    assert derive_seed(7, "venue") == derive_seed(7, "venue")
    assert derive_seed(7, "venue") != derive_seed(7, "strategy")
    assert derive_seed(7, "venue") != derive_seed(8, "venue")


def test_a_config_derives_its_own_seeds() -> None:
    assert config_for(run_seed=99).seed_for("venue") == derive_seed(99, "venue")


def test_a_blank_seed_label_is_refused() -> None:
    """An unlabelled stream is a stream two components can collide on."""
    with pytest.raises(RunConfigError, match="must not be blank"):
        derive_seed(7, "")


def test_a_bool_run_seed_is_refused_by_the_derivation_too() -> None:
    """`bool` type-checks as `int`, which is why this needs a runtime refusal at all."""
    with pytest.raises(RunConfigError, match="must be an int"):
        derive_seed(True, "venue")


def test_a_derived_seed_fits_a_readable_width() -> None:
    """A seed nobody can read back from a log is a seed nobody reproduces a run with."""
    assert 0 <= derive_seed(20260801, "venue") < 2**64
