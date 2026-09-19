"""Named regions for bulk cache warming.

A gazetteer, not a boundary dataset. Each entry is a bounding box, which is the right
shape for the job: the warmer tiles a bbox, and tiles that fall in the sea or over the
border cost one empty Overpass response each and then stay cached as empty. Carrying
real administrative polygons would add a dependency and a download to save a few
percent of requests.

Boxes are deliberately generous at the edges - a fire near a regional border needs the
tiles on the far side too.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import settings

BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class Region:
    key: str
    name: str
    bbox: BBox          # (min_lon, min_lat, max_lon, max_lat)
    note: str = ""

    @property
    def width_deg(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height_deg(self) -> float:
        return self.bbox[3] - self.bbox[1]

    def tile_count(self, deg: float | None = None) -> int:
        """Tiles in the bbox cover. This is the number that decides feasibility."""
        deg = deg or settings.osm_tile_deg
        x0, y0, x1, y1 = self.bbox
        nx = math.floor(x1 / deg) - math.floor(x0 / deg) + 1
        ny = math.floor(y1 / deg) - math.floor(y0 / deg) + 1
        return nx * ny

    def area_km2(self) -> float:
        x0, y0, x1, y1 = self.bbox
        k = math.cos(math.radians((y0 + y1) / 2))
        return (x1 - x0) * k * 111.32 * (y1 - y0) * 111.32


# Autonomous communities and the demo cities. Catalonia is split into provinces because
# warming the whole community in one go is an overnight job, while one province is not.
REGIONS: dict[str, Region] = {r.key: r for r in [
    # -- demo-scale: a city and its wildland-urban interface ------------------
    Region("barcelona", "Barcelona metropolitan area", (1.85, 41.25, 2.40, 41.62),
           "Includes Collserola, where the urban interface actually burns."),
    Region("girona", "Girona and the Gavarres", (2.55, 41.80, 3.10, 42.15)),
    Region("tarragona", "Tarragona and the Camp", (0.90, 40.95, 1.45, 41.35)),
    Region("lleida", "Lleida city and plain", (0.45, 41.45, 0.90, 41.80)),
    Region("emporda", "Alt and Baix Emporda", (2.75, 41.90, 3.30, 42.40),
           "Tramuntana-driven fire country."),
    Region("valencia", "Valencia metropolitan area", (-0.55, 39.30, -0.20, 39.60)),
    Region("madrid", "Madrid metropolitan area", (-4.00, 40.20, -3.40, 40.70)),
    Region("sevilla", "Seville metropolitan area", (-6.20, 37.20, -5.75, 37.55)),
    Region("zaragoza", "Zaragoza", (-1.05, 41.50, -0.70, 41.78)),
    Region("bilbao", "Bilbao and the Nervion", (-3.15, 43.15, -2.75, 43.40)),

    # -- province scale --------------------------------------------------------
    Region("prov_barcelona", "Province of Barcelona", (1.35, 41.20, 2.85, 42.35)),
    Region("prov_girona", "Province of Girona", (2.05, 41.60, 3.35, 42.55)),
    Region("prov_tarragona", "Province of Tarragona", (0.20, 40.50, 1.75, 41.60)),
    Region("prov_lleida", "Province of Lleida", (0.30, 41.20, 1.80, 42.90)),

    # -- autonomous communities -------------------------------------------------
    Region("catalunya", "Catalonia", (0.15, 40.50, 3.35, 42.90),
           "Where every resident registry in this build has coverage."),
    Region("comunitat_valenciana", "Valencian Community", (-1.55, 37.80, 0.75, 40.80)),
    Region("aragon", "Aragon", (-2.20, 39.85, 0.80, 42.95)),
    Region("illes_balears", "Balearic Islands", (1.15, 38.60, 4.35, 40.10)),
    Region("madrid_ccaa", "Community of Madrid", (-4.60, 39.85, -3.05, 41.20)),
    Region("andalucia", "Andalusia", (-7.55, 36.00, -1.60, 38.75)),
    Region("galicia", "Galicia", (-9.35, 41.80, -6.70, 43.80),
           "The highest fire-count region in Spain."),
    Region("castilla_leon", "Castile and Leon", (-7.10, 40.05, -1.75, 43.25)),
    Region("castilla_mancha", "Castile-La Mancha", (-5.45, 37.95, -0.90, 41.35)),
    Region("euskadi", "Basque Country", (-3.45, 42.45, -1.70, 43.50)),
    Region("navarra", "Navarre", (-2.55, 41.90, -0.70, 43.35)),
    Region("asturias", "Asturias", (-7.25, 42.85, -4.50, 43.70)),
    Region("cantabria", "Cantabria", (-4.90, 42.75, -3.10, 43.55)),
    Region("murcia", "Region of Murcia", (-2.35, 37.35, -0.65, 38.80)),
    Region("extremadura", "Extremadura", (-7.55, 37.95, -4.60, 40.50)),
    Region("la_rioja", "La Rioja", (-3.15, 41.90, -1.65, 42.65)),
    Region("canarias", "Canary Islands", (-18.20, 27.55, -13.35, 29.50)),

    # -- country ---------------------------------------------------------------
    Region("spain_mainland", "Peninsular Spain", (-9.40, 35.90, 3.40, 43.85),
           "Very large. Check the estimate before starting."),
    Region("spain", "Spain including the islands", (-18.20, 27.55, 4.40, 43.85),
           "Very large. Check the estimate before starting."),
]}

# Convenience groupings, expanded to their members.
GROUPS: dict[str, list[str]] = {
    "demo": ["barcelona", "girona", "emporda", "tarragona"],
    "catalunya_provinces": ["prov_barcelona", "prov_girona", "prov_tarragona",
                            "prov_lleida"],
}


def parse_bbox(text: str) -> BBox | None:
    """Accept ``min_lon,min_lat,max_lon,max_lat``. Returns None if it is not one."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(p) for p in parts)
    except ValueError:
        return None
    if not (-180 <= x0 < x1 <= 180 and -90 <= y0 < y1 <= 90):
        return None
    return (x0, y0, x1, y1)


def resolve(name: str) -> list[Region]:
    """Resolve a region name, group name or raw bbox into concrete regions."""
    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    if key in GROUPS:
        return [REGIONS[k] for k in GROUPS[key]]
    if key in REGIONS:
        return [REGIONS[key]]
    bbox = parse_bbox(name)
    if bbox:
        return [Region(key="bbox", name=f"bbox {name}", bbox=bbox)]
    raise KeyError(
        f"Unknown region {name!r}. Known regions: {', '.join(sorted(REGIONS))}. "
        f"Groups: {', '.join(sorted(GROUPS))}. "
        f"Or give a bbox as 'min_lon,min_lat,max_lon,max_lat'.")


def resolve_many(names: list[str]) -> list[Region]:
    """Resolve several names, de-duplicated, keeping the order given."""
    out: list[Region] = []
    seen: set[str] = set()
    for name in names:
        for region in resolve(name):
            ident = f"{region.key}:{region.bbox}"
            if ident not in seen:
                seen.add(ident)
                out.append(region)
    return out


def catalogue() -> list[dict]:
    """Every region with its size, for the CLI listing and the admin endpoint."""
    return [{"key": r.key, "name": r.name, "bbox": list(r.bbox),
             "tiles": r.tile_count(), "area_km2": round(r.area_km2()),
             "note": r.note}
            for r in sorted(REGIONS.values(), key=lambda r: r.tile_count())]
