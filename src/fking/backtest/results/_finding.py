"""One audit finding, and the seven checks the battery is fixed to.

`AuditCheck` is closed at seven members and `REQUIRED_AUDIT_CHECKS` is the frozenset of
all of them, in the order `BACKTEST_ENGINE.md` section 8 and this package's own
`CLAUDE.md` reference fix: look-ahead first because it is the only defect class that does
not fail, cost model second, timestamp alignment third, fill optimism fourth,
survivorship fifth, then parity and sample size. `assess_credibility`
(`fking.backtest.results._credibility`) reads this set to decide whether a battery is
complete; a caller cannot report six checks and call the seventh implied.

`evidence` is validated here rather than trusted from the caller, because "a finding with
an empty evidence string fails schema validation" is a property of `AuditFinding` itself,
not of whoever happens to construct one. `check` and `status` are declared as `StrEnum`
rather than a bare `Literal`, so the seven members and three statuses are values other
modules in this package can iterate and compare rather than strings that have to be
re-typed at every call site -- a `StrEnum` member still satisfies a `Literal[...]` field
on the wire, because a member *is* the string.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, field_validator


class AuditCheck(StrEnum):
    """The seven checks, fixed. Adding an eighth is a change to the battery, not a value."""

    LOOK_AHEAD = "look_ahead"
    COST_MODEL = "cost_model"
    TIMESTAMP_ALIGNMENT = "timestamp_alignment"
    SURVIVORSHIP = "survivorship"
    FILL_OPTIMISM = "fill_optimism"
    PARITY = "parity"
    SAMPLE_SIZE = "sample_size"


class AuditStatus(StrEnum):
    """What a check concluded. There is no fourth state.

    `INCONCLUSIVE` is not "probably fine" -- it blocks credibility exactly as `FAIL`
    does, by construction of `assess_credibility`. A check that could not be run is a
    check that was not run, and `unaudited` is the only honest verdict for a battery that
    contains one.
    """

    PASS = "pass"  # noqa: S105 - a check's verdict, not a credential
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


# Fixed order for reporting and for the completeness check. `frozenset` for
# `REQUIRED_AUDIT_CHECKS` because completeness is a set-membership question -- "were all
# seven present, exactly once" -- and a tuple would tempt an order-sensitive comparison
# that has nothing to do with what the battery is checking.
AUDIT_ORDER: Final[tuple[AuditCheck, ...]] = (
    AuditCheck.LOOK_AHEAD,
    AuditCheck.COST_MODEL,
    AuditCheck.TIMESTAMP_ALIGNMENT,
    AuditCheck.FILL_OPTIMISM,
    AuditCheck.SURVIVORSHIP,
    AuditCheck.PARITY,
    AuditCheck.SAMPLE_SIZE,
)
REQUIRED_AUDIT_CHECKS: Final[frozenset[AuditCheck]] = frozenset(AuditCheck)


class ResultCredibility(StrEnum):
    """What a `BacktestResult` may claim about itself, and the order the claims strengthen.

    `UNAUDITED` is the default a `BacktestResult` is constructed with and the only value
    an incomplete battery may report. It is not a synonym for `NOT_CREDIBLE` -- "we have
    not finished checking" and "we checked and it failed" are different statements, and
    the evolution engine's promotion gate treats them differently (`unaudited` never
    promotes and never *disqualifies* a strategy the way a `not_credible` history does).
    """

    UNAUDITED = "unaudited"
    CREDIBLE = "credible"
    NOT_CREDIBLE = "not_credible"


class AuditFinding(BaseModel):
    """One check's verdict, with the evidence that produced it.

    `frozen=True` because a finding is part of a result's identity once reported, and
    `docs/rules/append-only-audit.md` treats an audit record that the application could
    rewrite as no audit record at all. `strict=True` so `status="Pass"` or a bare `1` for
    an enum member is refused rather than coerced -- the whole point of a closed set of
    three statuses is that nothing outside it can arrive silently.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    check: AuditCheck
    status: AuditStatus
    evidence: str

    @field_validator("evidence")
    @classmethod
    def _evidence_is_not_a_claim(cls, candidate: str) -> str:
        """Refuse a blank string: `CLAUDE.md` section 7 -- evidence, not assertion.

        A plain `ValueError` here, not a package-specific exception: this is one field
        failing one bound, and pydantic collecting it into the surrounding
        `ValidationError` is exactly what "fails schema validation" (issue #44's own
        wording) means.
        """
        if not candidate.strip():
            raise ValueError(
                "evidence must be actual command or query output, not a claim; a blank "
                "string means the check that produced this finding was never run"
            )
        return candidate
