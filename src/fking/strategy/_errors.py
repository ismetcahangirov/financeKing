"""The strategy layer's own failures, all of them terminal.

Every class here is a leaf of `FkingError`, so a caller that genuinely means "any
failure this codebase raises on purpose" catches them too. They live here rather than
in `fking.platform.errors` for the reason `docs/rules/module-boundaries.md` gives:
`platform` holds mechanism and gets no trading vocabulary, and a class describing a
strategy declaring a feature the corpus has never held is vocabulary that belongs to
this package.

None of them is recoverable in-process, and none of them is caught anywhere. A run that
carried on past one of these would still emit signals, and the signals would be
indistinguishable from correct ones -- which is the shape of defect this whole package
is arranged against.
"""

from __future__ import annotations

from fking.platform.errors import FkingError


class StrategyError(FkingError):
    """Base for every error the strategy layer raises deliberately."""


class StrategyContractError(StrategyError):
    """A declaration is malformed, so no strategy can be identified by it.

    Raised at construction rather than at the first bar. A specification is the thing
    the evolution engine mutates and the trial ledger charges against
    (`EVOLUTION_ENGINE.md`); a spec that is only partly valid describes a lineage whose
    ancestors could never have run, and lineage is then a claim nobody can check.
    """


class FeatureUnavailableError(StrategyError):
    """A specification requires a feature the feature registry does not declare.

    Raised at *registration* and never at the first bar. A strategy that discovers its
    inputs are absent on bar one has already been scheduled, already been charged a
    trial, and already produced a run whose emptiness reads downstream as "no edge here"
    rather than as "no data here".

    The message names what the catalogue does hold, because the most frequent cause is
    an LLM-authored strategy asking for a feature that exists in the literature it was
    trained on rather than in this corpus (`ARCHITECTURE.md` section 6). Spelled
    `...Error` to match `DataUnavailableError` next door, and because `N818` refuses any
    other spelling.
    """


class DuplicateStrategyError(StrategyError):
    """Two strategies claim one `(strategy_id, strategy_version)`.

    Refused rather than resolved by insertion order. The pair is what a survival score,
    a lineage edge and an audit row all key on, so silently keeping the last registration
    would attribute one strategy's realised record to a different strategy's code.
    """


class ObservationRefusedError(StrategyError):
    """A bar was handed to a strategy that had not declared it, or could not see it.

    Four conditions, one class, because the response to all four is identical and none
    of them is a market event: an instrument the spec never named, a bar interval it
    never subscribed to, a bar whose close has not happened yet at the injected `as_of`,
    and a bar older than one already consumed. Each is a defect in the engine driving the
    strategy rather than in the strategy, and each would otherwise turn into a decision
    taken on data the run was never gated on.
    """


class SignalRefusedError(StrategyError):
    """A strategy emitted a `Signal` that contradicts its own specification.

    The specification is the contract the risk engine, the backtest and the evolution
    engine all read. A signal on an undeclared instrument, stamped at an instant other
    than the injected `as_of`, carrying a horizon the spec did not declare, or naming an
    invalidation level the declared rule does not produce, is a strategy whose behaviour
    cannot be predicted from its spec -- which makes the spec decorative and the lineage
    a lie.
    """
