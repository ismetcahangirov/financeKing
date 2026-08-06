"""The archive egress path's own failure, in a module that holds no client.

It lives apart from `archive.py` for one reason, and the reason is an `import-linter`
contract rather than tidiness. `fking.platform.errors` re-exports this name so the
taxonomy is complete from a reader's side; if it imported `archive.py` to get it, then
every module importing the taxonomy would sit one import edge from the archive *client* --
and `fking.execution` imports the taxonomy, which would break "The order path cannot
reach the archive egress client" through a chain nobody intended and nobody could see
from either file.

Splitting the exception out costs one module and buys the contract back at full strength,
including its refusal of indirect chains. That refusal is the part worth keeping: read
paths become write paths during refactors, and a contract that only catches the direct
import catches the version of the mistake nobody makes.
"""

from __future__ import annotations

__all__ = ["ArchiveUnavailableError"]


class ArchiveUnavailableError(Exception):
    """The archive host answered, but not with the file.

    A distinct condition from a checksum mismatch and from a transport failure: a 404
    usually means the symbol did not exist on that date, or that a monthly archive has
    not been published yet, and both are ordinary answers a backfill must be able to act
    on rather than crash under.

    Not a member of `FkingError`: this module must stay importable without pulling in
    `fking.platform.errors`, which imports the trading kernel. `fking.platform.errors`
    re-exports the name instead, so the taxonomy is complete from a reader's side
    without an import edge in the direction that matters.
    """
