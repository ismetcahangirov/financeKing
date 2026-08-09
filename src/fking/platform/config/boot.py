"""The ordered startup sequence, and the two derived views of a `Settings` object.

```
1.  Construct Settings  (pydantic field + model validators)      load_settings()
2.  Resolve every configured venue endpoint                      bootstrap()
3.  Validate every host against the compiled-in allowlist        bootstrap()
4.  Assert every risk limit is within its compiled-in ceiling    (step 1; see below)
5.  Verify secret file permissions                               bootstrap()
6.  Log the allowlist and every resolved endpoint                bootstrap()
7.  Log the full effective config, redacted                      bootstrap()
8.  Construct components, injecting settings                     caller
9.  Start                                                        caller
```

Step 4 is enforced by a model validator on `RiskSettings`, so it happens during step 1
rather than after it. That is stronger than the sequence above implies: a `Settings`
object holding an out-of-bounds limit cannot be constructed at all, so no code path --
including a test fixture -- can obtain one.

Step 3 delegates to `fking.platform.safety`, which raises `SafetyViolation`. That is a
`BaseException` and is never caught, here or anywhere, so a non-allowlisted endpoint
terminates the process rather than exiting cleanly with `EX_CONFIG`. The kernel
outranks the exit-code convention: a process whose model of what it is talking to is
wrong should die, not report tidily.

CONFIGURATION.md sections 3 and 4.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Final, cast

from pydantic import ValidationError

from fking.platform.config._errors import ConfigError
from fking.platform.config.settings import Settings
from fking.platform.logging import get_logger
from fking.platform.safety import PERMITTED_HOSTS, verify_endpoints_or_abort

_LOG: Final = get_logger(__name__)

DEFAULT_ENV_FILE: Final[Path] = Path(".env")

# Mode bits that must not be set on a private key file: any permission for group or
# other. SECURITY.md section 4.5.
_TOO_PERMISSIVE: Final[int] = stat.S_IRWXG | stat.S_IRWXO

type JsonValue = str | int | float | bool | list["JsonValue"] | dict[str, "JsonValue"] | None


def load_settings(*, env_file: Path | None = DEFAULT_ENV_FILE) -> Settings:
    """Construct the settings tree. Raises `ConfigError` on anything invalid.

    `env_file=None` means defaults and environment only, which is what tests and a
    bare container start use. A missing file at the given path is not an error -- the
    documented baseline is that a process with no `.env` runs.
    """
    try:
        # `_env_file` is pydantic-settings' documented per-call override. mypy cannot
        # see it: pydantic's dataclass_transform synthesises an __init__ from the
        # declared fields for every subclass, which shadows the one BaseSettings
        # declares. Unavoidable short of reimplementing dotenv parsing here.
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as invalid:
        # The pydantic rendering already names every offending field and its location
        # in the tree, which is the whole requirement. Re-wrapping to one project type
        # means callers do not have to know that the tree is built with pydantic.
        raise ConfigError(f"invalid configuration:\n{invalid}") from invalid


def effective_config(settings: Settings) -> dict[str, JsonValue]:
    """The full tree as JSON-safe data, with every credential already redacted.

    Redaction is by type, not by field name: every credential is `SecretStr`, whose
    JSON serialisation is `**********`. There is no list of field names to keep in
    sync, so a credential added tomorrow is redacted tomorrow rather than on the day
    somebody remembers it.
    """
    return cast("dict[str, JsonValue]", settings.model_dump(mode="json"))


def config_hash(settings: Settings) -> str:
    """A stable digest over the redacted dump.

    It changes when configuration changes and is identical across restarts with
    identical configuration. Carrying it on every backtest result and audit row is what
    ties a historical decision to the exact configuration that produced it.
    """
    canonical = json.dumps(effective_config(settings), sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def venue_endpoints(settings: Settings) -> tuple[str, ...]:
    """Every venue URL this process may contact.

    The archive host and the LLM provider base URLs are deliberately absent. They are
    not venues, they are not in the trading allowlist, and adding them so that one
    download or one completion works would widen the allowlist permanently -- which is
    the exact move `docs/rules/safety-kernel.md` refuses. Those paths get their own
    egress checks in #22 and in the agent gateway.
    """
    binance = settings.exchange.binance
    endpoints = [
        str(binance.spot_rest_url),
        str(binance.spot_ws_url),
        str(binance.futures_rest_url),
        str(binance.futures_ws_url),
    ]
    bybit = settings.exchange.bybit
    if bybit is not None:
        endpoints.extend([str(bybit.rest_url), str(bybit.ws_url)])
    return tuple(endpoints)


def _verify_key_file(key_path: Path) -> None:
    if not key_path.is_file():
        raise ConfigError(
            f"spot_ed25519_key_path={key_path} is not a readable file; the spot "
            f"session.logon handshake cannot be performed without it"
        )
    if os.name != "posix":
        # st_mode on Windows reports 0o666 or 0o444 for every ordinary file regardless
        # of its ACL, so a mode check here would either always pass or always fail --
        # in both cases answering a question the filesystem is not being asked.
        _LOG.warning(
            "secret_file_mode_unchecked",
            # The sanctioned startup value: nothing has happened yet from which a real
            # correlation id could be derived. docs/rules/logging-rules.md, the one
            # exception.
            correlation_id="boot",
            key_path=str(key_path),
            reason="file modes are not meaningful on this platform",
        )
        return
    mode = stat.S_IMODE(key_path.stat().st_mode)
    if mode & _TOO_PERMISSIVE:
        raise ConfigError(
            f"{key_path} has mode {mode:04o}; an Ed25519 private key must be no wider "
            f"than 0600. A key readable by other local accounts is a key that must be "
            f"rotated, and starting anyway would make this check a log line nobody reads"
        )


def bootstrap(settings: Settings) -> Settings:
    """Run startup steps 2 through 7 and return the settings unchanged.

    Returns rather than mutates, so the caller's object is the one that was verified.
    """
    endpoints = venue_endpoints(settings)
    verify_endpoints_or_abort(endpoints)

    key_path = settings.exchange.binance.spot_ed25519_key_path
    if key_path is not None:
        _verify_key_file(key_path)

    _LOG.info(
        "effective_config",
        correlation_id="boot",
        config_hash=config_hash(settings),
        git_sha=settings.telemetry.git_sha,
        allowed_hosts=sorted(PERMITTED_HOSTS),
        venue_endpoints=list(endpoints),
        config=effective_config(settings),
    )
    return settings
