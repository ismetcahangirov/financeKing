"""The single error type this package raises.

One type rather than a tree because every failure here is the same failure: a value
that cannot legally be a domain object was offered as one. Callers do not branch on
which invariant broke -- they cannot recover from any of them -- so a taxonomy would
buy nothing and cost an exception class per rule.

The wider system taxonomy in `fking.platform.errors` carries `DomainError` as a
member. `domain` cannot import it (this package imports nothing but the standard
library), so the two are related by name and by documentation rather than by
inheritance, and that is the price of the zero-dependency rule.
"""

from __future__ import annotations


class DomainError(Exception):
    """A domain invariant was violated.

    Raised at construction, never later. A `Bar` whose low exceeds its high does not
    exist as an object that some downstream check might catch -- constructing it
    fails.
    """
