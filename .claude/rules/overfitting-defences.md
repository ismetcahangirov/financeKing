# Rule — Overfitting Defences

## The rule

Every search over strategy space is charged to a **global, monotone trial counter** at *specification* time, and every claim of edge is stated as a **deflated Sharpe ratio computed with the project-wide trial count**, not the count of the study that produced it.

Concretely, all nine of these hold, together:

1. **The counter is global.** One number for the whole repository, for its whole history. Not per-study, not per-agent, not per-branch.
2. **The counter is monotone.** It never decreases. No expiry, no reset on refactor, no "those trials were on different data", no reset when the schema changes.
3. **The counter is charged at specification time for the full declared grid, and reconciled upward at execution.** The charge for a registered specification is `max(declared_grid_size, actual_executions)`. A declared 200-point grid abandoned after 12 points charges 200 — the selection happened when you wrote the grid down. A declared 5-point grid that ends up running 60 configurations charges 60 — the grid was extended, and extension is more selection. `BacktestEngine.run()` refuses to execute a `spec_hash` that was never registered, so there is no path to a result that skips the ledger entirely. See "Where the charge happens" below.
4. **Validation is combinatorial purged cross-validation**, with a purge equal to the label horizon and an embargo of `max(label_horizon, 0.01 * T)`. Walk-forward runs alongside it. A single train/test split is not evidence (`../../CLAUDE.md` §11).
5. **The permanently held-out period is burned the moment it is read.** Reading it requires prior human authorisation and is recorded in the ledger. There is no such thing as reading it and not counting it.
6. **Promotion requires forward out-of-sample performance.** A challenger becomes champion on evidence gathered *after* its specification hash was frozen. Validation performance alone never promotes anything.
7. **Sample sizes are stated in independent episodes**, never in observation counts. 41,208 hourly bars containing 37 distinct funding-extremity episodes is a sample of 37.
8. **Fold sign consistency is a promotion criterion.** A strategy whose CPCV fold Sharpes are 60% positive and 40% negative has a mean, not an edge.
9. **The parameter count raises the bar.** Each free parameter beyond the second must halve the residual probability that the result is selection noise. A 9-parameter strategy clears a bar 128 times tighter than a 2-parameter one.

And the rule that binds them: **the decision rule is written down, in full, with exact thresholds, before the data is touched, and applied literally.** A result that fails by 0.8bp failed. There is no "one more configuration".

## Why

Automated search over strategy space is a machine for producing overfit results (`../../ARCHITECTURE.md` §10). Run enough configurations against fixed history and some will look excellent by chance alone — that is not a risk of the design, it is an arithmetic certainty of it. The defences are not hygiene around the evolution engine; they *are* the evolution engine. The mutation operators are the easy part.

Each clause exists because of a specific way the arithmetic gets evaded:

| Defence | The evasion it closes |
|---|---|
| Global counter | Deflating by 24 trials when the project has run 1,847 understates the selection pool by two orders of magnitude |
| Monotone | A counter that can be reset will be reset, and deflation becomes decorative |
| Charged at specification | Abandoning a grid early after the first points look good is *the* selection event, and charging at execution prices it at zero |
| Purge + embargo sized to the label horizon | Overlapping labels leak the test fold into the train fold; every fold Sharpe comes back inflated and the inflation is invisible |
| Held-out burned on read | A holdout you consult twice is a validation set with extra ceremony |
| Forward OOS for promotion | Validation performance is the thing you selected on; using it to promote is circular |
| Episodes, not observations | Hourly resampling of 37 events does not produce 41,208 independent draws, and the t-statistic computed as if it does is off by a factor of ~33 |
| Fold sign consistency | A high mean Sharpe from three excellent folds and twenty-five mediocre ones is a regime artefact |
| Parameter penalty | Free parameters buy in-sample fit at a known rate, and a nine-parameter fit that beats a two-parameter fit has told you nothing |

The governing incentive, and the reason the counter must be visible in every report: **every test anyone runs makes every future result harder to prove.** That is correct. It is what makes a hypothesis with two theory-fixed parameters cheap and a 200-point grid search expensive to everyone, forever.

