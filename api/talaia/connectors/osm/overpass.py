"""OpenStreetMap connector - Tier B, materialised on demand with a tile cache.

OSM cannot be preloaded (it is the planet) and cannot be queried live per request
(Overpass is slow and rate-limited). The compromise is a fixed global tile grid: an AOI
is covered by tiles, only stale tiles are fetched, and results land in the same `assets`
table as every resident source. Overlapping AOIs - consecutive simulation timesteps,
buffered variants, neighbouring fires - then hit warm tiles.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Iterable

from shapely.geometry import LineString, Polygon

from ...config import settings
from ...geo import area_km2, tiles_for_geometry
from ...models import SourceMeta
from ...net import MirrorPool
from ...norm import (asset_id, clean_emails, clean_phones, clean_text, clean_url,
                     point_wkt, to_float, valid_lonlat)
from ...taxonomy import get_subcategory
from ..base import GLOBAL, Connector, RawAsset, Tier
from ..registry import register
from .tags import build_query, classify, classify_linear

log = logging.getLogger("talaia.osm")

_pool: MirrorPool | None = None


def pool() -> MirrorPool:
    global _pool
    if _pool is None:
        _pool = MirrorPool(settings.overpass_mirror_list)
    return _pool


def _element_geometry(el: dict) -> tuple[float, float, str, str, float | None] | None:
    """Return ``(lon, lat, wkt, kind, footprint_m2)`` for an Overpass element."""
    etype = el.get("type")
    if etype == "node":
        ll = valid_lonlat(el.get("lon"), el.get("lat"))
        if not ll:
            return None
        return ll[0], ll[1], point_wkt(*ll), "point", None

    geom = el.get("geometry")
    if geom and len(geom) >= 2:
        coords = [(p["lon"], p["lat"]) for p in geom
                  if p.get("lon") is not None and p.get("lat") is not None]
        if len(coords) < 2:
            return None
        closed = len(coords) >= 4 and coords[0] == coords[-1]
        try:
            if closed:
                poly = Polygon(coords)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                if poly.is_empty:
                    return None
                c = poly.centroid
                return (c.x, c.y, poly.wkt, "footprint", area_km2(poly) * 1e6)
            line = LineString(coords)
            c = line.centroid
            return (c.x, c.y, line.wkt, "line", None)
        except Exception:
            return None

    center = el.get("center")
    if center:
        ll = valid_lonlat(center.get("lon"), center.get("lat"))
        if ll:
            return ll[0], ll[1], point_wkt(*ll), "point", None
    return None


def _contacts(tags: dict) -> dict:
    return {
        "phone": clean_phones(tags.get("phone") or tags.get("contact:phone")
                              or tags.get("mobile")),
        "email": clean_emails(tags.get("email") or tags.get("contact:email")),
        "website": clean_url(tags.get("website") or tags.get("contact:website")
                             or tags.get("url")),
        "operator": clean_text(tags.get("operator")),
    }


def _address(tags: dict) -> dict:
    out = {
        "street": clean_text(tags.get("addr:street")),
        "housenumber": clean_text(tags.get("addr:housenumber")),
        "postcode": clean_text(tags.get("addr:postcode")),
        "municipality": clean_text(tags.get("addr:city")),
        "province": clean_text(tags.get("addr:province")),
        "country": clean_text(tags.get("addr:country")) or "ES",
    }
    return {k: v for k, v in out.items() if v}


def _capacity(tags: dict, sub: str) -> dict:
    out: dict[str, Any] = {}
    for key, field in (("beds", "beds"), ("capacity:beds", "beds"),
                       ("capacity:persons", "people"), ("capacity", "places"),
                       ("rooms", "places"), ("capacity:pitches", "places")):
        val = to_float(tags.get(key))
        if val and field not in out:
            out[field] = val
    if out:
        out["basis"] = "OSM capacity tags"
        out["confidence"] = 0.45
    return out


def _floors(tags: dict) -> float | None:
    return to_float(tags.get("building:levels") or tags.get("levels"))


@register
class OpenStreetMap(Connector):
    meta = SourceMeta(
        id="osm",
        name="OpenStreetMap",
        publisher="OpenStreetMap contributors",
        tier="on_demand", coverage="Global", country="*",
        licence="ODbL 1.0", licence_url="https://opendatacommons.org/licenses/odbl/",
        url="https://www.openstreetmap.org",
        categories=["education", "healthcare", "social_care", "emergency", "transport",
                    "energy", "water", "telecom", "industry", "agriculture", "livestock",
                    "residential", "commercial", "tourism", "heritage", "environment"],
        update_cadence="continuous (tile cache TTL 14 days)",
        provides=["name", "geometry", "footprint", "address", "phone", "email",
                  "website", "roads", "power lines", "railways"],
        limitations=[
            "Completeness varies by area and is contributor-dependent.",
            "Capacity and contact tags are sparse outside urban areas.",
            "Fetched live per tile; a cold area costs one Overpass round-trip.",
        ],
    )
    tier = Tier.ON_DEMAND
    coverage = GLOBAL

    # -- Connector interface (used by the CLI for bulk preloading an area) -----
    async def fetch(self, bbox: tuple[float, float, float, float] | None = None,
                    **kwargs: Any) -> AsyncIterator[dict]:
        if bbox is None:
            return
        for el in await self._fetch_bbox(bbox):
            yield el

    def normalise(self, raw: dict) -> Iterable[RawAsset]:
        tags = raw.get("tags") or {}
        sub = classify(tags)
        if not sub:
            return []
        g = _element_geometry(raw)
        if not g:
            return []
        lon, lat, wkt, kind, footprint = g
        return [RawAsset(
            source_ref=f"{raw.get('type')}/{raw.get('id')}",
            subcategory=sub,
            name=clean_text(tags.get("name") or tags.get("official_name")),
            lon=lon, lat=lat, wkt=wkt, geometry_kind=kind,
            address=_address(tags), contacts=_contacts(tags),
            capacity=_capacity(tags, sub),
            footprint_m2=footprint, floors=_floors(tags),
            attributes={"osm_id": raw.get("id"), "osm_type": raw.get("type"),
                        "tags": {k: v for k, v in tags.items()
                                 if k in ("amenity", "building", "landuse", "tourism",
                                          "healthcare", "social_facility", "power",
                                          "man_made", "historic", "shop", "leisure",
                                          "operator", "emergency", "wheelchair")}},
            confidence=0.6,
        )]

    # -- Tier B machinery --------------------------------------------------------
    async def _fetch_bbox(self, bbox: tuple[float, float, float, float]) -> list[dict]:
        query = build_query(bbox, timeout=int(settings.overpass_timeout_s))
        resp = await pool().post(data={"data": query},
                                 timeout=settings.overpass_timeout_s + 30)
        return (resp.json() or {}).get("elements", [])

    async def warm(self, store, geometry, *, ttl_hours: int | None = None,
                   max_tiles: int | None = None) -> dict:
        """Ensure every tile covering ``geometry`` is fresh. Returns fetch statistics.

        Never raises on upstream failure: a dead Overpass degrades the report to the
        resident tier plus a warning, which is far more useful than a 502.
        """
        stats = {"tiles_total": 0, "tiles_fetched": 0, "tiles_cached": 0,
                 "tiles_failed": 0, "assets": 0, "networks": 0, "ms": 0.0,
                 "warnings": []}
        t_start = time.perf_counter()
        deg = settings.osm_tile_deg
        tiles = tiles_for_geometry(geometry, deg)
        stats["tiles_total"] = len(tiles)
        cap = max_tiles or settings.osm_max_tiles_per_request
        if len(tiles) > cap:
            stats["warnings"].append(
                f"AOI covers {len(tiles)} OSM tiles, above the {cap}-tile limit; "
                f"live OSM was skipped. Results use resident sources only.")
            stats["ms"] = (time.perf_counter() - t_start) * 1000
            return stats

        by_key = dict(tiles)
        states = await store.tile_states(list(by_key))
        stale = {k for k, v in states.items() if v == "stale"}
        failed = {k for k, v in states.items() if v == "failed"}
        # Only tiles we actually hold data for count as cached. A tile inside its retry
        # backoff is NOT cached - there is nothing behind it - and reporting it as such
        # turns "we could not reach OpenStreetMap" into "there is nothing here".
        stats["tiles_cached"] = sum(1 for v in states.values() if v == "fresh")
        stats["tiles_failed"] = len(failed)
        if failed:
            # Emitted on every response served while the backoff holds, not only on the
            # request that first hit the failure. The backoff suppresses the network
            # call; it must never suppress the warning.
            stats["warnings"].append(
                f"{len(failed)} of {len(tiles)} OpenStreetMap tile(s) in this area could "
                f"not be fetched recently and are within their retry backoff, so roads, "
                f"power lines and OSM-sourced assets are MISSING here rather than absent. "
                f"Retry in up to {settings.osm_error_retry_minutes} minutes.")
        if not stale:
            stats["ms"] = (time.perf_counter() - t_start) * 1000
            return stats

        # Group adjacent stale tiles so a 12-tile AOI costs a handful of requests.
        # Each group remembers which tiles it covers: a group that fails must leave
        # exactly its own tiles stale. Marking every tile 'partial' because one group
        # failed would invalidate the whole cache on every partial outage.
        groups = self._group_tiles(sorted(stale), by_key,
                                   max_groups=max(1, min(8, len(stale))))
        sem = asyncio.Semaphore(settings.overpass_max_parallel)

        async def one(bbox):
            async with sem:
                try:
                    return await self._fetch_bbox(bbox)
                except Exception as exc:
                    log.warning("overpass group failed %s: %s", bbox, exc)
                    return exc

        # Bound the whole warm-up. Outstanding requests are cancelled rather than
        # allowed to hold up the response; their tiles simply stay stale and are
        # retried on a later request, which the tile cache makes cheap.
        tasks = [asyncio.ensure_future(one(bbox)) for bbox, _ in groups]
        timed_out = False
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True),
                                   timeout=settings.osm_deadline_s)
        except asyncio.TimeoutError:
            timed_out = True
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        results: list[Any] = []
        for t in tasks:
            if t.cancelled():
                results.append(asyncio.TimeoutError("deadline exceeded"))
            else:
                exc = t.exception()
                results.append(exc if exc else t.result())
        if timed_out:
            stats["warnings"].append(
                f"OpenStreetMap fetch exceeded the {settings.osm_deadline_s:.0f}s budget; "
                f"unfinished tiles were abandoned and will be retried on the next "
                f"request. Coverage in this area may be partial.")

        elements: list[dict] = []
        fresh_keys: list[str] = []
        failed_keys: list[str] = []
        failures: list[Exception] = []
        for (bbox, keys), res in zip(groups, results):
            if isinstance(res, Exception):
                failures.append(res)
                failed_keys.extend(keys)
            else:
                elements.extend(res)
                fresh_keys.extend(keys)

        if failures:
            stats["warnings"].append(
                f"{len(failures)}/{len(groups)} OpenStreetMap requests failed "
                f"({type(failures[0]).__name__}); {len(failed_keys)} tile(s) in this area "
                f"were not refreshed and will be retried after a short backoff.")
            # Record the failure so the backoff in stale_tiles can see it. Without this a
            # tile that cannot be fetched is simply absent from the cache, so it is stale
            # on every request for ever, and each of those requests pays the full deadline
            # retrying it. One dead tile would quietly make a whole area permanently slow.
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            await store.mark_tiles([
                {"tile_key": k, "min_lon": by_key[k][0], "min_lat": by_key[k][1],
                 "max_lon": by_key[k][2], "max_lat": by_key[k][3], "fetched_at": now,
                 "status": "error", "feature_count": 0, "network_count": 0,
                 "error": str(failures[0])[:200]} for k in failed_keys])

        if not fresh_keys:
            stats["ms"] = (time.perf_counter() - t_start) * 1000
            return stats

        assets, networks = self._elements_to_rows(elements, deg, by_key)
        # Only clear tiles we actually refetched, or a failed group would wipe good data.
        await store.clear_tile_assets(fresh_keys)
        assets = [a for a in assets if a["tile_key"] in set(fresh_keys)]
        networks = [n for n in networks if n["tile_key"] in set(fresh_keys)]
        if assets:
            stats["assets"] = await store.upsert_assets(assets)
        if networks:
            stats["networks"] = await store.upsert_networks(networks)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        feature_counts: dict[str, int] = {}
        for a in assets:
            feature_counts[a["tile_key"]] = feature_counts.get(a["tile_key"], 0) + 1
        await store.mark_tiles([
            {"tile_key": k, "min_lon": by_key[k][0], "min_lat": by_key[k][1],
             "max_lon": by_key[k][2], "max_lat": by_key[k][3], "fetched_at": now,
             "status": "ok", "feature_count": feature_counts.get(k, 0),
             "network_count": 0, "error": None}
            for k in fresh_keys])
        stats["tiles_fetched"] = len(fresh_keys)
        stats["ms"] = (time.perf_counter() - t_start) * 1000
        log.info("osm warm: %s tiles (%s cached), %s assets, %s networks in %.0f ms",
                 stats["tiles_total"], stats["tiles_cached"], stats["assets"],
                 stats["networks"], stats["ms"])
        return stats

    @staticmethod
    def _group_tiles(keys: list[str], by_key: dict, max_groups: int
                     ) -> list[tuple[tuple[float, float, float, float], list[str]]]:
        """Merge adjacent stale tiles into a few bboxes, keeping the mapping back to
        tile keys so success and failure can be attributed per tile."""
        if not keys:
            return []
        ordered = sorted(keys, key=lambda k: (by_key[k][1], by_key[k][0]))
        if len(ordered) <= max_groups:
            return [(by_key[k], [k]) for k in ordered]
        per = math.ceil(len(ordered) / max_groups)
        out = []
        for i in range(0, len(ordered), per):
            chunk = ordered[i:i + per]
            boxes = [by_key[k] for k in chunk]
            out.append(((min(b[0] for b in boxes), min(b[1] for b in boxes),
                         max(b[2] for b in boxes), max(b[3] for b in boxes)), chunk))
        return out

    def _tile_key_for(self, lon: float, lat: float, deg: float) -> str:
        return f"{deg:g}/{math.floor(lon / deg)}/{math.floor(lat / deg)}"

    def _elements_to_rows(self, elements: list[dict], deg: float,
                          by_key: dict) -> tuple[list[dict], list[dict]]:
        """Convert Overpass elements into asset and network rows.

        De-duplicates by OSM id: a feature tagged on a way appears in both the POI and
        the linear block, and the variant carrying real geometry always wins.
        """
        seen: dict[str, dict] = {}
        for el in elements:
            ref = f"{el.get('type')}/{el.get('id')}"
            prev = seen.get(ref)
            if prev is None or (not prev.get("geometry") and el.get("geometry")):
                seen[ref] = el

        assets: list[dict] = []
        networks: list[dict] = []
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        for ref, el in seen.items():
            tags = el.get("tags") or {}
            if not tags:
                continue
            g = _element_geometry(el)
            if not g:
                continue
            lon, lat, wkt, kind, footprint = g
            tile = self._tile_key_for(lon, lat, deg)

            linear_sub = classify_linear(tags)
            if linear_sub and kind == "line":
                spec = get_subcategory(linear_sub)
                networks.append({
                    "id": asset_id("osm", ref), "source_id": "osm", "source_ref": ref,
                    "category": spec.category, "subcategory": spec.key,
                    "name": clean_text(tags.get("name") or tags.get("ref")),
                    "wkt": wkt,
                    "attributes": {"osm_id": el.get("id"), "highway": tags.get("highway"),
                                   "ref": tags.get("ref"), "surface": tags.get("surface"),
                                   "maxspeed": tags.get("maxspeed"),
                                   "bridge": tags.get("bridge"),
                                   "tunnel": tags.get("tunnel"),
                                   "power": tags.get("power"),
                                   "railway": tags.get("railway")},
                    "tile_key": tile, "retrieved_at": now,
                })
                continue

            sub = classify(tags)
            if not sub:
                continue
            spec = get_subcategory(sub)
            assets.append({
                "id": asset_id("osm", ref), "source_id": "osm", "source_ref": ref,
                "category": spec.category, "subcategory": spec.key,
                "name": clean_text(tags.get("name") or tags.get("official_name")),
                "wkt": wkt, "lon": lon, "lat": lat, "geometry_kind": kind,
                "address": _address(tags), "contacts": _contacts(tags),
                "capacity": _capacity(tags, sub),
                "attributes": {"osm_id": el.get("id"), "osm_type": el.get("type"),
                               "tags": {k: v for k, v in tags.items() if k in (
                                   "amenity", "building", "landuse", "tourism",
                                   "healthcare", "social_facility", "power", "man_made",
                                   "historic", "shop", "leisure", "emergency",
                                   "operator", "wheelchair", "access")}},
                "footprint_m2": footprint, "floors": _floors(tags),
                "confidence": 0.6, "tile_key": tile, "retrieved_at": now,
            })
        return assets, networks
