"""Writing the artefact: once, at run time, into the run's own directory.

`reports/backtest/<run_id>/tearsheet.html`. The run id is the directory rather than part
of the filename because a run's artefacts accumulate -- the trace, the cost report, the
CPCV path table -- and a flat directory of `<uuid>-tearsheet.html` is a directory nobody
can list.

**Rewriting is refused, not silently performed.** `write_tearsheet` is idempotent when
the bytes match and raises `TearsheetRegenerationError` when they do not. Two cases hide
behind a differing byte string and both are serious: a nondeterministic renderer, or an
attempt to regenerate an old run's report against today's code, which produces a document
about today wearing an old run's id. Neither should be resolvable by the write succeeding.

Newlines are pinned to `\\n` on every platform. `Path.write_text` without `newline=`
translates to `\\r\\n` on Windows, which would make the same run's artefact differ between
a developer machine and CI -- a byte-identity criterion that fails for a reason that has
nothing to do with the run.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fking.backtest.tearsheet._errors import TearsheetRegenerationError
from fking.backtest.tearsheet._inputs import TearsheetInputs
from fking.backtest.tearsheet._render import render_tearsheet

__all__ = ["TEARSHEET_FILENAME", "tearsheet_path", "write_tearsheet"]

TEARSHEET_FILENAME = "tearsheet.html"


def tearsheet_path(run_id: UUID, *, reports_root: Path) -> Path:
    """Where this run's tearsheet lives, whether or not it has been written yet."""
    return reports_root / "backtest" / str(run_id) / TEARSHEET_FILENAME


def write_tearsheet(inputs: TearsheetInputs, *, reports_root: Path) -> Path:
    """Render one run's tearsheet and write it under `reports_root`.

    Returns the path written. Writing the same inputs twice is a no-op on the second
    call; writing *different* content to an existing run's path is refused.

    Raises:
        TearsheetRegenerationError: a tearsheet already exists at this run's path and its
            bytes differ from this render.
    """
    document = render_tearsheet(inputs)
    destination = tearsheet_path(inputs.backtest_result.run_id, reports_root=reports_root)

    if destination.exists():
        existing = destination.read_text(encoding="utf-8")
        if existing == document:
            return destination
        raise TearsheetRegenerationError(
            f"{destination} already holds a tearsheet for run "
            f"{inputs.backtest_result.run_id} whose bytes differ from this render "
            f"({len(existing)} stored characters against {len(document)}). Either the "
            f"renderer is not deterministic -- which outranks everything else on the "
            f"queue -- or this is a regeneration of an old run against current code, "
            f"which produces a document about today carrying an old run's id"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8", newline="\n")
    return destination