## Where the charge happens

This section is authoritative. Three components touch the ledger and their responsibilities do not overlap.

| Component | Responsibility | Explicitly not its job |
|---|---|---|
| `quant` agent | Registers the specification **before any data access**, declaring the full grid it may explore. This is the anti-HARKing gate and the point at which the declared charge is fixed. | Does not own ledger storage, and cannot amend a declared grid downward. |
| `optimizer` agent | Owns ledger **mechanics**: persistence, monotonicity, the `max(declared, executed)` reconciliation, per-lineage and global aggregates, and the deflated Sharpe computation. | Does not decide what gets registered, and cannot author a specification. |
| `BacktestEngine.run()` | **Enforcement.** Rejects any run whose `spec_hash` has no prior registration, and reports each execution to the ledger. | Does not compute deflation and does not interpret results. |

Two distinct evasions are closed by two distinct mechanisms, which is why both a declaration and an execution report exist:

- **Optional stopping.** Declare 200, run 12 because the 12th looked good, report 12. Closed by charging the declared grid — the decision to stop early is itself the selection event.
- **Undeclared search.** Run 60 backtests without registering anything, report the best. Closed by the engine refusing unregistered `spec_hash` values, and by reconciling the charge upward when executions exceed the declaration.

Charging `max()` rather than a sum is deliberate. Summing declaration and execution would double-charge the ordinary, honest case — declare 200, run 200 — and a defence that punishes correct behaviour is a defence people route around.

**The counter is the reason the evolution engine is trustworthy.** Treat any proposal to make it resettable, per-branch, per-agent, or "reset because the data changed" as a proposal to remove the primary overfitting defence, and require the same review as a change to risk limits.

## Incorrect

```python
# src/fking/evolution/search.py
async def sweep(space: ParameterSpace, data: FeatureFrame) -> StrategySpec | None:
    """Search a parameter space and return the best configuration found."""
    best: tuple[float, StrategySpec] | None = None
    for point in space.iter_points():                     # 200 points declared
        result = backtest(point, data)
        if result.sharpe < 0.4:
            continue
        charge_trial(n=1)                                 # charged per executed point
        if best is None or result.sharpe > best[0]:
            best = (result.sharpe, point)
        if result.sharpe > 2.0:
            break                                         # "found a good one, stop early"
    if best is None:
        return None
    study_trials = await count_trials_for(space.study_id)  # per-study count
    dsr = deflated_sharpe(observed=best[0], n_trials=study_trials, ...)
    return best[1] if dsr > 0.5 else None
```

What goes wrong at runtime: the loop breaks at point 12 with a Sharpe of 2.1, so `charge_trial` fires 12 times instead of 200 and `study_trials` returns 12 — but the `break` is itself the selection, and the 188 unrun points were the alternatives that would have been accepted had the first twelve looked worse. `expected_max_sharpe(12, ...)` is roughly 0.6 standard deviations below `expected_max_sharpe(200, ...)`, so the deflated Sharpe comes back around 0.72 instead of failing outright, and a noise configuration is returned as a strategy. Worse, `count_trials_for(space.study_id)` scopes the count to this study, so the 1,800 trials the project already spent selecting *which space to search* are priced at zero. The function returns a `StrategySpec` that is, statistically, the argmax of a lottery.

## Correct

