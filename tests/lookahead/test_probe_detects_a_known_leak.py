"""The probe, verified by breaking the guard on purpose.

If any test in this file passes without the probe raising, `test_probe.py` is asserting
`True == True` and every look-ahead result in the repository is unsupported. That is why
this file exists and why it is parametrised over `LEAKY_CASES` rather than enumerating
three of the four shapes: adding a leak shape to that tuple is how a newly discovered leak
class gets permanently guarded, and a probe that stops catching one fails here instead of
going quiet (`DATA_PIPELINE.md` section 7).
"""

from __future__ import annotations

import pytest

from tests.lookahead.leaky import LEAKY_CASES, LeakyCase

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("case", LEAKY_CASES, ids=lambda case: case.leak_shape)
def test_the_probe_raises_for_a_known_leak(case: LeakyCase) -> None:
    with pytest.raises(AssertionError):
        case.run_probe()


def test_every_known_leak_shape_is_covered() -> None:
    """A count, so that deleting a case is a failure rather than a quieter test run.

    Four shapes, from `DATA_PIPELINE.md` section 7's list of the ways look-ahead gets in.
    Raising this number is how a new shape is added; lowering it is a decision that has to
    be argued for in a diff.
    """
    expected_leak_shapes = 4
    assert len(LEAKY_CASES) == expected_leak_shapes
    assert len({case.leak_shape for case in LEAKY_CASES}) == expected_leak_shapes
