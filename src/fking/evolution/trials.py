"""The global trial ledger: what the project searched, and what that costs every result.

`SR*` -- the Sharpe a zero-edge strategy would show as the best of `K` trials -- grows as
roughly `sqrt(2 ln K)`. That is a slow function, and its slowness is the danger rather
than the reassurance: undercounting by 250x lowers the bar by about 43%, which never
produces an obviously wrong number. It produces a slightly too-generous threshold,
forever, applied to every strategy in the population at once.

So this module owns three properties and refuses to own a fourth.

**The count is keyed on the search context, not on the study.** `hash(symbol_universe,
date_range, feature_set)`. Trials against different data do not contaminate each other,
but every trial against *this* data does -- including the 1,800 the project spent
choosing which space to search. Counting within a generation and resetting between them
is the common real-world failure, and it gives `K` around 200 where the true figure is
nearer 50,000.

**Two figures per context.** `context_trial_count` feeds `SR*`; `lineage_trial_count`
feeds the family term. A system that deflates only by lineage treats each family as if it
were the only search ever run.

**The charge is `max(declared, executed)`, and neither half is optional.** Declaring 200
and stopping at 12 because the 12th looked good charges 200 -- the decision to stop early
*is* the selection event. Declaring 5 and running 60 charges 60 -- extending a grid is
more selection. `max()` rather than a sum, because summing double-charges the honest case
of declaring 200 and running 200, and a defence that punishes correct behaviour gets
routed around within a month.

**What this module does not own** is the decision to register. That belongs to whoever
authored the specification, and it happens before any data is read; a ledger that could
author a specification could author one after seeing the result. Nor does it own
enforcement: `EventLoop` refuses a `spec_hash` with no charge, because a counter the
caller maintains voluntarily is a counter that will be wrong.

The arithmetic that must be right under concurrency lives in the database. Both the
running total and the `max()` reconciliation are computed inside `BEFORE INSERT` triggers
under a shared advisory lock (`0002_audit_substrate`, `0018_trial_ledger_search_context`),
never by a read-then-write in Python -- fifty concurrent registrations reading the same
predecessor would produce a total below the sum of their charges, which understates the
selection pool in exactly the flattering direction.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from fking.backtest import SharpeEvidence, SpecRegistration
from fking.evolution._errors import TrialLedgerError, TrialSpecificationError

__all__ = [
    "ChargeReceipt",
    "ExecutionReceipt",
    "ObservedPerformance",
    "SearchContext",
    "TrialLedger",
    "TrialSpecification",
]

_ENTRY_KIND_REGISTRATION: Final[str] = "registration"

# A digest is a digest: hex on the Python side because a spec hash appears in log lines,
# issue titles and refusal messages, BYTEA in the database because 32 bytes stored as 64
# characters doubles every index that carries it.
_DIGEST_HEX_LENGTH: Final[int] = 64


def _digest(payload: Mapping[str, object]) -> str:
    """SHA-256 over the one JSON rendering two processes will both produce.

    `sort_keys` is what makes it canonical and `separators` removes the whitespace that
    would otherwise depend on nothing anybody chose. `ensure_ascii=False` keeps a
    non-ASCII symbol -- Binance testnet serves one deliberately -- as the code points the
    venue sent rather than as an escape whose casing varies by encoder.
    """
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _require_utc(moment: datetime, field_name: str) -> None:
    offset = moment.utcoffset()
    if offset is None:
        raise TrialSpecificationError(f"{field_name} must be timezone-aware; got {moment!r}")
    if offset != timedelta(0):
        # Rejected rather than converted. `astimezone(UTC)` would silently accept a value
        # whose offset was guessed wrong upstream, and the guess belongs where the data
        # enters -- here it would be baked into a search-context hash nobody can undo.
        raise TrialSpecificationError(f"{field_name} must be UTC; got offset {offset!r}")


def _require_text(candidate: str, field_name: str) -> None:
    if not candidate or candidate.strip() != candidate:
        raise TrialSpecificationError(
            f"{field_name} must be non-empty and free of surrounding whitespace; got {candidate!r}"
        )


@dataclass(frozen=True, slots=True)
class SearchContext:
    """The data a search ran against, reduced to the identity trials are counted under.

    Three components and no more. The symbol universe, the window and the feature set are
    what decide whether two searches could have selected the same artefact out of the
    same noise; anything else -- the strategy family, the agent that proposed it, the day
    it ran -- narrows the context without narrowing the selection, and a context that is
    too narrow is a counter that resets.
    """

    symbol_universe: tuple[str, ...]
    window_start_utc: datetime
    window_end_utc: datetime
    feature_ids: frozenset[str]

    def __post_init__(self) -> None:
        if not self.symbol_universe:
            raise TrialSpecificationError("symbol_universe must name at least one instrument")
        for symbol in self.symbol_universe:
            _require_text(symbol, "symbol")
        if len(set(self.symbol_universe)) != len(self.symbol_universe):
            raise TrialSpecificationError(
                f"symbol_universe contains a duplicate: {self.symbol_universe}. A repeated "
                f"symbol multiplies the declared grid without widening the search"
            )
        if not self.feature_ids:
            raise TrialSpecificationError("feature_ids must name at least one feature")
        for feature_id in sorted(self.feature_ids):
            _require_text(feature_id, "feature_id")
        _require_utc(self.window_start_utc, "window_start_utc")
        _require_utc(self.window_end_utc, "window_end_utc")
        if self.window_end_utc <= self.window_start_utc:
            raise TrialSpecificationError(
                f"window_end_utc {self.window_end_utc.isoformat()} must follow "
                f"window_start_utc {self.window_start_utc.isoformat()}"
            )

    @property
    def symbol_count(self) -> int:
        """How many instruments the search ranged over."""
        return len(self.symbol_universe)

    @property
    def context_hash(self) -> str:
        """The identity trials are counted under. Order-insensitive in both collections."""
        return _digest(
            {
                "symbol_universe": sorted(self.symbol_universe),
                "window_start_utc": self.window_start_utc.isoformat(),
                "window_end_utc": self.window_end_utc.isoformat(),
                "feature_ids": sorted(self.feature_ids),
            }
        )


@dataclass(frozen=True, slots=True)
class TrialSpecification:
    """A search, declared in full before any data is read.

    `parameter_grid` holds each parameter's candidate values as strings, not as numbers.
    The grid is hashed, and `0.1` reaching this dataclass as a `float` would already have
    lost the exactness the digest is supposed to preserve -- two callers writing the same
    grid would then produce two spec hashes, and the second registration would charge the
    same search twice with no way to undo it.
    """

    correlation_id: UUID
    statement: str
    registered_by: str
    parameter_grid: Mapping[str, Sequence[str]]
    search_context: SearchContext
    lineage_id: str
    variant_count: int = 1
    holdout_requested: bool = False
    human_authorisation_ref: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.statement, "statement")
        _require_text(self.registered_by, "registered_by")
        _require_text(self.lineage_id, "lineage_id")
        if self.variant_count < 1:
            raise TrialSpecificationError(
                f"variant_count must be at least 1; got {self.variant_count}"
            )
        for name, candidates in self.parameter_grid.items():
            _require_text(name, "parameter name")
            if not candidates:
                raise TrialSpecificationError(
                    f"parameter {name!r} declares no candidate values, so the declared "
                    f"grid multiplies out to zero and the search would charge nothing"
                )
            for candidate in candidates:
                _require_text(candidate, f"parameter {name!r} candidate")
        if self.holdout_requested and self.human_authorisation_ref is None:
            raise TrialSpecificationError(
                "the permanently held-out period requires a recorded human authorisation "
                "before registration; reading it burns it, and there is no such thing as "
                "reading it and not counting it"
            )

        # `frozen=True` protects the binding, not the object bound: a plain dict here
        # stays mutable through the reference the caller kept, and a grid edited after
        # registration would leave the ledger's spec_hash describing a search that no
        # longer exists -- in an append-only table, permanently.
        object.__setattr__(
            self,
            "parameter_grid",
            MappingProxyType(
                {name: tuple(candidates) for name, candidates in self.parameter_grid.items()}
            ),
        )

    @property
    def parameter_count(self) -> int:
        """How many free parameters the search declares."""
        return len(self.parameter_grid)

    @property
    def declared_grid_point_count(self) -> int:
        """Every point in the declared grid, whether or not it is ever run.

        The whole grid, multiplied by the symbol universe and by the variant count,
        because a configuration evaluated against a second symbol is a second
        configuration. Abandoning the search at point twelve does not reduce it: the
        188 unrun points were the alternatives that would have been accepted had the
        first twelve looked worse.
        """
        points = 1
        for candidates in self.parameter_grid.values():
            points *= len(candidates)
        return points * self.search_context.symbol_count * self.variant_count

    @property
    def spec_hash(self) -> str:
        """This search's identity, including the data it ran against.

        The search context is inside the digest deliberately. The same grid against a
        different symbol universe is a different search, and hashing only the grid would
        make the second registration a duplicate-charge error rather than a new charge.
        """
        return _digest(
            {
                "statement": self.statement,
                "grid": {
                    name: list(candidates)
                    for name, candidates in sorted(self.parameter_grid.items())
                },
                "search_context": self.search_context.context_hash,
                "lineage_id": self.lineage_id,
                "variant_count": self.variant_count,
            }
        )


@dataclass(frozen=True, slots=True)
class ObservedPerformance:
    """A measured Sharpe and its moments -- everything a deflation needs but the count.

    The trial count is deliberately absent. `SharpeEvidence` requires it and this type
    cannot supply it, so the only way to turn one of these into evidence is
    `TrialLedger.sharpe_evidence`, which reads the count from the ledger. A caller that
    could pass its own number would pass the count of its own study, which is the
    200-against-50,000 undercount this whole module exists to prevent.

    `independent_episode_count` is episodes, never bars: 41,208 hourly observations
    containing 37 distinct funding-extremity episodes is a sample of 37, and treating it
    as 41,208 overstates the t-statistic by a factor near 33.
    """

    observed_sharpe: Decimal
    independent_episode_count: int
    skewness: Decimal
    kurtosis: Decimal
    sharpe_variance_across_trials: Decimal


@dataclass(frozen=True, slots=True)
class ChargeReceipt:
    """What one registration cost, and what the counters read afterwards.

    Both counts are returned rather than left to a follow-up query. The number that
    matters is the one that was true at the moment of the charge, and a caller that has
    to ask again gets whatever concurrent registrations have done since.
    """

    seq: int
    spec_hash: str
    search_context_hash: str
    lineage_id: str
    trials_charged: int
    cumulative_trials: int
    context_trial_count: int
    lineage_trial_count: int


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    """One executed configuration, and whether it overran the declared grid.

    `charged` is `False` for every execution inside the declared grid -- that grid was
    charged in full at registration -- and `True` for each one beyond it. Reading it as
    "this execution was free" is the misreading to avoid: it was paid for in advance.
    """

    seq: int
    spec_hash: str
    execution_index: int
    charged: bool
    context_trial_count: int


# The key `0002_audit_substrate` reserved for the trial-ledger chain, taken here as well
# as inside the trigger. Advisory locks are re-entrant within a session, so the trigger's
# own acquisition is then free; what this buys is the ordering the trigger cannot fix on
# its own. `seq` is an IDENTITY column, allocated while the tuple is formed and therefore
# *before* any BEFORE INSERT trigger runs -- so without this, commit order and seq order
# diverge, and `fking.platform.persistence.chain`, which verifies the hash chain in seq
# order, reports a break on a chain that was linked correctly in commit order. Taking the
# lock before the INSERT statement pulls the sequence allocation inside it.
_TAKE_LEDGER_LOCK = sa.text("SELECT pg_advisory_xact_lock(8812331)")

_REGISTER = sa.text(
    """
    INSERT INTO trial_ledger (
        charged_at_utc, correlation_id, spec_hash, registered_by, statement,
        parameter_grid, n_parameters, n_symbols, n_variants, trials_charged,
        cumulative_trials, holdout_touched, human_authorisation_ref,
        prev_hash, row_hash, search_context_hash, lineage_id, entry_kind
    )
    VALUES (
        :charged_at_utc, :correlation_id, decode(:spec_hash, 'hex'), :registered_by,
        :statement, cast(:parameter_grid as jsonb), :n_parameters, :n_symbols,
        :n_variants, :trials_charged, 0, :holdout_touched, :human_authorisation_ref,
        '\\x00'::bytea, '\\x00'::bytea, decode(:search_context_hash, 'hex'),
        :lineage_id, :entry_kind
    )
    RETURNING seq, cumulative_trials
    """
)

# `execution_index` and `charged` are supplied as placeholders and overwritten by the
# BEFORE INSERT trigger, which is the same arrangement `trial_ledger` uses for its chain
# hashes and for the same reason: a writer that computes its own index can compute a
# convenient one, and the index is what decides whether the declared grid was overrun.
_REPORT_EXECUTION = sa.text(
    """
    INSERT INTO trial_execution (
        executed_at_utc, correlation_id, spec_hash, execution_index, config_hash,
        path_label, charged
    )
    VALUES (
        :executed_at_utc, :correlation_id, decode(:spec_hash, 'hex'), 1,
        decode(:config_hash, 'hex'), :path_label, false
    )
    RETURNING seq, execution_index, charged
    """
)

_REGISTRATION_FOR = sa.text(
    """
    SELECT trials_charged
      FROM trial_ledger
     WHERE spec_hash = decode(:spec_hash, 'hex')
       AND entry_kind = :entry_kind
    """
)

_CONTEXT_TRIALS = sa.text(
    """
    SELECT context_trials FROM trial_context_totals
     WHERE search_context_hash = decode(:search_context_hash, 'hex')
    """
)

_LINEAGE_TRIALS = sa.text(
    """
    SELECT lineage_trials FROM trial_lineage_totals
     WHERE search_context_hash = decode(:search_context_hash, 'hex')
       AND lineage_id = :lineage_id
    """
)


def _require_digest(candidate: str, field_name: str) -> str:
    if len(candidate) != _DIGEST_HEX_LENGTH or candidate != candidate.lower():
        raise TrialLedgerError(
            f"{field_name} must be a {_DIGEST_HEX_LENGTH}-character lowercase SHA-256 "
            f"digest; got {candidate!r}"
        )
    return candidate


class TrialLedger:
    """The only writer of `trial_ledger` and `trial_execution`.

    There is no `update`, no `delete` and no `correct`, and the database would refuse one
    anyway: `fking_app` holds `SELECT` and `INSERT` and nothing else, and a
    `BEFORE UPDATE OR DELETE` trigger raises regardless of who holds what. A counter that
    can be reset becomes decorative, and a reset is indistinguishable from a claim that
    the previous six months of searching never happened.
    """

    __slots__ = ("_engine",)

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def register(
        self, specification: TrialSpecification, *, charged_at_utc: datetime
    ) -> ChargeReceipt:
        """Charge the full declared grid and return both counters as they now read.

        `charged_at_utc` is a parameter rather than a clock read. A charge whose instant
        is decided inside the ledger cannot be replayed from the record, and the record
        is the whole point of the table.
        """
        _require_utc(charged_at_utc, "charged_at_utc")
        specification_hash = specification.spec_hash
        context_hash = specification.search_context.context_hash

        async with self._engine.begin() as connection:
            await connection.execute(_TAKE_LEDGER_LOCK)
            row = (
                await connection.execute(
                    _REGISTER,
                    {
                        "charged_at_utc": charged_at_utc,
                        "correlation_id": specification.correlation_id,
                        "spec_hash": specification_hash,
                        "registered_by": specification.registered_by,
                        "statement": specification.statement,
                        "parameter_grid": json.dumps(
                            {
                                name: list(candidates)
                                for name, candidates in specification.parameter_grid.items()
                            },
                            sort_keys=True,
                        ),
                        "n_parameters": specification.parameter_count,
                        "n_symbols": specification.search_context.symbol_count,
                        "n_variants": specification.variant_count,
                        "trials_charged": specification.declared_grid_point_count,
                        "holdout_touched": specification.holdout_requested,
                        "human_authorisation_ref": specification.human_authorisation_ref,
                        "search_context_hash": context_hash,
                        "lineage_id": specification.lineage_id,
                        "entry_kind": _ENTRY_KIND_REGISTRATION,
                    },
                )
            ).first()
            if row is None:  # pragma: no cover - RETURNING always yields on success
                raise TrialLedgerError("trial_ledger INSERT returned no cumulative total")
            context_trials = await self._context_trials(connection, context_hash)
            lineage_trials = await self._lineage_trials(
                connection, context_hash, specification.lineage_id
            )

        return ChargeReceipt(
            seq=int(row[0]),
            spec_hash=specification_hash,
            search_context_hash=context_hash,
            lineage_id=specification.lineage_id,
            trials_charged=specification.declared_grid_point_count,
            cumulative_trials=int(row[1]),
            context_trial_count=context_trials,
            lineage_trial_count=lineage_trials,
        )

    async def report_execution(
        self,
        *,
        spec_hash: str,
        config_hash: str,
        path_label: str,
        correlation_id: UUID,
        executed_at_utc: datetime,
    ) -> ExecutionReceipt:
        """Record one executed configuration, charging it only if it overran the grid.

        One call per CPCV path or walk-forward fold, made as the path completes rather
        than batched at the end. Batching lets a crashed or abandoned run launder its
        failed paths: run 28, keep the good ones, report the count of what you kept.

        `execution_index` and `charged` are assigned by the database and returned, never
        supplied. A caller that numbers its own executions can renumber them, and the
        index is what decides whether the grid was overrun.
        """
        _require_utc(executed_at_utc, "executed_at_utc")
        _require_digest(spec_hash, "spec_hash")
        _require_digest(config_hash, "config_hash")
        _require_text(path_label, "path_label")

        async with self._engine.begin() as connection:
            await connection.execute(_TAKE_LEDGER_LOCK)
            registration = await self._registration(connection, spec_hash)
            if not registration.is_registered:
                raise TrialLedgerError(
                    f"spec_hash {spec_hash} was never registered; an execution that "
                    f"skips the ledger is an undeclared search, and the best of sixty "
                    f"unregistered runs deflates as though it were the only one"
                )
            row = (
                await connection.execute(
                    _REPORT_EXECUTION,
                    {
                        "executed_at_utc": executed_at_utc,
                        "correlation_id": correlation_id,
                        "spec_hash": spec_hash,
                        "config_hash": config_hash,
                        "path_label": path_label,
                    },
                )
            ).first()
            if row is None:  # pragma: no cover - RETURNING always yields on success
                raise TrialLedgerError("trial_execution INSERT returned no execution index")
            context_hash = await self._context_hash_for(connection, spec_hash)
            context_trials = await self._context_trials(connection, context_hash)

        return ExecutionReceipt(
            seq=int(row[0]),
            spec_hash=spec_hash,
            execution_index=int(row[1]),
            charged=bool(row[2]),
            context_trial_count=context_trials,
        )

    async def registration_for(self, spec_hash: str) -> SpecRegistration:
        """What the engine checks before it dispatches a single event.

        Returns `trials_charged=0` for a specification the ledger has never seen, and
        the engine treats zero as a refusal. Zero is not "no deflation needed" -- it is
        "this search is undeclared", which is the one state a result may not come from.
        """
        _require_digest(spec_hash, "spec_hash")
        async with self._engine.connect() as connection:
            return await self._registration(connection, spec_hash)

    async def context_trial_count(self, search_context_hash: str) -> int:
        """The count that feeds `SR*`: every trial charged against this data."""
        _require_digest(search_context_hash, "search_context_hash")
        async with self._engine.connect() as connection:
            return await self._context_trials(connection, search_context_hash)

    async def lineage_trial_count(self, search_context_hash: str, lineage_id: str) -> int:
        """The count that feeds the family deflation term."""
        _require_digest(search_context_hash, "search_context_hash")
        _require_text(lineage_id, "lineage_id")
        async with self._engine.connect() as connection:
            return await self._lineage_trials(connection, search_context_hash, lineage_id)

    async def sharpe_evidence(
        self, performance: ObservedPerformance, *, search_context_hash: str
    ) -> SharpeEvidence:
        """A `SharpeEvidence` whose trial count came from the ledger rather than a caller.

        This is the join the whole module exists for. `SharpeEvidence` already refuses to
        be constructed without `trials_at_time_of_run`, so the remaining failure is a
        caller supplying a number it made up -- typically the count of its own study,
        which is the 200-against-50,000 undercount. Reading it here removes the
        opportunity.

        An empty context refuses rather than deflating against zero. A search whose
        context holds no charges has not been registered, and reporting its Sharpe
        against a benchmark of zero states that nothing was searched to find it.
        """
        trial_count = await self.context_trial_count(search_context_hash)
        if trial_count < 1:
            raise TrialLedgerError(
                f"search context {search_context_hash} holds no charged trials; refusing "
                f"to deflate against an empty ledger, because a benchmark of zero reports "
                f"a searched result as an unsearched one"
            )
        return SharpeEvidence(
            observed_sharpe=performance.observed_sharpe,
            trials_at_time_of_run=trial_count,
            independent_episode_count=performance.independent_episode_count,
            skewness=performance.skewness,
            kurtosis=performance.kurtosis,
            sharpe_variance_across_trials=performance.sharpe_variance_across_trials,
        )

    @staticmethod
    async def _registration(connection: AsyncConnection, spec_hash: str) -> SpecRegistration:
        charged = (
            await connection.execute(
                _REGISTRATION_FOR,
                {"spec_hash": spec_hash, "entry_kind": _ENTRY_KIND_REGISTRATION},
            )
        ).scalar_one_or_none()
        return SpecRegistration(spec_hash=spec_hash, trials_charged=int(charged or 0))

    @staticmethod
    async def _context_trials(connection: AsyncConnection, search_context_hash: str) -> int:
        charged = (
            await connection.execute(_CONTEXT_TRIALS, {"search_context_hash": search_context_hash})
        ).scalar_one_or_none()
        return int(charged or 0)

    @staticmethod
    async def _lineage_trials(
        connection: AsyncConnection, search_context_hash: str, lineage_id: str
    ) -> int:
        charged = (
            await connection.execute(
                _LINEAGE_TRIALS,
                {"search_context_hash": search_context_hash, "lineage_id": lineage_id},
            )
        ).scalar_one_or_none()
        return int(charged or 0)

    @staticmethod
    async def _context_hash_for(connection: AsyncConnection, spec_hash: str) -> str:
        found = (
            await connection.execute(
                sa.text(
                    """
                    SELECT encode(search_context_hash, 'hex')
                      FROM trial_ledger
                     WHERE spec_hash = decode(:spec_hash, 'hex')
                       AND entry_kind = :entry_kind
                    """
                ),
                {"spec_hash": spec_hash, "entry_kind": _ENTRY_KIND_REGISTRATION},
            )
        ).scalar_one_or_none()
        if found is None:  # pragma: no cover - the caller checked registration first
            raise TrialLedgerError(f"spec_hash {spec_hash} carries no search context")
        return str(found)
