"""A secret must stop before the logger. SECURITY.md section 4.6.

Redaction here is by *type*, not by field name: every credential is `SecretStr`, whose
serialisation yields `**********`. That is why these tests search for the sentinel
across a whole rendering rather than checking a list of known field names -- a list is
a thing that falls behind the model, and the day it falls behind is the day a new
credential is logged in full.
"""

from __future__ import annotations

import json
import traceback
from collections.abc import Callable, Mapping
from typing import Final

import pytest
import structlog
from pydantic import ValidationError

from fking.platform.config import JsonValue, Settings, bootstrap, effective_config

pytestmark = pytest.mark.unit

# Distinctive enough that a substring search cannot match anything else in a rendering.
SENTINEL: Final[str] = "SENTINEL-9f3ac1d7e05b4482"


def _settings_holding_the_sentinel() -> Settings:
    return Settings.model_validate(
        {
            "exchange": {
                "enabled": True,
                "binance": {
                    "spot_api_key": SENTINEL,
                    "futures_api_key": SENTINEL,
                    "futures_api_secret": SENTINEL,
                },
            }
        }
    )


def test_the_sentinel_is_actually_stored() -> None:
    """Guards every other test in this file: searching for a value that was never set
    would pass regardless of whether redaction works."""
    settings = _settings_holding_the_sentinel()
    assert settings.exchange.binance.futures_api_secret is not None
    assert settings.exchange.binance.futures_api_secret.get_secret_value() == SENTINEL


RENDERERS: Final[dict[str, Callable[[Settings], str]]] = {
    "repr": repr,
    "str": str,
    "model_dump_json": lambda settings: settings.model_dump_json(),
    "effective_config": lambda settings: json.dumps(effective_config(settings)),
}


def _leaf(dump: Mapping[str, JsonValue], *path: str) -> JsonValue:
    node: JsonValue = dict(dump)
    for key in path:
        assert isinstance(node, dict)
        node = node[key]
    return node


@pytest.mark.parametrize("name", sorted(RENDERERS))
def test_no_rendering_of_settings_contains_a_secret(name: str) -> None:
    assert SENTINEL not in RENDERERS[name](_settings_holding_the_sentinel())


def _fail_while_holding(settings: Settings) -> None:
    """A frame whose locals include the settings object, exactly like a real handler."""
    raise RuntimeError(f"boom with {settings!r}")


def test_a_traceback_capturing_locals_contains_no_secret() -> None:
    """The realistic leak is not a print. It is an exception whose frame holds the
    settings object, rendered by a reporter that captures locals."""
    try:
        _fail_while_holding(_settings_holding_the_sentinel())
    except RuntimeError as raised:
        rendered = "".join(
            traceback.TracebackException.from_exception(raised, capture_locals=True).format()
        )
    assert SENTINEL not in rendered


def test_a_validation_error_beside_a_secret_contains_no_secret() -> None:
    """Pydantic renders the offending input. A sibling field failing must not drag the
    credential into the message."""
    with pytest.raises(ValidationError) as raised:
        Settings.model_validate(
            {
                "exchange": {
                    "enabled": True,
                    "binance": {
                        "futures_api_key": SENTINEL,
                        "futures_api_secret": SENTINEL,
                        "spot_api_key": SENTINEL,
                        "recv_window_ms": "not-an-integer",
                    },
                }
            }
        )
    assert SENTINEL not in str(raised.value)


def test_the_boot_log_record_contains_no_secret() -> None:
    """CONFIGURATION.md section 4: the full effective config is logged at boot. That is
    only safe because the credentials are already `**********` by the time it happens."""
    settings = _settings_holding_the_sentinel()
    with structlog.testing.capture_logs() as captured:
        bootstrap(settings)
    rendered = json.dumps(captured, default=str)
    assert SENTINEL not in rendered
    assert any(record["event"] == "effective_config" for record in captured)


def test_the_redacted_dump_still_shows_that_a_secret_is_present() -> None:
    """Redaction that removes the key entirely hides a misconfiguration: "no futures
    credential" and "futures credential redacted" must not look the same in the log."""
    redacted = effective_config(_settings_holding_the_sentinel())
    assert _leaf(redacted, "exchange", "binance", "futures_api_secret") == "**********"


def test_a_key_path_is_logged_even_though_the_key_is_not() -> None:
    """Knowing which key file was in use is essential to an investigation; the key
    material is not. SECURITY.md section 4.4."""
    settings = Settings.model_validate(
        {"exchange": {"binance": {"spot_ed25519_key_path": "secrets/ed25519_spot.pem"}}}
    )
    key_path = _leaf(effective_config(settings), "exchange", "binance", "spot_ed25519_key_path")
    assert "ed25519_spot.pem" in str(key_path)
