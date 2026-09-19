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

from shapely.geometry import shape

from ..core_shim import cell_overlap_fractions, polygon_rings
from ..models import PopulationResult

log = logging.getLogger("talaia.population")

# Mean household size in Spain (INE). Used only by the fallback path.
PEOPLE_PER_DWELLING = 2.47


async def population_for(store, aoi, bands) -> PopulationResult:
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

        return PopulationResult(
            total=round(total, 1), method="ine_grid_area_weighted",
            cell_count=len(cells), confidence=0.75, by_band=by_band)

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
