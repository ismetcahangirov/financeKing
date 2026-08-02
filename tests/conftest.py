"""Shared pytest configuration.

The Hypothesis profiles are registered here rather than in each property-test module
so that the risk and domain property tests arriving with #12 inherit them without
repeating the settings -- and so the derandomize split below is decided once.
"""

from __future__ import annotations

import os
from datetime import timedelta

from hypothesis import HealthCheck, Verbosity, settings

settings.register_profile(
    "dev",
    max_examples=100,
    deadline=timedelta(milliseconds=500),
    print_blob=True,
)
settings.register_profile(
    "ci",
    max_examples=1000,
    deadline=None,
    # A pull-request gate that fails on a newly drawn example is a flaky gate. The
    # search for genuinely new counterexamples belongs in the nightly job, whose
    # failure opens an issue rather than blocking a merge.
    derandomize=True,
    # No .hypothesis cache in CI: the run must be self-contained and reproducible
    # from the log alone.
    database=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
settings.register_profile(
    "nightly",
    max_examples=20_000,
    deadline=None,
    derandomize=False,
    verbosity=Verbosity.verbose,
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
