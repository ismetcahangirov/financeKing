"""What a feature must declare before it is allowed to exist.

Every field here is required and none has a default. That is the mechanism, not a style
choice: a default is a value somebody forgets to override, and the values they would
forget are `lookback=0` and `availability_lag=0`, both of which are the permissive
answer.

Three of the fields are load-bearing in ways that are not obvious from their names.

**`lookback` is not documentation.** `BACKTEST_ENGINE.md` section 6 derives the
walk-forward embargo floor from `max_feature_lookback + max_holding_horizon`, so an
understated lookback shortens the embargo, lets adjacent folds share information, and
makes cross-validation report a stable edge that is partly the same data seen twice.
Nothing about it looks wrong, which is what makes it the quietest way to break the
validation machinery. It is also the window `compute` actually uses -- the spec hands it
to the function rather than the function carrying its own constant -- so the declared
number and the computed number cannot drift apart.

**`availability_lag` is the distance between `event_time_utc` and `available_at_utc`.**
It is how long after the fact this system could first have known the value. Zero is a
legal declaration for a value derived only from a bar that has closed, and it is a lie
for anything a venue publishes after the instant it stamps.

**`point_in_time_proof` is a required schema field.** If the proof cannot be stated in
one sentence, the feature is probably leaking (`DATA_PIPELINE.md` section 7). Requiring
it converts an unverifiable intention into a claim a reviewer can check against the
code.

`version` is part of the feature's identity everywhere, including in the primary key of
`feature_values`. A definition change is a new version; values computed under the old
one remain and stay attributable to the definition that produced them. The digest in
`_definition_digests` is what makes "changed the definition without bumping the version"
a failed test rather than a thing somebody notices later.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from fking.data.format_resolver import Market
from fking.platform.errors import FeatureContractError

__all__ = [
    "FeatureCompute",
    "FeatureObservation",
    "FeaturePoint",
    "FeatureRef",
    "FeatureSpec",
    "FeatureWindow",
    "SettlementRateCompute",
    "SettlementRateObservation",
    "definition_digest",
]

# A proof shorter than this is not a sentence. The bound is deliberately low: the check
# is against an empty or placeholder string, not an attempt to grade prose.
_MINIMUM_PROOF_CHARACTERS: Final[int] = 20


@dataclass(frozen=True, slots=True)
class FeatureObservation:
    """One closed bar, carrying the two prices that mean different things.

    `event_time_utc` is the instant the bar's value became true, which for a closed bar is
    its close time and never its open time. A feature computed from an *open* bar's close
    is the single most common look-ahead defect there is, and the way to make it
    unrepresentable is to give this type no field that could carry one.

    `close_quote_price` is what features are computed from. `open_quote_price` is what a
    decision taken at the *previous* bar's close could actually have transacted at, and it
    exists so that label alignment is checkable rather than described: a label measured
    from the decision bar's own close inflates the measured edge by exactly the move the
    feature was computed from, which reliably produces a strategy that looks profitable
    and is not (`fking.data.features.labels`).
    """

    event_time_utc: datetime
    open_quote_price: Decimal
    close_quote_price: Decimal


@dataclass(frozen=True, slots=True)
class FeaturePoint:
    """One computed value, carrying both times that matter.

    `available_at_utc` is what the store filters on. It is derived by `compute` from the
    declared `availability_lag` rather than supplied by a caller, so a writer cannot
    quietly claim a value was knowable earlier than the declaration says.
    """

    event_time_utc: datetime
    available_at_utc: datetime
    feature_value: Decimal


@dataclass(frozen=True, slots=True)
class FeatureWindow:
    """The two durations `compute` is given, taken from the spec that declared them.

    Passed in rather than read off a module constant inside the function. A window
    baked into the computation can differ from the window the spec declares, and the
    difference is invisible to every test that only exercises the function.
    """

    lookback: timedelta
    availability_lag: timedelta


@dataclass(frozen=True, slots=True)
class SettlementRateObservation:
    """One funding settlement a perpetual venue has already published.

    A second observation kind rather than three more fields on `FeatureObservation`,
    because the two series do not share a clock and merging them would hide that. Closed
    bars arrive on a regular grid and are known at the instant they close; a funding
    settlement arrives every eight hours on Binance perpetuals and is *stamped* at the
    settlement instant while being readable only afterwards, which is why every spec built
    over this type carries a positive `availability_lag`. A single type carrying both would
    have to declare one lag for two publication behaviours, and the permissive answer --
    zero -- is the one a reader would assume.

    `settlement_rate` is signed and is the fraction of notional exchanged at one
    settlement, not an annualised rate and not a percentage. Positive means longs pay
    shorts. It is a fraction rather than a price, so it is not quantised to any tick, and
    it is still a `Decimal`: it multiplies a notional, and a float here reintroduces the
    accumulation error that `docs/rules/decimal-and-money.md` exists to stop.
    """

    event_time_utc: datetime
    settlement_rate: Decimal


FeatureCompute = Callable[[Sequence[FeatureObservation], FeatureWindow], tuple[FeaturePoint, ...]]
SettlementRateCompute = Callable[
    [Sequence[SettlementRateObservation], FeatureWindow], tuple[FeaturePoint, ...]
]


@dataclass(frozen=True, slots=True)
class FeatureRef:
    """A series of one feature: which definition, on which instrument.

    `version` travels with the name everywhere. A reference that carried only the name
    would resolve to "whatever definition is current", which is exactly the read that
    makes a historical result irreproducible.
    """

    name: str
    version: int
    market: Market
    symbol: str


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureSpec:
    """A registrable feature declaration.

    Keyword-only and fully required: omitting a field is a `TypeError` that names it,
    which is a better failure than a default that silently declares no lag.

    The two computation fields are the exception, and they default to `None` for the
    opposite reason to the rule above rather than in spite of it. Exactly one must be
    supplied, and `__post_init__` refuses both zero and two by name -- so the failure for
    the mistake this pair actually invites ("I set the wrong one", "I set neither") states
    which kinds exist and which this feature consumes, where a `TypeError` about a missing
    positional would only name the field the author already knew about.
    """

    name: str
    version: int
    compute: FeatureCompute | None = None
    settlement_rate_compute: SettlementRateCompute | None = None
    inputs: frozenset[str]
    lookback: timedelta
    availability_lag: timedelta
    label_horizon: timedelta
    point_in_time_proof: str
    uses_trailing_statistics_only: bool

    def __post_init__(self) -> None:
        _require_text(self.name, "name")
        self._require_exactly_one_computation()
        if self.version < 1:
            raise FeatureContractError(f"version must be at least 1; got {self.version}")
        _require_positive_duration(self.lookback, "lookback")
        _require_non_negative_duration(self.availability_lag, "availability_lag")
        _require_positive_duration(self.label_horizon, "label_horizon")
        if not self.inputs:
            raise FeatureContractError(
                f"{self.name} declares no inputs; a feature computed from nothing cannot "
                f"be checked against the availability contract"
            )
        proof = _require_text(self.point_in_time_proof, "point_in_time_proof")
        if len(proof) < _MINIMUM_PROOF_CHARACTERS:
            raise FeatureContractError(
                f"point_in_time_proof for {self.name} is {len(proof)} characters and states "
                f"nothing checkable; say which window is used and how the join is bounded"
            )
        if not self.uses_trailing_statistics_only:
            # There is no supported way to register one. A full-sample statistic is the
            # leak that most often survives review -- the slice is bounded by `t` and
            # looks point-in-time while every row inside it sees every other row -- so
            # the flag exists to be asserted, not to be chosen.
            raise FeatureContractError(
                f"{self.name} declares uses_trailing_statistics_only=False; a full-sample "
                f"statistic cannot be point-in-time (docs/rules/no-lookahead.md)"
            )

    def _require_exactly_one_computation(self) -> None:
        supplied = [
            field_name
            for field_name, candidate in (
                ("compute", self.compute),
                ("settlement_rate_compute", self.settlement_rate_compute),
            )
            if candidate is not None
        ]
        if len(supplied) == 1:
            return
        if not supplied:
            raise FeatureContractError(
                f"{self.name} declares neither compute nor settlement_rate_compute; a "
                f"feature is a computation over one observation kind, and which kind it "
                f"reads is what the availability contract checks its inputs against"
            )
        raise FeatureContractError(
            f"{self.name} declares both compute and settlement_rate_compute; a feature is "
            f"one series with one lookback and one lag, so two computations under one name "
            f"means two features whose values would share a primary key"
        )

    @property
    def computation(self) -> FeatureCompute | SettlementRateCompute:
        """Whichever computation was declared, for callers that only need to digest it."""
        if self.compute is not None:
            return self.compute
        if self.settlement_rate_compute is not None:
            return self.settlement_rate_compute
        raise FeatureContractError(
            f"{self.name} has no computation; __post_init__ refuses that, so reaching here "
            f"means the instance was assembled without it"
        )

    def window(self) -> FeatureWindow:
        """The durations handed to `compute`, so the declaration is the computation."""
        return FeatureWindow(lookback=self.lookback, availability_lag=self.availability_lag)

    def key(self) -> tuple[str, int]:
        """The registry key. Name alone is not an identity -- versions coexist."""
        return (self.name, self.version)


def definition_digest(compute: FeatureCompute | SettlementRateCompute) -> str:
    """A digest of what `compute` *does*, over its parsed syntax rather than its text.

    Parsed rather than hashed as source, so reformatting, re-indenting or adding a
    comment does not read as a definition change -- a digest that moves on formatting is
    a digest people learn to update without looking at what moved.

    The function's *name* is excluded, along with its decorators: a rename is a change
    to how the definition is referred to, not to what it computes, and a digest that
    moved on one would train the same reflex. Its signature and body are included, and
    so is its docstring -- the sentence a feature states about its own semantics is part
    of the definition it is claiming.
    """
    source = textwrap.dedent(inspect.getsource(compute))
    parsed = ast.parse(source).body[0]
    if not isinstance(parsed, ast.FunctionDef | ast.AsyncFunctionDef):
        raise FeatureContractError(
            f"compute must be a plain function to be digestible; {compute!r} parses as "
            f"{type(parsed).__name__}. A lambda, a partial or a callable object has no "
            f"source a reviewer can diff against a version number"
        )
    material = "".join(
        ast.dump(node, annotate_fields=True, include_attributes=False)
        for node in (parsed.args, *parsed.body)
    )
    return hashlib.blake2b(material.encode("utf-8"), digest_size=16).hexdigest()


def _require_text(candidate: str, field_name: str) -> str:
    if not candidate.strip():
        raise FeatureContractError(f"{field_name} must not be blank")
    return candidate


def _require_positive_duration(candidate: timedelta, field_name: str) -> timedelta:
    if candidate <= timedelta(0):
        raise FeatureContractError(f"{field_name} must be positive; got {candidate}")
    return candidate


def _require_non_negative_duration(candidate: timedelta, field_name: str) -> timedelta:
    if candidate < timedelta(0):
        raise FeatureContractError(
            f"{field_name} must not be negative; got {candidate}. A negative lag claims "
            f"the value was knowable before it happened"
        )
    return candidate
