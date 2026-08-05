"""The engine's own failures, all of them terminal.

Every class here is a leaf of `FkingError` rather than a sibling of it, because each one
names a condition under which the run's output is not evidence -- and a caller that
wanted "any failure this codebase raises on purpose" must catch these too. That is the
opposite of `fking.platform.scheduler`, which deliberately sits outside the tree so the
beat's own bugs cannot be recorded as failed jobs.

They live here rather than in `fking.platform.errors` for the reason
`.claude/rules/module-boundaries.md` gives: `platform` holds mechanism and gets no
trading vocabulary. A class named `CausalityError` describing an event scheduled into a
simulated past is vocabulary that belongs to the engine.

None of them is recoverable in-process. There is no retry, no clamp and no continue:
a backtest that carried on past one of these would still produce a number, and the
number would be indistinguishable from a correct one.
"""

from __future__ import annotations

from fking.platform.errors import FkingError

__all__ = [
    "BacktestError",
    "CausalityError",
    "EventBudgetExhaustedError",
    "RunConfigError",
]


class BacktestError(FkingError):
    """Base for every error the backtest engine raises deliberately."""


class RunConfigError(BacktestError):
    """A run configuration is malformed, so no run can be identified by it.

    Raised at construction rather than at the first use of the offending field. A
    `RunConfig` is content-hashed and the hash is the run's identity
    (`BACKTEST_ENGINE.md` section 5); a config that is only partly valid produces a
    hash for a run that could never have executed, and that hash then appears in a
    result nobody can reproduce.
    """


class CausalityError(BacktestError):
    """Something tried to move simulated time backwards.

    Two shapes, one condition. A handler scheduling an event at an instant earlier than
    the one the loop is currently dispatching is asking for a decision to be observed
    before its cause; and the clock refusing to be advanced backwards is the same
    property asserted from the other side, where the only possible source is a defect in
    the queue's ordering.

    Not clamped to `now`. Clamping turns a causality violation into a fill that happened
    at a plausible-looking time, which is exactly the shape of bug that produces an
    excellent equity curve and no error (`.claude/rules/no-lookahead.md`).
    """


class EventBudgetExhaustedError(BacktestError):
    """The run dispatched more events than its configuration declared it would.

    The failure this catches is a handler that schedules an event at its own timestamp
    on every call: simulated time never advances, the queue never drains, and the loop
    spins forever. Under an unattended evolution cycle that is a hung machine rather
    than a crash -- nothing times out, nothing alerts, and the generation never
    completes.

    The budget is declared per run rather than inferred, because "more events than
    expected" is only meaningful against a number somebody stated.
    """
