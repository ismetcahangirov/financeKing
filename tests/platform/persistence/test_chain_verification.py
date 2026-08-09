"""The chain verifier, against constructed runs and against a real database.

The pure half is exercised with hand-built `ChainRow` runs because the corruptions that
matter -- a rewritten row, a dropped row, a truncated tail -- are cheap to construct and
expensive to provoke against a database whose triggers exist specifically to prevent
them. The database half then proves the SQL re-derivation agrees with the trigger that
wrote the rows, which is the assumption the pure half rests on.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.platform.persistence.chain import (
    ChainRow,
    ChainTip,
    UnchainedTableError,
    read_chain,
    read_tip,
    verify_chain,
    verify_rows,
)

GENESIS = b"\x00"

# How many rows the database-backed tests insert. Named because the assertion reading it
# is about the count agreeing with what was written, not about the number four.
INSERTED_AUDIT_ROWS = 4


def chain_of(row_count: int, *, table_name: str = "audit_log") -> list[ChainRow]:
    """A well-formed run whose hashes link. Content is irrelevant to the linking."""
    del table_name
    rows: list[ChainRow] = []
    previous = GENESIS
    for index in range(1, row_count + 1):
        row_hash = f"hash-{index}".encode()
        rows.append(
            ChainRow(seq=index, prev_hash=previous, row_hash=row_hash, rederived_hash=row_hash)
        )
        previous = row_hash
    return rows


def tip_of(rows: list[ChainRow], *, table_name: str = "audit_log") -> ChainTip:
    if not rows:
        return ChainTip.empty(table_name)
    return ChainTip(table_name=table_name, seq=rows[-1].seq, row_hash_hex=rows[-1].row_hash.hex())


# ---------------------------------------------------------------------------
# The pure half.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_intact_chain_verifies_and_reports_every_row() -> None:
    rows = chain_of(5)
    verification = verify_rows("audit_log", rows)
    assert verification.is_intact
    assert verification.checked_rows == len(rows)
    assert verification.reason is None
    assert "chain intact" in verification.describe()


@pytest.mark.unit
def test_an_empty_chain_with_no_anchor_is_intact() -> None:
    assert verify_rows("audit_log", []).is_intact


@pytest.mark.unit
def test_a_rewritten_row_is_caught_at_its_own_seq() -> None:
    """The digest re-derived from the row's content no longer matches its row_hash.

    This is the superuser-with-the-trigger-disabled case from
    `docs/rules/append-only-audit.md`.
    """
    rows = chain_of(4)
    rows[2] = ChainRow(
        seq=rows[2].seq,
        prev_hash=rows[2].prev_hash,
        row_hash=rows[2].row_hash,
        rederived_hash=b"content-was-edited",
    )

    verification = verify_rows("audit_log", rows)

    assert verification.first_broken_seq == rows[2].seq
    assert verification.reason is not None
    assert "row_hash does not match" in verification.reason


@pytest.mark.unit
def test_a_dropped_row_breaks_the_link_at_its_successor() -> None:
    rows = chain_of(5)
    del rows[2]

    verification = verify_rows("audit_log", rows)

    assert verification.first_broken_seq == rows[2].seq, "the successor of the dropped row"
    assert verification.reason is not None
    assert "missing or was rewritten" in verification.reason


@pytest.mark.unit
def test_a_seq_gap_alone_is_not_a_break() -> None:
    """`seq` is an identity column: a rolled-back transaction consumes a value.

    A verifier that treats a hole in the numbering as tampering pages a human every
    time a transaction aborts, and a pager that cries wolf is a pager that gets muted.
    """
    rows = chain_of(3)
    renumbered = [
        ChainRow(
            seq=row.seq * 7,
            prev_hash=row.prev_hash,
            row_hash=row.row_hash,
            rederived_hash=row.rederived_hash,
        )
        for row in rows
    ]

    assert verify_rows("audit_log", renumbered).is_intact


@pytest.mark.unit
def test_a_truncated_tail_verifies_internally_and_is_caught_only_by_the_anchor() -> None:
    """The load-bearing case: without the manifest tip there is nothing to notice."""
    full = chain_of(10)
    truncated = full[:6]

    assert verify_rows("audit_log", truncated).is_intact, (
        "a prefix of a valid chain is a valid chain -- if this fails the test below proves nothing"
    )

    verification = verify_rows("audit_log", truncated, expected_tip=tip_of(full))

    assert verification.first_broken_seq == truncated[-1].seq + 1, (
        "the first missing seq, not the last present one"
    )
    assert verification.reason is not None
    assert "truncated" in verification.reason


@pytest.mark.unit
def test_an_entirely_empty_restore_reports_the_first_missing_seq() -> None:
    verification = verify_rows("audit_log", [], expected_tip=tip_of(chain_of(4)))

    assert verification.first_broken_seq == 1
    assert verification.reason is not None
    assert "manifest records a tip" in verification.reason


@pytest.mark.unit
def test_rows_appended_after_the_backup_are_reported_rather_than_ignored() -> None:
    """A restored database that is *longer* than its manifest is also not the backup."""
    stale_tip = tip_of(chain_of(6))
    verification = verify_rows("audit_log", chain_of(9), expected_tip=stale_tip)

    assert verification.first_broken_seq == stale_tip.seq + 1
    assert verification.reason is not None
    assert "appended after the backup" in verification.reason


@pytest.mark.unit
def test_a_rebuilt_chain_over_different_content_fails_at_the_tip() -> None:
    rows = chain_of(3)
    stale_tip = ChainTip(table_name="audit_log", seq=3, row_hash_hex="dead" * 16)

    verification = verify_rows("audit_log", rows, expected_tip=stale_tip)

    assert verification.first_broken_seq == rows[-1].seq
    assert verification.reason is not None
    assert "different content" in verification.reason
    assert "BROKEN at seq 3" in verification.describe()


@pytest.mark.unit
def test_an_incremental_run_links_back_to_the_supplied_hash() -> None:
    rows = chain_of(6)
    tail = rows[3:]

    assert verify_rows("audit_log", tail, since_hash=rows[2].row_hash).is_intact
    assert not verify_rows("audit_log", tail, since_hash=b"not-the-predecessor").is_intact


# ---------------------------------------------------------------------------
# Against a real database. The digest functions are the shared assumption.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("table_name", ["audit_log", "trial_ledger"])
async def test_an_empty_chained_table_verifies(engine: AsyncEngine, table_name: str) -> None:
    async with engine.connect() as connection:
        assert (await verify_chain(connection, table_name)).is_intact
        assert await read_tip(connection, table_name) == ChainTip.empty(table_name)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_sql_rederivation_agrees_with_the_insert_trigger(engine: AsyncEngine) -> None:
    """If these disagree the verifier reports every row as tampered and gets muted."""
    async with engine.begin() as connection:
        for index in range(INSERTED_AUDIT_ROWS):
            await connection.execute(
                sa.text(
                    """
                    INSERT INTO audit_log (
                        occurred_at_utc, correlation_id, causation_id, actor,
                        event_type, subject_id, payload, prev_hash, row_hash
                    ) VALUES (
                        now(), gen_random_uuid(), NULL, 'test',
                        'backup.drill', :subject, '{"k": 1}'::jsonb,
                        '\\x00'::bytea, '\\x00'::bytea
                    )
                    """
                ),
                {"subject": f"row-{index}"},
            )

    async with engine.connect() as connection:
        rows = await read_chain(connection, "audit_log")
        tip = await read_tip(connection, "audit_log")
        verification = await verify_chain(connection, "audit_log", expected_tip=tip)

    assert len(rows) == INSERTED_AUDIT_ROWS
    assert all(row.rederived_hash == row.row_hash for row in rows)
    assert verification.is_intact
    assert tip.row_hash_hex == rows[-1].row_hash.hex()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_stale_tip_from_an_older_backup_fails_the_live_chain(engine: AsyncEngine) -> None:
    """The archival-versus-truncation distinction, against real rows."""
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                """
                INSERT INTO audit_log (
                    occurred_at_utc, correlation_id, actor, event_type, subject_id,
                    payload, prev_hash, row_hash
                ) VALUES (
                    now(), gen_random_uuid(), 'test', 'backup.drill', 'only-row',
                    '{}'::jsonb, '\\x00'::bytea, '\\x00'::bytea
                )
                """
            )
        )

    async with engine.connect() as connection:
        rows = await read_chain(connection, "audit_log")
        newer_tip = ChainTip(table_name="audit_log", seq=rows[-1].seq + 5, row_hash_hex="ab" * 32)
        verification = await verify_chain(connection, "audit_log", expected_tip=newer_tip)

    assert verification.first_broken_seq == rows[-1].seq + 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unchained_table_is_refused_rather_than_silently_passing(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        with pytest.raises(UnchainedTableError, match="carries no hash chain"):
            await read_chain(connection, "bar")
        with pytest.raises(UnchainedTableError, match="carries no hash chain"):
            await read_tip(connection, "bar")
