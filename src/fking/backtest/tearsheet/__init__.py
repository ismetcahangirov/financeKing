"""One self-contained HTML tearsheet per run, written once and never regenerated.

The document is ordered against its reader. Header, credibility banner, audit findings,
economics, and only then the equity curve -- the numbers most likely to invalidate the
result before the number most likely to excite. A `not_credible` run renders red and its
curve is not drawn at all, because a reader who has looked at an attractive curve reads
the findings that follow it looking for reasons they are too strict.

Three properties carry the design.

**It is a record, not a view.** `write_tearsheet` runs inside the run and refuses to
overwrite an existing artefact with different bytes
(`fking.backtest.tearsheet._errors.TearsheetRegenerationError`). Regenerating a report
later against current code produces a document about today -- new metric definitions, a
revised cost model, a changed regime labeller, all silently applied to an old result.

**It renders with the network off.** No CDN, no web font, no chart library: the styling
is inline, the chart is a hand-computed inline `<svg>`, and there is no `<script>` in the
document at all. A report that needs the internet is a report you cannot read during the
incident that made you open it (`tests/backtest/test_tearsheet_offline.py`).

**It is byte-identical on re-render.** `render_tearsheet` is pure -- no clock, no `git`,
no environment, no filesystem, no unordered iteration -- and the chart arithmetic pins
its own `Decimal` context so the coordinates do not depend on whether the process
bootstrapped one.

Everything not in `__all__` is private and may change without notice.
"""

from __future__ import annotations

from fking.backtest.tearsheet._chart import (
    CHART_HEIGHT_PX,
    CHART_WIDTH_PX,
    ChartGeometry,
    EnvelopeProjection,
    chart_geometry,
    project_envelope,
)
from fking.backtest.tearsheet._errors import (
    TearsheetError,
    TearsheetInputError,
    TearsheetRegenerationError,
)
from fking.backtest.tearsheet._inputs import (
    MIN_ENGINE_SHA_LENGTH,
    EngineBuild,
    HeldOutStatus,
    TearsheetInputs,
)
from fking.backtest.tearsheet._render import SECTION_IDS, render_tearsheet
from fking.backtest.tearsheet._write import TEARSHEET_FILENAME, tearsheet_path, write_tearsheet

__all__: tuple[str, ...] = (
    "CHART_HEIGHT_PX",
    "CHART_WIDTH_PX",
    "MIN_ENGINE_SHA_LENGTH",
    "SECTION_IDS",
    "TEARSHEET_FILENAME",
    "ChartGeometry",
    "EngineBuild",
    "EnvelopeProjection",
    "HeldOutStatus",
    "TearsheetError",
    "TearsheetInputError",
    "TearsheetInputs",
    "TearsheetRegenerationError",
    "chart_geometry",
    "project_envelope",
    "render_tearsheet",
    "tearsheet_path",
    "write_tearsheet",
)
