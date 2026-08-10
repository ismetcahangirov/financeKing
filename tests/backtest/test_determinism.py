"""Two runs of one `config_hash` must produce byte-identical traces.

This is the check that outranks everything else on the queue when it fails
(`BACKTEST_ENGINE.md` section 5), so it is also the check that most needs proving it can
fail. `test_an_unseeded_draw_is_caught` is the deliberate defect: a handler drawing from
the machine's entropy instead of from `context.seed_for`. If that test ever passes, the
comparison below has stopped comparing anything and every determinism result the engine
has reported is void.

Same shape as the look-ahead probe, which is verified against features that are known to
leak, and for the same reason: a gate that has never rejected anything has not been
shown to be a gate.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fking.backtest import EventLoop, RunTrace, config_hash
from tests.backtest.registration_support import (
    PATH_LABEL,
    REGISTERED,
    RecordingReporter,
)
from tests.support.backtest_events import DrawingHandler, RecordingHandler, bar_events
from tests.support.run_config import config_for

pytestmark = pytest.mark.unit

START = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
BAR_COUNT = 12


def _run(*, use_run_seed: bool) -> RunTrace:
    config = config_for(start_utc=START)
    return EventLoop(
        config,
        DrawingHandler(use_run_seed=use_run_seed),
        registration=REGISTERED,
        reporter=RecordingReporter(),
        path_label=PATH_LABEL,
    ).run(bar_events(START, how_many=BAR_COUNT))


def test_same_config_twice_produces_an_identical_trace() -> None:
    """The acceptance criterion, stated directly."""
    first = _run(use_run_seed=True)
    second = _run(use_run_seed=True)

    assert first.config_hash == second.config_hash
    assert first.digest == second.digest
    assert first.entries == second.entries
    assert first.event_count == BAR_COUNT * 2  # each bar wakes one timer


def test_same_config_twice_agrees_entry_by_entry_and_not_only_on_the_digest() -> None:
    """A digest comparison alone cannot say *where* two runs diverged.

    Asserting the entries too means a failure names the first differing instant, which
    is where an investigation into a determinism failure has to start.
    """
    first = _run(use_run_seed=True)
    second = _run(use_run_seed=True)

    divergences = [
        (left, right)
        for left, right in zip(first.entries, second.entries, strict=True)
        if left != right
    ]
    assert divergences == []


def test_an_unseeded_draw_is_caught() -> None:
    """The deliberate defect. If this passes, the check above proves nothing.

    The two runs share a `config_hash` -- the configuration is identical -- and their
    traces must therefore differ *only* because the handler took its randomness from
    somewhere the run seed does not reach.
    """
    first = _run(use_run_seed=False)
    second = _run(use_run_seed=False)

    assert first.config_hash == second.config_hash
    assert first.digest != second.digest, (
        "an unseeded draw produced an identical trace, so the determinism comparison is "
        "insensitive to exactly the defect it exists to catch"
    )


def test_the_seeded_draws_are_the_ones_the_run_seed_dictates() -> None:
    """Reproducibility is a property of the numbers, not only of the digest over them."""
    config = config_for(start_utc=START)
    first = DrawingHandler(use_run_seed=True)
    second = DrawingHandler(use_run_seed=True)

    EventLoop(
        config, first, registration=REGISTERED, reporter=RecordingReporter(), path_label=PATH_LABEL
    ).run(bar_events(START, how_many=BAR_COUNT))
    EventLoop(
        config, second, registration=REGISTERED, reporter=RecordingReporter(), path_label=PATH_LABEL
    ).run(bar_events(START, how_many=BAR_COUNT))

    assert first.drawn == second.drawn
    assert len(set(first.drawn)) > 1, "a stream that repeats one value would hide a reseed"


def test_a_different_run_seed_changes_the_trace_and_the_identity_together() -> None:
    """Changing the seed must move both, or a re-seeded run would masquerade as a repeat."""
    one = EventLoop(
        config_for(start_utc=START, run_seed=1),
        DrawingHandler(use_run_seed=True),
        registration=REGISTERED,
        reporter=RecordingReporter(),
        path_label=PATH_LABEL,
    ).run(bar_events(START, how_many=BAR_COUNT))
    two = EventLoop(
        config_for(start_utc=START, run_seed=2),
        DrawingHandler(use_run_seed=True),
        registration=REGISTERED,
        reporter=RecordingReporter(),
        path_label=PATH_LABEL,
    ).run(bar_events(START, how_many=BAR_COUNT))

    assert one.config_hash != two.config_hash
    assert one.digest != two.digest


def test_the_trace_digest_changes_when_the_events_change() -> None:
    """A digest insensitive to its input would make every comparison above vacuous."""
    config = config_for(start_utc=START)
    shorter = EventLoop(
        config,
        RecordingHandler(),
        registration=REGISTERED,
        reporter=RecordingReporter(),
        path_label=PATH_LABEL,
    ).run(bar_events(START, how_many=3))
    longer = EventLoop(
        config,
        RecordingHandler(),
        registration=REGISTERED,
        reporter=RecordingReporter(),
        path_label=PATH_LABEL,
    ).run(bar_events(START, how_many=4))

    assert shorter.config_hash == longer.config_hash
    assert shorter.digest != longer.digest


def test_the_digest_is_stable_across_two_traces_built_from_equal_entries() -> None:
    """Equal entries, equal digest -- so a difference in the digest is a real difference."""
    config = config_for(start_utc=START)
    first = EventLoop(
        config,
        RecordingHandler(),
        registration=REGISTERED,
        reporter=RecordingReporter(),
        path_label=PATH_LABEL,
    ).run(bar_events(START, how_many=5))
    second = EventLoop(
        config,
        RecordingHandler(),
        registration=REGISTERED,
        reporter=RecordingReporter(),
        path_label=PATH_LABEL,
    ).run(bar_events(START, how_many=5))

    assert first.entries == second.entries
    assert first.digest == second.digest
    assert first.config_hash == config_hash(config)
