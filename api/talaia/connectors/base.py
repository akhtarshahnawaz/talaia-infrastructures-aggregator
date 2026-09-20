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

    def contains_point(self, lon: float, lat: float, *, pad_deg: float = 0.5) -> bool:
        """Is this coordinate plausibly one of ours? Padded, because a coverage box is
        a rough rectangle and a real asset can sit just outside it."""
        a0, b0, a1, b1 = self.bbox
        return (a0 - pad_deg <= lon <= a1 + pad_deg
                and b0 - pad_deg <= lat <= b1 + pad_deg)


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


@dataclass(frozen=True)
class IngestFilter:
    """Load part of a source instead of all of it.

    The national registries are national: the school one is ~51,000 addresses and the
    best part of an hour, almost all of it geocoding. For a demo of one city that is
    wasted, so this narrows the work *before* the geocoder is asked anything.

    ``places`` is the one that saves the time. Every address-bearing registry publishes a
    municipality and usually a province, so matching those by name discards the records
    we will never use while they are still free. ``bbox`` is a coordinate test and can
    only be applied once a coordinate exists - immediately for sources that publish one,
    after geocoding for the rest - so it narrows the data but not the work. ``limit`` is
    the blunt instrument that works on anything.
    """
    bbox: tuple[float, float, float, float] | None = None
    places: frozenset[str] = frozenset()
    limit: int | None = None

    @property
    def is_partial(self) -> bool:
        return bool(self.bbox or self.places or self.limit)

    def describe(self) -> str:
        bits = []
        if self.places:
            bits.append("places=" + "/".join(sorted(self.places)))
        if self.bbox:
            bits.append("bbox=" + ",".join(f"{c:g}" for c in self.bbox))
        if self.limit:
            bits.append(f"limit={self.limit:,}")
        return ", ".join(bits) or "everything"

    _PLACE_FIELDS = ("municipality", "province", "comarca", "region", "locality", "town")

    def wanted_place(self, item: "RawAsset") -> bool:
        """Text test, applied before any geocoding.

        A record that carries no place at all cannot be judged by name - the population
        grid publishes cells, not addresses - so it passes here and is left to the
        coordinate test. Dropping it instead would mean naming a city silently emptied
        every source that does not happen to publish a municipality column.
        """
        if not self.places:
            return True
        from ..norm import fold
        addr = item.address or {}
        values = [fold(addr.get(k)) for k in self._PLACE_FIELDS]
        if not any(values):
            return True
        return any(v and v in self.places for v in values)

    def wanted_point(self, lon: float | None, lat: float | None) -> bool:
        """Coordinate test. Unknown coordinates pass; they are judged after geocoding."""
        if not self.bbox or lon is None or lat is None:
            return True
        x0, y0, x1, y1 = self.bbox
        return x0 <= lon <= x1 and y0 <= lat <= y1


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
    async def ingest(self, store, batch_size: int = 5_000, geocode_chunk: int = 500,
                     select: "IngestFilter | None" = None) -> int:
        """Fetch -> normalise -> upsert. Used by the CLI and the boot bootstrap.

        Address-only records are geocoded in chunks and written as each chunk lands,
        rather than all at the end. The national school registry is ~51,000 addresses,
        which is the best part of an hour; writing only at the end meant any restart in
        that window - and every redeploy is one - threw the whole source away and started
        from nothing. Progress is recorded per chunk so a resumed boot can tell a source
        that finished from one that was interrupted.

        ``select`` loads part of the source - a city, a province, or simply the first N
        records. A filtered run is recorded as ``partial`` however it ends, because it
        deliberately did not load everything and a later full load must still run.
        """
        from ..norm import asset_id, point_wkt, valid_lonlat
        from ..taxonomy import get_subcategory

        started = datetime.now(timezone.utc).replace(tzinfo=None)
        total, skipped, geocoded, out_of_coverage, batch = 0, 0, 0, 0, []
        filtered = 0
        pending_geocode: list[RawAsset] = []
        select = select or IngestFilter()
        if select.is_partial:
            log.info("%s: partial load (%s)", self.meta.id, select.describe())
        try:
            async for raw in self.fetch():
                if select.limit is not None and total + len(batch) + len(
                        pending_geocode) >= select.limit:
                    break
                for item in self.normalise(raw):
                    # Discard what this run does not want while it is still free - before
                    # the geocoder is asked anything, which is where the hour goes.
                    if not select.wanted_place(item):
                        filtered += 1
                        continue
                    if not select.wanted_point(item.lon, item.lat):
                        filtered += 1
                        continue
                    # Registries that publish an address but no coordinates are resolved
                    # through the Tier C geocoder, in chunks once the fetch completes.
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
                    # A coordinate outside the source's own declared coverage is a
                    # mangled record, not a discovery: the Catalan registries publish a
                    # handful of rows at longitude -81.9 and latitude 0.000009. They can
                    # never match a real AOI, so they only ever distort statistics.
                    if (item.lon is not None and item.lat is not None
                            and not self.coverage.contains_point(item.lon, item.lat)):
                        out_of_coverage += 1
                        continue
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
            # Land everything that already had coordinates before starting on the slow
            # part, so an interruption during geocoding does not also cost the rows that
            # never needed it.
            if batch:
                total += await store.upsert_assets(batch)
                batch = []
            if pending_geocode:
                log.info("%s: geocoding %s addresses in chunks of %s",
                         self.meta.id, f"{len(pending_geocode):,}", geocode_chunk)
            for start in range(0, len(pending_geocode), geocode_chunk):
                chunk = pending_geocode[start:start + geocode_chunk]
                geocoded_rows, failed = await self._geocode_batch(chunk, store)
                skipped += failed
                if select.bbox:
                    # These had no coordinate to test until now.
                    kept = [r for r in geocoded_rows
                            if select.wanted_point(r.get("lon"), r.get("lat"))]
                    filtered += len(geocoded_rows) - len(kept)
                    geocoded_rows = kept
                geocoded += len(geocoded_rows)
                if geocoded_rows:
                    total += await store.upsert_assets(geocoded_rows)
                # Durable progress. Without it the rows are visible but the source looks
                # finished, and a resumed boot would skip the remaining addresses.
                await store.record_run(self.meta.id, "partial", total, started)
                log.info("%s: geocoded %s/%s addresses (%s rows so far)", self.meta.id,
                         f"{min(start + geocode_chunk, len(pending_geocode)):,}",
                         f"{len(pending_geocode):,}", f"{total:,}")
            # A filtered run never claims the source is complete: it deliberately left
            # records out, and the boot bootstrap must still come back for them.
            await store.record_run(
                self.meta.id, "partial" if select.is_partial else "ok", total, started,
                f"partial load: {select.describe()}" if select.is_partial else None)
            log.info("%s: ingested %s rows (%s geocoded, %s skipped for bad geometry, "
                     "%s outside %s, %s filtered out)", self.meta.id, total, geocoded,
                     skipped, out_of_coverage, self.coverage.label, filtered)
            return total
        except Exception as exc:
            await store.record_run(self.meta.id, "error", total, started, str(exc)[:500])
            log.exception("%s: ingest failed", self.meta.id)
            raise

    async def _geocode_batch(self, items: list["RawAsset"], store
                             ) -> tuple[list[dict], int]:
        """Resolve address-only records to coordinates, carrying geocoder quality into
        the asset's confidence so a street-level match outranks a town centroid."""
        from .es import gazetteer
        from .es.cartociudad import geocode_many
        from ..config import settings
        from ..norm import asset_id, geocode_query, point_wkt
        from ..taxonomy import get_subcategory

        mode = (settings.geocode_mode or "offline").lower()
        results: list[dict | None] = [None] * len(items)

        # Offline first unless explicitly told otherwise. One 3.2 MB table resolves a
        # municipality to a point in memory; the alternative is one HTTP request per
        # address against somebody else's free server.
        if mode in ("offline", "hybrid"):
            table = await gazetteer.load(store)
            for idx, item in enumerate(items):
                addr = item.address or {}
                results[idx] = gazetteer.lookup(
                    table, addr.get("municipality"), addr.get("province"))
            got = sum(1 for r in results if r)
            log.info("%s: %s/%s placed offline from the gazetteer",
                     self.meta.id, f"{got:,}", f"{len(items):,}")

        if mode in ("hybrid", "street"):
            need = [i for i, r in enumerate(results) if r is None]
            if need:
                queries = [geocode_query((items[i].address or {}).get("street"),
                                         (items[i].address or {}).get("municipality")) or ""
                           for i in need]
                log.info("%s: geocoding %s addresses via CartoCiudad",
                         self.meta.id, f"{len(queries):,}")
                for idx, res in zip(need, await geocode_many(queries, store)):
                    if res:
                        results[idx] = res
            if mode == "street":
                # Offline is the safety net here, not the first answer.
                missing = [i for i, r in enumerate(results) if r is None]
                if missing:
                    table = await gazetteer.load(store)
                    for idx in missing:
                        addr = items[idx].address or {}
                        results[idx] = gazetteer.lookup(
                            table, addr.get("municipality"), addr.get("province"))

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
                # Loud on purpose. A point placed at a town centroid can sit inside a
                # fire perimeter the building is outside of, or the reverse, and anyone
                # acting on this row needs to know which kind of point it is.
                "geocode_approximate": bool(res.get("approximate")),
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
