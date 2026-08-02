"""`python -m fking.platform.safety --print-allowlist`, documented in CONTRIBUTING.md.

Exists so an operator can read the compiled-in allowlist out of a running checkout
without opening a Python REPL or trusting a document. The answer comes from the same
constant the request path uses, so it cannot drift from what the process will actually
permit -- which is the whole reason not to answer this question from documentation.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from fking.platform.safety._allowlist import PERMITTED_HOSTS


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m fking.platform.safety")
    parser.add_argument(
        "--print-allowlist",
        action="store_true",
        required=True,
        help="print every host this process is permitted to contact",
    )
    parser.parse_args(argv)

    for host in sorted(PERMITTED_HOSTS):
        print(host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
