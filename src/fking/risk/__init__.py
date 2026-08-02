"""Sizing, exposure limits, netting and the kill switch. Knows about signals,
exposure and capital.

This module holds sole authority to construct an `Order`. Everything upstream
proposes; this decides. It is pure for the same reason `strategy` is -- a risk
decision that depends on the wall clock cannot be replayed, and a risk decision that
cannot be replayed cannot be audited.
"""

__all__: tuple[str, ...] = ()