```python
# src/fking/evolution/trials.py
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from math import e, sqrt
from statistics import NormalDist
from typing import Any, Final, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Euler-Mascheroni constant. Appears in the expected value of the maximum of N
# independent Gaussians; see Bailey & Lopez de Prado (2014), "The Deflated
# Sharpe Ratio", eq. 5. Recorded here because a bare 0.577 in this file would
# eventually be "simplified" by someone who did not know what it was.
_EULER_MASCHERONI: Final[float] = 0.5772156649015329


@dataclass(frozen=True, slots=True)
class TrialSpecification:
    """A search, declared in full before any data is read."""

    correlation_id: str
    statement: str
    registered_by: str
    parameter_grid: Mapping[str, Sequence[Any]]
    n_symbols: int
    n_variants: int
    holdout_requested: bool
    human_authorisation_ref: str | None

    @property
    def n_parameters(self) -> int:
        return len(self.parameter_grid)

    @property
    def trials_charged(self) -> int:
        """Every point in the declared grid, whether or not it is ever run."""
        points = 1
        for values in self.parameter_grid.values():
            points *= len(values)
        return points * self.n_symbols * self.n_variants

    def spec_hash(self) -> str:
        canonical = json.dumps(
            {
                "statement": self.statement,
                "grid": {k: list(v) for k, v in sorted(self.parameter_grid.items())},
                "n_symbols": self.n_symbols,
                "n_variants": self.n_variants,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TrialLedgerError(RuntimeError):
    """The trial ledger is unreadable, inconsistent, or refused a charge."""


async def register_and_charge(
    conn: AsyncConnection, spec: TrialSpecification, *, now: datetime
) -> int:
    """Charge the full declared grid and return the new global cumulative count.

    Registration precedes data access. This function is the only writer of
    trial_ledger, and the table forbids UPDATE and DELETE (./append-only-audit.md).
    """
    if spec.holdout_requested and spec.human_authorisation_ref is None:
        raise TrialLedgerError(
            "the permanently held-out period requires a recorded human authorisation "
            "before registration; reading it burns it"
        )
    row = (
        await conn.execute(
            text(
                """
                INSERT INTO trial_ledger (
                    charged_at, correlation_id, spec_hash, registered_by, statement,
                    parameter_grid, n_parameters, n_symbols, n_variants,
                    trials_charged, holdout_touched, human_authorisation_ref
                )
                VALUES (
                    :charged_at, :correlation_id, decode(:spec_hash, 'hex'),
                    :registered_by, :statement, cast(:grid as jsonb), :n_parameters,
                    :n_symbols, :n_variants, :trials_charged, :holdout,
                    :authorisation
                )
                RETURNING cumulative_trials
                """
            ),
            {
                "charged_at": now,
                "correlation_id": spec.correlation_id,
                "spec_hash": spec.spec_hash(),
                "registered_by": spec.registered_by,
                "statement": spec.statement,
                "grid": json.dumps(
                    {k: list(v) for k, v in spec.parameter_grid.items()}, default=str
                ),
                "n_parameters": spec.n_parameters,
                "n_symbols": spec.n_symbols,
                "n_variants": spec.n_variants,
                "trials_charged": spec.trials_charged,
                "holdout": spec.holdout_requested,
                "authorisation": spec.human_authorisation_ref,
            },
        )
    ).first()
    if row is None:  # pragma: no cover - the RETURNING clause always yields a row
        raise TrialLedgerError("trial_ledger INSERT returned no cumulative count")
    return int(row[0])


async def global_trial_count(conn: AsyncConnection) -> int:
    row = (await conn.execute(text("SELECT n FROM global_trial_count"))).first()
    if row is None:
        raise TrialLedgerError("global_trial_count view returned no row")
    return int(row[0])


def expected_max_sharpe(n_trials: int, sharpe_variance_across_trials: float) -> float:
    """E[max SR] over n_trials independent strategies with the given SR dispersion."""
    if n_trials < 2:
        raise ValueError("expected_max_sharpe is undefined below two trials")
    if sharpe_variance_across_trials <= 0.0:
        raise ValueError("SR variance across trials must be positive")
    normal = NormalDist()
    z_high = normal.inv_cdf(1.0 - 1.0 / n_trials)
    z_low = normal.inv_cdf(1.0 - 1.0 / (n_trials * e))
    return sqrt(sharpe_variance_across_trials) * (
        (1.0 - _EULER_MASCHERONI) * z_high + _EULER_MASCHERONI * z_low
    )


def deflated_sharpe(
    *,
    observed_sharpe: float,
    n_trials: int,
    n_independent_episodes: int,
    skewness: float,
    kurtosis: float,
    sharpe_variance_across_trials: float,
) -> float:
    """Probability the observed (per-episode) Sharpe exceeds the selection benchmark.

    n_independent_episodes, never the raw observation count: the correction is
    driven by the effective sample, and hourly resampling of 37 events does not
    manufacture 41,208 independent draws.
    """
    if n_independent_episodes < 2:
        raise ValueError("deflated Sharpe needs at least two independent episodes")
    benchmark = expected_max_sharpe(n_trials, sharpe_variance_across_trials)
    denominator = sqrt(
        1.0 - skewness * observed_sharpe + ((kurtosis - 1.0) / 4.0) * observed_sharpe**2
    )
    statistic = (
        (observed_sharpe - benchmark) * sqrt(n_independent_episodes - 1) / denominator
    )
    return NormalDist().cdf(statistic)
```

