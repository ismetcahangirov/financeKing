"""Registration refusals: what a job must declare before the beat will run it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fking.platform.scheduler import (
    IntervalSchedule,
    JobCatalogue,
    JobFire,
    JobRegistrationError,
    JobSpec,
    MisfirePolicy,
)

pytestmark = pytest.mark.unit

ANCHOR = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
HOURLY = IntervalSchedule(period=timedelta(hours=1), anchor_utc=ANCHOR)
REGISTERED_JOBS = 3


async def _noop(fire: JobFire) -> None:
    del fire


def _spec(
    *,
    job_id: str = "data.ingest_klines",
    misfire_policy: MisfirePolicy = MisfirePolicy.RUN_EVERY_MISSED,
    max_catch_up_runs: int = 24,
) -> JobSpec:
    return JobSpec(
        job_id=job_id,
        schedule=HOURLY,
        misfire_policy=misfire_policy,
        max_catch_up_runs=max_catch_up_runs,
        run=_noop,
    )


def test_a_job_declared_without_a_misfire_policy_is_refused_and_the_error_names_it() -> None:
    """The first acceptance criterion of #108, and it is enforced by the type rather than
    by a check: `misfire_policy` is a required keyword-only field with no default, so
    omitting it cannot reach a running beat."""
    declared = {
        "job_id": "data.ingest_klines",
        "schedule": HOURLY,
        "max_catch_up_runs": 24,
        "run": _noop,
    }
    with pytest.raises(TypeError, match="misfire_policy"):
        JobSpec(**declared)  # type: ignore[arg-type]  # the omission under test


@pytest.mark.parametrize(
    "job_id", ["Data.Ingest", "data ingest", "9data.ingest", "", "data..ingest", "data.ingest."]
)
def test_a_malformed_job_id_is_refused(job_id: str) -> None:
    with pytest.raises(JobRegistrationError, match=r"subsystem\.operation"):
        _spec(job_id=job_id)


@pytest.mark.parametrize("max_catch_up_runs", [0, -1])
def test_a_job_that_may_replay_nothing_is_refused(max_catch_up_runs: int) -> None:
    with pytest.raises(JobRegistrationError, match="can never recover from an outage"):
        _spec(max_catch_up_runs=max_catch_up_runs)


@pytest.mark.parametrize("misfire_policy", [MisfirePolicy.SKIP_TO_LATEST, MisfirePolicy.RUN_NOW])
def test_a_single_run_policy_may_not_declare_a_backlog_bound_it_can_never_reach(
    misfire_policy: MisfirePolicy,
) -> None:
    with pytest.raises(JobRegistrationError, match="exactly one run"):
        _spec(misfire_policy=misfire_policy, max_catch_up_runs=24)


def test_a_duplicate_job_id_is_refused_rather_than_replaced() -> None:
    catalogue = JobCatalogue()
    catalogue.register(_spec())
    with pytest.raises(JobRegistrationError, match="already registered"):
        catalogue.register(_spec())


def test_the_catalogue_iterates_in_a_fixed_order_whatever_the_registration_order() -> None:
    """A tick that fires three jobs must fire them in the same order in every process, or
    an assertion on the sequence passes locally and fails under the import shuffle."""
    catalogue = JobCatalogue()
    for job_id in ("platform.verify_chain", "data.ingest_klines", "execution.reconcile"):
        catalogue.register(
            _spec(job_id=job_id, misfire_policy=MisfirePolicy.RUN_NOW, max_catch_up_runs=1)
        )
    assert catalogue.job_ids() == (
        "data.ingest_klines",
        "execution.reconcile",
        "platform.verify_chain",
    )
    assert [spec.job_id for spec in catalogue] == list(catalogue.job_ids())
    assert len(catalogue) == REGISTERED_JOBS


def test_the_catalogue_ships_empty() -> None:
    """Ingestion, reconciliation (#66) and chain verification (#95) register their own
    jobs. Declaring their cadences here would freeze schedules whose owners are unwritten."""
    assert len(JobCatalogue()) == 0
