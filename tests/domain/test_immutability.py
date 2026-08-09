"""Immutability, asserted by walking the package rather than by listing types.

A test that enumerates the twelve types known today covers exactly those twelve. The
thirteenth is added by whoever needs it, and they will not think to add it here --
which is the moment a mutable domain object enters, and the failure it causes is a
position that depends on read order and therefore on scheduling, and therefore cannot
be reproduced.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import pkgutil
from collections.abc import Iterator
from enum import Enum
from typing import get_args, get_origin, get_type_hints

import pytest

import fking.domain

pytestmark = pytest.mark.unit

MUTABLE_ORIGINS = (list, dict, set, bytearray)


def _public_domain_classes() -> Iterator[type]:
    for module_info in pkgutil.walk_packages(fking.domain.__path__, prefix="fking.domain."):
        module = importlib.import_module(module_info.name)
        for name, member in inspect.getmembers(module, inspect.isclass):
            if name.startswith("_") or member.__module__ != module.__name__:
                continue
            yield member


DOMAIN_CLASSES = sorted(_public_domain_classes(), key=lambda cls: cls.__qualname__)


def test_the_walk_actually_found_the_package() -> None:
    """A walk that silently finds nothing turns every test below into a no-op."""
    found = {cls.__name__ for cls in DOMAIN_CLASSES}
    assert {"Bar", "Fill", "Instrument", "Order", "Position", "Signal"} <= found


@pytest.mark.parametrize("cls", DOMAIN_CLASSES, ids=lambda cls: cls.__qualname__)
def test_every_domain_class_is_frozen_and_slotted(cls: type) -> None:
    if issubclass(cls, Enum | Exception):
        return
    assert dataclasses.is_dataclass(cls), f"{cls.__qualname__} is not a dataclass"
    # __dataclass_params__ is documented behaviour and is the only way to read the
    # frozen flag back off a class; typeshed does not declare it.
    params = cls.__dataclass_params__  # type: ignore[attr-defined]
    assert params.frozen, f"{cls.__qualname__} is a mutable dataclass"
    # slots=True removes __dict__, so `position.qty = ...` raises AttributeError instead
    # of silently creating a shadow attribute that nothing ever reads.
    assert "__slots__" in cls.__dict__, f"{cls.__qualname__} is missing slots=True"


@pytest.mark.parametrize("cls", DOMAIN_CLASSES, ids=lambda cls: cls.__qualname__)
def test_no_domain_class_exposes_a_mutable_collection_field(cls: type) -> None:
    """`frozen=True` protects the binding, not the object bound.

    A `list` field on a frozen dataclass still supports `.append()`. This is the
    immutability bug that passes review, because `frozen=True` is right there at the
    top of the class and the reviewer stops reading.
    """
    if not dataclasses.is_dataclass(cls):
        return
    offenders = [
        name
        for name, annotation in get_type_hints(cls).items()
        if (get_origin(annotation) or annotation) in MUTABLE_ORIGINS
        or any(
            (get_origin(arg) or arg) in MUTABLE_ORIGINS
            for arg in get_args(annotation)
            if arg is not Ellipsis
        )
    ]
    assert offenders == [], f"{cls.__qualname__} exposes mutable fields {offenders}"


@pytest.mark.parametrize("cls", DOMAIN_CLASSES, ids=lambda cls: cls.__qualname__)
def test_no_domain_class_has_a_mutable_default(cls: type) -> None:
    if not dataclasses.is_dataclass(cls):
        return
    offenders = [
        field.name
        for field in dataclasses.fields(cls)
        if isinstance(field.default, MUTABLE_ORIGINS)
    ]
    assert offenders == [], f"{cls.__qualname__} has mutable defaults {offenders}"


@pytest.mark.parametrize("cls", DOMAIN_CLASSES, ids=lambda cls: cls.__qualname__)
def test_no_state_transition_returns_none(cls: type) -> None:
    """A method on a domain object returning `None` is a mutation, by construction.

    `docs/rules/immutability.md` fixes the naming: transitions are `with_*` and
    return a new value. This catches the shape regardless of the name.
    """
    if not dataclasses.is_dataclass(cls):
        return
    for name, member in inspect.getmembers(cls, inspect.isfunction):
        if name.startswith("_"):
            continue
        assert get_type_hints(member).get("return") is not None, (
            f"{cls.__qualname__}.{name} returns None, which means it mutates"
        )