```python
# src/fking/evolution/promotion.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncConnection

from fking.evolution.trials import (
    TrialLedgerError,
    deflated_sharpe,
    global_trial_count,
)


@dataclass(frozen=True, slots=True)
class PromotionThresholds:
    min_deflated_sharpe: float
    min_fold_sign_consistency: Decimal
    min_independent_episodes: int
    min_forward_independent_episodes: int


def thresholds_for(n_parameters: int) -> PromotionThresholds:
    """Each free parameter past the second halves the tolerated residual noise.

    Base 0.95 is the promotion floor; the per-hypothesis research verdict uses
    its own pre-registered thresholds (../agents/quant.md). Promotion is the
    stricter gate because it commits capital, not a conclusion.
    """
    extra = max(0, n_parameters - 2)
    return PromotionThresholds(
        min_deflated_sharpe=1.0 - (1.0 - 0.95) / float(2**extra),
        min_fold_sign_consistency=min(
            Decimal("0.95"), Decimal("0.75") + Decimal("0.02") * extra
        ),
        min_independent_episodes=30 + 15 * extra,
        min_forward_independent_episodes=20 + 10 * extra,
    )


class PromotionRefused(RuntimeError):
    """The challenger did not clear the pre-registered gate."""


async def gate(
    conn: AsyncConnection, challenger: ChallengerEvidence
) -> PromotionThresholds:
    if not challenger.spec_hash_matches:
        raise PromotionRefused(
            f"spec_hash changed between registration and test for "
            f"{challenger.correlation_id}; the result is void, not weak"
        )

    n_trials = await global_trial_count(conn)
    if n_trials < 2:
        # Zero means the ledger is empty or unreachable. It never means
        # "nothing was tried, so no deflation is needed".
        raise TrialLedgerError(
            f"global trial count is {n_trials}; refusing to promote against an "
            f"unreadable ledger"
        )

    limits = thresholds_for(challenger.n_parameters)
    dsr = deflated_sharpe(
        observed_sharpe=challenger.forward_sharpe,
        n_trials=n_trials,
        n_independent_episodes=challenger.forward_independent_episodes,
        skewness=challenger.forward_skewness,
        kurtosis=challenger.forward_kurtosis,
        sharpe_variance_across_trials=challenger.sharpe_variance_across_trials,
    )

    failures: list[str] = []
    if dsr < limits.min_deflated_sharpe:
        failures.append(f"deflated Sharpe {dsr:.4f} < {limits.min_deflated_sharpe:.4f}")
    if challenger.fold_sign_consistency < limits.min_fold_sign_consistency:
        failures.append(
            f"fold sign consistency {challenger.fold_sign_consistency} "
            f"< {limits.min_fold_sign_consistency}"
        )
    if challenger.independent_episodes < limits.min_independent_episodes:
        failures.append(
            f"{challenger.independent_episodes} independent episodes "
            f"< {limits.min_independent_episodes}"
        )
    if (
        challenger.forward_independent_episodes
        < limits.min_forward_independent_episodes
    ):
        failures.append(
            f"{challenger.forward_independent_episodes} forward episodes "
            f"< {limits.min_forward_independent_episodes}"
        )
    if failures:
        raise PromotionRefused(
            f"{challenger.correlation_id} refused at N={n_trials}: "
            + "; ".join(failures)
        )
    return limits
```

