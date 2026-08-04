"""The registered job set: declared in code, iterated in a fixed order.

Job definitions are code and are never persisted. That is the opposite of what a
general-purpose scheduler's job store does, and it is deliberate: a persisted job row
naming a callable that no longer exists is a startup failure with no diff to review, and
the fix is to hand-edit a database row. What needs persisting is *run state*, which is
data and lives in `fking.platform.scheduler._store`.

Iteration is sorted by `job_id` rather than by registration order. A tick that fires
three jobs must fire them in the same order in every process, or a test that asserts on
the sequence passes locally and fails under `pytest-randomly`'s import shuffle for
reasons that have nothing to do with the scheduler.
"""

from __future__ import annotations

from collections.abc import Iterator

from fking.platform.scheduler._errors import JobRegistrationError
from fking.platform.scheduler._spec import JobSpec

__all__ = ["JobCatalogue"]


class JobCatalogue:
    """Every job the beat will run. Empty until something registers one."""

    __slots__ = ("_jobs",)

    def __init__(self) -> None:
        self._jobs: dict[str, JobSpec] = {}

    def register(self, spec: JobSpec) -> JobSpec:
        """Add `spec`, refusing a `job_id` that is already registered.

        Refused rather than replaced. Two registrations of one id means which
        implementation runs depends on import order, and the symptom is a job that does
        the right thing on one machine and the previous thing on another.
        """
        existing = self._jobs.get(spec.job_id)
        if existing is not None:
            raise JobRegistrationError(
                f"job_id {spec.job_id!r} is already registered; two jobs sharing one id "
                f"share one row in the run ledger, so each would read the other's last "
                f"fire time as its own cursor"
            )
        self._jobs[spec.job_id] = spec
        return spec

    def __len__(self) -> int:
        return len(self._jobs)

    def __iter__(self) -> Iterator[JobSpec]:
        return iter(self._jobs[job_id] for job_id in sorted(self._jobs))

    def job_ids(self) -> tuple[str, ...]:
        """Every registered id, sorted."""
        return tuple(sorted(self._jobs))
