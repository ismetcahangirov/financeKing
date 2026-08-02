"""Checks that need pull-request metadata, not just the source tree.

These live apart from `tools/checks/` because they cannot run from a working copy
alone: they need the PR title, its labels and its changed-file set. `make check`
therefore does not run them, and CI does.
"""

__all__: tuple[str, ...] = ()
