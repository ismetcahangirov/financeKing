"""What a run is, and the content hash that is its identity.

`config_hash` is the answer to "is this the same run?", and it has to give that answer
across processes, across machines and across a restart -- because the determinism check
that outranks everything else on the queue is *two runs of the same `config_hash`
producing the same digest*, and two hashes that disagree for uninteresting reasons make
that check unrunnable.

So the hash is a SHA-256 over canonical JSON produced by `fking.domain.codec.encode`,
never over `repr`, `pickle` or Python's `hash()`:

- `hash()` of a `str` is salted per process by `PYTHONHASHSEED`, so it differs between
  two runs on one machine, let alone two machines.
- `repr` of a `Decimal` is stable, but `repr` of a `Mapping` carries insertion order, so
  the same parameters written in a different order would hash differently.
- `pickle` encodes the class's module path, so moving this file would invalidate every
  historical run id.

`encode` sorts mapping keys through `json.dumps(sort_keys=True)` below, renders every
`Decimal` as its exact string, and every `datetime` as an ISO 8601 instant -- so the
digest is a function of the values and of nothing else.

Seeds are *derived*, not drawn. Every source of randomness in a run takes
`derive_seed(run_seed, label)` for its own label, so two components never share a stream
and adding a third component cannot shift the numbers the first two draw. A single
shared `random` module would make the seventh strategy's existence change the sixth
strategy's results (`BACKTEST_ENGINE.md` section 5).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from fking.backtest._errors import RunConfigError
from fking.backtest._guards import (
    require_finite_decimal,
    require_positive_int,
    require_text,
    require_utc,
)
from fking.domain import JsonValue, encode

__all__ = ["DEFAULT_EVENT_BUDGET", "RunConfig", "canonical_digest", "config_hash", "derive_seed"]

# One million events is roughly eighteen months of one-minute bars across four symbols
# with their fills, which is the largest run this engine is designed for on one machine.
# It is a backstop against a handler that schedules at its own timestamp, not a capacity
# statement: a legitimate run that exceeds it declares a larger budget and says why.
DEFAULT_EVENT_BUDGET: Final = 1_000_000

# 8 bytes rather than the full digest: `random.Random` accepts an int of any width, but a
# 64-bit seed is the widest one that appears unchanged in a log line and in a trace, and
# a seed nobody can read back is a seed nobody reproduces a run with.
_SEED_BYTES: Final = 8


def canonical_digest(payload: JsonValue) -> str:
    """SHA-256 over the one JSON rendering of `payload` that both runs will produce.

    `sort_keys` is what makes it canonical; `separators` removes the whitespace that
    would otherwise depend on nothing anybody chose. `ensure_ascii=False` keeps a
    non-ASCII symbol -- Binance testnet serves one deliberately -- as the code points the
    venue sent rather than as an escape sequence whose casing varies by encoder.
    """
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def derive_seed(run_seed: int, label: str) -> int:
    """The seed for one named source of randomness inside a run.

    Deterministic in both arguments and independent across labels. Two components with
    different labels get uncorrelated streams from one run seed, and a component added
    later cannot perturb the streams of the components that already existed.
    """
    if not isinstance(run_seed, int) or isinstance(run_seed, bool):
        raise RunConfigError(f"run_seed must be an int, got {run_seed!r}")
    require_text(label, "label")
    material = f"{run_seed}:{label}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:_SEED_BYTES], "big")


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Everything that decides what a run produces, and nothing that does not.

    Deliberately absent: output paths, report flags, log levels, worker counts. Anything
    that can change without changing the result must stay out of the hash, or two runs
    that produced identical numbers will carry different identities and the determinism
    check will pass vacuously by never comparing anything.

    Equally deliberately absent: an `is_backtest` flag or anything else naming the venue.
    A strategy that can detect which venue it is running against is a strategy that can
    behave differently there, and every result becomes unfalsifiable
    (`BACKTEST_ENGINE.md` section 3).
    """

    strategy_id: str
    strategy_version: str
    symbols: tuple[str, ...]
    parameters: Mapping[str, Decimal]
    start_utc: datetime
    end_utc: datetime
    run_seed: int
    event_budget: int = DEFAULT_EVENT_BUDGET

    def __post_init__(self) -> None:
        require_text(self.strategy_id, "strategy_id")
        require_text(self.strategy_version, "strategy_version")
        require_utc(self.start_utc, "start_utc")
        require_utc(self.end_utc, "end_utc")
        require_positive_int(self.event_budget, "event_budget")

        if not isinstance(self.run_seed, int) or isinstance(self.run_seed, bool):
            raise RunConfigError(f"run_seed must be an int, got {self.run_seed!r}")
        if self.run_seed < 0:
            raise RunConfigError(f"run_seed must not be negative; got {self.run_seed}")
        if self.end_utc <= self.start_utc:
            raise RunConfigError(
                f"end_utc {self.end_utc.isoformat()} must follow "
                f"start_utc {self.start_utc.isoformat()}"
            )
        if not self.symbols:
            raise RunConfigError("symbols must name at least one instrument")
        for symbol in self.symbols:
            require_text(symbol, "symbol")
        if len(set(self.symbols)) != len(self.symbols):
            # Not merely untidy: a duplicated symbol subscribes its market data twice, so
            # every bar is dispatched twice and every count derived from the run doubles
            # for that symbol alone.
            raise RunConfigError(f"symbols contains a duplicate: {self.symbols}")
        for name, parameter in self.parameters.items():
            require_text(name, "parameter name")
            require_finite_decimal(parameter, f"parameter {name!r}")

        # `frozen=True` protects the binding, not the object bound: a plain dict here is
        # still mutable through the reference a caller kept, and a parameter changed
        # mid-run would leave the config hash describing a configuration that no longer
        # exists (`.claude/rules/immutability.md`).
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    def seed_for(self, label: str) -> int:
        """This run's seed for the named source of randomness."""
        return derive_seed(self.run_seed, label)


def config_hash(config: RunConfig) -> str:
    """The run's identity: a digest of every field that can change its output."""
    return canonical_digest(encode(config))
