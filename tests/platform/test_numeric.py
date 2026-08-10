"""The process-wide decimal context, and the two places its trap set is one line wrong.

`configure_decimal_context()` is called first thing in `fking.platform.supervisor.start`,
so these are properties of every money value the process ever parses.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal, FloatOperation, getcontext, localcontext

import pytest

from fking.platform.numeric import DECIMAL_PRECISION, TRAPPED_SIGNALS, configure_decimal_context

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_decimal_context() -> Iterator[None]:
    """The function under test mutates process-wide state, which is the point of it.

    `localcontext()` installs a copy for the duration of the test, so the rest of the
    suite runs under the traps it chose rather than under the ones installed here.
    """
    with localcontext():
        yield


def test_the_context_carries_the_precision_the_database_and_the_corpus_agree_on() -> None:
    """38 matches NUMERIC(38, 18) and decimal128(38, 18), so the round trip through
    either cannot lose digits."""
    configure_decimal_context()

    assert getcontext().prec == DECIMAL_PRECISION
    assert all(getcontext().traps[signal] for signal in TRAPPED_SIGNALS)


def test_the_configured_context_traps_float_construction_and_ordering() -> None:
    """Both are silent without the trap, and the first is the one that puts a binary
    approximation into a price."""
    configure_decimal_context()
    approximate = 0.1

    with pytest.raises(FloatOperation):
        Decimal(approximate)
    with pytest.raises(FloatOperation):
        _ = Decimal("0.1") < approximate


def test_equality_against_a_float_stays_silent_and_answers_false() -> None:
    """The documented boundary of the trap, asserted so nobody relies on it.

    CPython leaves equality comparisons and explicit conversions silent even with
    `FloatOperation` trapped, so this is the one float mistake the decimal context cannot
    catch. `tools/checks/money_types.py` is what keeps a float away from the comparison.
    """
    configure_decimal_context()
    approximate = 0.1

    assert (Decimal("0.1") == approximate) is False


def test_ordinary_inexact_arithmetic_still_works_under_the_configured_context() -> None:
    """The trap set stops at the anomalies that mean a value was altered against the
    caller's intent. `Inexact` and `Rounded` are not among them: one third signals both,
    so trapping either -- or their common base -- leaves the process unable to compute a
    fee. This is the mistake the trap list is one line away from at all times.
    """
    configure_decimal_context()

    assert Decimal("1") / Decimal("3") > Decimal("0.333")


def test_configuring_the_context_twice_is_the_same_as_once() -> None:
    """The supervisor calls it before it has proved anything else about the process; a
    second call must not be a reason to abort a start that is otherwise fine."""
    configure_decimal_context()
    first = getcontext().copy()
    configure_decimal_context()

    assert getcontext().prec == first.prec
    assert getcontext().rounding == first.rounding
    assert getcontext().traps == first.traps
