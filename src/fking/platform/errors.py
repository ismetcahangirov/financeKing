"""The system-wide error taxonomy.

Nothing in `fking` raises an exception outside this tree, and the tree grows one member
at a time: a class arrives in the pull request that first raises it, with the failure it
names. A taxonomy written ahead of its raisers is a list of guesses about which failures
will matter, and the guesses that turn out wrong are never deleted -- they are caught,
speculatively, by handlers written against a class nothing raises.

Two names deliberately live elsewhere:

- `SafetyViolation` is defined in `fking.platform.safety` so the kernel needs no import
  from outside itself and sits inside its own 100% coverage boundary. It is re-exported
  here rather than redefined -- two classes sharing one name is how an `except` clause
  stops catching what its author believed it caught. It inherits `BaseException`, so it
  is *not* a member of `FkingError` and no handler for `FkingError` can absorb it.
- `DomainError` is defined in `fking.domain`, which imports nothing but the standard
  library and therefore cannot inherit from anything here. The two are related by
  documentation rather than by inheritance, and that is the price of the
  zero-dependency rule.
"""

from __future__ import annotations

from fking.platform.safety import SafetyViolation
from fking.platform.safety._archive_errors import ArchiveUnavailableError

__all__ = [
    "ArchiveUnavailableError",
    "DataIntegrityError",
    "DataUnavailableError",
    "FeatureContractError",
    "FkingError",
    "SafetyViolation",
    "SeamDisagreementError",
]


class FkingError(Exception):
    """Base for every error this system raises deliberately.

    Its purpose is not to be caught. It exists so that a handler which genuinely means
    "any failure this codebase raises on purpose" can say so without writing
    `except Exception`, which would also swallow a `KeyError` from a typo and a
    `TimeoutError` from a socket -- failures whose correct response is to stop.
    """


class DataIntegrityError(FkingError):
    """Ingested data failed a validation invariant, or its format is undeclared.

    Terminal, never retried. The distinction from a transient venue failure is that
    re-reading the same bytes produces the same answer: a timestamp that normalises to
    1970 does so on every attempt, and a `(market, dataset, date)` combination nobody
    has declared stays undeclared until somebody declares it.

    The response is to stop and write nothing partial. A partially ingested file is
    worse than none, because a short series parses cleanly and changes every statistic
    computed from it without changing anything that looks like an error.
    """


class FeatureContractError(FkingError):
    """A feature declaration or an as-of read violated the point-in-time contract.

    One class rather than a pair, because no caller can recover from either: a spec that
    declares no lookback and a lookup for a feature nobody registered are both
    programming errors that must be fixed before the process is worth running. The
    message names the field or the feature, which is the part a caller actually needs.

    Deliberately not a subclass of `DataIntegrityError`: that one means the *bytes* were
    wrong, and re-reading them will not help. This one means the *declaration* is wrong,
    and the data may be fine.
    """


class SeamDisagreementError(FkingError):
    """Two sources describe the same closed bar differently, and neither one wins.

    Raised where a REST backfill overlaps records the corpus already holds
    (`fking.data.backfill.seam`). Deliberately not a subclass of `DataIntegrityError`:
    that one says a source's bytes are malformed, and re-reading them will not help.
    This one says both sources are well-formed and they disagree, which is a fact about
    the venue rather than about a parser.

    It escalates rather than resolving, and that is the whole point. A closed bar is
    immutable, so a disagreement means one of the two views is wrong about a period that
    is already final -- and picking a winner by source preference records one view as
    fact while destroying the only evidence that they ever differed. Nothing is written
    on this path, including the records that did agree.
    """


class DataUnavailableError(FkingError):
    """A read asked for data this corpus does not hold, and will not be approximated.

    Distinct from `FeatureContractError`, which says the *declaration* is wrong, and from
    `DataIntegrityError`, which says the bytes are wrong. This one says the declaration
    and the bytes are both fine and the answer is simply absent -- which is the one of the
    three a caller can sometimes act on, by narrowing a window or by running a backfill.

    Its message is part of its contract: it names what the corpus *does* hold. A bare
    refusal sends a reader looking for a bug, and the most frequent caller is an
    LLM-authored strategy asking for a feature that exists in the literature it was
    trained on rather than in this corpus (`DATA_PIPELINE.md` section 8).
    """


# `ArchiveUnavailableError` is the third name defined elsewhere, and for the same reason as
# `SafetyViolation`: it is raised by `fking.platform.safety.archive`, which must stay
# importable without pulling this module in -- this module imports the trading kernel,
# and the archive egress path exists precisely so that it does not have to. It is
# re-exported rather than redefined, because two classes with one name is how an
# `except` clause stops catching what its author believed it caught.
#
# It is imported from `fking.platform.safety._archive_errors` rather than from
# `archive.py`, and that indirection is load-bearing. `fking.execution` imports this
# module for `FkingError`; importing the archive *client's* module here would put every
# taxonomy importer one edge from the archive egress path and break the "The order path
# cannot reach the archive egress client" contract through a chain neither file shows.
