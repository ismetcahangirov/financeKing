"""Configuration tests run against a known-empty environment.

Without this, a developer with `FKING_RISK__MAX_LEVERAGE` exported gets different
results from CI, and the difference shows up as a test that only fails on one machine.
The fixture is autouse rather than opt-in because the failure it prevents is silent: a
leaked variable makes an assertion pass for the wrong reason.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _no_inherited_fking_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [key for key in os.environ if key.startswith("FKING_")]:
        monkeypatch.delenv(name, raising=False)
