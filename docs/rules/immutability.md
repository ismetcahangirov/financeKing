# Rule — Immutability

## The rule

Every type in `fking.domain` is frozen — `@dataclass(frozen=True, slots=True)` — and every state transition returns a new object rather than mutating an existing one. Domain collections are `tuple` and immutable `Mapping`, never `list`, `dict` or `set`. Functions accept `Sequence` and `Mapping`, so a caller's data cannot be mutated through the parameter.

## Why

A `Position` is read by the risk engine, the execution venue, the reconciler, the backtest replay and three telemetry consumers. If any one of them can mutate it, the value observed by the others depends on scheduling order, and the bug that follows cannot be reproduced.

The specific mechanism that bites here is idempotency. Redis Streams delivery is at-least-once — `CLAUDE.md` §2 states this as a design constraint, not a discovery. A consumer therefore sees the same `FillApplied` event twice and must be able to detect the duplicate. With immutable state the check is trivial and total:

```python
if fill.fill_id in position.applied_fill_ids:
    return position
```

With mutable state it is not, because the object the consumer is holding may already have been advanced by a different consumer between the duplicate check and the application. The observable symptom is a position of 0.02 BTC where the exchange reports 0.01, appearing roughly once per thousand redeliveries, never in tests, and never twice in the same place. Reconciliation flags it, you flatten and restart, and the cause is gone.

Frozen objects are also hashable when `eq=True`, which is what allows a dedup key to be the object itself rather than a hand-maintained tuple of its fields — a hand-maintained key drifts the first time someone adds a field.

The backtest makes this worse, not better. In live trading each event is processed once, so a mutation bug may hide for months. In backtest the whole history is in memory and the replay walks it repeatedly across walk-forward folds; a mutated `Bar` or `Position` carried between folds contaminates fold *n+1* with state from fold *n*. That is look-ahead by aliasing — it does not raise, and it inflates the fold Sharpe. See `ARCHITECTURE.md` §10 on why an inflated fold Sharpe is the most expensive kind of wrong number in this project.

And the reason it must be structural rather than a convention: this system writes its own strategies. An LLM-authored consumer will mutate whatever the type system lets it mutate. `frozen=True` turns that into `FrozenInstanceError` at the point of the attempt, in a test, in CI.

## Incorrect

```python
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass
class Position:
    symbol: str
    base_quantity: Decimal
    average_entry_price: Decimal
    fills: list[Fill] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)

    def apply_fill(self, fill: Fill) -> None:
        total_cost = self.average_entry_price * self.base_quantity + fill.quote_price * fill.base_quantity
        self.base_quantity += fill.base_quantity
        self.average_entry_price = total_cost / self.base_quantity
        self.fills.append(fill)


def summarise(positions: list[Position], tags: dict[str, str]) -> str:
    positions.sort(key=lambda p: p.symbol)      # reorders the caller's list
    tags["summarised"] = "true"                 # writes into the caller's dict
    return ", ".join(p.symbol for p in positions)
```

`apply_fill` returns `None`, so every caller holds a reference to an object whose meaning changes underneath it. Applying the same fill twice doubles the position and there is no record that would let you tell — `fills` grows, but nothing compares against it. `base_quantity` is mutated before `average_entry_price` is computed from it, so a partial rewrite on an exception mid-method leaves an object that is internally inconsistent yet perfectly valid to the type system. `summarise` reorders the caller's list and writes into the caller's dict; the caller has no way to know from the signature, and the telemetry consumer that iterates the same list concurrently sees the reorder.

## Correct

```python
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    base_quantity: Decimal
    average_entry_quote_price: Decimal
    opened_at_utc: datetime
    applied_fill_ids: frozenset[UUID] = frozenset()
    fills: tuple[Fill, ...] = ()

    def with_fill(self, fill: Fill) -> "Position":
        """Return the position after `fill`. Idempotent by fill_id."""
        if fill.fill_id in self.applied_fill_ids:
            return self

        new_quantity = self.base_quantity + fill.base_quantity
        if new_quantity == 0:
            new_price = Decimal("0")
        else:
            prior_cost = self.average_entry_quote_price * self.base_quantity
            new_price = (prior_cost + fill.quote_price * fill.base_quantity) / new_quantity

        return replace(
            self,
            base_quantity=new_quantity,
            average_entry_quote_price=new_price,
            applied_fill_ids=self.applied_fill_ids | {fill.fill_id},
            fills=(*self.fills, fill),
        )


def summarise(positions: Sequence[Position], tags: Mapping[str, str]) -> str:
    ordered = sorted(positions, key=lambda position: position.symbol)
    return ", ".join(f"{position.symbol}[{tags.get(position.symbol, '-')}]" for position in ordered)
```

`with_fill` either returns a fully-formed new `Position` or raises before anything is observable; there is no half-applied state, so an exception mid-transition is safe by construction. `Sequence` and `Mapping` in `summarise` mean `sorted()` — which copies — is the only sort available; `.sort()` and `tags["x"] = ...` are `mypy --strict` errors, not review comments.

`slots=True` is not a micro-optimization here. It removes `__dict__`, so `position.qty = ...` raises `AttributeError` instead of silently creating a shadow attribute that nothing reads — the failure mode where a typo becomes a field.

Where a `Mapping` genuinely must be a field, wrap it so the immutability is real rather than nominal:

