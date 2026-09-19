"""Resident population exposure.

Population is the one layer that must never be monetised and never be presented as
precise. TALAIA reports census residents, area-weighted from grid cells to the AOI, and
states the method and its limits on every response.

Two paths, in order of preference:

1. **INE 1 km2 census grid** when the AOI is covered by it - area-weighted overlay.
2. **Dwelling-based fallback** derived from residential assets in the AOI, used only when
   no grid coverage exists, at markedly lower confidence.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from shapely.geometry import Point, shape

from ..core_shim import cell_overlap_fractions, polygon_rings
from ..models import PopulationCell, PopulationResult

log = logging.getLogger("talaia.population")

# Mean household size in Spain (INE). Used only by the fallback path.
PEOPLE_PER_DWELLING = 2.47


# A 1 km grid over a large AOI is a lot of rows. 5,000 cells is 5,000 km2 of ground,
# well past any single fire, and keeps the response to something a client can hold.
MAX_CELLS = 5_000


def _build_cells(rows: list[dict], bands, *, with_geometry: bool
                 ) -> tuple[list[PopulationCell], bool, float]:
    """Turn raw grid rows into the per-cell breakdown, densest first.

    Density is the cell's own figure - population over the cell's full area - not the
    clipped share over the clipped area. A cell half inside the AOI describes the same
    neighbourhood as a cell fully inside it; scaling its density by the overlap would
    invent a gradient at the AOI edge that does not exist on the ground.
    """
    out: list[PopulationCell] = []
    peak = 0.0
    # Bands are ordered earliest-first, so the first hit is the earliest arrival.
    ordered = list(bands) if len(bands) > 1 else []
    for row in rows:
        frac = row.get("frac")
        if frac is None or row.get("lon") is None:
            continue
        frac = max(0.0, min(1.0, float(frac)))
        pop = float(row["population"])
        area_km2 = float(row.get("area_m2") or 1e6) / 1e6
        density = pop / area_km2 if area_km2 > 0 else 0.0
        peak = max(peak, density)
        lon, lat = float(row["lon"]), float(row["lat"])
        band = None
        if ordered:
            point = Point(lon, lat)
            for candidate in ordered:
                if candidate.geometry.covers(point):
                    band = candidate.label
                    break
        geometry = None
        if with_geometry:
            try:
                geometry = json.loads(row["geojson"])
            except Exception:
                geometry = None
        out.append(PopulationCell(
            cell_id=row["cell_id"], lon=lon, lat=lat,
            population=round(pop, 1), population_in_aoi=round(pop * frac, 1),
            overlap_fraction=round(frac, 4), density_per_km2=round(density, 1),
            area_km2=round(area_km2, 4), band=band, geometry=geometry))

    # Densest first: if the list has to be cut, the cells that matter for evacuation
    # load are the ones that survive.
    out.sort(key=lambda c: -c.density_per_km2)
    truncated = len(out) > MAX_CELLS
    return out[:MAX_CELLS], truncated, peak


async def population_for(store, aoi, bands, *, include_cells: bool = False,
                         with_geometry: bool = False) -> PopulationResult:
    """Area-weighted resident population for the AOI and each band."""
    cells = await store.query_population(aoi.wkt)
    if cells:
        total = 0.0
        for c in cells:
            frac = c.get("frac")
            if frac is None:
                continue
            total += float(c["population"]) * max(0.0, min(1.0, float(frac)))

        by_band: dict[str, float] = {}
        if len(bands) > 1:
            boxes, pops = [], []
            for c in cells:
                try:
                    geom = shape(json.loads(c["geojson"]))
                except Exception:
                    continue
                boxes.append(geom.bounds)
                pops.append(float(c["population"]))

            # Bands from a spread model are nested: the 6 h perimeter contains the 1 h
            # one. Overlaying each band as published would report the same residents in
            # every band, while assets are assigned to the earliest band only - so the
            # two columns of the report would not add up the same way. Each band is
            # therefore reduced to the ground it adds over all earlier bands, making
            # population exclusive and directly comparable with asset counts.
            seen = None
            for band in bands:
                exclusive = band.geometry if seen is None else band.geometry.difference(seen)
                seen = band.geometry if seen is None else seen.union(band.geometry)
                acc = 0.0
                if not exclusive.is_empty:
                    for poly in polygon_rings(exclusive):
                        fracs = cell_overlap_fractions(boxes, poly, 10)
                        acc += sum(p * f for p, f in zip(pops, fracs))
                by_band[band.label] = round(min(acc, total), 1)

        detail: list[PopulationCell] = []
        truncated = False
        peak = 0.0
        if include_cells:
            detail, truncated, peak = _build_cells(cells, bands,
                                                   with_geometry=with_geometry)
        else:
            for c in cells:
                area_km2 = float(c.get("area_m2") or 1e6) / 1e6
                if area_km2 > 0:
                    peak = max(peak, float(c["population"]) / area_km2)

        return PopulationResult(
            total=round(total, 1), method="ine_grid_area_weighted",
            cell_count=len(cells), confidence=0.75, by_band=by_band,
            cells=detail, cells_truncated=truncated,
            peak_density_per_km2=round(peak, 1))

    return PopulationResult(total=0.0, method="no_grid_coverage", cell_count=0,
                            confidence=0.0,
                            note="No census population grid covers this area. Use the "
                                 "residential asset counts as a rough proxy.")


def dwelling_fallback(assets: list[dict]) -> float:
    """Rough resident estimate from residential assets, when no grid exists."""
    total = 0.0
    for a in assets:
        if a.get("category") != "residential":
            continue
        cap = a.get("capacity") or {}
        if cap.get("people"):
            total += float(cap["people"])
        elif cap.get("dwellings"):
            total += float(cap["dwellings"]) * PEOPLE_PER_DWELLING
    return round(total, 1)
