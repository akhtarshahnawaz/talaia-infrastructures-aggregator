"""Connector registry.

Adding a source is a decorator, not an edit to the core. ``/v1/sources`` renders this
registry, so the documentation website can never drift from what is actually deployed.
"""
from __future__ import annotations

from typing import Callable, Iterable, Type

from .base import Connector, Tier

_REGISTRY: dict[str, Type[Connector]] = {}


def register(cls: Type[Connector]) -> Type[Connector]:
    if not getattr(cls, "meta", None):
        raise ValueError(f"{cls.__name__} must define meta")
    _REGISTRY[cls.meta.id] = cls
    return cls


def get(source_id: str) -> Type[Connector] | None:
    return _REGISTRY.get(source_id)


def all_connectors() -> list[Type[Connector]]:
    return sorted(_REGISTRY.values(), key=lambda c: c.meta.id)


def by_tier(tier: Tier) -> list[Type[Connector]]:
    return [c for c in all_connectors() if c.tier == tier]


def resident() -> list[Type[Connector]]:
    return by_tier(Tier.RESIDENT)


def covering(bbox: tuple[float, float, float, float]) -> list[Type[Connector]]:
    return [c for c in all_connectors() if c.coverage.contains(bbox)]


def for_categories(categories: Iterable[str]) -> list[Type[Connector]]:
    wanted = set(categories)
    return [c for c in all_connectors()
            if not c.meta.categories or wanted.intersection(c.meta.categories)]


def load_all() -> None:
    """Import connector modules for their registration side effects."""
    from . import es  # noqa: F401
    from . import osm  # noqa: F401
    es.load()
    osm.load()
