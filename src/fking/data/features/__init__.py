"""The feature store and the registry of what may be computed at all.

Two guarantees, and neither is implemented in Python.

**A value computed at `t` is reproducible from only what existed at `t`.** Enforced by
the storage layer: `fking_app` holds no privilege on `feature_values`, and reaches it
only through `fking_feature_as_of()`, which is `SECURITY DEFINER` and carries the
`available_at_utc <= as_of` predicate in its body. A look-ahead defect here is
`permission denied for table feature_values`, not a review miss.

**A feature that does not declare its timing cannot be registered, and therefore cannot
be used.** `FeatureSpec` has no defaults, so omitting `lookback`, `availability_lag` or
`point_in_time_proof` is a `TypeError` that names the field.

The distinction the whole package turns on: `event_time_utc` is when the thing happened,
`available_at_utc` is the earliest instant this system could have known it. They are
different, `available_at_utc >= event_time_utc` is a database `CHECK`, and only the
second governs visibility. `WHERE event_time <= :t` is the single most common spelling of
look-ahead and it looks completely correct, which is why the filter is not left anywhere
a caller can write it.

`docs/rules/no-lookahead.md`, `DATA_PIPELINE.md` sections 7 and 8.
"""

from fking.data.features.registry import (
    FEATURES,
    evaluate,
    evaluate_settlement_rates,
    registered,
    registered_names,
)
from fking.data.features.spec import (
    FeatureCompute,
    FeatureObservation,
    FeaturePoint,
    FeatureRef,
    FeatureSpec,
    FeatureWindow,
    SettlementRateCompute,
    SettlementRateObservation,
    definition_digest,
)
from fking.data.features.store import (
    FeatureSeries,
    FeatureStore,
    FeatureValue,
    FeatureValueWriter,
    PostgresFeatureStore,
)

__all__: tuple[str, ...] = (
    "FEATURES",
    "FeatureCompute",
    "FeatureObservation",
    "FeaturePoint",
    "FeatureRef",
    "FeatureSeries",
    "FeatureSpec",
    "FeatureStore",
    "FeatureValue",
    "FeatureValueWriter",
    "FeatureWindow",
    "PostgresFeatureStore",
    "SettlementRateCompute",
    "SettlementRateObservation",
    "definition_digest",
    "evaluate",
    "evaluate_settlement_rates",
    "registered",
    "registered_names",
)