```python
@dataclass(frozen=True, slots=True)
class StrategyParameters:
    values: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
```

`frozen=True` protects the binding, not the object bound. A `dict` field on a frozen dataclass is still mutable through the reference — the copy plus `MappingProxyType` is what makes the guarantee hold, and `object.__setattr__` is the sanctioned way to normalize a field inside `__post_init__` of a frozen dataclass.

Pydantic models — which live in `api`, `agents` and `platform`, never in `domain`, because `domain` imports nothing but stdlib (see [`./module-boundaries.md`](./module-boundaries.md)) — carry the same guarantee:

```python
class SignalPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
```

## Enforcement

**A test that walks the whole domain package** rather than asserting on the classes someone remembered to list. New types are covered the moment they exist:

```python
import dataclasses
import importlib
import inspect
import pkgutil
from collections.abc import Iterator
from typing import Any, get_args, get_origin, get_type_hints

import pytest

import fking.domain

MUTABLE_ORIGINS = (list, dict, set, bytearray)


def public_domain_classes() -> Iterator[type]:
    for module_info in pkgutil.walk_packages(fking.domain.__path__, prefix="fking.domain."):
        module = importlib.import_module(module_info.name)
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if name.startswith("_") or obj.__module__ != module.__name__:
                continue
            yield obj


@pytest.mark.parametrize("cls", list(public_domain_classes()), ids=lambda c: c.__qualname__)
def test_domain_class_is_frozen(cls: type) -> None:
    if dataclasses.is_dataclass(cls):
        assert cls.__dataclass_params__.frozen, f"{cls.__qualname__} is a mutable dataclass"
        assert "__slots__" in cls.__dict__, f"{cls.__qualname__} is missing slots=True"
    elif hasattr(cls, "model_config"):
        assert cls.model_config.get("frozen") is True, f"{cls.__qualname__} is a mutable model"
    else:
        pytest.fail(f"{cls.__qualname__} is neither a frozen dataclass nor a frozen model")


@pytest.mark.parametrize("cls", list(public_domain_classes()), ids=lambda c: c.__qualname__)
def test_domain_class_has_no_mutable_collection_fields(cls: type) -> None:
    hints: dict[str, Any] = get_type_hints(cls)
    offenders = [
        field_name
        for field_name, annotation in hints.items()
        if (get_origin(annotation) or annotation) in MUTABLE_ORIGINS
    ]
    assert offenders == [], f"{cls.__qualname__} exposes mutable fields {offenders}"


@pytest.mark.parametrize("cls", list(public_domain_classes()), ids=lambda c: c.__qualname__)
def test_domain_class_has_no_mutable_defaults(cls: type) -> None:
    if not dataclasses.is_dataclass(cls):
        return
    offenders = [
        field.name
        for field in dataclasses.fields(cls)
        if isinstance(field.default, MUTABLE_ORIGINS)
    ]
    assert offenders == [], f"{cls.__qualname__} has mutable defaults {offenders}"
```

**A Hypothesis property** that the transition is genuinely non-destructive and idempotent, which is the behaviour the freeze exists to protect. Property tests are mandatory for position math (`CLAUDE.md` §5):

```python
@given(position=positions(), fill=fills())
def test_with_fill_leaves_the_original_untouched_and_is_idempotent(position: Position, fill: Fill) -> None:
    before = dataclasses.asdict(position)
    once = position.with_fill(fill)
    twice = once.with_fill(fill)

    assert dataclasses.asdict(position) == before
    assert twice == once
```

**ruff**, catching the constructs before they reach a review:

```toml
[tool.ruff.lint]
select = ["E", "F", "B", "N", "UP", "RUF", "TRY", "DTZ", "BLE", "FURB", "PL", "SIM", "ANN"]
```

- `RUF008` — mutable default in a dataclass field.
- `RUF009` — function call used as a dataclass field default, which is evaluated once at class definition time and then shared by every instance.
- `RUF012` — mutable class attribute not annotated `ClassVar`.
- `B006` — mutable default argument, the version of the same bug that shares state across every call of a function.
- `B008` — function call in a default argument.

**mypy --strict** enforces the `Sequence`/`Mapping` half. It is the only mechanism that catches "the callee mutated my list", because that is a typing question, not a runtime one — there is nothing to observe until the damage is done.

## The one exception

A local mutable accumulator inside a single function body, which never escapes the call frame.

```python
def net_positions(fills: Sequence[Fill]) -> Mapping[str, Decimal]:
    totals: dict[str, Decimal] = {}          # local, never returned as-is
    for fill in fills:
        totals[fill.symbol] = totals.get(fill.symbol, Decimal("0")) + fill.base_quantity
    return MappingProxyType(dict(totals))    # converted at the boundary
```

Building a `dict` and then freezing it is clearer and faster than a fold, and it is safe for exactly one reason: the mutable object has no other reference. The exception ends the moment it does. Specifically, it does not permit:

- returning the `list` or `dict` directly, even with a `Sequence`/`Mapping` return annotation — the annotation is a promise the caller cannot verify and a downstream `cast` will break it;
- storing the accumulator on `self`, in a module-level variable, in a cache, or in a closure that outlives the call;
- passing it to a function that keeps a reference, including anything that publishes to the event bus.

Convert at the `return`, in the same function that created it. If the conversion is anywhere else, the object escaped and this exception does not apply.
