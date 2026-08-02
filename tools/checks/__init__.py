"""AST checks for rules no off-the-shelf linter knows about.

Each module here enforces one rule from .claude/rules/ that ruff and mypy cannot
express, exposes a `check_source` usable from a test, and returns 1 from `main` on
any failure. A check that cannot fail proves nothing, so each has a test asserting
it catches a known violation as well as one asserting it passes clean code.
"""

__all__: tuple[str, ...] = ()
