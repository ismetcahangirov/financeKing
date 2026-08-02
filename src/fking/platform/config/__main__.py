"""`python -m fking.platform.config` -- validate configuration and print it, redacted.

The operator-facing half of the boot sequence. Running it against a candidate `.env`
answers "would the system start, and what would it be configured to do" without
starting anything, which is the check people otherwise perform by starting the system.

Exits `EX_CONFIG` (78) on invalid configuration, so a supervisor or a deploy script can
tell a configuration error from a crash and decline to retry it.
"""

from __future__ import annotations

import json
import sys

from fking.platform.config._errors import EX_CONFIG, ConfigError
from fking.platform.config.boot import bootstrap, config_hash, effective_config, load_settings


def main() -> int:
    try:
        settings = bootstrap(load_settings())
    except ConfigError as invalid:
        print(f"configuration error: {invalid}", file=sys.stderr)
        return EX_CONFIG

    # Printed as well as logged: the log record goes wherever the logging pipeline
    # points, and an operator running this command wants the answer on stdout.
    print(
        json.dumps(
            {
                "event": "effective_config",
                "config_hash": config_hash(settings),
                "config": effective_config(settings),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
