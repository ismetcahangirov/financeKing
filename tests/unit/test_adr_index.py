"""The ADR record's structural invariants, and the index that is derived from them.

Every rejection tested here is a way the decision record stops being trustworthy while
still looking fine in a file listing. A reused number makes two decisions share an
address. A `superseded_by` pointing at nothing leaves two live contradictory decisions
in the tree with no way to tell which one won. A stale index is worse than no index,
because a reader who checks it and finds an answer stops looking.

The fixtures build ADR trees in `tmp_path` rather than asserting against `docs/adr/`.
Asserting against the real tree would make every new ADR a test failure, which trains
people to edit the test rather than read it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path

import pytest

from tools.checks.adr_index import (
    INDEX_BEGIN,
    INDEX_END,
    AdrRecord,
    check_tree,
    main,
    parse_front_matter,
    render_index,
)

pytestmark = pytest.mark.unit


DEFAULT_TITLE = "Modular monolith over microservices"
DEFAULT_FIELDS: Mapping[str, str] = {
    "number": "0001",
    "title": DEFAULT_TITLE,
    "date": "2026-08-03",
    "status": "accepted",
    "deciders": "[ismetcahangirov]",
    "supersedes": "null",
    "superseded_by": "null",
    "related_issues": '["#16"]',
    "related_adrs": "[]",
}


def adr_source(**overrides: str) -> str:
    """A well-formed ADR header, with any field replaced by keyword."""
    fields = {**DEFAULT_FIELDS, **overrides}
    block = "\n".join(f"{key}: {raw}" for key, raw in fields.items())
    return f"---\n{block}\n---\n\n## Context\n\nSomething forced a decision.\n"


def write_adr(root: Path, filename: str, source: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    path.write_text(source, encoding="utf-8")
    return path


def write_readme(root: Path, table: str) -> Path:
    path = root / "README.md"
    path.write_text(
        f"# Architecture Decision Records\n\n## Index\n\n{INDEX_BEGIN}\n{table}{INDEX_END}\n",
        encoding="utf-8",
    )
    return path


def well_formed_tree(root: Path) -> None:
    """One accepted record, one superseded record, and an index that matches both."""
    write_adr(
        root,
        "0001-modular-monolith.md",
        adr_source(number="0001", status="superseded by ADR-0002", superseded_by="ADR-0002"),
    )
    write_adr(
        root,
        "0002-microservices-after-all.md",
        adr_source(
            number="0002",
            title="Microservices after all",
            date="2026-09-01",
            supersedes="ADR-0001",
        ),
    )
    records = [
        AdrRecord(
            number=1,
            title=DEFAULT_TITLE,
            date_utc=date(2026, 8, 3),
            status="superseded by ADR-0002",
            supersedes=None,
            superseded_by=2,
            filename="0001-modular-monolith.md",
        ),
        AdrRecord(
            number=2,
            title="Microservices after all",
            date_utc=date(2026, 9, 1),
            status="accepted",
            supersedes=1,
            superseded_by=None,
            filename="0002-microservices-after-all.md",
        ),
    ]
    write_readme(root, render_index(records))


class TestParseFrontMatter:
    def test_a_well_formed_block_parses_every_field(self) -> None:
        parsed = parse_front_matter(
            adr_source(
                number="0007",
                title="Binance testnet primary",
                status="superseded by ADR-0009",
                superseded_by="ADR-0009",
            ),
            filename="0007-binance-testnet-primary.md",
        )
        assert parsed == AdrRecord(
            number=7,
            title="Binance testnet primary",
            date_utc=date(2026, 8, 3),
            status="superseded by ADR-0009",
            supersedes=None,
            superseded_by=9,
            filename="0007-binance-testnet-primary.md",
        )

    def test_a_missing_front_matter_block_is_reported(self) -> None:
        failures = parse_front_matter("# Just a heading\n", filename="0001-x.md")
        assert isinstance(failures, list)
        assert any("front matter" in failure for failure in failures)

    @pytest.mark.parametrize("key", ["number", "title", "date", "status", "superseded_by"])
    def test_each_required_key_is_required(self, key: str) -> None:
        source = "\n".join(
            line for line in adr_source().splitlines() if not line.startswith(f"{key}:")
        )
        failures = parse_front_matter(source + "\n", filename="0001-x.md")
        assert isinstance(failures, list)
        assert any(key in failure for failure in failures)

    def test_an_unrecognised_status_is_reported(self) -> None:
        failures = parse_front_matter(adr_source(status="draft-ish"), filename="0001-x.md")
        assert isinstance(failures, list)
        assert any("status" in failure for failure in failures)

    def test_a_non_iso_date_is_reported(self) -> None:
        failures = parse_front_matter(adr_source(date="03/08/2026"), filename="0001-x.md")
        assert isinstance(failures, list)
        assert any("date" in failure for failure in failures)

    def test_a_superseded_by_field_without_a_matching_status_is_reported(self) -> None:
        """The status line is what a human reads; the field is what the index reads."""
        failures = parse_front_matter(
            adr_source(status="accepted", superseded_by="ADR-0009"), filename="0001-x.md"
        )
        assert isinstance(failures, list)
        assert any("status line" in failure for failure in failures)

    def test_a_status_naming_a_different_adr_than_superseded_by_is_reported(self) -> None:
        failures = parse_front_matter(
            adr_source(status="superseded by ADR-0009", superseded_by="ADR-0011"),
            filename="0001-x.md",
        )
        assert isinstance(failures, list)
        assert any("0009" in failure and "0011" in failure for failure in failures)


class TestCheckTree:
    def test_a_well_formed_tree_passes(self, tmp_path: Path) -> None:
        well_formed_tree(tmp_path)
        assert check_tree(tmp_path) == []

    def test_a_filename_that_disagrees_with_its_number_is_reported(self, tmp_path: Path) -> None:
        write_adr(tmp_path, "0003-mismatched.md", adr_source(number="0004"))
        write_readme(tmp_path, "")
        assert any("0003-mismatched.md" in failure for failure in check_tree(tmp_path))

    def test_a_filename_outside_the_naming_scheme_is_reported(self, tmp_path: Path) -> None:
        write_adr(tmp_path, "adr-modular-monolith.md", adr_source())
        write_readme(tmp_path, "")
        assert any("adr-modular-monolith.md" in failure for failure in check_tree(tmp_path))

    def test_a_reused_number_is_reported(self, tmp_path: Path) -> None:
        write_adr(tmp_path, "0001-first.md", adr_source(number="0001"))
        write_adr(tmp_path, "0001-second.md", adr_source(number="0001", title="Second"))
        write_readme(tmp_path, "")
        assert any("reused" in failure for failure in check_tree(tmp_path))

    def test_a_gap_in_the_sequence_is_allowed(self, tmp_path: Path) -> None:
        """0013 is reserved for #21. A checker that demanded contiguity would block it."""
        write_adr(tmp_path, "0001-first.md", adr_source(number="0001"))
        write_adr(tmp_path, "0005-fifth.md", adr_source(number="0005", title="Fifth"))
        records = [
            AdrRecord(
                number=1,
                title=DEFAULT_TITLE,
                date_utc=date(2026, 8, 3),
                status="accepted",
                supersedes=None,
                superseded_by=None,
                filename="0001-first.md",
            ),
            AdrRecord(
                number=5,
                title="Fifth",
                date_utc=date(2026, 8, 3),
                status="accepted",
                supersedes=None,
                superseded_by=None,
                filename="0005-fifth.md",
            ),
        ]
        write_readme(tmp_path, render_index(records))
        assert check_tree(tmp_path) == []

    def test_superseding_a_record_that_does_not_exist_is_reported(self, tmp_path: Path) -> None:
        write_adr(
            tmp_path,
            "0001-first.md",
            adr_source(number="0001", status="superseded by ADR-0099", superseded_by="ADR-0099"),
        )
        write_readme(tmp_path, "")
        assert any("0099" in failure for failure in check_tree(tmp_path))

    def test_a_supersession_without_its_reciprocal_is_reported(self, tmp_path: Path) -> None:
        """0001 claims 0002 replaced it; 0002 does not agree. One of them is wrong."""
        write_adr(
            tmp_path,
            "0001-first.md",
            adr_source(number="0001", status="superseded by ADR-0002", superseded_by="ADR-0002"),
        )
        write_adr(tmp_path, "0002-second.md", adr_source(number="0002", title="Second"))
        write_readme(tmp_path, "")
        assert any("supersedes" in failure for failure in check_tree(tmp_path))

    def test_a_stale_index_is_reported(self, tmp_path: Path) -> None:
        write_adr(tmp_path, "0001-first.md", adr_source(number="0001"))
        write_readme(tmp_path, "| nothing | at | all |\n")
        assert any("README.md" in failure for failure in check_tree(tmp_path))

    def test_a_readme_without_the_sentinels_is_reported(self, tmp_path: Path) -> None:
        write_adr(tmp_path, "0001-first.md", adr_source(number="0001"))
        (tmp_path / "README.md").write_text("# ADRs\n\nNo index here.\n", encoding="utf-8")
        assert any("sentinel" in failure for failure in check_tree(tmp_path))

    def test_a_missing_readme_is_reported(self, tmp_path: Path) -> None:
        write_adr(tmp_path, "0001-first.md", adr_source(number="0001"))
        assert any("README.md" in failure for failure in check_tree(tmp_path))