What this gets right that the incorrect version does not: `trials_charged` multiplies out the **declared** grid, so abandoning the search early changes nothing; `global_trial_count` reads one project-wide number and treats `0` as a ledger fault rather than an absence of selection; the DSR is computed on **forward** episodes with a spec hash that must still match; and `thresholds_for` makes the ninth parameter cost 128× the residual-noise budget of the second.

## Enforcement

**The ledger is append-only in the database, not in the ORM** (`./append-only-audit.md` carries the full pattern; this migration applies it to `trial_ledger`).

```python
# migrations/versions/0011_trial_ledger.py
"""Global trial ledger: append-only, monotone, spec-hash unique.

Revision ID: 0011_trial_ledger
Revises: 0010_agent_episodic_memory
"""

from __future__ import annotations

from alembic import op

revision: str = "0011_trial_ledger"
down_revision: str = "0010_agent_episodic_memory"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE trial_ledger (
            seq                      bigint      PRIMARY KEY
                                     GENERATED ALWAYS AS IDENTITY,
            charged_at               timestamptz NOT NULL,
            correlation_id           uuid        NOT NULL,
            spec_hash                bytea       NOT NULL UNIQUE,
            registered_by            text        NOT NULL,
            statement                text        NOT NULL,
            parameter_grid           jsonb       NOT NULL,
            n_parameters             integer     NOT NULL CHECK (n_parameters >= 0),
            n_symbols                integer     NOT NULL CHECK (n_symbols >= 1),
            n_variants               integer     NOT NULL CHECK (n_variants >= 1),
            trials_charged           integer     NOT NULL CHECK (trials_charged >= 1),
            cumulative_trials        bigint      NOT NULL,
            holdout_touched          boolean     NOT NULL DEFAULT false,
            human_authorisation_ref  text,
            CONSTRAINT holdout_needs_authorisation
                CHECK (NOT holdout_touched OR human_authorisation_ref IS NOT NULL)
        )
        """
    )

    # Monotonicity is computed in the database, under an advisory lock, so two
    # concurrent registrations cannot both read the same predecessor and produce
    # a cumulative total lower than the sum of charges.
    op.execute(
        """
        CREATE FUNCTION trial_ledger_accumulate() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            previous bigint;
        BEGIN
            -- 8812331 is an arbitrary but fixed lock key reserved for this chain.
            PERFORM pg_advisory_xact_lock(8812331);
            SELECT COALESCE(max(cumulative_trials), 0) INTO previous FROM trial_ledger;
            NEW.cumulative_trials := previous + NEW.trials_charged;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trial_ledger_accumulate_before_insert
            BEFORE INSERT ON trial_ledger
            FOR EACH ROW EXECUTE FUNCTION trial_ledger_accumulate()
        """
    )

    op.execute(
        """
        CREATE FUNCTION trial_ledger_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION
                'trial_ledger is append-only: % is forbidden', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trial_ledger_no_update_delete
            BEFORE UPDATE OR DELETE ON trial_ledger
            FOR EACH ROW EXECUTE FUNCTION trial_ledger_immutable()
        """
    )

    # The trigger is the backstop. The grant is the control: TRUNCATE does not
    # fire row triggers, so it must be revoked rather than intercepted.
    op.execute("REVOKE ALL ON trial_ledger FROM PUBLIC")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON trial_ledger FROM fking_app")
    op.execute("GRANT INSERT, SELECT ON trial_ledger TO fking_app")

    op.execute(
        """
        CREATE VIEW global_trial_count AS
        SELECT COALESCE(max(cumulative_trials), 0)::bigint AS n FROM trial_ledger
        """
    )
    op.execute("GRANT SELECT ON global_trial_count TO fking_app")


def downgrade() -> None:
    raise RuntimeError(
        "0011 is irreversible: dropping trial_ledger resets the global trial "
        "counter, which invalidates every deflated Sharpe the project has ever "
        "reported. Roll forward with a new migration."
    )
```

