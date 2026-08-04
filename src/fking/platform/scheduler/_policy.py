"""What a missed run means, per job. There is no correct global answer.

A process that was down over three scheduled fires can do three defensible things when it
comes back, and which one is right depends entirely on what the job does:

- **Gap detection** re-scans a window that ends now. Six catch-up runs produce five
  duplicate findings over the same range, so it runs **once**, stamped at the most recent
  missed fire.
- **Hourly ingestion** covers a distinct window per fire. Skipping five leaves five holes
  that no later run will notice, because "the last run succeeded" is the only state most
  catch-up logic checks. So it runs **all six, in ascending order**.
- **Reconciliation** converges to current exchange state and history is irrelevant to it.
  It runs **once, stamped now** -- not at a historical fire time, because a reconciliation
  labelled 04:00 that read the exchange at 10:00 is a record that will be misread later.

So the policy is declared per job and there is no default. A default here would be a
decision somebody skipped, and two of the three readings are wrong for any given job.

`due_fire_times` is pure: no clock, no database, no randomness. Every branch of the
outage behaviour is therefore assertable without waiting six hours or restarting a
process, which is what makes the misfire semantics testable at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from fking.platform.scheduler._errors import CatchUpBacklogTooLargeError
from fking.platform.scheduler._schedule import IntervalSchedule

__all__ = ["DueRuns", "MisfirePolicy", "due_fire_times"]


class MisfirePolicy(StrEnum):
    """What happens to the fires that elapsed while the job was not running."""

    SKIP_TO_LATEST = "skip_to_latest"
    """One run, stamped at the most recent missed fire time. For a job whose window ends
    at its fire time, so replaying earlier fires re-derives the same answer."""

    RUN_EVERY_MISSED = "run_every_missed"
    """One run per missed fire time, ascending. For a job whose fires each cover a
    distinct window, where a skipped fire is a hole nothing later will notice."""

    RUN_NOW = "run_now"
    """One run, stamped at the current instant rather than at a historical fire time. For
    a job that converges to present state, where the historical stamp would be a lie
    about what the run observed."""


@dataclass(frozen=True, slots=True)
class DueRuns:
    """The fire times to execute, and how many fires elapsed to produce them.

    `missed_fire_count` is reported separately because it is not derivable from the
    tuple: under `SKIP_TO_LATEST` a six-hour outage yields one fire time and six missed
    fires, and collapsing the two would make an outage indistinguishable from a normal
    tick in every log line and every metric.
    """

    fire_times: tuple[datetime, ...]
    missed_fire_count: int

    @property
    def is_catch_up(self) -> bool:
        """True when more than one fire elapsed, i.e. the process was behind."""
        return self.missed_fire_count > 1


def due_fire_times(
    *,
    schedule: IntervalSchedule,
    misfire_policy: MisfirePolicy,
    last_fire_utc: datetime | None,
    now_utc: datetime,
    max_catch_up_runs: int,
) -> DueRuns:
    """The runs owed at `now_utc`, given the last fire time already recorded.

    `last_fire_utc` is the cursor and it comes from the run ledger, never from process
    memory: process memory is empty exactly when the question matters, which is the tick
    immediately after a restart.

    A job that has never run is given a cursor one period behind the most recent fire, so
    it starts working immediately rather than idling for up to a full period. Note that a
    schedule has fire times *before* its anchor -- the anchor sets the phase, not the
    beginning -- so this is well defined even for an anchor in the future.
    """
    after_utc = (
        last_fire_utc
        if last_fire_utc is not None
        else schedule.fire_time_for_index(schedule.index_at_or_before(now_utc) - 1)
    )
    missed_fire_count = schedule.pending_fire_count(after_utc=after_utc, through_utc=now_utc)
    if missed_fire_count == 0:
        return DueRuns(fire_times=(), missed_fire_count=0)

    if misfire_policy is MisfirePolicy.SKIP_TO_LATEST:
        latest = schedule.fire_time_for_index(schedule.index_at_or_before(now_utc))
        return DueRuns(fire_times=(latest,), missed_fire_count=missed_fire_count)

    if misfire_policy is MisfirePolicy.RUN_NOW:
        return DueRuns(fire_times=(now_utc,), missed_fire_count=missed_fire_count)

    if missed_fire_count > max_catch_up_runs:
        raise CatchUpBacklogTooLargeError(
            f"{missed_fire_count} missed fire times are due but the job declared "
            f"max_catch_up_runs={max_catch_up_runs}. Running them all would flood every "
            f"downstream consumer and dropping all but the last would leave "
            f"{missed_fire_count - 1} windows unprocessed with nothing to notice it. "
            f"Backfill the range deliberately, then advance the cursor"
        )
    return DueRuns(
        fire_times=schedule.fire_times_in(after_utc=after_utc, through_utc=now_utc),
        missed_fire_count=missed_fire_count,
    )
