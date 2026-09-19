"""Connector contract.

A connector is the only thing you write to add a new registry or a new country.
It declares what it is (``meta``), how to get raw records (``fetch``) and how to turn
them into canonical assets (``normalise``). Everything downstream - storage, conflation,
valuation, scoring, the API and the documentation website - works off the canonical
model and needs no knowledge of the source.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator, Iterable

from ..models import SourceMeta

log = logging.getLogger("talaia.connectors")


class Tier(str, Enum):
    """Where a source sits in the three-tier strategy (see ARCHITECTURE.md §4)."""
    RESIDENT = "resident"       # bulk-loaded, queried locally
    ON_DEMAND = "on_demand"     # fetched lazily, tile-cached
    ENRICHMENT = "enrichment"   # per-asset lookups, cached forever


@dataclass
class Coverage:
    """Where a source has data. Lets the API tell a caller which regime they are in."""
    bbox: tuple[float, float, float, float] = (-180.0, -90.0, 180.0, 90.0)
    label: str = "global"

    def contains(self, bbox: tuple[float, float, float, float]) -> bool:
        x0, y0, x1, y1 = bbox
        a0, b0, a1, b1 = self.bbox
        return not (x1 < a0 or x0 > a1 or y1 < b0 or y0 > b1)


# Convenience coverages used by the Spanish connectors.
SPAIN = Coverage((-18.2, 27.5, 4.4, 43.9), "Spain")
CATALONIA = Coverage((0.15, 40.5, 3.35, 42.9), "Catalonia")
GLOBAL = Coverage()


@dataclass
class RawAsset:
    """What ``normalise`` yields. Deliberately loose - the connector's job is mapping,
    not validation; the ingest pipeline validates and fills defaults."""
    source_ref: str
    subcategory: str
    name: str | None = None
    lon: float | None = None
    lat: float | None = None
    wkt: str | None = None
    geometry_kind: str = "point"
    address: dict[str, Any] = field(default_factory=dict)
    contacts: dict[str, Any] = field(default_factory=dict)
    capacity: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)
    footprint_m2: float | None = None
    floors: float | None = None
    confidence: float = 0.6
    needs_geocoding: bool = False


class Connector(ABC):
    """Base class. Subclasses set ``meta`` and implement ``fetch`` + ``normalise``."""

    meta: SourceMeta
    tier: Tier = Tier.RESIDENT
    coverage: Coverage = GLOBAL

    @abstractmethod
    async def fetch(self, **kwargs: Any) -> AsyncIterator[dict]:
        """Yield raw upstream records."""
        raise NotImplementedError

    @abstractmethod
    def normalise(self, raw: dict) -> Iterable[RawAsset]:
        """Map one raw record to zero or more canonical assets."""
        raise NotImplementedError

    # -- shared plumbing ---------------------------------------------------
    async def ingest(self, store, batch_size: int = 5_000) -> int:
        """Fetch -> normalise -> upsert. Used by the CLI and the boot bootstrap."""
        from ..norm import asset_id, point_wkt, valid_lonlat
        from ..taxonomy import get_subcategory

        started = datetime.now(timezone.utc).replace(tzinfo=None)
        total, skipped, geocoded, batch = 0, 0, 0, []
        pending_geocode: list[RawAsset] = []
        try:
            async for raw in self.fetch():
                for item in self.normalise(raw):
                    # Registries that publish an address but no coordinates are resolved
                    # through the Tier C geocoder, in one batch after the fetch completes.
                    if item.needs_geocoding and not valid_lonlat(item.lon, item.lat):
                        pending_geocode.append(item)
                        continue
                    wkt = item.wkt
                    if not wkt:
                        ll = valid_lonlat(item.lon, item.lat)
                        if not ll:
                            skipped += 1
                            continue
                        wkt = point_wkt(*ll)
                    spec = get_subcategory(item.subcategory)
                    batch.append({
                        "id": asset_id(self.meta.id, item.source_ref),
                        "source_id": self.meta.id,
                        "source_ref": item.source_ref,
                        "category": spec.category,
                        "subcategory": spec.key,
                        "name": item.name,
                        "wkt": wkt,
                        "lon": item.lon,
                        "lat": item.lat,
                        "geometry_kind": item.geometry_kind,
                        "address": item.address,
                        "contacts": item.contacts,
                        "capacity": item.capacity,
                        "attributes": item.attributes,
                        "footprint_m2": item.footprint_m2,
                        "floors": item.floors,
                        "confidence": item.confidence,
                        "tile_key": None,
                    })
                    if len(batch) >= batch_size:
                        total += await store.upsert_assets(batch)
                        batch = []
            if pending_geocode:
                geocoded_rows, failed = await self._geocode_batch(pending_geocode, store)
                skipped += failed
                geocoded = len(geocoded_rows)
                batch.extend(geocoded_rows)
            if batch:
                total += await store.upsert_assets(batch)
            await store.record_run(self.meta.id, "ok", total, started)
            log.info("%s: ingested %s rows (%s geocoded, %s skipped for bad geometry)",
                     self.meta.id, total, geocoded, skipped)
            return total
        except Exception as exc:
            await store.record_run(self.meta.id, "error", total, started, str(exc)[:500])
            log.exception("%s: ingest failed", self.meta.id)
            raise

    async def _geocode_batch(self, items: list["RawAsset"], store
                             ) -> tuple[list[dict], int]:
        """Resolve address-only records to coordinates, carrying geocoder quality into
        the asset's confidence so a street-level match outranks a town centroid."""
        from .es.cartociudad import geocode_many
        from ..norm import asset_id, point_wkt
        from ..taxonomy import get_subcategory

        from ..norm import geocode_query

        queries = [geocode_query((i.address or {}).get("street"),
                                 (i.address or {}).get("municipality")) or ""
                   for i in items]
        log.info("%s: geocoding %s addresses via CartoCiudad", self.meta.id, len(queries))
        results = await geocode_many(queries, store)

        rows: list[dict] = []
        failed = 0
        for item, res in zip(items, results):
            if not res:
                failed += 1
                continue
            spec = get_subcategory(item.subcategory)
            address = dict(item.address or {})
            for field in ("municipality", "province", "postcode"):
                if res.get(field) and not address.get(field):
                    address[field] = res[field]
            attributes = dict(item.attributes or {})
            attributes.update({
                "geocoded": True,
                "geocode_quality": res.get("quality"),
                "geocode_match_type": res.get("match_type"),
                "geocoder": res.get("geocoder"),
            })
            if res.get("cadastral_ref"):
                attributes["cadastral_ref"] = res["cadastral_ref"]
            rows.append({
                "id": asset_id(self.meta.id, item.source_ref),
                "source_id": self.meta.id, "source_ref": item.source_ref,
                "category": spec.category, "subcategory": spec.key, "name": item.name,
                "wkt": point_wkt(res["lon"], res["lat"]),
                "lon": res["lon"], "lat": res["lat"], "geometry_kind": "point",
                "address": address, "contacts": item.contacts,
                "capacity": item.capacity, "attributes": attributes,
                "footprint_m2": item.footprint_m2, "floors": item.floors,
                "confidence": round(item.confidence * float(res.get("quality") or 0.5)
                                    / 0.9, 3),
                "tile_key": None,
            })
        return rows, failed
