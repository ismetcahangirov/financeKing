"""`python -m fking.backtest.feed <config.toml>` -- what `make backtest` runs.

Two tokens of glue. Everything it does is in `fking.backtest.feed._cli.main`, which takes
its argument vector as a parameter so the command is testable without a subprocess -- and
is separately exercised through one, because an entrypoint that has only ever been called
in-process is an entrypoint whose `python -m` spelling nobody has run.
"""

from __future__ import annotations

import sys

from fking.backtest.feed._cli import main

# Re-exported explicitly so that the identity of the function `python -m` reaches is
# assertable from a test. Without it the name is a private import and a `__main__` that
# quietly grew a second code path would be invisible to the suite.
__all__ = ["main"]

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