class TestWriteMode:
    def test_write_repairs_a_stale_index_and_then_the_check_passes(self, tmp_path: Path) -> None:
        write_adr(tmp_path, "0001-first.md", adr_source(number="0001"))
        write_readme(tmp_path, "| stale |\n")

        assert check_tree(tmp_path, write=True) == []
        assert check_tree(tmp_path) == []
        assert "Modular monolith over microservices" in (tmp_path / "README.md").read_text(
            encoding="utf-8"
        )

    def test_write_leaves_prose_outside_the_sentinels_alone(self, tmp_path: Path) -> None:
        write_adr(tmp_path, "0001-first.md", adr_source(number="0001"))
        (tmp_path / "README.md").write_text(
            f"# Head\n\nProse above.\n\n{INDEX_BEGIN}\n| stale |\n{INDEX_END}\n\nProse below.\n",
            encoding="utf-8",
        )

        check_tree(tmp_path, write=True)

        rendered = (tmp_path / "README.md").read_text(encoding="utf-8")
        assert "Prose above." in rendered
        assert "Prose below." in rendered
        assert "| stale |" not in rendered

    def test_write_does_not_repair_a_tree_with_structural_failures(self, tmp_path: Path) -> None:
        """A generator that runs over a broken tree writes a confident, wrong index."""
        write_adr(tmp_path, "0001-first.md", adr_source(number="0001"))
        write_adr(tmp_path, "0001-clash.md", adr_source(number="0001", title="Clash"))
        write_readme(tmp_path, "| stale |\n")

        assert check_tree(tmp_path, write=True) != []
        assert "| stale |" in (tmp_path / "README.md").read_text(encoding="utf-8")


class TestMain:
    def test_a_clean_tree_exits_zero(self, tmp_path: Path) -> None:
        well_formed_tree(tmp_path)
        assert main([str(tmp_path)]) == 0

    def test_an_empty_argv_exits_zero(self) -> None:
        """`make checks` runs the whole row of checks; none may raise on no arguments."""
        assert main([]) == 0

    def test_a_broken_tree_exits_non_zero(self, tmp_path: Path) -> None:
        write_adr(tmp_path, "0001-first.md", adr_source(number="0002"))
        write_readme(tmp_path, "")
        assert main([str(tmp_path)]) == 1

    def test_the_repository_adr_tree_is_consistent(self) -> None:
        """The one assertion against the real tree: the shipped index is not stale."""
        repository_root = Path(__file__).resolve().parents[2]
        assert check_tree(repository_root / "docs" / "adr") == []
