"""Geometry utilities: AOI parsing, time bands, metric buffering, tiling.

All geometry is WGS84 lon/lat. Metric operations use a local equirectangular
approximation (scale longitude by cos(latitude)), which is accurate to well under a
percent at the scale of a wildfire AOI and avoids a projection dependency.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from shapely import wkt as shapely_wkt
from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

EARTH_R = 6_371_008.8
DEG_LAT_M = 111_320.0

# Checked before anything reaches shapely. Without this an unknown type raises
# GeometryTypeError, which is not a ValueError, so it escaped the router's 422 handling
# and surfaced as a 500 with the exception class in the body.
ACCEPTED_GEOMETRY_TYPES = frozenset({
    "Polygon", "MultiPolygon", "Feature", "FeatureCollection",
    "GeometryCollection", "Point", "MultiPoint", "LineString", "MultiLineString",
})


# ---------------------------------------------------------------------------
# Bands
# ---------------------------------------------------------------------------
@dataclass
class Band:
    """One time-banded fire perimeter (or the single AOI when no bands are given)."""
    label: str
    index: int
    geometry: BaseGeometry
    minutes: float | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def area_km2(self) -> float:
        return area_km2(self.geometry)


@dataclass
class AOI:
    """A parsed area of interest: the union plus its ordered bands."""
    union: BaseGeometry
    bands: list[Band]
    buffered_m: float = 0.0
    # Ways the input was altered to make it usable. Surfaced on the response, because a
    # caller whose geometry we silently changed is reasoning about a different shape
    # than the one we measured.
    notes: list[str] = field(default_factory=list)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return tuple(self.union.bounds)  # type: ignore[return-value]

    @property
    def wkt(self) -> str:
        return self.union.wkt

    @property
    def area_km2(self) -> float:
        return area_km2(self.union)

    @property
    def is_banded(self) -> bool:
        return len(self.bands) > 1


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------
def area_km2(geom: BaseGeometry) -> float:
    """Area via a local equirectangular projection about the geometry's centroid."""
    if geom.is_empty:
        return 0.0
    lat0 = geom.centroid.y
    k = math.cos(math.radians(lat0))
    projected = transform(lambda x, y, z=None: (x * k * DEG_LAT_M, y * DEG_LAT_M), geom)
    return abs(projected.area) / 1e6


def length_km(geom: BaseGeometry) -> float:
    if geom.is_empty:
        return 0.0
    lat0 = geom.centroid.y
    k = math.cos(math.radians(lat0))
    projected = transform(lambda x, y, z=None: (x * k * DEG_LAT_M, y * DEG_LAT_M), geom)
    return projected.length / 1000.0


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def buffer_m(geom: BaseGeometry, metres: float) -> BaseGeometry:
    """Buffer by an approximate metric distance, correcting for longitude convergence."""
    if metres <= 0 or geom.is_empty:
        return geom
    lat0 = geom.centroid.y
    k = max(math.cos(math.radians(lat0)), 1e-6)
    to_m = lambda x, y, z=None: (x * k * DEG_LAT_M, y * DEG_LAT_M)      # noqa: E731
    to_deg = lambda x, y, z=None: (x / (k * DEG_LAT_M), y / DEG_LAT_M)  # noqa: E731
    return transform(to_deg, transform(to_m, geom).buffer(metres, quad_segs=8))


# ---------------------------------------------------------------------------
# AOI parsing
# ---------------------------------------------------------------------------
def _clean(geom: BaseGeometry, notes: list[str] | None = None) -> BaseGeometry:
    """Repair self-intersections; simulation output is frequently not OGC-valid."""
    if geom.is_valid:
        return geom
    fixed = geom.buffer(0)
    if not fixed.is_empty:
        if notes is not None:
            notes.append("AOI geometry was not OGC-valid (self-intersecting or "
                         "similar) and was repaired before use.")
        return fixed
    return geom


def _ring_notes(geojson: dict, notes: list[str]) -> None:
    """Record rings that RFC 7946 would reject, which shapely silently closes.

    Accepting them is the right call for a tool fed by simulation output, but the caller
    should be told their shape was altered rather than discover it from a boundary that
    does not match theirs.
    """
    def walk(coords) -> bool:
        if not isinstance(coords, list) or not coords:
            return False
        first = coords[0]
        if isinstance(first, (int, float)):
            return False
        if isinstance(first, list) and first and isinstance(first[0], (int, float)):
            return len(coords) < 4 or list(coords[0]) != list(coords[-1])
        return any(walk(c) for c in coords)

    gtype = geojson.get("type")
    if gtype in ("Polygon", "MultiPolygon"):
        if walk(geojson.get("coordinates") or []):
            notes.append(
                "AOI ring was not closed (RFC 7946 requires at least four positions "
                "with the first equal to the last); it was closed automatically.")
    elif gtype == "Feature":
        _ring_notes(geojson.get("geometry") or {}, notes)
    elif gtype == "FeatureCollection":
        for feat in geojson.get("features") or []:
            _ring_notes((feat or {}).get("geometry") or {}, notes)


def _minutes_from(props: dict, minutes_property: str, label: str) -> float | None:
    for key in (minutes_property, "minutes", "t_minutes", "eta_minutes", "offset_minutes"):
        val = props.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    for key in ("hours", "t_hours", "hour"):
        val = props.get(key)
        if isinstance(val, (int, float)):
            return float(val) * 60.0
    # Fall back to parsing a label like "0-3h", "t+90min", "6h"
    import re
    m = re.search(r"(\d+(?:\.\d+)?)\s*h", str(label), re.I)
    if m:
        return float(m.group(1)) * 60.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:min|m)\b", str(label), re.I)
    if m:
        return float(m.group(1))
    return None


