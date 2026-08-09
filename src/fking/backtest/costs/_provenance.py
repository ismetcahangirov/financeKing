"""The one check that decides whether a cost model is evidence or fiction.

`CLAUDE.md` section 2 states it as a non-negotiable: cost parameters are calibrated from
production market data, never from testnet. This module is the mechanism, and it is a
function rather than a review checklist because a checklist is applied by the person who
wants the run to proceed.

The containment test runs over a *condensed* form of the string -- casefolded, with every
non-alphanumeric character removed -- so `TESTNET`, `Test-Net`, `test_net` and
`binance um test net 2026-05` are all refused by the same line. Condensing cannot create
a false positive against any provenance string this project would legitimately write:
`binance_um_production_2026-03..2026-05` condenses to
`binanceumproduction202603202605`, which contains no `testnet` substring.
"""

from __future__ import annotations

from typing import Final

from fking.backtest.costs._errors import CalibrationProvenanceError

# Lowercase and alphanumeric, because that is the form `_condensed` produces.
_FORBIDDEN_SUBSTRING: Final = "testnet"


def _condensed(candidate: str) -> str:
    """Casefold and drop every separator, so `Test-Net` and `TESTNET` compare equal."""
    return "".join(character for character in candidate.casefold() if character.isalnum())


def require_production_provenance(candidate: str, field_name: str) -> str:
    """Return `candidate` unchanged, or refuse it because it names testnet.

    Also refuses a blank string. A cost model whose provenance is `""` records nothing an
    investigator could check months later, and `BACKTEST_ENGINE.md` section 7 makes
    `cost_model_calibration_source` a credibility metric precisely so that it can be
    read back.
    """
    if not candidate.strip():
        raise CalibrationProvenanceError(
            f"{field_name} must name where the parameters came from; got a blank string"
        )
    if _FORBIDDEN_SUBSTRING in _condensed(candidate):
        raise CalibrationProvenanceError(
            f"{field_name} names testnet ({candidate!r}); cost parameters are calibrated "
            f"from production market data only. Testnet-measured cost feeds exactly one "
            f"consumer -- the divergence monitor in fking.execution -- and never a "
            f"calibration. See docs/rules/exchange-integration.md clause 9."
        )
    return candidate
