"""Release versions and the tag names derived from them.

Three digits, nothing else. No `-rc1`, no `+build`, no `v0.4.0-hotfix`.

The exclusion is not pedantry about the SemVer grammar, it is what a version *means*
here. `RELEASE_PROCESS.md` section 1: a release is "a known-good, reproducible commit of
the demo runtime -- the point you return to when the running system starts behaving
strangely". A pre-release tag is by construction a state nobody has agreed to return to,
so a tag list containing them answers the question "what can I roll back to?" with a
list that has to be filtered by hand, at the moment when hand-filtering is least
reliable.

The second reason is mechanical: `preflight` refuses a version that does not exceed the
latest existing tag, and an ordering over pre-release identifiers is a rule set that has
to be right in an edge case nobody will exercise until it matters. Three integers order
themselves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

_SEMVER: Final[re.Pattern[str]] = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$"
)

TAG_PREFIX: Final[str] = "v"


class VersionError(ValueError):
    """A version string is not three dot-separated integers."""


@dataclass(frozen=True, order=True, slots=True)
class Version:
    """A release version. `order=True` gives the comparison `preflight` needs."""

    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def tag(self) -> str:
        return f"{TAG_PREFIX}{self}"

    @classmethod
    def parse(cls, text: str) -> Version:
        """Parse `x.y.z`. Leading `v` is accepted so a tag round-trips."""
        candidate = text.strip()
        candidate = candidate.removeprefix(TAG_PREFIX)
        matched = _SEMVER.match(candidate)
        if matched is None:
            raise VersionError(
                f"{text!r} is not a release version. Three dot-separated integers, no "
                f"pre-release or build metadata: a tag nobody would roll back to should "
                f"not be in the list of things to roll back to (RELEASE_PROCESS.md 1)"
            )
        return cls(
            major=int(matched.group("major")),
            minor=int(matched.group("minor")),
            patch=int(matched.group("patch")),
        )


def latest(tags: frozenset[str]) -> Version | None:
    """The highest release version among `tags`, or None when there is no release yet.

    Tags that are not release versions are ignored rather than refused: the repository
    may carry annotation tags for other purposes, and a release process that fails
    because somebody tagged a spike is a release process people work around. What is
    *not* ignored is a malformed tag that looks like a release -- `preflight` handles
    that, because "v0.4.0-hotfix" silently sorting below "v0.4.0" is the failure this
    module's grammar exists to prevent.
    """
    versions = [
        Version.parse(tag)
        for tag in tags
        if tag.startswith(TAG_PREFIX) and _SEMVER.match(tag[len(TAG_PREFIX) :]) is not None
    ]
    return max(versions) if versions else None


def release_shaped_but_unparseable(tags: frozenset[str]) -> tuple[str, ...]:
    """Tags that begin with `v` and a digit but are not release versions.

    These are the dangerous ones. `v0.4.0-rc1` reads as a release to a human scanning
    `git tag`, sorts unpredictably against `v0.4.0` under any comparison someone writes
    in a hurry, and is invisible to `latest`. Surfacing them is the whole point.
    """
    return tuple(
        sorted(
            tag
            for tag in tags
            if tag.startswith(TAG_PREFIX)
            and len(tag) > 1
            and tag[1].isdigit()
            and _SEMVER.match(tag[len(TAG_PREFIX) :]) is None
        )
    )
