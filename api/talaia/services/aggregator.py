"""Request orchestration: polygon in, ExposureReport out.

Implements the lifecycle documented in ARCHITECTURE.md §9. Everything that can fail
independently degrades into ``warnings`` rather than failing the request: a dead
Overpass, an uncovered AOI or a missing population grid all still produce a usable
report, because a partial answer during an incident beats an error page.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from ..config import settings
from ..core_shim import assign_bands, distance_to_front, front_segments, impl, polygon_rings, rollup
from ..geo import AOI, length_km, parse_aoi
from ..models import (Address, Asset, BandSummary, Capacity, CategorySummary, Contacts,
                      ExposureInfo, ExposureReport, ExposureRequest, NetworkClass,
                      NetworkResult, Provenance, ReportSummary, Timing)
from ..taxonomy import (CATEGORIES, HAZARDOUS, RESPONSE_ASSETS, get_subcategory,
                        resolve_layers)
from .conflation import conflate
from .population import population_for
from .scoring import people_estimate, priority
from .valuation import estimate

log = logging.getLogger("talaia.aggregator")

LINEAR_CATEGORIES = {"transport", "energy"}


def _loads(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        out = json.loads(value)
        return out if isinstance(out, dict) else {}
    except (TypeError, ValueError):
        return {}


async def build_report(req: ExposureRequest, store) -> ExposureReport:
    t0 = time.perf_counter()
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    warnings: list[str] = []
    timing = Timing(core_impl=impl())

    # -- 1. AOI ------------------------------------------------------------
    aoi: AOI = parse_aoi(req.aoi, buffer_metres=req.buffer_m,
                         band_property=req.band_property,
                         minutes_property=req.minutes_property)
    if aoi.area_km2 > settings.max_aoi_km2:
        raise ValueError(f"AOI area {aoi.area_km2:,.0f} km2 exceeds the "
                         f"{settings.max_aoi_km2:,.0f} km2 limit")

    warnings.extend(aoi.notes)

    # -- 2. layers ---------------------------------------------------------
    categories = resolve_layers(req.layers)

    # -- 3. Tier B warm-up (OSM) -------------------------------------------
    live_osm = settings.enable_live_osm if req.live_osm is None else req.live_osm
    if live_osm:
        from ..connectors.osm.overpass import OpenStreetMap
        try:
            stats = await OpenStreetMap().warm(store, aoi.union)
            timing.osm_fetch_ms = round(stats["ms"], 1)
            timing.tiles_total = stats["tiles_total"]
            timing.tiles_fetched = stats["tiles_fetched"]
            timing.tiles_cached = stats["tiles_cached"]
            timing.tiles_failed = stats.get("tiles_failed", 0)
            warnings.extend(stats["warnings"])
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("osm warm failed")
            warnings.append(f"Live OpenStreetMap fetch failed ({type(exc).__name__}); "
                            f"results use locally held sources only.")

    # -- 4. Tier A query (+ population concurrently) -----------------------
    t = time.perf_counter()
    asset_task = store.query_assets(aoi.wkt, aoi.bbox, sorted(categories),
                                    limit=settings.max_assets_returned)
    pop_task = (population_for(store, aoi, aoi.bands,
                               include_cells=req.include_population_grid,
                               with_geometry=req.include_population_grid
                               and req.include_geometry)
                if req.include_population else asyncio.sleep(0, result=None))
    net_task = (store.query_networks(aoi.wkt, aoi.bbox)
                if req.include_networks else asyncio.sleep(0, result=[]))
    (rows, strategy), population, net_rows = await asyncio.gather(
        asset_task, pop_task, net_task)
    timing.store_query_ms = round((time.perf_counter() - t) * 1000, 1)

    if len(rows) >= settings.max_assets_returned:
        warnings.append(
            f"Asset query hit the {settings.max_assets_returned:,} row cap; "
            f"the report is truncated. Use a smaller AOI or fewer layers.")

    # -- 5. normalise rows --------------------------------------------------
    parsed: list[dict] = []
    for r in rows:
        parsed.append({
            "id": r["id"], "source_id": r["source_id"], "source_ref": r["source_ref"],
            "category": r["category"], "subcategory": r["subcategory"],
            "name": r["name"], "geojson": r["geojson"], "lon": r["lon"], "lat": r["lat"],
            "geometry_kind": r["geometry_kind"],
            "address": _loads(r["address"]), "contacts": _loads(r["contacts"]),
            "capacity": _loads(r["capacity"]), "attributes": _loads(r["attributes"]),
            "footprint_m2": r["footprint_m2"], "floors": r["floors"],
            "confidence": r["confidence"], "retrieved_at": r["retrieved_at"],
        })

    # -- 6. conflate --------------------------------------------------------
    t = time.perf_counter()
    if req.conflate:
        parsed, conflation_stats = conflate(parsed)
    else:
        conflation_stats = {"input": len(parsed), "output": len(parsed), "merged": 0}
        for a in parsed:
            a["_provenance"] = [{"source_id": a["source_id"],
                                 "source_ref": a.get("source_ref"),
                                 "retrieved_at": a.get("retrieved_at"),
                                 "fields": ["identity", "geometry"]}]
            a["merged_count"] = 1
            a["possible_duplicate_of"] = []
    timing.conflation_ms = round((time.perf_counter() - t) * 1000, 1)

    # -- 7. band assignment + distance to front (numeric core) --------------
    t = time.perf_counter()
    polys: list[Any] = []
    poly_band: list[int] = []
    for band in aoi.bands:
        for poly in polygon_rings(band.geometry):
            polys.append(poly)
            poly_band.append(band.index)

    xs = [a["lon"] or 0.0 for a in parsed]
    ys = [a["lat"] or 0.0 for a in parsed]
    band_idx_raw = assign_bands(xs, ys, polys) if polys and parsed else [-1] * len(parsed)
    band_of = [poly_band[i] if i >= 0 else -1 for i in band_idx_raw]

    # The "front" is the boundary of the earliest band - the fire's current edge.
    segments = front_segments(aoi.bands[0].geometry) if aoi.bands else []
    distances = distance_to_front(xs, ys, segments) if parsed and segments else \
        [None] * len(parsed)

    # -- 8. valuation + scoring ---------------------------------------------
    n_bands = len(aoi.bands)
    assets: list[Asset] = []
    registry_people_total = 0.0
    default_people_total = 0.0
    for i, a in enumerate(parsed):
        spec = get_subcategory(a["subcategory"])
        val = estimate(subcategory=a["subcategory"], footprint_m2=a["footprint_m2"],
                       floors=a["floors"], address=a["address"],
                       capacity=a["capacity"], attributes=a["attributes"],
                       geometry_kind=a["geometry_kind"])
        people, basis, is_default = people_estimate(a["subcategory"], a["capacity"])
        bi = band_of[i]
        band = aoi.bands[bi] if bi >= 0 else None
        score = priority(subcategory=a["subcategory"], people=people,
                         total_value_eur=val.total_eur,
                         band_minutes=band.minutes if band else None,
                         band_index=bi if bi >= 0 else None, n_bands=n_bands)
        cap = dict(a["capacity"])
        cap.setdefault("people", people or None)
        cap.setdefault("basis", basis)
        if is_default:
            default_people_total += people
            # `is_default` describes the PEOPLE estimate, not the whole capacity record.
            # A REGA holding publishes a real headcount and only its human occupancy is
            # guessed; overwriting the source's own confidence here rated a registry
            # figure exactly as trustworthy as a guess, and made the field carry no
            # information at all. Only fill it in when the source gave us nothing.
            cap.setdefault("confidence", 0.2)
        else:
            registry_people_total += people

        geometry = None
        if req.include_geometry and a["geojson"]:
            try:
                geometry = json.loads(a["geojson"])
            except ValueError:
                geometry = None

        dist = distances[i] if i < len(distances) else None
        assets.append(Asset(
            id=a["id"], category=a["category"], subcategory=spec.key, name=a["name"],
            geometry=geometry, geometry_kind=a["geometry_kind"],
            lon=a["lon"], lat=a["lat"],
            address=Address(**a["address"]) if a["address"] else None,
            contacts=Contacts(**{k: v for k, v in a["contacts"].items()
                                 if k in Contacts.model_fields}),
            capacity=Capacity(**{k: v for k, v in cap.items()
                                 if k in Capacity.model_fields}),
            valuation=val,
            vulnerability=spec.vulnerability, criticality=spec.criticality,
            hazardous=spec.key in HAZARDOUS,
            response_asset=spec.key in RESPONSE_ASSETS,
            human_bearing=spec.human_bearing,
            exposure=ExposureInfo(
                band=band.label if band else None,
                band_index=bi if bi >= 0 else None,
                band_minutes=band.minutes if band else None,
                distance_to_front_m=(round(dist, 1)
                                     if dist is not None and dist != float("inf")
                                     else None),
                inside_aoi=bi >= 0, priority_score=score),
            occupancy_note=(spec.notes or None) if spec.human_bearing else None,
            provenance=[Provenance(**p) for p in a.get("_provenance", [])],
            confidence=a["confidence"], merged_count=a.get("merged_count", 1),
            possible_duplicate_of=a.get("possible_duplicate_of", []),
            attributes=a["attributes"],
        ))
    timing.scoring_ms = round((time.perf_counter() - t) * 1000, 1)

    # -- 9. rollups ----------------------------------------------------------
    cat_keys = sorted(CATEGORIES)
    cat_index = {c: i for i, c in enumerate(cat_keys)}
    vsum, psum, counts = rollup(
        [a.exposure.band_index if a.exposure.band_index is not None else -1
         for a in assets],
        [cat_index.get(a.category, -1) for a in assets],
        [a.valuation.total_eur for a in assets],
        [a.capacity.people or 0.0 for a in assets],
        max(n_bands, 1), len(cat_keys))

    band_summaries: list[BandSummary] = []
    for band in aoi.bands:
        base = band.index * len(cat_keys)
        by_cat = {cat_keys[c]: counts[base + c] for c in range(len(cat_keys))
                  if counts[base + c]}
        val_by_cat = {cat_keys[c]: round(vsum[base + c], 2) for c in range(len(cat_keys))
                      if vsum[base + c]}
        members = [a for a in assets if a.exposure.band_index == band.index]
        band_summaries.append(BandSummary(
            band=band.label, band_index=band.index, minutes=band.minutes,
            area_km2=round(band.area_km2, 3),
            asset_count=sum(by_cat.values()),
            people_estimate=round(sum(psum[base:base + len(cat_keys)]), 1),
            population_resident=(population.by_band.get(band.label, 0.0)
                                 if population else 0.0),
            total_value_eur=round(sum(vsum[base:base + len(cat_keys)]), 2),
            by_category=by_cat, value_by_category=val_by_cat,
            critical_assets=sum(1 for a in members if a.criticality >= 90),
            hazardous_assets=sum(1 for a in members if a.hazardous)))

    by_category = []
    for key in cat_keys:
        members = [a for a in assets if a.category == key]
        if not members:
            continue
        by_category.append(CategorySummary(
            category=key, label=CATEGORIES[key].label, count=len(members),
            people_estimate=round(sum(a.capacity.people or 0 for a in members), 1),
            total_value_eur=round(sum(a.valuation.total_eur for a in members), 2),
            human_bearing=CATEGORIES[key].human_bearing))
    by_category.sort(key=lambda c: -c.total_value_eur)

    # -- 10. networks --------------------------------------------------------
    networks = None
    if req.include_networks:
        by_class: dict[str, NetworkClass] = {}
        features = []
        for n in net_rows:
            sub = n["subcategory"]
            spec = get_subcategory(sub)
            entry = by_class.setdefault(sub, NetworkClass(subcategory=sub,
                                                          label=spec.label))
            entry.length_km += (n["clipped_m"] or 0.0) / 1000.0
            entry.feature_count += 1
            if req.include_geometry and n["geojson"]:
                try:
                    features.append({"type": "Feature",
                                     "geometry": json.loads(n["geojson"]),
                                     "properties": {"subcategory": sub,
                                                    "name": n["name"]}})
                except ValueError:
                    pass
        for entry in by_class.values():
            entry.length_km = round(entry.length_km, 3)
        networks = NetworkResult(
            by_class=sorted(by_class.values(), key=lambda c: -c.length_km),
            total_length_km=round(sum(c.length_km for c in by_class.values()), 3),
            geojson=({"type": "FeatureCollection", "features": features}
                     if features else None))

    # -- 11. sort, truncate, assemble ----------------------------------------
    if req.sort_by == "priority":
        assets.sort(key=lambda a: -a.exposure.priority_score)
    elif req.sort_by == "value":
        assets.sort(key=lambda a: -a.valuation.total_eur)
    elif req.sort_by == "distance":
        assets.sort(key=lambda a: (a.exposure.distance_to_front_m is None,
                                   a.exposure.distance_to_front_m or 0))
    else:
        assets.sort(key=lambda a: (a.category, a.subcategory, a.name or ""))

    truncated = False
    limit = req.max_assets or settings.max_assets_returned
    if len(assets) > limit:
        assets = assets[:limit]
        truncated = True

    summary = ReportSummary(
        asset_count=len(parsed),
        people_estimate=round(sum(psum), 1),
        people_from_registry=round(registry_people_total, 1),
        people_from_defaults=round(default_people_total, 1),
        population_resident=population.total if population else 0.0,
        total_value_eur=round(sum(vsum), 2),
        aoi_area_km2=round(aoi.area_km2, 3),
        critical_assets=sum(1 for a in assets if a.criticality >= 90),
        hazardous_assets=sum(1 for a in assets if a.hazardous),
        response_assets=sum(1 for a in assets if a.response_asset),
        livestock_units=round(sum(a.capacity.livestock_units or 0 for a in assets), 1),
        by_category=by_category,
        top_priority=[{"id": a.id, "name": a.name, "subcategory": a.subcategory,
                       "priority_score": a.exposure.priority_score,
                       "people": a.capacity.people, "band": a.exposure.band,
                       "phone": (a.contacts.phone or [None])[0]}
                      for a in sorted(assets,
                                      key=lambda x: -x.exposure.priority_score)[:10]],
        coverage_regime=_coverage_regime(aoi.bbox, warnings))

    if conflation_stats.get("merged"):
        warnings.append(
            f"Conflation merged {conflation_stats['merged']} duplicate record(s) across "
            f"sources into {conflation_stats['output']} distinct assets.")

    if population is not None and population.cells_truncated:
        warnings.append(
            f"Population grid truncated to the {len(population.cells):,} densest cells of "
            f"{population.cell_count:,}. 'total' still counts every cell; only the "
            f"per-cell list is cut.")

    from ..connectors import registry
    sources = [c.meta for c in registry.all_connectors()
               if not c.meta.categories or categories.intersection(c.meta.categories)]

    timing.total_ms = round((time.perf_counter() - t0) * 1000, 1)
    return ExposureReport(
        request_id=request_id, generated_at=datetime.now(timezone.utc),
        aoi_bbox=[round(v, 6) for v in aoi.bbox],
        bands=band_summaries, summary=summary,
        population=population or None, networks=networks,
        assets=assets if req.include_assets else [],
        assets_truncated=truncated, sources=sources,
        warnings=warnings, timing=timing)


def _coverage_regime(bbox, warnings: list[str]) -> str:
    """Tell the caller how deep the data is here, rather than letting them assume."""
    from ..connectors.base import CATALONIA, SPAIN
    if CATALONIA.contains(bbox):
        return "catalonia_full: regional registries + national sources + OpenStreetMap"
    if SPAIN.contains(bbox):
        return "spain_national: national registries + OpenStreetMap"
    warnings.append("This AOI is outside Spain; only OpenStreetMap covers it. "
                    "Registry-derived capacity, contacts and valuations are unavailable.")
    return "osm_only: OpenStreetMap coverage only"
