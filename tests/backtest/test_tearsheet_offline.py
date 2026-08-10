"""The tearsheet renders and is readable with the network switched off.

Issue #45's first acceptance criterion, and the one that is easiest to believe without
checking. The suite does two independent things, because either alone would pass a
document that fails the requirement:

- It renders and writes with `socket.socket` replaced by something that raises, so a
  render that reached out for a font, a template or a git SHA fails here rather than in
  the incident that made somebody open the report.
- It greps the emitted document for every way an HTML file can make a browser fetch
  something later -- `<script>`, `<link>`, `src=`, `@import`, a bare scheme, a
  protocol-relative URL. Rendering offline proves the *renderer* is offline; only the
  second check proves the *document* is.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Final, NoReturn

import pytest

from fking.backtest.tearsheet import render_tearsheet, tearsheet_path, write_tearsheet
from tests.backtest.tearsheet_support import inputs_for, path_distribution

#: Every substring that would make a browser open a connection after the file is opened.
#: `href=` is absent from the list on purpose -- an in-document `#anchor` is an `href` and
#: is perfectly offline -- and is covered by the `<link>` and scheme checks instead.
NETWORK_TOKENS: Final[tuple[str, ...]] = (
    "<script",
    "<link",
    "<iframe",
    "<img",
    " src=",
    "@import",
    "url(",
    "http://",
    "https://",
    '="//',
    "@font-face",
)


class _NetworkDisabledError(RuntimeError):
    """Raised by the patched socket factory: nothing here may open a connection."""


@pytest.fixture
def network_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the socket factory: anything that reaches for the network raises here.

    `monkeypatch` undoes both patches at teardown, so the fixture has nothing of its own
    to clean up and returns rather than yielding.
    """

    def _refuse(*_args: object, **_kwargs: object) -> NoReturn:
        raise _NetworkDisabledError("the tearsheet renderer opened a socket")

    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


@pytest.mark.usefixtures("network_disabled")
def test_renders_with_the_network_disabled(tmp_path: Path) -> None:
    written = write_tearsheet(inputs_for(distribution=path_distribution()), reports_root=tmp_path)

    assert written == tearsheet_path(inputs_for().backtest_result.run_id, reports_root=tmp_path)
    assert written.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


@pytest.mark.usefixtures("network_disabled")
def test_the_written_path_is_reports_backtest_run_id_tearsheet(tmp_path: Path) -> None:
    inputs = inputs_for(distribution=path_distribution())

    written = write_tearsheet(inputs, reports_root=tmp_path)

    assert written.relative_to(tmp_path).as_posix() == (
        f"backtest/{inputs.backtest_result.run_id}/tearsheet.html"
    )


@pytest.mark.parametrize("token", NETWORK_TOKENS)
def test_the_document_references_nothing_external(token: str) -> None:
    document = render_tearsheet(inputs_for(distribution=path_distribution()))

    assert token not in document, f"the tearsheet contains {token!r}, which fetches at open time"


def test_the_document_is_utf8_and_newline_pinned(tmp_path: Path) -> None:
    written = write_tearsheet(inputs_for(distribution=path_distribution()), reports_root=tmp_path)

    raw = written.read_bytes()

    # `\r\n` would make the same run's artefact differ between Windows and CI, breaking
    # byte-identity for a reason that has nothing to do with the run.
    assert b"\r\n" not in raw
    assert raw.decode("utf-8").endswith("</html>\n")
