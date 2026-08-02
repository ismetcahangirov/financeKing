"""The allowlist. This file is the safety kernel.

Any change here requires a pull request labelled `safety:critical` and is blocked in
CI otherwise. Do not add a host because it would make testing easier. Do not add a
host "read-only" -- read paths become write paths during refactors, and the allowlist
does not distinguish intent because intent is not a property of a socket.

It lives alone in its own module so that the diff which changes it cannot be buried
among unrelated edits.

See .claude/rules/safety-kernel.md and CLAUDE.md section 0.
"""

from __future__ import annotations

from typing import Final

# Source: .claude/contexts/binance-testnet.md, the researched endpoint table.
#
# `stream.binancefuture.com`, NOT `fstream.binancefuture.com`. The `fstream.` prefix
# belongs to Binance *production* (fstream.binance.com); issue #11's body carried it
# by analogy, and importing a production naming pattern into a testnet allowlist is
# the exact class of mistake this module exists to prevent. Decided 2026-08-02 on #11.
PERMITTED_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "testnet.binance.vision",  # spot testnet REST
        "stream.testnet.binance.vision",  # spot testnet market-data WS
        "ws-api.testnet.binance.vision",  # spot testnet WS API: session.logon, userDataStream
        "testnet.binancefuture.com",  # USD-M futures testnet REST
        "stream.binancefuture.com",  # USD-M futures testnet WS
        "api-testnet.bybit.com",  # fallback venue REST, ARCHITECTURE.md section 7
        "stream-testnet.bybit.com",  # fallback venue WS
    }
)
