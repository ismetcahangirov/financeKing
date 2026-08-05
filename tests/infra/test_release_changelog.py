"""The changelog is derived from what merged, and the derivation has to be provable.

The load-bearing test in this file is
`test_a_safety_critical_release_cannot_produce_a_changelog_that_omits_it`, which is the
acceptance criterion from issue #115 stated as an assertion: a `safety:critical` pull
request in the range must appear in the leading section no matter what else is in the
range, what type label it carries, or how many other entries bury it.

The rollback tests are the other half of the deliverable. A rollback path is not a
sentence; it is a procedure that differs depending on whether the range contains a
migration that will not undo itself, and the generator picks between two literal
procedures rather than emitting one hedged paragraph.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

import pytest

from tools.release.changelog import (
    ChangelogError,
    MergedPullRequest,
    ReleaseNotes,
    ResultsInvalidatingChange,
    parse_results_invalidating,
    pull_request_numbers,
    render,
    section_of,
)
from tools.release.migrations import DowngradeKind, Migration
from tools.release.version import Version

pytestmark = pytest.mark.unit

_CUT_AT: Final[datetime] = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
_HEAD: Final[str] = "c0ffee1234567890"

REVERSIBLE: Final[Migration] = Migration("0003_market_data.py", DowngradeKind.REVERSIBLE)
IRREVERSIBLE: Final[Migration] = Migration("0002_audit_substrate.py", DowngradeKind.IRREVERSIBLE)
CONDITIONAL: Final[Migration] = Migration("0012_gap_resolution.py", DowngradeKind.CONDITIONAL)

# The leading safety section and the entry's own type section.
_LISTED_IN_BOTH_SECTIONS: Final[int] = 2


def pull_request(
    number: int,
    *,
    title: str = "feat(risk): net correlated exposure",
    labels: tuple[str, ...] = ("type:feat",),
) -> MergedPullRequest:
    return MergedPullRequest(
        number=number,
        title=title,
        url=f"https://github.com/ismetcahangirov/financeKing/pull/{number}",
        labels=frozenset(labels),
    )


def notes(
    *,
    pull_requests: tuple[MergedPullRequest, ...] = (),
    migrations: tuple[Migration, ...] = (REVERSIBLE,),
    previous_tag: str | None = "v0.3.0",
    results_invalidating: tuple[ResultsInvalidatingChange, ...] = (),
    safety_diff: str = "",
) -> ReleaseNotes:
    return ReleaseNotes(
        version=Version(0, 4, 0),
        previous_tag=previous_tag,
        commit_sha=_HEAD,
        generated_at_utc=_CUT_AT,
        pull_requests=pull_requests,
        results_invalidating=results_invalidating,
        migrations=migrations,
        safety_diff=safety_diff,
    )


# ---------------------------------------------------------------------------
# Grouping by type label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "heading"),
    [
        ("type:feat", "Added"),
        ("type:fix", "Fixed"),
        ("type:refactor", "Changed"),
        ("type:perf", "Performance"),
        ("type:adr", "Architecture decisions"),
        ("type:research", "Research"),
        ("type:test", "Tests"),
        ("type:docs", "Documentation"),
        ("type:chore", "Housekeeping"),
    ],
)
def test_every_repository_type_label_maps_to_a_section(label: str, heading: str) -> None:
    """The parametrisation is `gh label list`'s `type:` set; a new label with no
    section is a refusal at release time rather than a silent miscategorisation."""
    assert section_of(pull_request(1, labels=(label,))) == heading
    rendered = render(notes(pull_requests=(pull_request(1, labels=(label,)),)))
    assert f"## {heading}" in rendered


def test_a_pull_request_with_no_type_label_is_listed_not_dropped() -> None:
    rendered = render(notes(pull_requests=(pull_request(42, labels=("area:infra",)),)))
    assert "## Uncategorised" in rendered
    assert "#42" in rendered


def test_two_type_labels_are_refused_rather_than_resolved() -> None:
    with pytest.raises(ChangelogError, match="two type labels"):
        section_of(pull_request(7, labels=("type:feat", "type:fix")))


def test_an_unknown_type_label_is_refused_rather_than_bucketed() -> None:
    with pytest.raises(ChangelogError, match="maps to no changelog"):
        section_of(pull_request(7, labels=("type:hotfix",)))


def test_entries_are_ordered_by_number_within_a_section() -> None:
    rendered = render(
        notes(
            pull_requests=(
                pull_request(158, title="feat(backtest): event loop"),
                pull_request(12, title="feat(risk): netting"),
            )
        )
    )
    assert rendered.index("#12 ") < rendered.index("#158 ")


# ---------------------------------------------------------------------------
# safety:critical -- issue #115's acceptance criterion
# ---------------------------------------------------------------------------


def test_the_safety_section_says_none_explicitly_when_there_is_nothing_in_it() -> None:
    """An absent section is ambiguous between 'nothing changed' and 'nobody checked'."""
    rendered = render(notes(pull_requests=(pull_request(1),)))
    body = rendered.split("## Safety-relevant changes", 1)[1]
    assert body.lstrip().startswith("None.")


@pytest.mark.parametrize(
    "type_label", ["type:feat", "type:fix", "type:chore", "type:docs", "type:refactor"]
)
def test_a_safety_critical_release_cannot_produce_a_changelog_that_omits_it(
    type_label: str,
) -> None:
    """Issue #115: whatever else is in the range, the safety entry is at the top.

    The noise is forty ordinary entries, because burying is the failure mode the
    label exists to prevent -- not omission by a bug, omission by scroll depth.
    """
    critical = pull_request(
        99, title="feat(platform): add bybit testnet host", labels=(type_label, "safety:critical")
    )
    noise = tuple(pull_request(number) for number in range(100, 140))
    rendered = render(
        notes(pull_requests=(*noise, critical), safety_diff="+    'api-testnet.bybit.com',")
    )

    _, remainder = rendered.split("## Safety-relevant changes", 1)
    leading_section = remainder.split("\n## ", 1)[0]
    assert "#99" in leading_section, "the safety entry is not in the leading section"
    assert "api-testnet.bybit.com" in leading_section, "the kernel diff is inlined, not summarised"


def test_a_safety_critical_entry_also_appears_in_its_type_section() -> None:
    """Listing it only at the top would make the feature list a lie by omission."""
    rendered = render(
        notes(pull_requests=(pull_request(99, labels=("type:feat", "safety:critical")),))
    )
    added = rendered.split("## Added", 1)[1]
    assert "#99" in added
    assert rendered.count("#99") == _LISTED_IN_BOTH_SECTIONS


# ---------------------------------------------------------------------------
# Results-invalidating changes
# ---------------------------------------------------------------------------


def test_results_invalidating_changes_are_listed_with_their_trailer_text() -> None:
    rendered = render(
        notes(
            results_invalidating=(
                ResultsInvalidatingChange(
                    sha="deadbeefcafebabe",
                    subject="feat(backtest): model maker/taker split",
                    note="all Sharpe figures before this assumed 100% taker",
                ),
            )
        )
    )
    assert "deadbeefcafe" in rendered
    assert "assumed 100% taker" in rendered
    assert "Re-score before the next lifecycle decision." in rendered


def test_the_results_invalidating_section_says_none_explicitly() -> None:
    body = render(notes()).split("## Results-invalidating changes", 1)[1]
    assert body.lstrip().startswith("None.")


def test_git_log_trailer_records_parse_and_commits_without_the_trailer_are_skipped() -> None:
    """The second record is the trap: Python counts U+001F as whitespace, so a bare
    `.strip()` on it eats the trailing field separator and every ordinary commit then
    reports as malformed. Regression guard, not decoration."""
    log = (
        "aaa\x1ffeat(backtest): split\x1fall Sharpe figures shift\x1e\n"
        "bbb\x1fchore(infra): tidy\x1f\x1e\n"
    )
    parsed = parse_results_invalidating(log)
    assert [change.sha for change in parsed] == ["aaa"]
    assert parsed[0].note == "all Sharpe figures shift"


def test_a_malformed_git_log_record_is_refused() -> None:
    with pytest.raises(ChangelogError, match="three unit-separated"):
        parse_results_invalidating("aaa\x1fonly-two-fields\x1e")


# ---------------------------------------------------------------------------
# Finding the merged pull requests from commit subjects
# ---------------------------------------------------------------------------


def test_squash_and_merge_commit_subjects_both_yield_their_number() -> None:
    assert pull_request_numbers(
        [
            "feat(backtest): the deterministic event loop every run rides on (#158)",
            "Merge pull request #157 from ismetcahangirov/feat/157-open-interest",
            "chore(data): declare ALT_DATASETS as part of the module's surface",
        ]
    ) == (158, 157)


def test_a_number_referenced_twice_is_listed_once() -> None:
    assert pull_request_numbers(["feat: a (#12)", 'revert: "feat: a" (#12)']) == (12,)


# ---------------------------------------------------------------------------
# The rollback path -- the deliverable, not a sentence
# ---------------------------------------------------------------------------


def test_a_reversible_range_publishes_the_symmetric_procedure() -> None:
    rendered = render(notes(migrations=(REVERSIBLE,)))
    assert "Every migration in this range reverses cleanly" in rendered
    assert "make up && make migrate" in rendered
    assert "git checkout v0.3.0" in rendered


@pytest.mark.parametrize("migration", [IRREVERSIBLE, CONDITIONAL])
def test_an_irreversible_range_publishes_the_schema_forward_procedure(
    migration: Migration,
) -> None:
    rendered = render(notes(migrations=(REVERSIBLE, migration)))
    assert "Code rolls back; schema rolls forward" in rendered
    assert migration.filename in rendered
    assert "NOT `make migrate`" in rendered


@pytest.mark.parametrize("migration", [IRREVERSIBLE, CONDITIONAL])
def test_an_irreversible_rollback_never_tells_anyone_to_run_alembic_downgrade(
    migration: Migration,
) -> None:
    """The one instruction that must never appear: it either raises, wasting an
    outage, or succeeds against an empty database and teaches you that it works."""
    rendered = render(notes(migrations=(migration,)))
    procedure = rendered.split("## Rollback", 1)[1]
    assert "alembic downgrade" not in procedure.replace(
        "`alembic downgrade` is **not** part of this procedure", ""
    ).replace("reaching for `downgrade`", "")


def test_a_conditionally_irreversible_migration_is_treated_as_irreversible() -> None:
    """Whether 0012 raises is a property of the database at rollback time, which is
    not knowable at tag time. The only safe reading of an unknowable rollback is that
    it will not work."""
    assert "schema rolls forward" in render(notes(migrations=(CONDITIONAL,)))


@pytest.mark.parametrize("migrations", [(REVERSIBLE,), (IRREVERSIBLE,), (CONDITIONAL,), ()])
def test_no_rollback_procedure_ever_moves_a_tag_or_force_pushes(
    migrations: tuple[Migration, ...],
) -> None:
    rendered = render(notes(migrations=migrations))
    assert "git tag -f" not in rendered
    assert "push --force" not in rendered
    assert "force-push" in rendered, "the prohibition is stated, not merely obeyed"


@pytest.mark.parametrize("migrations", [(REVERSIBLE,), (IRREVERSIBLE,)])
def test_every_rollback_procedure_demands_reconciliation_and_protects_the_counters(
    migrations: tuple[Migration, ...],
) -> None:
    rendered = render(notes(migrations=migrations))
    assert "reconcile --full" in rendered
    assert "Reconciliation is mandatory" in rendered
    assert "trial counter" in rendered
    assert "held-out" in rendered


def test_the_first_release_states_that_there_is_nothing_to_roll_back_to() -> None:
    rendered = render(notes(previous_tag=None, migrations=(IRREVERSIBLE,)))
    assert "There is no previous release to roll back to" in rendered
    assert "_none — first release_" in rendered


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_rendering_is_pure() -> None:
    """The notes go into an immutable tag; two runs must not disagree about them."""
    payload = notes(
        pull_requests=(pull_request(1), pull_request(2, labels=("type:fix",))),
        migrations=(REVERSIBLE, IRREVERSIBLE),
    )
    assert render(payload) == render(payload)


def test_the_notes_name_the_commit_they_were_cut_from() -> None:
    assert _HEAD in render(notes())
