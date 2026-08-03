"""The correlation scope: nests, restores, and survives an await.

`OBSERVABILITY.md` section 3 names the boundary where propagation actually breaks: an
async task spawned from the wrong context silently starts fresh, and the result is two
chains that each look complete. `contextvars` copy into a task at creation time, which is
what makes this work -- and it is worth a test rather than a belief, because the failure
is invisible.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from fking.platform.correlation import (
    BOOT,
    MissingCorrelationIdError,
    correlation_scope,
    current_correlation_id,
    require_current_correlation_id,
)

pytestmark = pytest.mark.unit

OUTER = UUID("0192f3c8-1e5b-7c0d-8a41-2b9d4e6f8a11")
INNER = UUID("0192f3c8-2222-7c0d-8a41-2b9d4e6f8a22")


def test_no_scope_means_no_id() -> None:
    assert current_correlation_id() is None


def test_a_scope_binds_and_then_restores() -> None:
    with correlation_scope(OUTER):
        assert current_correlation_id() == str(OUTER)
    assert current_correlation_id() is None


def test_a_nested_scope_restores_the_outer_one_rather_than_clearing() -> None:
    """A scheduled job opening its own root scope inside a request must not leave the
    request's remaining frames unlabelled."""
    with correlation_scope(OUTER):
        with correlation_scope(INNER):
            assert current_correlation_id() == str(INNER)
        assert current_correlation_id() == str(OUTER)


def test_the_scope_is_restored_even_when_the_body_raises() -> None:
    with pytest.raises(RuntimeError), correlation_scope(OUTER):
        raise RuntimeError("boom")
    assert current_correlation_id() is None


def test_the_boot_literal_is_accepted() -> None:
    """The one sanctioned non-UUID value, for records emitted before any flow exists."""
    with correlation_scope(BOOT):
        assert current_correlation_id() == "boot"


def test_requiring_an_id_outside_a_scope_names_the_operation() -> None:
    with pytest.raises(MissingCorrelationIdError, match=r"risk\.size_position"):
        require_current_correlation_id(at="risk.size_position")


@pytest.mark.asyncio
async def test_a_spawned_task_inherits_the_scope() -> None:
    """The boundary OBSERVABILITY.md section 3 calls out: a task created from the wrong
    context starts fresh, and nothing about the resulting log looks broken."""

    async def observed() -> str | None:
        await asyncio.sleep(0)
        return current_correlation_id()

    with correlation_scope(OUTER):
        async with asyncio.TaskGroup() as group:
            task = group.create_task(observed())
    assert task.result() == str(OUTER)


@pytest.mark.asyncio
async def test_a_scope_opened_inside_a_task_does_not_leak_to_its_sibling() -> None:
    """Contextvars are copied, not shared. Without this, two concurrent flows would
    overwrite each other's id and both sets of records would be mislabelled."""
    started = asyncio.Event()

    async def holds_inner() -> None:
        with correlation_scope(INNER):
            started.set()
            await asyncio.sleep(0.01)

    async def observes() -> str | None:
        await started.wait()
        return current_correlation_id()

    with correlation_scope(OUTER):
        async with asyncio.TaskGroup() as group:
            group.create_task(holds_inner())
            sibling = group.create_task(observes())
    assert sibling.result() == str(OUTER)
