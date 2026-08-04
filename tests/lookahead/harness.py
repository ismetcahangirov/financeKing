"""Poison the future, digest the past, and require the past not to have moved.

Three pieces, each with an obvious wrong version.

**`poison_after` is gross, not subtle.** Closes are tripled and then alternately divided,
which triples the magnitude of every return and inverts the sign of every one of them. A
small perturbation can be absorbed by rounding and produce a **false pass**, which is the
worst possible outcome for the one test guarding the most dangerous defect class in the
project. The poison is deterministic -- no seed, no randomness -- so a failure reproduces
exactly from the parameter id in the CI log.

**`canonical_digest` means byte-identical.** `Decimal` values are serialised in their
exact positional form, so `0.10` and `0.1` digest differently and a `1e-15` difference
fails. That sensitivity is asserted directly in `test_probe.py` rather than assumed: a
comparison that cannot see a small difference is a comparison that will pass on the leak
that only moves the fifteenth digit today and the third digit on a different fold.

**The probe requires the comparison to have matched something.** A poisoned replay that
produces no points at or before the cut compares two empty tuples, which are equal, and
verifies nothing. Every clause below asserts non-emptiness before it asserts equality.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Protocol

from fking.data.features.labels import LabelPoint
from fking.data.features.registry import evaluate
from fking.data.features.spec import FeatureObservation, FeaturePoint, FeatureSpec

__all__ = [
    "LabelCompute",
    "bars",
    "canonical_digest",
    "label_digest",
    "poison_after",
    "poison_one_close",
    "probe_feature",
    "probe_label",
]


class LabelCompute(Protocol):
    """The shape both the correct label and every deliberately broken one satisfy.

    Two concrete implementations exist before this interface does -- `forward_return_label`
    and the misaligned one in `leaky.py` -- which is the bar an abstraction has to clear
    (`CLAUDE.md` section 3).
    """

    def __call__(
        self, observations: Sequence[FeatureObservation], *, horizon: timedelta
    ) -> tuple[LabelPoint, ...]:
        """Realised outcomes for every decision that has one."""


# Tripling and then thirding in alternation: every return's magnitude is multiplied by
# nine and its sign is flipped. Nothing about the poisoned tail resembles the series it
# replaced, which is the property that makes a false pass implausible rather than merely
# unlikely.
_POISON_FACTOR: Final[Decimal] = Decimal("3")

_START: Final[datetime] = datetime.fromisoformat("2026-03-01T12:00:00+00:00")
_STEP: Final[timedelta] = timedelta(minutes=15)


def bars(closes: Sequence[str], *, step: timedelta = _STEP) -> tuple[FeatureObservation, ...]:
    """A regular series of closed bars.

    The open of each bar is the previous bar's close, which is what a continuously traded
    market produces and, more usefully here, means a label measured from `open[i+1]` and
    one measured from `close[i]` would agree -- unless the poison moves `close[i]`, which
    is exactly what `poison_one_close` does. The two definitions are made to differ only
    at the point where the leak lives.
    """
    return tuple(
        FeatureObservation(
            event_time_utc=_START + step * index,
            open_quote_price=Decimal(closes[index - 1] if index else closes[0]),
            close_quote_price=Decimal(close),
        )
        for index, close in enumerate(closes)
    )


def poison_after(
    observations: Sequence[FeatureObservation], *, cut: datetime
) -> tuple[FeatureObservation, ...]:
    """Replace everything strictly after `cut` with something unrecognisable."""
    poisoned: list[FeatureObservation] = []
    for index, observation in enumerate(observations):
        if observation.event_time_utc <= cut:
            poisoned.append(observation)
            continue
        factor = _POISON_FACTOR if index % 2 == 0 else 1 / _POISON_FACTOR
        poisoned.append(
            FeatureObservation(
                event_time_utc=observation.event_time_utc,
                open_quote_price=observation.open_quote_price * factor,
                close_quote_price=observation.close_quote_price * factor,
            )
        )
    return tuple(poisoned)


def poison_one_close(
    observations: Sequence[FeatureObservation], *, at: datetime
) -> tuple[FeatureObservation, ...]:
    """Move the close of exactly one bar, leaving every other price untouched.

    The label probe's instrument. A decision taken on that close could not have transacted
    at it, so a correctly aligned label for that bar does not move; a label that used the
    bar's own close as its entry moves by the full perturbation.
    """
    return tuple(
        (
            FeatureObservation(
                event_time_utc=observation.event_time_utc,
                open_quote_price=observation.open_quote_price,
                close_quote_price=observation.close_quote_price * _POISON_FACTOR,
            )
            if observation.event_time_utc == at
            else observation
        )
        for observation in observations
    )


def canonical_digest(points: Sequence[FeaturePoint]) -> str:
    """A digest over the exact decimal text of every field, in series order.

    `format(value, "f")` rather than `str(value)`: it never falls back to scientific
    notation, and it preserves trailing zeros, so a value that changed from `0.1` to
    `0.10` -- a rescaling somewhere upstream -- is a different digest rather than the same
    one.
    """
    material = "\n".join(
        "|".join(
            (
                point.event_time_utc.isoformat(),
                point.available_at_utc.isoformat(),
                format(point.feature_value, "f"),
            )
        )
        for point in points
    )
    return hashlib.blake2b(material.encode("utf-8"), digest_size=32).hexdigest()


def label_digest(points: Sequence[LabelPoint]) -> str:
    """The same thing for labels, carrying the entry instant the alignment turns on."""
    material = "\n".join(
        "|".join(
            (
                point.decision_time_utc.isoformat(),
                point.entry_time_utc.isoformat(),
                point.exit_time_utc.isoformat(),
                format(point.return_fraction, "f"),
            )
        )
        for point in points
    )
    return hashlib.blake2b(material.encode("utf-8"), digest_size=32).hexdigest()


def probe_feature(spec: FeatureSpec, observations: Sequence[FeatureObservation]) -> None:
    """Two clauses. Raises `AssertionError` on a leak; returns `None` otherwise.

    1. **Nothing at or before the cut moves when everything after it is replaced.** This
       catches a full-sample statistic, a centred or right-labelled window, and any join
       that reached forward.
    2. **Every point's `available_at_utc` is its `event_time_utc` plus the declared lag.**
       This catches the other family entirely: a value that did not read the future but
       claims to have been knowable before the venue published it. The store filters on
       `available_at_utc`, so a value that understates it is visible to a decision that
       could not have seen it -- and no amount of future-poisoning would reveal that.
    """
    cut = observations[len(observations) // 2].event_time_utc

    baseline = evaluate(spec, observations)
    poisoned = evaluate(spec, poison_after(observations, cut=cut))

    before = tuple(point for point in baseline if point.event_time_utc <= cut)
    after = tuple(point for point in poisoned if point.event_time_utc <= cut)

    assert before, (
        f"{spec.name} v{spec.version} produced no points at or before {cut.isoformat()}, "
        f"so the comparison verified nothing"
    )
    assert canonical_digest(before) == canonical_digest(after), (
        f"{spec.name} v{spec.version} changed a value at or before {cut.isoformat()} when "
        f"the data after it changed; it read the future"
    )

    assert baseline, f"{spec.name} v{spec.version} produced nothing to check the lag against"
    for point in baseline:
        assert point.available_at_utc == point.event_time_utc + spec.availability_lag, (
            f"{spec.name} v{spec.version} stamped a point at "
            f"{point.event_time_utc.isoformat()} as available at "
            f"{point.available_at_utc.isoformat()}, which is not its event time plus the "
            f"declared lag of {spec.availability_lag}"
        )


def probe_label(
    label: LabelCompute,
    observations: Sequence[FeatureObservation],
    *,
    horizon: timedelta,
) -> None:
    """The label at bar *i* must not move when bar *i*'s own close moves.

    That is the whole alignment rule, stated as something a machine can check. A label
    entered at the open of bar *i+1* cannot depend on `close[i]`; a label entered at
    `close[i]` moves by the full perturbation.
    """
    target = observations[len(observations) // 2].event_time_utc

    baseline = label(observations, horizon=horizon)
    perturbed = label(poison_one_close(observations, at=target), horizon=horizon)

    before = tuple(point for point in baseline if point.decision_time_utc == target)
    after = tuple(point for point in perturbed if point.decision_time_utc == target)

    assert before, f"no label was produced at {target.isoformat()}, so nothing was verified"
    assert label_digest(before) == label_digest(after), (
        f"the label at {target.isoformat()} moved when that bar's own close moved; it was "
        f"measured from a price the decision could not have transacted at"
    )