def parse_aoi(geojson: dict, *, buffer_metres: float = 0.0,
              band_property: str = "band", minutes_property: str = "minutes") -> AOI:
    """Parse a GeoJSON Geometry, Feature or FeatureCollection into an :class:`AOI`.

    A FeatureCollection whose features carry a band property is interpreted as
    time-banded fire perimeters and each feature becomes a :class:`Band`, ordered by
    minutes-to-arrival where available.
    """
    if not isinstance(geojson, dict) or "type" not in geojson:
        raise ValueError("aoi must be a GeoJSON object with a 'type' member")

    gtype = geojson["type"]
    if gtype not in ACCEPTED_GEOMETRY_TYPES:
        raise ValueError(
            f"aoi type {gtype!r} is not supported. Use one of: "
            f"{', '.join(sorted(ACCEPTED_GEOMETRY_TYPES))}.")
    notes: list[str] = []
    _ring_notes(geojson, notes)
    raw_bands: list[tuple[str, BaseGeometry, float | None, dict]] = []

    if gtype == "FeatureCollection":
        feats = geojson.get("features") or []
        if not feats:
            raise ValueError("aoi FeatureCollection contains no features")
        for i, feat in enumerate(feats):
            if not feat.get("geometry"):
                continue
            props = feat.get("properties") or {}
            label = str(props.get(band_property)
                        or props.get("label")
                        or props.get("name")
                        or f"band_{i}")
            geom = _clean(shape(feat["geometry"]), notes)
            raw_bands.append((label, geom, _minutes_from(props, minutes_property, label), props))
    elif gtype == "Feature":
        props = geojson.get("properties") or {}
        label = str(props.get(band_property) or "aoi")
        geom = _clean(shape(geojson["geometry"]), notes)
        raw_bands.append((label, geom, _minutes_from(props, minutes_property, label), props))
    else:
        raw_bands.append(("aoi", _clean(shape(geojson), notes), None, {}))

    if not raw_bands:
        raise ValueError("aoi contains no usable geometry")

    # Order by arrival time so "earliest band containing the asset" is well defined.
    if any(b[2] is not None for b in raw_bands):
        raw_bands.sort(key=lambda b: (b[2] is None, b[2] if b[2] is not None else 0.0))
    else:
        raw_bands.sort(key=lambda b: b[1].area)

    bands: list[Band] = []
    for i, (label, geom, minutes, props) in enumerate(raw_bands):
        if buffer_metres > 0:
            geom = buffer_m(geom, buffer_metres)
        bands.append(Band(label=label, index=i, geometry=geom, minutes=minutes,
                          properties=props))

    union = _clean(unary_union([b.geometry for b in bands]))
    if union.is_empty:
        raise ValueError("aoi geometry is empty")
    return AOI(union=union, bands=bands, buffered_m=buffer_metres,
               notes=list(dict.fromkeys(notes)))


# ---------------------------------------------------------------------------
# Tiling (Tier B cache keys)
# ---------------------------------------------------------------------------
def tile_key(ix: int, iy: int, deg: float) -> str:
    return f"{deg:g}/{ix}/{iy}"


def tiles_for_bounds(bounds: tuple[float, float, float, float],
                     deg: float) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Fixed-grid tile cover of a bbox. The grid is global and stable, which is what
    makes the cache hit across different but overlapping AOIs."""
    x0, y0, x1, y1 = bounds
    ix0, ix1 = math.floor(x0 / deg), math.floor(x1 / deg)
    iy0, iy1 = math.floor(y0 / deg), math.floor(y1 / deg)
    out = []
    for ix in range(ix0, ix1 + 1):
        for iy in range(iy0, iy1 + 1):
            out.append((tile_key(ix, iy, deg),
                        (ix * deg, iy * deg, (ix + 1) * deg, (iy + 1) * deg)))
    return out


def tiles_for_geometry(geom: BaseGeometry, deg: float
                       ) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Tile cover restricted to tiles the geometry actually touches.

    For a long, narrow fire perimeter this is dramatically fewer tiles than the bbox
    cover - which is the difference between one Overpass call and thirty.
    """
    out = []
    for key, bounds in tiles_for_bounds(geom.bounds, deg):
        if box(*bounds).intersects(geom):
            out.append((key, bounds))
    return out


def merge_bboxes(boxes: list[tuple[float, float, float, float]], max_groups: int = 8
                 ) -> list[tuple[float, float, float, float]]:
    """Group adjacent tile bboxes into a few larger requests.

    Overpass round-trips dominate cold-AOI latency, so fetching one slightly larger
    rectangle beats fetching nine small ones.
    """
    if not boxes:
        return []
    if len(boxes) <= max_groups:
        return boxes
    boxes = sorted(boxes, key=lambda b: (b[1], b[0]))
    per = math.ceil(len(boxes) / max_groups)
    groups = []
    for i in range(0, len(boxes), per):
        chunk = boxes[i:i + per]
        groups.append((min(b[0] for b in chunk), min(b[1] for b in chunk),
                       max(b[2] for b in chunk), max(b[3] for b in chunk)))
    return groups


def to_geojson(geom: BaseGeometry) -> dict:
    return mapping(geom)


def from_wkt(text: str) -> BaseGeometry:
    return shapely_wkt.loads(text)
