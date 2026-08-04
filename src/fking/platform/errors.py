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
from fking.platform.safety.archive import ArchiveUnavailableError

__all__ = [
    "ArchiveUnavailableError",
    "DataIntegrityError",
    "FeatureContractError",
    "FkingError",
    "SafetyViolation",
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


# `ArchiveUnavailableError` is the third name defined elsewhere, and for the same reason as
# `SafetyViolation`: it is raised by `fking.platform.safety.archive`, which must stay
# importable without pulling this module in -- this module imports the trading kernel,
# and the archive egress path exists precisely so that it does not have to. It is
# re-exported rather than redefined, because two classes with one name is how an
# `except` clause stops catching what its author believed it caught.