**Tests that must exist** (`tests/evolution/test_trial_ledger.py`, real Postgres via testcontainers — `../../CLAUDE.md` §5 forbids mocking the database):

```python
import pytest
from sqlalchemy.exc import DBAPIError

from fking.evolution.promotion import PromotionRefused, gate, thresholds_for
from fking.evolution.trials import TrialLedgerError, register_and_charge


async def test_full_grid_is_charged_even_when_the_search_is_abandoned(
    conn, clock
) -> None:
    spec = TrialSpecification(
        correlation_id="c-1",
        statement="declared 200-point grid",
        registered_by="evolution",
        parameter_grid={"fast": list(range(10)), "slow": list(range(20))},
        n_symbols=1,
        n_variants=1,
        holdout_requested=False,
        human_authorisation_ref=None,
    )
    assert spec.trials_charged == 200
    assert await register_and_charge(conn, spec, now=clock.now()) == 200


async def test_ledger_rows_cannot_be_updated_or_deleted(conn, registered_spec) -> None:
    for statement in (
        "UPDATE trial_ledger SET trials_charged = 1",
        "DELETE FROM trial_ledger",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            await conn.execute(text(statement))


async def test_spec_hash_mismatch_voids_rather_than_weakens(conn, challenger) -> None:
    voided = replace(challenger, spec_hash_matches=False)
    with pytest.raises(PromotionRefused, match="void, not weak"):
        await gate(conn, voided)


async def test_promotion_refuses_when_the_ledger_is_empty(conn, challenger) -> None:
    with pytest.raises(TrialLedgerError, match="unreadable ledger"):
        await gate(conn, challenger)


@pytest.mark.parametrize(
    ("n_parameters", "expected_dsr_floor"),
    [(2, 0.95), (3, 0.975), (5, 0.996875), (9, 0.99998046875)],
)
def test_parameter_count_halves_the_residual_noise_budget(
    n_parameters: int, expected_dsr_floor: float
) -> None:
    assert thresholds_for(n_parameters).min_deflated_sharpe == pytest.approx(
        expected_dsr_floor
    )


async def test_holdout_registration_without_authorisation_is_refused(conn, clock):
    spec = TrialSpecification(..., holdout_requested=True, human_authorisation_ref=None)
    with pytest.raises(TrialLedgerError, match="reading it burns it"):
        await register_and_charge(conn, spec, now=clock.now())
```

**CI gates.** `make check` runs the above under `pytest`. A separate scheduled job asserts the ledger is internally consistent — `cumulative_trials` equals the running sum of `trials_charged` ordered by `seq`, and `max(seq)` never decreases between runs. A discrepancy is escalated as `needs-human` and outranks all other work, because it means every deflated Sharpe in the project is wrong (`../agents/quant.md`, escalation rules).

## The one exception

**None.**

The exception people reach for is exempting "exploratory" runs — quick sweeps, sanity checks, "just to see whether the data supports anything at all". Refuse it, because it is not a small hole; it is the entire hole.

Exploratory runs are exactly the runs that select what gets registered. If you sweep thirty ideas informally, keep the one that looked promising, and register that one with a two-point grid, the ledger records 2 trials against a selection that was drawn from 30-plus. `expected_max_sharpe(2, ...)` is near zero, so the deflation subtracts almost nothing, and the DSR reports a result that survived a thirty-way search as though it had survived a coin flip. Every number downstream — the survival score, the promotion decision, the forward-decay metric that grades the whole research programme — is then computed from a benchmark that is wrong in the optimistic direction.

The exemption is also unenforceable by construction. "Exploratory" is a self-declared label applied by the party who benefits from applying it, at the moment they benefit from applying it. There is no test that distinguishes an exploratory sweep from a search whose author decided afterwards that it had been exploratory. Any counter with a free-form escape hatch converges to a counter with nothing in it.

The cost of no exception is real and it is the point: informal exploration becomes expensive, so it stops happening, and hypotheses arrive with one or two parameters fixed a priori from a mechanism instead of a grid fixed a posteriori from a plot. If you cannot fix a parameter from theory, you do not have a mechanism — you have a search, and the search must pay.
