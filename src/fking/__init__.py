"""financeKing -- an autonomous trading platform that trades only on demo accounts.

The package is a modular monolith. Dependencies point inward toward `domain`, and
that direction is enforced by import-linter contracts in pyproject.toml rather than
by convention, because conventions do not survive a hurried change at 2am.

`platform` sits outside the layering and may be imported by anyone: it holds
mechanism, not policy.
"""

__all__: tuple[str, ...] = ()
