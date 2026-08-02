"""The configuration failure type and the exit code that reports it."""

from __future__ import annotations

from typing import Final

# EX_CONFIG from sysexits.h. A supervisor can distinguish a configuration error from a
# crash and decline to restart-loop on it -- restarting a process whose configuration
# is wrong produces the same failure every few seconds until someone reads the log.
EX_CONFIG: Final[int] = 78


class ConfigError(Exception):
    """Configuration is invalid, missing, or describes something the host cannot honour.

    Raised at startup only. Nothing in this system reads configuration at call time, so
    there is no runtime path that can produce one of these after boot.
    """
