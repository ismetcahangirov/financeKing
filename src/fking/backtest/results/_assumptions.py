"""The frozen-assumptions hash: cost, slippage and fill assumptions, content-addressed.

`fking.backtest._config.config_hash` already content-addresses everything a `RunConfig`
carries -- strategy, symbols, parameters, window, seed. It deliberately does not carry the
cost model, because `RunConfig` and `CostModel` are constructed and calibrated by
different components (`BACKTEST_ENGINE.md` section 3). This module is the composition: a
second digest of the `CostModel` alone, mixed with the run's own hash, so that changing a
fee, a spread quantile or a latency parameter after a result has been reported produces a
*different* `config_hash` -- a new trial, never a revised one
(`docs/rules/overfitting-defences.md`: "Assumptions are fixed before the run and recorded
with the run. Post-hoc adjustment is the purest form of the overfitting this system exists
to prevent.")

`CostModel` is a pydantic `BaseModel`, not one of the dataclasses
`fking.domain.codec.encode` knows how to walk, so it is hashed through its own
`model_dump(mode="json")` rather than forced through that encoder. `mode="json"` is the
load-bearing choice: pydantic renders every `Decimal` field as its exact string form in
that mode (confirmed for this project's pydantic version -- `Decimal("1.50")` dumps as
`"1.50"`, not `"1.5"`), which is the same "no precision lost, no implicit float" property
`encode` enforces for dataclasses.
"""

from __future__ import annotations

import hashlib
import json

from fking.backtest._config import RunConfig, canonical_digest, config_hash
from fking.backtest.costs import CostModel
from fking.domain import JsonValue


def cost_model_digest(cost_model: CostModel) -> str:
    """SHA-256 over the model's own canonical JSON rendering.

    `sort_keys=True` and fixed separators for the same reason `canonical_digest` uses
    them: the digest must not depend on pydantic's field-declaration order, which is an
    implementation detail of the class, not a value anyone chose.
    """
    rendered = json.dumps(
        cost_model.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def frozen_assumptions_hash(*, run_config: RunConfig, cost_model: CostModel) -> str:
    """The `config_hash` a `BacktestResult` carries: the run, plus every cost assumption.

    Composing two independently-computed digests rather than hashing one merged
    structure: `RunConfig` and `CostModel` are frozen by two different components at two
    different times (`quant` registers the run, `optimizer`/the cost calibrator supplies
    the model), and a composition of their own identities is what lets either one change
    without the other module having to know the other's internal shape.
    """
    payload: JsonValue = {
        "run_config_hash": config_hash(run_config),
        "cost_model_digest": cost_model_digest(cost_model),
    }
    return canonical_digest(payload)
