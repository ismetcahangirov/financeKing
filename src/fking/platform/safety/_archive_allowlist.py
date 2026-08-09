"""The archive allowlist. A **second** compiled-in literal, never an extension of the first.

`PERMITTED_HOSTS` in `_allowlist.py` is not a permission list. It is a proof about which
hosts a process holding order-placement code can reach *at all*. Adding
`data.binance.vision` to it would make the archive host reachable by the OMS, by the
venue adapter, and by every future refactor of a shared `_request()` helper -- and read
paths become write paths during refactors (CLAUDE.md section 11). The archive is a data
host, and the trading allowlist is not widened to reach it
(docs/rules/exchange-integration.md, ARCHITECTURE.md section 8).

So the two sets are disjoint, they live in separate modules, and the clients that carry
them cannot see each other. Two clients that cannot reach each other's hosts is the
property being bought; one client with a longer list is what is being refused.

Both files are covered by the `safety-kernel-diff` CI job, so a change here is a change
to the safety kernel and requires a pull request labelled `safety:critical`. The
argument for refusing an addition is the same as in `_allowlist.py`: not "read-only",
not "just to compare", not behind a flag.

See docs/rules/safety-kernel.md and issue #22.
"""

from __future__ import annotations

from typing import Final

# Source: DATA_PIPELINE.md section 2 and VF-014. One host, unauthenticated, serving the
# public spot and USDⓈ-M futures archives together with a `.zip.CHECKSUM` sibling for
# every file.
#
# There is no `api.binance.com` here and there never will be: this path holds no
# credentials and cannot sign a request, so an endpoint requiring one is not merely
# disallowed, it is unreachable in practice as well as by rule.
ARCHIVE_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "data.binance.vision",  # public bulk archive, no auth (VF-013, VF-014)
    }
)
