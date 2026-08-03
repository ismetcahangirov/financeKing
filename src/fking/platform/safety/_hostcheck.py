"""URL parsing and host comparison, shared by the two egress paths.

This module holds *how* a host is checked. It holds no allowlist, and it never learns
one: every function takes the permitted set as an argument. That is the whole reason it
exists as a separate file -- the trading allowlist and the archive allowlist must be
able to share a hardened URL parser without either becoming reachable from the other's
client.

Extracted at the point there were two concrete callers, not before (CLAUDE.md section
3). The first caller is `client.py`, which validates against `PERMITTED_HOSTS`; the
second is `archive.py`, which validates against `ARCHIVE_HOSTS`.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

from fking.platform.safety._errors import SafetyViolation

# TLS only. A downgrade to plaintext puts credentials on the wire on the trading path,
# and on the archive path it lets anything between here and the CDN substitute the
# bytes -- which the checksum would then have to catch on its own, without the
# transport having made the substitution difficult in the first place.
PERMITTED_SCHEMES: Final[frozenset[str]] = frozenset({"https", "wss"})


def inspect_url(
    url: str, *, permitted_hosts: frozenset[str], allowlist_name: str
) -> tuple[str | None, str | None]:
    """Return `(normalised_host, rejection_reason)`; exactly one is None.

    Returning the reason rather than raising it lets a startup gate collect every
    rejection in one pass without an `except SafetyViolation` anywhere in the tree --
    which `tools/checks/no_catch_safety.py` forbids, and rightly: the moment one such
    handler exists, the next one has a precedent to point at.

    `allowlist_name` appears in the rejection so a reader can tell *which* proof was
    violated. "not in the permitted host set" is ambiguous once there are two sets, and
    the ambiguity matters precisely when someone is deciding whether to add a host.
    """
    parts = urlsplit(url)

    if parts.scheme not in PERMITTED_SCHEMES:
        return None, (
            f"non-TLS scheme {parts.scheme!r} in {url}; "
            f"permitted schemes are {sorted(PERMITTED_SCHEMES)}"
        )

    # `hostname` rather than `netloc`: it drops userinfo and the port, so
    # `https://testnet.binance.vision@api.binance.com/` yields api.binance.com --
    # which is the host that actually answers, and the one a netloc comparison would
    # get wrong.
    host = parts.hostname
    if not host:
        return None, f"no host in {url}"

    # A trailing dot makes the DNS root label explicit; `example.com.` and
    # `example.com` are the same name. Stripping it before comparison stops
    # `api.binance.com.` from being treated as an unrecognised -- and therefore
    # separately evaluated -- host. urlsplit already lowercases; casefold is
    # belt-and-braces for non-ASCII.
    normalised = host.rstrip(".").casefold()
    if normalised not in permitted_hosts:
        return None, (
            f"host {normalised!r} is not in the compiled-in {allowlist_name}, which is "
            f"{sorted(permitted_hosts)}"
        )
    return normalised, None


def assert_permitted(url: str, *, permitted_hosts: frozenset[str], allowlist_name: str) -> str:
    """Validate one URL's host, returning the normalised host or raising.

    Returns the host so callers can log what was actually contacted rather than what
    they believe they asked for.
    """
    host, reason = inspect_url(url, permitted_hosts=permitted_hosts, allowlist_name=allowlist_name)
    if host is None:
        # Narrowing on `host` rather than on `reason`, so the return type follows
        # without an assert -- an assert here would vanish under `python -O` and take
        # the guarantee with it, which is what S101 exists to prevent.
        raise SafetyViolation(reason or f"unvalidatable url {url}")
    return host
